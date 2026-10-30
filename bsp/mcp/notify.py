"""
Completion notifications (webhooks) for finished MCP background jobs.

When a job reaches a terminal state the :class:`~bsp.mcp.jobs.JobManager`
calls :func:`notify_job` from a background thread.  The feature is configured
entirely through the environment and is off by default:

* ``BSP_MCP_NOTIFY_URL``        — the endpoint; unset means disabled.
* ``BSP_MCP_NOTIFY_TOKEN_ENV``  — the *name* of the environment variable that
  holds the bearer token.  The token itself is never accepted in a config
  file, a tool argument or a log, and never lands in ``job.json``.
* ``BSP_MCP_NOTIFY_FORMAT``     — ``openclaw`` (default) or ``generic``.
* ``BSP_MCP_NOTIFY_KINDS``      — comma list of job kinds to notify
  (default: all).
* ``BSP_MCP_NOTIFY_AGENT``      — default ``agentId`` for the openclaw
  format when the job carries no ``notify_agent``.

The openclaw message is built only from job summary fields (id, kind,
target, state, duration, return code) — never from log content — so device
output cannot inject instructions into the receiving agent's prompt.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request

__all__ = [
    "NOTIFY_AGENT_ENV",
    "NOTIFY_FORMAT_ENV",
    "NOTIFY_KINDS_ENV",
    "NOTIFY_TOKEN_ENV_ENV",
    "NOTIFY_URL_ENV",
    "REQUEST_TIMEOUT_S",
    "RETRY_DELAYS_S",
    "notify_job",
    "should_notify",
]

NOTIFY_URL_ENV = "BSP_MCP_NOTIFY_URL"
NOTIFY_TOKEN_ENV_ENV = "BSP_MCP_NOTIFY_TOKEN_ENV"
NOTIFY_FORMAT_ENV = "BSP_MCP_NOTIFY_FORMAT"
NOTIFY_KINDS_ENV = "BSP_MCP_NOTIFY_KINDS"
NOTIFY_AGENT_ENV = "BSP_MCP_NOTIFY_AGENT"

#: Per-request timeout and the delays before each retry (then give up).
REQUEST_TIMEOUT_S = 10
RETRY_DELAYS_S = (2, 10, 30)


def should_notify(job) -> bool:
    """True when the webhook is configured and *job*'s kind is selected."""
    if not os.environ.get(NOTIFY_URL_ENV):
        return False
    raw = os.environ.get(NOTIFY_KINDS_ENV, "").strip()
    if not raw:
        return True
    kinds = {k.strip() for k in raw.split(",") if k.strip()}
    return job.kind in kinds


def _bearer_token() -> str | None:
    """Resolve the token from the env var named by BSP_MCP_NOTIFY_TOKEN_ENV."""
    name = os.environ.get(NOTIFY_TOKEN_ENV_ENV, "").strip()
    return os.environ.get(name) if name else None


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown duration"
    total = int(seconds)
    if total >= 3600:
        return f"{total // 3600}h{(total % 3600) // 60:02d}m"
    if total >= 60:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total}s"


def _openclaw_payload(job) -> dict:
    """OpenClaw gateway payload (POST <hooks.path>/agent)."""
    summary = job.summary()
    agent = job.extra.get("notify_agent") or os.environ.get(NOTIFY_AGENT_ENV) or "main"
    rc = summary.get("return_code")
    # Only job summary fields — never log content — may enter the message,
    # so untrusted device output cannot reach the receiving agent's prompt.
    message = (
        f"bsp-mcp job {job.id} ({job.kind} {job.target}) finished: "
        f"{job.state} after {_format_duration(summary.get('duration'))}. "
        f"Exit code {rc if rc is not None else 'n/a'}. "
        "Next: call job_status/job_log or analyze_build_failure for "
        "details, then continue your plan."
    )
    payload = {
        "name": f"bsp-mcp job {job.id} {job.state}",
        "agentId": agent,
        "deliver": False,
        "message": message,
    }
    session = job.extra.get("notify_session")
    if session:
        payload["sessionKey"] = session
    return payload


def _generic_payload(job) -> dict:
    return {
        "event": "job.finished",
        "job": job.summary(),
        "server": socket.gethostname(),
    }


def notify_job(job) -> str:
    """
    POST the completion webhook for a terminal *job*.

    Returns the outcome to record in ``job.extra["notify"]``: ``"sent"``,
    ``"failed: <reason>"`` or ``"disabled"``.  Blocking — call it from a
    background thread.
    """
    if not should_notify(job):
        return "disabled"
    url = os.environ[NOTIFY_URL_ENV]
    fmt = os.environ.get(NOTIFY_FORMAT_ENV, "openclaw")
    payload = _generic_payload(job) if fmt == "generic" else _openclaw_payload(job)
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": f"bsp-mcp-{job.id}-{job.state}",
    }
    token = _bearer_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(payload).encode("utf-8")

    last_error = "unknown error"
    for delay in (0,) + tuple(RETRY_DELAYS_S):
        if delay:
            time.sleep(delay)
        try:
            request = urllib.request.Request(
                url, data=body, headers=headers, method="POST"
            )
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
                status = getattr(response, "status", 200)
                if 200 <= status < 300:
                    return "sent"
                last_error = f"HTTP {status}"
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", None)
            last_error = str(reason) if reason is not None else str(exc)
    # The reason never contains the token: it is an HTTP status or a
    # connection error message.
    return f"failed: {last_error}"
