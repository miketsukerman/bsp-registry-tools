"""
``bsp-mcp`` — an MCP (Model Context Protocol) server exposing the BSP
workflow to AI agents.

Design rules:

* Long work (build/scan/test) never blocks a tool call: it is started as a
  background job running the real ``bsp`` CLI and the agent polls
  ``job_status``.
* stdout is the MCP protocol on the stdio transport, so all logging goes to
  stderr and every in-process :class:`~bsp.bsp_manager.BspManager` call runs
  with stdout/stderr redirected and a temporary logging handler attached.
* Errors the agent can act on are raised as ``ToolError`` so the model sees
  the message; genuine crashes stay generic.
* Every agent-supplied name is validated so nothing can inject CLI flags.
* No arbitrary shell tool is exposed.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import inspect
import io
import ipaddress
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# MCP SDK compatibility shim (2.x renamed FastMCP to MCPServer)
# ---------------------------------------------------------------------------

try:  # mcp >= 2.0
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver import MCPServer as _ServerClass
    from mcp.server.mcpserver.exceptions import ToolError
    _MCP_V2 = True
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import Context  # type: ignore
    from mcp.server.fastmcp import FastMCP as _ServerClass  # type: ignore
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore
    _MCP_V2 = False

import anyio
from mcp.types import ToolAnnotations

from bsp.mcp import jobs as jobs_mod
from bsp.mcp import pins as pins_mod
from bsp.mcp import triage as triage_mod
from bsp.mcp.jobs import TERMINAL_STATES, JobError, JobManager

logger = logging.getLogger("bsp.mcp")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+:-]{0,127}$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
TEST_SUITE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
NOTIFY_ROUTE_RE = re.compile(r"^[A-Za-z0-9._:@/-]{1,128}$")

#: wait_job: default and maximum server-side wait, progress cadence.
WAIT_DEFAULT_MAX_S = 900
WAIT_MAX_ENV = "BSP_MCP_WAIT_MAX_S"
WAIT_PROGRESS_INTERVAL_S = 15
WAIT_LOG_TAIL_LINES = 15


def _server_types():
    """
    Import :mod:`bsp.server.types` (the pydantic converters) without pulling
    in the rest of ``bsp.server`` — its ``__init__`` imports FastAPI, which
    belongs to the separate ``server`` extra.
    """
    name = "bsp.server.types"
    if name in sys.modules:
        return sys.modules[name]
    try:
        import bsp.server.types as mod
        return mod
    except ImportError:
        import importlib.util

        import bsp
        path = Path(bsp.__file__).parent / "server" / "types.py"
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

SCAN_SEVERITIES = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
SCAN_FAIL_ON = ["NONE"] + SCAN_SEVERITIES
SCAN_TOOLS = ["trivy", "syft+grype"]
TEST_BACKENDS = ["lava", "direct-local", "direct-ssh", "direct-serial"]
DEPLOY_PROVIDERS = ["azure", "aws"]
FLASH_TOOLS = ["bmaptool", "dd", "uuu"]
TREE_MODES = ["default", "full", "compact"]
_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3,
                   "NEGLIGIBLE": 4, "UNKNOWN": 5}

TEST_REPORT_FILES = {"html": "direct-test-report.html",
                     "json": "direct-test-summary.json"}
TEST_REPORT_MAX_FILE_BYTES = 2 * 1024 * 1024
TEST_REPORT_EXCERPT_LINES = 20
TEST_REPORT_EXCERPT_BYTES = 4096


def _path_under_roots(path: Path, roots: List[Path]) -> bool:
    """True when *path* resolves to a location under one of *roots*."""
    resolved = Path(path).resolve()
    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return True
        except ValueError:
            continue
    return False


def _case_log_excerpt(log_path: Optional[str], roots: List[Path]) -> Optional[str]:
    """
    Last lines of a test case log, or None when the path is missing,
    unreadable or outside *roots*. The content is untrusted device output
    and is returned as data only.
    """
    if not log_path:
        return None
    path = Path(log_path)
    if not _path_under_roots(path, roots):
        return None
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    excerpt = "\n".join(lines[-TEST_REPORT_EXCERPT_LINES:])
    if len(excerpt) > TEST_REPORT_EXCERPT_BYTES:
        excerpt = excerpt[-TEST_REPORT_EXCERPT_BYTES:]
    return excerpt

INSTRUCTIONS = """\
This server drives the BSP (Board Support Package) build workflow.

* Builds, scans and HIL tests run as background jobs: start_build/start_scan/
  start_hil_test return a job id immediately. Builds can take hours — prefer
  wait_job(job_id) over polling job_status in a loop: it blocks server-side
  until the job is terminal (or its timeout passes) and returns the summary
  plus a short log tail. When a completion webhook is configured
  (BSP_MCP_NOTIFY_URL) you can also just end your turn after starting a job:
  the server pushes a notification when the job finishes. Every start_* tool
  accepts optional notify_agent/notify_session to route that notification.
* When a build job fails, call analyze_build_failure(job_id) FIRST: it
  classifies the failure (network, fetch, compile, ...) and gives a fix hint
  and the bitbake task log path (readable via read_task_log).
* Use start_build(..., checkout_only=true) to validate a configuration
  cheaply before committing to a full build.
* Registry changes (bump_layer_pin) only edit the working tree: commit them
  on a branch and open a pull request — never push to main directly.
"""


def make_tool_annotations(
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
    open_world: bool = False,
) -> ToolAnnotations:
    """Build ToolAnnotations across SDK generations (snake_case vs camelCase)."""
    fields = getattr(ToolAnnotations, "model_fields", {})
    if "read_only_hint" in fields:  # mcp 2.x
        return ToolAnnotations(
            read_only_hint=read_only,
            destructive_hint=destructive,
            idempotent_hint=idempotent,
            open_world_hint=open_world,
        )
    try:  # mcp 1.x
        return ToolAnnotations(
            readOnlyHint=read_only,
            destructiveHint=destructive,
            idempotentHint=idempotent,
            openWorldHint=open_world,
        )
    except (TypeError, ValueError):
        return ToolAnnotations(
            read_only_hint=read_only,
            destructive_hint=destructive,
            idempotent_hint=idempotent,
            open_world_hint=open_world,
        )


def _tool_errors(func):
    """Convert actionable exceptions to ToolError so the model sees them."""

    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except ToolError:
                raise
            except (ValueError, LookupError, PermissionError, FileNotFoundError,
                    JobError) as exc:
                raise ToolError(str(exc)) from exc

        return async_wrapper

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ToolError:
            raise
        except (ValueError, LookupError, PermissionError, FileNotFoundError, JobError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_name(value: str, what: str) -> str:
    """Validate an agent-supplied identifier (preset, device, release, ...)."""
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise ValueError(
            f"Invalid {what} '{value}': must match {NAME_RE.pattern} "
            "(letters, digits, '_.+:-'; must not start with '-')"
        )
    return value


def validate_free_value(value: str, what: str) -> str:
    """
    Validate an agent-supplied free-form value (path, URL, prefix template).

    These may legitimately contain '/', '{placeholders}', '@', etc., so only
    flag injection (leading '-') and control characters are rejected.
    """
    if (not isinstance(value, str) or not value or value.startswith("-")
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError(
            f"Invalid {what} '{value}': must be non-empty, must not start "
            "with '-' and must not contain control characters"
        )
    return value


def validate_test_suite(value: str) -> str:
    """Validate an agent-supplied test-suite name (``--test-suite``)."""
    if (not isinstance(value, str) or value.startswith("-")
            or not TEST_SUITE_RE.match(value)):
        raise ValueError(
            f"Invalid test suite '{value}': must match {TEST_SUITE_RE.pattern} "
            "and must not start with '-'"
        )
    return value


def validate_notify_route(value: str, what: str) -> str:
    """Validate a notify_agent/notify_session routing id."""
    if not isinstance(value, str) or not NOTIFY_ROUTE_RE.fullmatch(value):
        raise ValueError(
            f"Invalid {what} '{value}': must match {NOTIFY_ROUTE_RE.pattern}"
        )
    return value


def notify_routing_extra(
    notify_agent: Optional[str],
    notify_session: Optional[str],
    extra: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, str]]:
    """Merge validated per-job notification routing into a job's extra dict."""
    merged = dict(extra or {})
    if notify_agent is not None:
        merged["notify_agent"] = validate_notify_route(notify_agent, "notify_agent")
    if notify_session is not None:
        merged["notify_session"] = validate_notify_route(notify_session,
                                                         "notify_session")
    return merged or None


def wait_max_s() -> int:
    """Upper clamp for wait_job's timeout_s (env: BSP_MCP_WAIT_MAX_S)."""
    try:
        value = int(os.environ.get(WAIT_MAX_ENV, ""))
    except ValueError:
        return WAIT_DEFAULT_MAX_S
    return max(1, value)


def validate_ssh_host(value: str) -> str:
    """
    Validate an agent-supplied SSH host (IP address or hostname).

    The value may originate from a device's serial console, so it is treated
    as hostile: only literal IPv4/IPv6 addresses or plain hostnames pass.
    This rules out option injection ('-oProxyCommand=...'), 'user@host'
    forms, spaces and embedded ports.
    """
    if isinstance(value, str) and value:
        try:
            ipaddress.ip_address(value)
            return value
        except ValueError:
            pass
        if HOSTNAME_RE.match(value):
            return value
    raise ValueError(
        f"Invalid ssh_host '{value}': must be an IPv4/IPv6 address or a "
        f"hostname matching {HOSTNAME_RE.pattern}"
    )


def validate_ssh_port(value: int) -> int:
    """Validate an agent-supplied SSH port (1..65535)."""
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise ValueError(f"Invalid ssh_port '{value}': must be an integer in 1..65535")
    return value


def selection_args(
    bsp_name: Optional[str] = None,
    device: Optional[str] = None,
    release: Optional[str] = None,
    features: Optional[List[str]] = None,
) -> List[str]:
    """Build the CLI selection arguments, validating every name."""
    if bsp_name and (device or release):
        raise ValueError("Provide either 'bsp_name' or 'device'/'release', not both.")
    args: List[str] = []
    if bsp_name:
        args.append(validate_name(bsp_name, "bsp_name"))
    else:
        if not device or not release:
            raise ValueError("Provide 'bsp_name' or both 'device' and 'release'.")
        args += ["--device", validate_name(device, "device")]
        args += ["--release", validate_name(release, "release")]
    for feature in features or []:
        args += ["--feature", validate_name(feature, "feature")]
    return args


# ---------------------------------------------------------------------------
# Output capture for in-process BspManager calls
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _capture():
    """
    Redirect stdout/stderr AND capture logging while a BspManager call runs.

    BspManager reports kas/registry errors through ``logging``, so without
    the temporary handler the agent would only see "Export failed: 1".
    stdout must stay clean because it carries the MCP stdio protocol.
    """
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.INFO)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            yield buf
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


# ---------------------------------------------------------------------------
# Server context
# ---------------------------------------------------------------------------


class ServerContext:
    """Holds resolved options, the lazy BspManager and the JobManager."""

    def __init__(
        self,
        registry: Optional[str] = None,
        remote: Optional[str] = None,
        branch: Optional[str] = None,
        state_dir: Optional[str] = None,
        max_jobs: int = 1,
        allow_roots: Optional[List[str]] = None,
        read_only: bool = False,
    ):
        self._registry_opt = registry
        self._remote = remote
        self._branch = branch
        self.read_only = read_only
        self.state_dir = Path(state_dir) if state_dir else jobs_mod.DEFAULT_STATE_DIR
        self.jobs = JobManager(state_dir=self.state_dir, max_jobs=max_jobs)
        self._extra_roots = [Path(r) for r in (allow_roots or [])]
        self._registry_path: Optional[Path] = None
        self._manager = None

    # -- registry ------------------------------------------------------

    @property
    def registry_path(self) -> Path:
        if self._registry_path is None:
            if self._registry_opt:
                path = Path(self._registry_opt).expanduser().resolve()
                if not path.exists():
                    raise FileNotFoundError(f"Registry file not found: {path}")
                self._registry_path = path
            else:
                from bsp.registry_fetcher import (
                    DEFAULT_BRANCH,
                    RegistryFetcher,
                    RemoteRegistrySpec,
                )
                from bsp.remotes_manager import RemotesManager

                if self._remote:
                    spec = RemoteRegistrySpec(
                        url=self._remote,
                        branch=self._branch or DEFAULT_BRANCH,
                    )
                else:
                    # Pick the first configured/default remote, much like git
                    # picks 'origin'.
                    stored = RemotesManager().ensure_default_remote(
                        branch=self._branch or DEFAULT_BRANCH
                    )
                    if not stored:
                        raise ValueError(
                            "No configured remotes and no --registry/--remote "
                            "given; add one with 'bsp remotes add'."
                        )
                    first = stored[0]
                    spec = RemoteRegistrySpec(
                        url=first.url,
                        branch=self._branch or first.branch,
                        name=first.name,
                    )
                # fetch_multiple clones each remote into its own subdirectory;
                # the cache dir itself is never used as a git repository.
                fetcher = RegistryFetcher()
                with _capture() as buf:
                    try:
                        pairs = fetcher.fetch_multiple([spec])
                        self._registry_path = pairs[0][1]
                    except SystemExit as exc:
                        raise ValueError(
                            "Failed to fetch the remote registry: "
                            + (buf.getvalue().strip() or str(exc))
                        ) from exc
        return self._registry_path

    @property
    def manager(self):
        """Lazy in-process BspManager for the read-only registry tools."""
        if self._manager is None:
            from bsp.bsp_manager import BspManager
            registry = self.registry_path
            with _capture() as buf:
                try:
                    mgr = BspManager(config_path=str(registry))
                    mgr.initialize()
                except SystemExit as exc:
                    raise ValueError(
                        "Failed to load the registry: "
                        + (buf.getvalue().strip() or str(exc))
                    ) from exc
            self._manager = mgr
        return self._manager

    @property
    def allowed_roots(self) -> List[Path]:
        return [self.registry_path.parent, self.state_dir] + self._extra_roots

    # -- jobs ----------------------------------------------------------

    def require_writable(self, action: str) -> None:
        if self.read_only:
            raise PermissionError(
                f"Server is running in read-only mode: '{action}' is disabled."
            )

    def cli_command(self) -> List[str]:
        """Locate the ``bsp`` CLI for job subprocesses."""
        candidate = Path(sys.executable).parent / "bsp"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            base = [str(candidate)]
        else:
            which = shutil.which("bsp")
            if which:
                base = [which]
            else:
                base = [sys.executable, "-m", "bsp.cli_runner"]
        return base + ["--no-color", "--registry", str(self.registry_path)]


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def build_server(ctx: ServerContext):
    server = _ServerClass(name="bsp-registry-tools", instructions=INSTRUCTIONS)

    read_only_ann = make_tool_annotations(read_only=True, idempotent=True)
    job_ann = make_tool_annotations()
    destructive_ann = make_tool_annotations(destructive=True)

    def tool(annotations, description):
        def decorate(func):
            wrapped = _tool_errors(func)
            server.tool(name=func.__name__, description=description,
                        annotations=annotations)(wrapped)
            return wrapped
        return decorate

    # -- Registry (read-only) ------------------------------------------

    @tool(read_only_ann, "List all named BSP presets defined in the registry.")
    def list_presets() -> List[Dict]:
        preset_to_response = _server_types().preset_to_response
        mgr = ctx.manager
        presets = mgr.resolver.list_presets() if mgr.resolver else []
        return [preset_to_response(p).model_dump() for p in presets]

    @tool(read_only_ann, "List all hardware device definitions.")
    def list_devices() -> List[Dict]:
        device_to_response = _server_types().device_to_response
        mgr = ctx.manager
        devices = mgr.model.registry.devices if mgr.model else []
        return [device_to_response(d).model_dump() for d in devices]

    @tool(read_only_ann,
          "List release definitions, optionally filtered by device slug "
          "(only releases applicable to that device's vendor).")
    def list_releases(device: Optional[str] = None) -> List[Dict]:
        release_to_response = _server_types().release_to_response
        mgr = ctx.manager
        releases = mgr.model.registry.releases if mgr.model else []
        if device:
            validate_name(device, "device")
            with _capture():
                try:
                    dev = mgr.resolver.get_device(device)
                except SystemExit:
                    raise ValueError(f"Unknown device '{device}'. Use list_devices.")
            releases = [
                r for r in releases
                if not r.vendor_overrides
                or any(vo.vendor == dev.vendor for vo in r.vendor_overrides)
            ]
        return [release_to_response(r).model_dump() for r in releases]

    @tool(read_only_ann, "List all optional BSP feature definitions.")
    def list_features() -> List[Dict]:
        feature_to_response = _server_types().feature_to_response
        mgr = ctx.manager
        features = mgr.model.registry.features if mgr.model else []
        return [feature_to_response(f).model_dump() for f in features]

    @tool(read_only_ann,
          "Export the resolved BSP configuration as KAS-compatible YAML. "
          "Supply either bsp_name OR both device and release.")
    def export_config(
        bsp_name: Optional[str] = None,
        device: Optional[str] = None,
        release: Optional[str] = None,
        features: Optional[List[str]] = None,
    ) -> str:
        # Validates names and bsp_name/device exclusivity.
        selection_args(bsp_name, device, release, features)
        mgr = ctx.manager
        with _capture() as buf:
            try:
                if bsp_name:
                    mgr.export_bsp_config(bsp_name=bsp_name, output_file=None)
                else:
                    mgr.export_by_components(device, release, features or [],
                                             output_file=None)
            except SystemExit as exc:
                raise ValueError(
                    "Export failed: " + (buf.getvalue().strip() or str(exc))
                ) from exc
        return buf.getvalue()

    @tool(read_only_ann,
          "List all container image definitions from the registry "
          "(name, base image, Dockerfile and build args per registry).")
    def list_containers() -> List[Dict]:
        mgr = ctx.manager
        containers: List[Dict] = []
        for reg_name, reg_model, _, _ in mgr._iter_registries():
            reg_containers = (reg_model.containers or {}) if reg_model else {}
            for name, cfg in reg_containers.items():
                containers.append({
                    "registry": reg_name,
                    "name": name,
                    "image": cfg.image,
                    "file": cfg.file,
                    "args": {a.name: a.value for a in (cfg.args or [])},
                })
        return containers

    @tool(read_only_ann,
          "Render an ASCII tree view of the whole BSP registry hierarchy. "
          "mode: default | full (includes and descriptions) | compact "
          "(names/slugs only). remote filters to one named remote registry.")
    def registry_tree(mode: str = "default", remote: Optional[str] = None) -> str:
        if mode not in TREE_MODES:
            raise ValueError(f"mode must be one of {TREE_MODES}")
        if remote:
            validate_name(remote, "remote")
        mgr = ctx.manager
        with _capture() as buf:
            try:
                mgr.tree_bsp(use_color=False, mode=mode, registry_filter=remote)
            except SystemExit as exc:
                raise ValueError(
                    "Tree rendering failed: " + (buf.getvalue().strip() or str(exc))
                ) from exc
        return buf.getvalue()

    # -- Jobs -----------------------------------------------------------

    @tool(job_ann,
          "Start a BSP build as a background job and return its job id "
          "immediately (poll job_status). checkout_only validates the "
          "configuration without building; clean forces a clean build. "
          "target overrides the registry build targets with a specific "
          "bitbake image or recipe (e.g. 'core-image-minimal').")
    def start_build(
        bsp_name: Optional[str] = None,
        device: Optional[str] = None,
        release: Optional[str] = None,
        features: Optional[List[str]] = None,
        target: Optional[str] = None,
        checkout_only: bool = False,
        clean: bool = False,
        notify_agent: Optional[str] = None,
        notify_session: Optional[str] = None,
    ) -> Dict:
        ctx.require_writable("start_build")
        sel = selection_args(bsp_name, device, release, features)
        cmd = ctx.cli_command() + ["build"] + sel
        if target:
            cmd += ["--target", validate_free_value(target, "target")]
        if checkout_only:
            cmd.append("--checkout")
        if clean:
            cmd.append("--clean")
        kind = "checkout" if checkout_only else "build"
        job = ctx.jobs.start(kind, bsp_name or f"{device}/{release}", cmd,
                             extra=notify_routing_extra(notify_agent,
                                                        notify_session))
        return job.summary()

    @tool(job_ann,
          "Start a CVE scan (and SBOM generation) of previously built "
          "artifacts as a background job. Reports land in the job's "
          "reports directory; use scan_findings when it finishes.")
    def start_scan(
        bsp_name: Optional[str] = None,
        device: Optional[str] = None,
        release: Optional[str] = None,
        features: Optional[List[str]] = None,
        severity: str = "HIGH",
        fail_on: str = "CRITICAL",
        tool: str = "trivy",
        notify_agent: Optional[str] = None,
        notify_session: Optional[str] = None,
    ) -> Dict:
        ctx.require_writable("start_scan")
        if severity not in SCAN_SEVERITIES:
            raise ValueError(f"severity must be one of {SCAN_SEVERITIES}")
        if fail_on not in SCAN_FAIL_ON:
            raise ValueError(f"fail_on must be one of {SCAN_FAIL_ON}")
        if tool not in SCAN_TOOLS:
            raise ValueError(f"tool must be one of {SCAN_TOOLS}")
        sel = selection_args(bsp_name, device, release, features)
        cmd = ctx.cli_command() + ["scan"] + sel + [
            "--tool", tool,
            "--severity", severity,
            "--fail-on", fail_on,
            "--output-dir", "{job_dir}/reports",
        ]
        job = ctx.jobs.start("scan", bsp_name or f"{device}/{release}", cmd,
                             extra=notify_routing_extra(
                                 notify_agent, notify_session,
                                 {"reports_dir": "{job_dir}/reports"}))
        return job.summary()

    @tool(job_ann,
          "Start a hardware-in-the-loop test run as a background job "
          "(always waits for the result inside the job). Tokens are never "
          "passed as arguments; they stay in the registry as $ENV{...}. "
          "ssh_host/ssh_port override the registry's testing.direct.transport "
          "for this run (e.g. the DUT's current DHCP address). For direct "
          "backends the test suites come from the registry's "
          "testing.direct.definitions and/or the LAVA job referenced by "
          "testing.direct.lava_job; test_suites restricts the run to the "
          "named suites. Direct backends write their JSON/HTML/PDF reports "
          "to a per-job directory; call test_report when the job is "
          "terminal for the structured result.")
    def start_hil_test(
        bsp_name: Optional[str] = None,
        device: Optional[str] = None,
        release: Optional[str] = None,
        features: Optional[List[str]] = None,
        backend: str = "lava",
        ssh_host: Optional[str] = None,
        ssh_port: Optional[int] = None,
        test_suites: Optional[List[str]] = None,
        notify_agent: Optional[str] = None,
        notify_session: Optional[str] = None,
    ) -> Dict:
        ctx.require_writable("start_hil_test")
        if backend not in TEST_BACKENDS:
            raise ValueError(f"backend must be one of {TEST_BACKENDS}")
        if (ssh_host is not None or ssh_port is not None) and \
                backend not in ("direct-ssh", "direct-serial"):
            raise ValueError("ssh_host/ssh_port only apply to direct-ssh and direct-serial")
        sel = selection_args(bsp_name, device, release, features)
        cmd = ctx.cli_command() + ["test"] + sel + ["--backend", backend, "--wait"]
        lava_server = os.environ.get("BSP_MCP_LAVA_SERVER")
        if backend == "lava" and lava_server:
            cmd += ["--lava-server", lava_server]
        extra = {}
        if backend in ("direct-local", "direct-ssh", "direct-serial"):
            cmd += ["--direct-output-dir", "{job_dir}/test-results"]
            extra["reports_dir"] = "{job_dir}/test-results"
        for suite in test_suites or []:
            cmd += ["--test-suite", validate_test_suite(suite)]
        if ssh_host is not None:
            cmd += ["--ssh-host", validate_ssh_host(ssh_host)]
            extra["ssh_host"] = ssh_host
        if ssh_port is not None:
            cmd += ["--ssh-port", str(validate_ssh_port(ssh_port))]
            extra["ssh_port"] = str(ssh_port)
        job = ctx.jobs.start("test", bsp_name or f"{device}/{release}", cmd,
                             extra=notify_routing_extra(notify_agent,
                                                        notify_session, extra))
        return job.summary()

    @tool(job_ann,
          "Fetch (download) all sources for a BSP configuration as a "
          "background job, without building. Cheaper than a build for "
          "pre-warming caches or validating that all sources resolve.")
    def start_fetch(
        bsp_name: Optional[str] = None,
        device: Optional[str] = None,
        release: Optional[str] = None,
        features: Optional[List[str]] = None,
        notify_agent: Optional[str] = None,
        notify_session: Optional[str] = None,
    ) -> Dict:
        ctx.require_writable("start_fetch")
        sel = selection_args(bsp_name, device, release, features)
        cmd = ctx.cli_command() + ["fetch"] + sel
        job = ctx.jobs.start("fetch", bsp_name or f"{device}/{release}", cmd,
                             extra=notify_routing_extra(notify_agent,
                                                        notify_session))
        return job.summary()

    @tool(job_ann,
          "Deploy previously built artifacts to cloud storage (Azure Blob or "
          "AWS S3) as a background job. Credentials come from the registry/"
          "environment, never from tool arguments. Use dry_run=true first to "
          "list what would be uploaded.")
    def start_deploy(
        bsp_name: Optional[str] = None,
        device: Optional[str] = None,
        release: Optional[str] = None,
        features: Optional[List[str]] = None,
        provider: Optional[str] = None,
        container: Optional[str] = None,
        prefix: Optional[str] = None,
        dry_run: bool = False,
        notify_agent: Optional[str] = None,
        notify_session: Optional[str] = None,
    ) -> Dict:
        ctx.require_writable("start_deploy")
        if provider is not None and provider not in DEPLOY_PROVIDERS:
            raise ValueError(f"provider must be one of {DEPLOY_PROVIDERS}")
        sel = selection_args(bsp_name, device, release, features)
        cmd = ctx.cli_command() + ["deploy"] + sel
        if provider:
            cmd += ["--provider", provider]
        if container:
            cmd += ["--container", validate_name(container, "container")]
        if prefix:
            cmd += ["--prefix", validate_free_value(prefix, "prefix")]
        if dry_run:
            cmd.append("--dry-run")
        job = ctx.jobs.start("deploy", bsp_name or f"{device}/{release}", cmd,
                             extra=notify_routing_extra(notify_agent,
                                                        notify_session))
        return job.summary()

    @tool(job_ann,
          "Download (gather) previously deployed artifacts from cloud "
          "storage as a background job. dest_dir defaults to "
          "<build_path>/build/tmp/deploy/images under the registry's "
          "build path; use dry_run=true first to list what would be "
          "downloaded.")
    def start_gather(
        bsp_name: Optional[str] = None,
        device: Optional[str] = None,
        release: Optional[str] = None,
        features: Optional[List[str]] = None,
        provider: Optional[str] = None,
        container: Optional[str] = None,
        prefix: Optional[str] = None,
        dest_dir: Optional[str] = None,
        dry_run: bool = False,
        notify_agent: Optional[str] = None,
        notify_session: Optional[str] = None,
    ) -> Dict:
        ctx.require_writable("start_gather")
        if provider is not None and provider not in DEPLOY_PROVIDERS:
            raise ValueError(f"provider must be one of {DEPLOY_PROVIDERS}")
        sel = selection_args(bsp_name, device, release, features)
        cmd = ctx.cli_command() + ["gather"] + sel
        if provider:
            cmd += ["--provider", provider]
        if container:
            cmd += ["--container", validate_name(container, "container")]
        if prefix:
            cmd += ["--prefix", validate_free_value(prefix, "prefix")]
        if dest_dir:
            cmd += ["--dest-dir", validate_free_value(dest_dir, "dest_dir")]
        if dry_run:
            cmd.append("--dry-run")
        job = ctx.jobs.start("gather", bsp_name or f"{device}/{release}", cmd,
                             extra=notify_routing_extra(notify_agent,
                                                        notify_session))
        return job.summary()

    @tool(destructive_ann,
          "Flash a built image to a block device as a background job. "
          "DESTRUCTIVE: overwrites the target device. target is the block "
          "device path (e.g. /dev/sdb); ALWAYS run with dry_run=true first "
          "and confirm the device with the user before flashing for real.")
    def start_flash(
        bsp_name: Optional[str] = None,
        device: Optional[str] = None,
        release: Optional[str] = None,
        features: Optional[List[str]] = None,
        target: Optional[str] = None,
        image_path: Optional[str] = None,
        flash_tool: Optional[str] = None,
        dry_run: bool = True,
        notify_agent: Optional[str] = None,
        notify_session: Optional[str] = None,
    ) -> Dict:
        ctx.require_writable("start_flash")
        if flash_tool is not None and flash_tool not in FLASH_TOOLS:
            raise ValueError(f"flash_tool must be one of {FLASH_TOOLS}")
        sel = selection_args(bsp_name, device, release, features)
        cmd = ctx.cli_command() + ["flash"] + sel
        if target:
            cmd += ["--target", validate_free_value(target, "target")]
        if image_path:
            cmd += ["--image-path", validate_free_value(image_path, "image_path")]
        if flash_tool:
            cmd += ["--tool", flash_tool]
        if dry_run:
            cmd.append("--dry-run")
        job = ctx.jobs.start("flash", bsp_name or f"{device}/{release}", cmd,
                             extra=notify_routing_extra(notify_agent,
                                                        notify_session))
        return job.summary()

    @tool(job_ann,
          "Build container image(s) defined in the registry as a background "
          "job. Omit container_name to build all; no_cache disables the "
          "Docker layer cache.")
    def start_container_build(
        container_name: Optional[str] = None,
        no_cache: bool = False,
        notify_agent: Optional[str] = None,
        notify_session: Optional[str] = None,
    ) -> Dict:
        ctx.require_writable("start_container_build")
        cmd = ctx.cli_command() + ["containers", "build"]
        if container_name:
            cmd.append(validate_name(container_name, "container_name"))
        if no_cache:
            cmd.append("--docker-no-cache")
        job = ctx.jobs.start("container_build", container_name or "all", cmd,
                             extra=notify_routing_extra(notify_agent,
                                                        notify_session))
        return job.summary()

    @tool(read_only_ann, "Get the current status and summary of a job.")
    def job_status(job_id: str) -> Dict:
        return ctx.jobs.get(job_id).summary()

    @tool(read_only_ann,
          "Wait for a background job to finish (server-side, no polling). "
          "Returns as soon as the job is terminal or after timeout_s. "
          "Prefer this over repeated job_status calls.")
    async def wait_job(job_id: str, timeout_s: int = 300,
                       mcp_context: Context = None) -> Dict:
        ctx.jobs.get(job_id)  # validate the id before waiting
        timeout = max(1, min(int(timeout_s), wait_max_s()))
        start = time.monotonic()
        deadline = start + timeout
        # Wait in short chunks so streamable-HTTP connections stay alive via
        # progress notifications; the wait itself is condition-based, never a
        # busy poll. Missing progress support must not break the tool.
        terminal = ctx.jobs.wait(job_id, timeout=0)
        while not terminal:
            chunk = min(WAIT_PROGRESS_INTERVAL_S, deadline - time.monotonic())
            if chunk <= 0:
                break
            terminal = await anyio.to_thread.run_sync(
                functools.partial(ctx.jobs.wait, job_id, timeout=chunk))
            if terminal:
                break
            if mcp_context is not None:
                elapsed = time.monotonic() - start
                with contextlib.suppress(Exception):
                    await mcp_context.report_progress(
                        min(elapsed, timeout), timeout,
                        f"waited {elapsed:.0f}s; job {job_id} is "
                        f"{ctx.jobs.get(job_id).state}")
        job = ctx.jobs.get(job_id)
        result = job.summary()
        result["waited_s"] = round(time.monotonic() - start, 3)
        result["terminal"] = job.state in TERMINAL_STATES
        try:
            result["log_tail"] = ctx.jobs.read_log(job_id,
                                                   tail=WAIT_LOG_TAIL_LINES)
        except JobError:
            result["log_tail"] = ""
        result["hint"] = (
            "job finished" if result["terminal"] else
            "still running; call wait_job again or end your turn and wait "
            "for the completion notification"
        )
        return result

    @tool(read_only_ann, "List all known jobs (including finished and lost ones).")
    def list_jobs() -> List[Dict]:
        return [j.summary() for j in ctx.jobs.list()]

    @tool(read_only_ann,
          "Read the tail of a job's combined output log. tail is clamped to "
          "1..2000 lines; grep filters lines with a regex.")
    def job_log(job_id: str, tail: int = 200, grep: Optional[str] = None) -> str:
        return ctx.jobs.read_log(job_id, tail=tail, grep=grep)

    @tool(destructive_ann,
          "Cancel a running job: SIGTERMs the whole process group so kas, "
          "docker and bitbake children terminate too.")
    def cancel_job(job_id: str) -> Dict:
        ctx.require_writable("cancel_job")
        return ctx.jobs.cancel(job_id).summary()

    # -- Triage ----------------------------------------------------------

    @tool(read_only_ann,
          "Analyze a failed build/checkout job's log: classifies each "
          "failure (network, fetch, compile, ...), gives a fix hint, the "
          "failing recipe/task and the bitbake task log path.")
    def analyze_build_failure(job_id: str) -> Dict:
        job = ctx.jobs.get(job_id)
        try:
            text = job.log_path.read_text(errors="replace")
        except FileNotFoundError:
            raise JobError(f"Job {job_id} has no log yet")
        result = triage_mod.analyze_log(text)
        result["job_id"] = job_id
        result["job_state"] = job.state
        return result

    @tool(read_only_ann,
          "Read the tail of a bitbake task log (path from "
          "analyze_build_failure). Only paths under the allowed roots are "
          "readable.")
    def read_task_log(path: str, tail: int = 200) -> str:
        return triage_mod.read_task_log(path, ctx.allowed_roots, tail=tail)

    @tool(read_only_ann,
          "Parse a finished scan job's Trivy/Grype JSON reports: returns "
          "counts by severity, the fixable count, findings sorted by "
          "severity and the SBOM file names.")
    def scan_findings(job_id: str) -> Dict:
        job = ctx.jobs.get(job_id)
        reports_dir = Path(job.extra.get("reports_dir") or (Path(job.job_dir) / "reports"))
        if not reports_dir.is_dir():
            raise JobError(
                f"Job {job_id} has no reports directory yet ({reports_dir}). "
                "Wait for the scan job to finish."
            )
        findings: List[Dict] = []
        sboms: List[str] = []
        for path in sorted(reports_dir.glob("*.json")):
            name = path.name
            if name.endswith(".cdx.json") or name.endswith(".spdx.json"):
                sboms.append(name)
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and "Results" in data:  # Trivy
                for result in data.get("Results") or []:
                    for vuln in result.get("Vulnerabilities") or []:
                        findings.append({
                            "id": vuln.get("VulnerabilityID"),
                            "package": vuln.get("PkgName"),
                            "installed": vuln.get("InstalledVersion"),
                            "fixed": vuln.get("FixedVersion") or None,
                            "severity": (vuln.get("Severity") or "UNKNOWN").upper(),
                            "title": vuln.get("Title"),
                            "source": name,
                        })
            elif isinstance(data, dict) and "matches" in data:  # Grype
                for match in data.get("matches") or []:
                    vuln = match.get("vulnerability") or {}
                    artifact = match.get("artifact") or {}
                    fix = (vuln.get("fix") or {})
                    versions = fix.get("versions") or []
                    findings.append({
                        "id": vuln.get("id"),
                        "package": artifact.get("name"),
                        "installed": artifact.get("version"),
                        "fixed": versions[0] if versions else None,
                        "severity": (vuln.get("severity") or "UNKNOWN").upper(),
                        "title": vuln.get("description"),
                        "source": name,
                    })
        findings.sort(key=lambda f: _SEVERITY_ORDER.get(f["severity"], 99))
        counts: Dict[str, int] = {}
        for f in findings:
            counts[f["severity"]] = counts.get(f["severity"], 0) + 1
        return {
            "job_id": job_id,
            "total": len(findings),
            "counts_by_severity": counts,
            "fixable": sum(1 for f in findings if f["fixed"]),
            "findings": findings,
            "sbom_files": sorted(sboms),
        }

    def _finished_test_job(job_id: str):
        """Return (job, reports_dir) for a terminal test job, or raise."""
        job = ctx.jobs.get(job_id)
        if job.kind != "test":
            raise JobError(f"Job {job_id} is a '{job.kind}' job, not a test job")
        if job.state not in TERMINAL_STATES:
            raise JobError(
                f"Job {job_id} is still running; call test_report when "
                "job_status is terminal"
            )
        reports_dir = Path(job.extra.get("reports_dir")
                           or (Path(job.job_dir) / "test-results"))
        return job, reports_dir

    @tool(read_only_ann,
          "Structured result of a finished HIL test job: totals, per-suite "
          "and per-case results, failures with short log excerpts, and the "
          "paths of the HTML/PDF report on the bench. Call after job_status "
          "is terminal.")
    def test_report(job_id: str, include_cases: bool = True,
                    max_cases: int = 200) -> Dict:
        job, reports_dir = _finished_test_job(job_id)
        summary_path = reports_dir / TEST_REPORT_FILES["json"]
        if not summary_path.is_file():
            raise JobError(
                f"Job {job_id} has no test summary ({summary_path}): "
                "the run failed before tests started; see job_log"
            )
        data = json.loads(summary_path.read_text(errors="replace"))
        roots = [Path(job.job_dir)] + ctx.allowed_roots
        totals = {"suites": 0, "cases": 0, "pass": 0, "fail": 0,
                  "skip": 0, "unknown": 0, "duration_s": 0.0}
        suites_out: List[Dict] = []
        failures: List[Dict] = []
        truncated = False
        case_budget = max(0, int(max_cases))
        for suite in data.get("suites") or []:
            totals["suites"] += 1
            try:
                totals["duration_s"] += float(suite.get("duration") or 0)
            except (TypeError, ValueError):
                pass
            suite_out = {
                "name": suite.get("name"),
                "status": suite.get("status"),
                "duration_s": suite.get("duration"),
                "cases": [],
            }
            for case in suite.get("cases") or []:
                signals = case.get("lava_signals") or []
                if signals:
                    entries = [
                        {
                            "name": sig.get("test_case_id") or case.get("name"),
                            "result": (sig.get("result") or "unknown").lower(),
                            "measurement": sig.get("measurement"),
                            "units": sig.get("units"),
                            "requirement_id": sig.get("requirement_id") or None,
                            "timed_out": bool(case.get("timed_out")),
                        }
                        for sig in signals
                    ]
                else:
                    entries = [{
                        "name": case.get("name"),
                        "result": (case.get("status") or "unknown").lower(),
                        "measurement": None,
                        "units": None,
                        "requirement_id": None,
                        "timed_out": bool(case.get("timed_out")),
                    }]
                for entry in entries:
                    totals["cases"] += 1
                    result = entry["result"]
                    totals[result if result in ("pass", "fail", "skip")
                           else "unknown"] += 1
                    if result == "fail":
                        failures.append({
                            "suite": suite.get("name"),
                            "case": entry["name"],
                            "result": result,
                            "log_excerpt": _case_log_excerpt(
                                case.get("log_path"), roots),
                        })
                    if include_cases:
                        if case_budget > 0:
                            suite_out["cases"].append(entry)
                            case_budget -= 1
                        else:
                            truncated = True
            suites_out.append(suite_out)
        totals["duration_s"] = round(totals["duration_s"], 3)
        pdf_path = reports_dir / "direct-test-report.pdf"
        return {
            "job_id": job_id,
            "job_state": job.state,
            "passed": bool(data.get("passed")),
            "label": data.get("label"),
            "backend": data.get("backend"),
            "ssh_host": job.extra.get("ssh_host"),
            "totals": totals,
            "suites": suites_out,
            "failures": failures,
            "report_files": {
                "json": str(summary_path),
                "html": str(reports_dir / TEST_REPORT_FILES["html"]),
                "pdf": str(pdf_path) if pdf_path.is_file() else None,
            },
            "truncated": truncated,
        }

    @tool(read_only_ann,
          "Raw content of a finished HIL test job's report file: kind='html' "
          "for direct-test-report.html, kind='json' for "
          "direct-test-summary.json. Capped at 2 MB; only paths under the "
          "allowed roots are readable.")
    def test_report_file(job_id: str, kind: str = "html") -> str:
        job, reports_dir = _finished_test_job(job_id)
        if kind not in TEST_REPORT_FILES:
            raise ValueError(
                f"kind must be one of {sorted(TEST_REPORT_FILES)}")
        path = reports_dir / TEST_REPORT_FILES[kind]
        roots = [Path(job.job_dir)] + ctx.allowed_roots
        if not _path_under_roots(path, roots):
            raise PermissionError(
                f"Path '{path.resolve()}' is outside the allowed roots: "
                + ", ".join(str(r) for r in roots)
            )
        if not path.is_file():
            raise JobError(
                f"Job {job_id} has no {TEST_REPORT_FILES[kind]} ({path}): "
                "the run failed before tests started; see job_log"
            )
        if path.stat().st_size > TEST_REPORT_MAX_FILE_BYTES:
            raise JobError(
                f"{path} exceeds the {TEST_REPORT_MAX_FILE_BYTES // (1024 * 1024)} MB "
                "limit for test_report_file"
            )
        return path.read_text(errors="replace")

    # -- Maintenance ------------------------------------------------------

    @tool(read_only_ann,
          "List every pinned layer repo (repos.<name> with a commit) across "
          "all YAML files in the registry, with its branch and url (resolved "
          "from included base files when needed).")
    def list_layer_pins(repo_filter: Optional[str] = None) -> List[Dict]:
        if repo_filter:
            validate_name(repo_filter, "repo_filter")
        return pins_mod.list_pins(ctx.registry_path.parent, repo_filter)

    @tool(read_only_ann,
          "Check every layer pin against the remote branch head via "
          "git ls-remote. Status: up_to_date | behind | unreachable | "
          "no_branch_or_url.")
    def check_layer_pins(repo_filter: Optional[str] = None) -> List[Dict]:
        if repo_filter:
            validate_name(repo_filter, "repo_filter")
        return pins_mod.check_pins(ctx.registry_path.parent, repo_filter)

    @tool(destructive_ann,
          "Bump a layer pin to a new full 40-char commit SHA. Edits the file "
          "textually in the working tree only (comments and formatting are "
          "preserved); commit the change on a branch and open a PR.")
    def bump_layer_pin(file: str, repo: str, new_commit: str) -> Dict:
        ctx.require_writable("bump_layer_pin")
        validate_name(repo, "repo")
        return pins_mod.bump_pin(ctx.registry_path.parent, file, repo, new_commit)

    # -- Remotes ----------------------------------------------------------

    def _remotes_manager():
        from bsp.remotes_manager import RemotesManager
        return RemotesManager()

    def _remotes_call(action, *args, **kwargs):
        """Run a RemotesManager call, converting SystemExit to ValueError."""
        with _capture() as buf:
            try:
                return action(*args, **kwargs)
            except SystemExit as exc:
                raise ValueError(
                    buf.getvalue().strip() or f"Remotes operation failed: {exc}"
                ) from exc

    @tool(read_only_ann,
          "List the configured named remote registries (the first one is the "
          "default, like git's 'origin').")
    def list_remotes() -> List[Dict]:
        return [r.to_dict() for r in _remotes_manager().ensure_default_remote()]

    @tool(job_ann,
          "Add a named remote registry (git URL + branch). Takes effect for "
          "new bsp CLI runs; this server keeps using the registry it "
          "resolved at startup.")
    def add_remote(name: str, url: str, branch: str = "main") -> Dict:
        ctx.require_writable("add_remote")
        validate_name(name, "name")
        validate_free_value(url, "url")
        validate_name(branch, "branch")
        mgr = _remotes_manager()
        return _remotes_call(mgr.add, name, url, branch).to_dict()

    @tool(destructive_ann,
          "Remove a named remote registry from the remotes configuration.")
    def remove_remote(name: str) -> Dict:
        ctx.require_writable("remove_remote")
        validate_name(name, "name")
        mgr = _remotes_manager()
        _remotes_call(mgr.remove, name)
        return {"removed": name}

    @tool(job_ann, "Rename a configured remote registry.")
    def rename_remote(old_name: str, new_name: str) -> Dict:
        ctx.require_writable("rename_remote")
        validate_name(old_name, "old_name")
        validate_name(new_name, "new_name")
        mgr = _remotes_manager()
        return _remotes_call(mgr.rename, old_name, new_name).to_dict()

    @tool(job_ann, "Change the git URL of a configured remote registry.")
    def set_remote_url(name: str, url: str) -> Dict:
        ctx.require_writable("set_remote_url")
        validate_name(name, "name")
        validate_free_value(url, "url")
        mgr = _remotes_manager()
        return _remotes_call(mgr.set_url, name, url).to_dict()

    # -- Prompts ----------------------------------------------------------
    # Ready-made prompts for testing the server from any MCP client (they
    # appear as slash commands / prompt pickers in Copilot, Claude, etc.).

    @server.prompt(name="smoke_test",
                   description="Exercise every read-only tool and report "
                               "which ones work.")
    def smoke_test() -> str:
        return (
            "Run a smoke test of this BSP MCP server using only read-only "
            "tools. Step by step: 1) call list_presets, list_devices, "
            "list_releases, list_features and list_containers and summarise "
            "how many entries each returns; 2) call registry_tree with "
            "mode='compact' and show the first 20 lines; 3) call list_remotes "
            "and list_jobs; 4) pick the first preset and call export_config "
            "for it; 5) deliberately call export_config with bsp_name "
            "'--clean' and confirm it is rejected with a clear validation "
            "error. Finish with a table of each tool, whether it succeeded, "
            "and any error message."
        )

    @server.prompt(name="explore_registry",
                   description="Discover what can be built from the registry.")
    def explore_registry() -> str:
        return (
            "Give me an overview of this BSP registry. List all presets, "
            "devices, releases and optional features, and render a compact "
            "registry tree. Then recommend which preset or device/release "
            "combination to build first and why."
        )

    @server.prompt(name="validate_preset",
                   description="Cheaply validate a preset before a full build.")
    def validate_preset(preset: str) -> str:
        return (
            f"Validate the BSP preset '{preset}' without doing a full build. "
            "First call export_config for it and check the resolved KAS YAML "
            "looks sane (repos, targets, machine). Then start_build with "
            "checkout_only=true, poll job_status until it finishes, and if it "
            "fails run analyze_build_failure and explain the root cause and "
            "the fix."
        )

    @server.prompt(name="build_and_triage",
                   description="Run a full build and triage any failure.")
    def build_and_triage(preset: str) -> str:
        return (
            f"Build the BSP preset '{preset}'. Start the build job, then "
            "poll job_status (builds can take hours — report progress from "
            "job_log occasionally, don't spam). If it succeeds, summarise "
            "the job duration and log tail. If it fails, run "
            "analyze_build_failure, read the relevant bitbake task log via "
            "read_task_log, and propose a concrete fix. Do not retry more "
            "than once."
        )

    @server.prompt(name="scan_image",
                   description="CVE-scan built artifacts and summarise findings.")
    def scan_image(preset: str) -> str:
        return (
            f"Run a CVE scan for the already-built preset '{preset}' with "
            "start_scan. Wait for the job to finish, then call scan_findings "
            "and report: total findings, counts by severity, the fixable "
            "count, and the top 10 CRITICAL/HIGH findings with their fixed "
            "versions. Recommend which packages to update first."
        )

    @server.prompt(name="audit_layer_pins",
                   description="Check layer pins against remote heads and "
                               "propose bumps.")
    def audit_layer_pins() -> str:
        return (
            "Audit the layer pins in this registry. Call check_layer_pins "
            "and report each repo's status (up_to_date, behind, unreachable, "
            "no_branch_or_url). For every pin that is behind, say which file "
            "to edit and what the new commit would be, but do NOT call "
            "bump_layer_pin unless I explicitly confirm."
        )

    return server


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _env_roots() -> List[str]:
    raw = os.environ.get("BSP_MCP_ALLOW_ROOTS", "")
    return [r for r in raw.split(os.pathsep) if r]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bsp-mcp",
        description="MCP server exposing bsp-registry-tools to AI agents",
    )
    parser.add_argument("--registry", default=os.environ.get("BSP_MCP_REGISTRY"),
                        help="Path to a local bsp-registry.yaml (env: BSP_MCP_REGISTRY). "
                             "Default: fetch the remote registry.")
    parser.add_argument("--remote", default=os.environ.get("BSP_MCP_REMOTE"),
                        help="Remote registry git URL (env: BSP_MCP_REMOTE)")
    parser.add_argument("--branch", default=os.environ.get("BSP_MCP_BRANCH"),
                        help="Remote registry branch (env: BSP_MCP_BRANCH)")
    parser.add_argument("--state-dir", default=os.environ.get("BSP_MCP_STATE_DIR"),
                        help="Job state directory (env: BSP_MCP_STATE_DIR, "
                             "default: ~/.cache/bsp/mcp/jobs)")
    parser.add_argument("--max-jobs", type=int,
                        default=int(os.environ.get("BSP_MCP_MAX_JOBS", "1")),
                        help="Maximum concurrently running jobs (env: BSP_MCP_MAX_JOBS, default: 1)")
    parser.add_argument("--allow-root", action="append", dest="allow_roots",
                        default=_env_roots(),
                        help="Extra directory readable via read_task_log; repeatable "
                             "(env: BSP_MCP_ALLOW_ROOTS, os.pathsep-separated)")
    parser.add_argument("--read-only", action="store_true",
                        default=os.environ.get("BSP_MCP_READ_ONLY") == "1",
                        help="Disable all job, cancel and bump tools (env: BSP_MCP_READ_ONLY=1)")
    parser.add_argument("--transport", choices=["stdio", "http"],
                        default=os.environ.get("BSP_MCP_TRANSPORT", "stdio"),
                        help="Transport: stdio (default) or http (streamable-http)")
    parser.add_argument("--host", default=os.environ.get("BSP_MCP_HOST", "127.0.0.1"),
                        help="HTTP host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("BSP_MCP_PORT", "8765")),
                        help="HTTP port (default: 8765)")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    # stdout carries the MCP stdio protocol — send all logging to stderr.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parse_args(argv)

    ctx = ServerContext(
        registry=args.registry,
        remote=args.remote,
        branch=args.branch,
        state_dir=args.state_dir,
        max_jobs=args.max_jobs,
        allow_roots=args.allow_roots,
        read_only=args.read_only,
    )
    server = build_server(ctx)

    if args.transport == "http":
        if _MCP_V2:
            server.run(transport="streamable-http", host=args.host, port=args.port)
        else:
            server.settings.host = args.host
            server.settings.port = args.port
            server.run(transport="streamable-http")
    else:
        server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
