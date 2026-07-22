"""Direct Lava-Test definition runner for local, SSH, and serial execution backends."""

import datetime
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml
from jinja2 import Environment, FileSystemLoader, TemplateError

from .models import DirectTestConfig, DirectTransportConfig, TestDefinitionSource
from .resolver import ResolvedConfig


_HTML_REPORT_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>BSP Test Report{% if label %} — {{ label }}{% endif %}</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: Inter, "Segoe UI", Roboto, Arial, sans-serif; background: #f3f5f8; color: #1f2937; padding: 1.6rem; line-height: 1.35; }
  .page { max-width: 1280px; margin: 0 auto; }
  h1 { font-size: 1.65rem; margin-bottom: 0.35rem; }
  h2 { font-size: 1.15rem; margin-bottom: 0.5rem; }
  .meta { color: #4b5563; font-size: 0.92rem; margin-bottom: 1rem; }
  .panel { background: #fff; border: 1px solid #d7dde6; border-radius: 8px; padding: 0.9rem 1rem; margin-bottom: 1rem; }
  .badge { display: inline-block; padding: 0.2rem 0.62rem; border-radius: 999px; font-weight: 700; font-size: 0.74rem; letter-spacing: 0.01em; white-space: nowrap; }
  .pass  { background: #d1fae5; color: #065f46; }
  .fail  { background: #fee2e2; color: #991b1b; }
  .warn  { background: #fef3c7; color: #92400e; }
  .timeout { background: #ffedd5; color: #9a3412; }
  .label-row { display: flex; flex-wrap: wrap; gap: 0.55rem; margin-top: 0.35rem; }
  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(185px, 1fr)); gap: 0.6rem; }
  .kpi { border: 1px solid #e3e7ee; border-radius: 6px; padding: 0.55rem 0.7rem; background: #fafbfc; }
  .kpi .name { font-size: 0.78rem; color: #4b5563; margin-bottom: 0.2rem; text-transform: uppercase; letter-spacing: 0.03em; }
  .kpi .value { font-size: 1.2rem; font-weight: 700; color: #111827; }
  .toc-list { list-style: none; display: flex; flex-wrap: wrap; gap: 0.45rem; margin-top: 0.25rem; }
  .toc-list li a { text-decoration: none; color: #1d4ed8; background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 999px; padding: 0.2rem 0.55rem; font-size: 0.78rem; }
  .failure-table { width: 100%; border-collapse: collapse; font-size: 0.86rem; }
  .failure-table th { text-align: left; padding: 0.48rem 0.55rem; background: #fee2e2; color: #7f1d1d; border-bottom: 1px solid #fecaca; }
  .failure-table td { padding: 0.42rem 0.55rem; border-bottom: 1px solid #f3d2d2; vertical-align: top; }
  .failure-table tr:last-child td { border-bottom: none; }
  .suite-card { background: #fff; border: 1px solid #d7dde6; border-radius: 8px; margin-bottom: 1rem; overflow: hidden; }
  .suite-header { display: flex; align-items: center; gap: 0.7rem; padding: 0.7rem 0.9rem; background: #f8fafc; border-bottom: 1px solid #e3e8f0; }
  .suite-header h3 { font-size: 1rem; flex: 1; }
  .suite-duration { color: #4b5563; font-size: 0.82rem; font-weight: 600; }
  .suite-stats { display: flex; flex-wrap: wrap; gap: 1rem; padding: 0.55rem 0.9rem; border-bottom: 1px solid #eef2f7; font-size: 0.82rem; color: #374151; }
  .suite-stats strong { color: #111827; }
  table { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
  th { text-align: left; padding: 0.45rem 0.55rem; background: #f3f6fb; color: #1f2937; border-bottom: 1px solid #dbe2ec; }
  td { padding: 0.4rem 0.55rem; border-bottom: 1px solid #edf1f7; vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  .cmd { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.78rem; background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 4px; padding: 0.14rem 0.3rem; word-break: break-all; }
  details { margin-top: 0.35rem; padding: 0.35rem 0.45rem; background: #fbfcfe; border: 1px dashed #d9e0eb; border-radius: 6px; }
  details > summary { cursor: pointer; color: #374151; font-size: 0.78rem; font-weight: 600; }
  .mini-table { width: 100%; margin-top: 0.35rem; border-collapse: collapse; font-size: 0.78rem; }
  .mini-table th { background: #eef2f7; padding: 0.3rem 0.45rem; border-bottom: 1px solid #dde5f0; }
  .mini-table td { padding: 0.28rem 0.45rem; border-bottom: 1px solid #ebeff5; }
  .mini-table tr:last-child td { border-bottom: none; }
  .issue-note { color: #7f1d1d; font-size: 0.76rem; margin-top: 0.2rem; font-weight: 600; }
  .muted { color: #4b5563; }
  .log-link { white-space: nowrap; }
  .log-link a { color: #1d4ed8; text-decoration: none; }
</style>
</head>
<body>
<div class="page">
<h1>BSP Test Report{% if label %}: {{ label }}{% endif %}</h1>
<p class="meta">
  Generated: {{ generated_at }} &nbsp;|&nbsp;
  Backend: <strong>{{ backend }}</strong> &nbsp;|&nbsp;
  Overall:
  <span class="badge {{ 'pass' if passed else 'fail' }}">{{ 'PASS' if passed else 'FAIL' }}</span>
</p>

<div class="panel">
  <h2>Aggregate summary</h2>
  <div class="kpi-grid">
    <div class="kpi"><div class="name">Total suites</div><div class="value">{{ report.total_suites }}</div></div>
    <div class="kpi"><div class="name">Suites with issues</div><div class="value">{{ report.failing_suites }}</div></div>
    <div class="kpi"><div class="name">Total steps</div><div class="value">{{ report.total_steps }}</div></div>
    <div class="kpi"><div class="name">Failed steps</div><div class="value">{{ report.failed_steps }}</div></div>
    <div class="kpi"><div class="name">Timed-out steps</div><div class="value">{{ report.timed_out_steps }}</div></div>
    <div class="kpi"><div class="name">Failed LAVA cases</div><div class="value">{{ report.failed_lava_cases }}</div></div>
  </div>
</div>

{% if preset_info %}
<div class="panel">
  <h2>Preset info</h2>
  <table>
    {% if preset_info.device %}<tr><td>Device:</td><td>{{ preset_info.device }}{% if preset_info.device_description %} — {{ preset_info.device_description }}{% endif %}</td></tr>{% endif %}
    {% if preset_info.release %}<tr><td>Release:</td><td>{{ preset_info.release }}{% if preset_info.release_description %} — {{ preset_info.release_description }}{% endif %}</td></tr>{% endif %}
    {% if preset_info.features %}<tr><td>Features:</td><td>{{ preset_info.features }}</td></tr>{% endif %}
  </table>
</div>
{% endif %}

<div class="panel">
  <h2>Suite navigation</h2>
  {% if suites %}
  <ul class="toc-list">
    {% for suite in suites %}
    <li><a href="#{{ suite.id }}">{{ suite.name }}</a></li>
    {% endfor %}
  </ul>
  {% else %}
  <p class="muted">No suites available.</p>
  {% endif %}
</div>

<div class="panel">
  <h2>Failures first</h2>
  {% if failures %}
  <table class="failure-table">
    <thead>
      <tr>
        <th>Suite</th>
        <th>Step</th>
        <th>Status</th>
        <th>Issue</th>
        <th>Duration</th>
        <th>Command</th>
        <th>Log</th>
      </tr>
    </thead>
    <tbody>
      {% for failure in failures %}
      <tr>
        <td>{{ failure.suite_name }}</td>
        <td>{{ failure.step_name }}</td>
        <td><span class="badge {{ failure.status_class }}">{{ failure.status }}</span></td>
        <td>{{ failure.issue }}</td>
        <td>{{ "%.2f"|format(failure.duration) }}s</td>
        <td><span class="cmd">{{ failure.command | e }}</span></td>
        <td class="log-link">
          {% if failure.log_path %}
          <a href="file://{{ failure.log_path | e }}">open</a>
          {% else %}
          <span class="muted">—</span>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p class="muted">No failures detected.</p>
  {% endif %}
</div>

{% for suite in suites %}
<div class="suite-card" id="{{ suite.id }}">
  <div class="suite-header">
    <span class="badge {{ suite.status_class }}">{{ suite.status }}</span>
    <h3>{{ suite.name }}</h3>
    <span class="suite-duration">{{ "%.2f"|format(suite.duration) }}s</span>
  </div>
  <div class="suite-stats">
    <span>Steps: <strong>{{ suite.total_steps }}</strong></span>
    <span>Failed steps: <strong>{{ suite.failed_steps }}</strong></span>
    <span>Timed out: <strong>{{ suite.timed_out_steps }}</strong></span>
    <span>LAVA cases: <strong>{{ suite.lava_total }}</strong></span>
    <span>Failed LAVA cases: <strong>{{ suite.lava_failed }}</strong></span>
    {% if suite.log_dir %}
    <span>Log dir: <span class="cmd">{{ suite.log_dir | e }}</span></span>
    {% endif %}
  </div>

  {% if suite.cases %}
  <table>
    <thead>
      <tr>
        <th>Step</th>
        <th>Status</th>
        <th>Duration</th>
        <th>Command</th>
        <th>Log</th>
      </tr>
    </thead>
    <tbody>
    {% for case in suite.cases %}
      <tr>
        <td>
          <strong>{{ case.name }}</strong>
          {% if case.timed_out %}<span class="badge timeout">TIMEOUT</span>{% endif %}
          {% if case.issue %}<div class="issue-note">{{ case.issue }}</div>{% endif %}
        </td>
        <td><span class="badge {{ case.status_class }}">{{ case.status }}</span></td>
        <td>{{ "%.2f"|format(case.duration) }}s</td>
        <td><span class="cmd">{{ case.command | e }}</span></td>
        <td class="log-link">
          {% if case.log_path %}
          <a href="file://{{ case.log_path | e }}">open</a>
          {% else %}
          <span class="muted">—</span>
          {% endif %}
        </td>
      </tr>
      <tr>
        <td colspan="5">
          {% if case.has_details %}
          <details>
            <summary>Step details</summary>
            {% if case.params %}
            <p class="muted" style="margin-top:0.3rem;">Parameters used by this step:</p>
            <table class="mini-table">
              <thead>
                <tr><th>NAME</th><th>VALUE</th></tr>
              </thead>
              <tbody>
              {% for key, value in case.params | dictsort %}
                <tr>
                  <td>{{ key | e }}</td>
                  <td><span class="cmd">{{ value | e }}</span></td>
                </tr>
              {% endfor %}
              </tbody>
            </table>
            {% endif %}
            {% if case.lava_signals %}
            <p class="muted" style="margin-top:0.45rem;">LAVA test cases reported by this step:</p>
            <table class="mini-table">
                <thead>
                  <tr><th>TEST_CASE_ID</th><th>RESULT</th></tr>
                </thead>
                <tbody>
                {% for sig in case.lava_signals %}
                  {% set sig_pass = sig.result == 'pass' %}
                  <tr>
                    <td>{{ sig.test_case_id | e }}</td>
                    <td><span class="badge {{ 'pass' if sig_pass else 'fail' }}">{{ sig.result | upper }}</span></td>
                  </tr>
                {% endfor %}
                </tbody>
            </table>
            {% endif %}
          </details>
          {% endif %}
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="panel" style="border: none; border-top: 1px solid #edf1f7; border-radius: 0; margin-bottom: 0;">
    <p class="muted">No steps executed for this suite.</p>
  </div>
  {% endif %}
</div>
{% endfor %}

{% if not suites %}
<p class="muted">No test suites were executed.</p>
{% endif %}
</div>
</body>
</html>
"""


_VAR_BRACE_RE = re.compile(r"(?<!\$)\{([A-Za-z_][A-Za-z0-9_]*)\}")
_VAR_DOLLAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# LAVA signal format (case-insensitive to tolerate minor variations in test scripts):
# <LAVA_SIGNAL_TESTCASE TEST_CASE_ID=<id> RESULT=pass|fail>
_LAVA_SIGNAL_RE = re.compile(
    r"<LAVA_SIGNAL_TESTCASE\s+TEST_CASE_ID=(\S+)\s+RESULT=(pass|fail)>",
    re.IGNORECASE,
)
_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


@dataclass
class LavaSignalCase:
    """A single LAVA test case parsed from a ``<LAVA_SIGNAL_TESTCASE ...>`` signal line."""
    test_case_id: str
    result: str  # "pass" or "fail"

    @property
    def passed(self) -> bool:
        return self.result.lower() == "pass"


@dataclass
class DirectTestCaseResult:
    name: str
    status: str
    duration: float
    command: str
    params: Dict[str, str] = field(default_factory=dict)
    log_path: Optional[str] = None
    timed_out: bool = False
    lava_signals: List["LavaSignalCase"] = field(default_factory=list)
    _execution_succeeded: Optional[bool] = None

    @property
    def execution_succeeded(self) -> bool:
        # Older in-memory/manual results may not populate execution_succeeded,
        # so fall back to the stored status for report rendering.
        if self._execution_succeeded is not None:
            return self._execution_succeeded
        return self.status.upper() == "PASS"

    @property
    def has_failed_lava_signals(self) -> bool:
        return any(not sig.passed for sig in self.lava_signals)

    @property
    def lava_failed_count(self) -> int:
        return sum(1 for sig in self.lava_signals if not sig.passed)

    @property
    def report_status(self) -> str:
        if self.execution_succeeded and self.has_failed_lava_signals:
            return "EXEC PASS / LAVA FAIL"
        return self.status.upper()

    @property
    def report_status_class(self) -> str:
        if self.execution_succeeded and self.has_failed_lava_signals:
            return "warn"
        return "pass" if self.status.upper() == "PASS" else "fail"


@dataclass
class DirectTestSuiteResult:
    name: str
    status: str
    duration: float
    log_dir: Optional[str] = None
    cases: List[DirectTestCaseResult] = field(default_factory=list)

    @property
    def execution_succeeded(self) -> bool:
        """Return True when every step command succeeded, regardless of LAVA signal results."""
        if self.cases:
            return all(case.execution_succeeded for case in self.cases)
        # Suites without explicit cases rely on their stored aggregate status.
        return self.status.upper() == "PASS"

    @property
    def has_failed_lava_signals(self) -> bool:
        return any(case.has_failed_lava_signals for case in self.cases)

    @property
    def report_status(self) -> str:
        if self.execution_succeeded and self.has_failed_lava_signals:
            return "EXEC PASS / LAVA FAIL"
        return self.status.upper()

    @property
    def report_status_class(self) -> str:
        if self.execution_succeeded and self.has_failed_lava_signals:
            return "warn"
        return "pass" if self.status.upper() == "PASS" else "fail"


@dataclass
class DirectRunResult:
    backend: str
    passed: bool
    suites: List[DirectTestSuiteResult] = field(default_factory=list)
    preset_info: Dict[str, str] = field(default_factory=dict)


@dataclass
class DirectRunOverrides:
    backend: Optional[str] = None
    repo_url: Optional[str] = None
    repo_ref: Optional[str] = None
    definition_paths: Optional[List[str]] = None
    local_job_paths: Optional[List[str]] = None
    params: Optional[Dict[str, str]] = None
    timeout: Optional[int] = None
    output_dir: Optional[str] = None
    ssh_host: Optional[str] = None
    ssh_user: Optional[str] = None
    ssh_port: Optional[int] = None
    ssh_key: Optional[str] = None
    ssh_password: Optional[str] = None
    ssh_known_hosts_file: Optional[str] = None
    ssh_strict_host_key_checking: Optional[bool] = None
    ssh_remote_workdir: Optional[str] = None
    ssh_serial_device: Optional[str] = None
    ssh_serial_baudrate: Optional[int] = None


class _BaseTransport:
    def run(
        self,
        command: str,
        cwd: str,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> Tuple[int, str, str]:
        raise NotImplementedError

    def stage_directory(self, local_dir: Path, remote_dir: str) -> None:
        return None

    def collect_directory(self, remote_dir: str, local_dir: Path) -> None:
        return None


class _LocalTransport(_BaseTransport):
    def run(
        self,
        command: str,
        cwd: str,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> Tuple[int, str, str]:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        proc = subprocess.run(
            ["bash", "-lc", command],
            cwd=cwd,
            env=merged_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr


class _SshTransport(_BaseTransport):
    def __init__(self, config: DirectTransportConfig):
        self.config = config

    def _target(self) -> str:
        host = self.config.host or ("localhost" if self.config.serial_device else "")
        if not host:
            raise ValueError("SSH transport requires a host (set testing.direct.transport.host or --ssh-host).")
        if self.config.user:
            return f"{self.config.user}@{host}"
        return host

    def _ssh_common_options(self) -> List[str]:
        opts: List[str] = ["-p", str(self.config.port)]
        strict = "yes" if self.config.strict_host_key_checking else "no"
        opts += ["-o", f"StrictHostKeyChecking={strict}"]
        if self.config.known_hosts_file:
            opts += ["-o", f"UserKnownHostsFile={self.config.known_hosts_file}"]
        if self.config.key_path:
            opts += ["-i", self.config.key_path]
        if self.config.password:
            opts += ["-o", "PreferredAuthentications=password,keyboard-interactive"]
        else:
            opts += ["-o", "BatchMode=yes"]

        if self.config.serial_device:
            serial = self.config.serial_device
            baud = int(self.config.serial_baudrate)
            proxy = f"socat - FILE:{serial},raw,echo=0,b{baud}"
            opts += ["-o", f"ProxyCommand={proxy}"]

        return opts

    def _with_password_prefix(self, base_cmd: List[str]) -> List[str]:
        if not self.config.password:
            return base_cmd
        if shutil.which("sshpass") is None:
            raise RuntimeError("Password authentication requires 'sshpass' to be installed.")
        # sshpass exposes the password in process arguments; prefer key-based
        # authentication whenever possible and use this only when required.
        logging.warning(
            "Using SSH password authentication via sshpass; this can expose credentials in process listings."
        )
        return ["sshpass", "-p", self.config.password, *base_cmd]

    def _run_subprocess(
        self,
        cmd: Sequence[str],
        timeout: Optional[int] = None,
    ) -> Tuple[int, str, str]:
        proc = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def run(
        self,
        command: str,
        cwd: str,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> Tuple[int, str, str]:
        exports = ""
        if env:
            exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
        remote_cmd = f"cd {shlex.quote(cwd)} && "
        if exports:
            remote_cmd += f"export {exports} && "
        remote_cmd += command

        cmd = [
            "ssh",
            *self._ssh_common_options(),
            self._target(),
            f"bash -lc {shlex.quote(remote_cmd)}",
        ]
        return self._run_subprocess(self._with_password_prefix(cmd), timeout=timeout)

    def stage_directory(self, local_dir: Path, remote_dir: str) -> None:
        remote_root = remote_dir.rstrip("/")
        remote_repo_dir = f"{remote_root}/{local_dir.name}"

        # Ensure staged repository content is fresh for each run.
        # This prevents stale test output files from previous runs from being
        # replayed by send-to-lava scripts that process accumulated result logs.
        clean_cmd = [
            "ssh",
            *self._ssh_common_options(),
            self._target(),
            f"mkdir -p {shlex.quote(remote_root)} && rm -rf {shlex.quote(remote_repo_dir)}",
        ]
        rc, _out, err = self._run_subprocess(self._with_password_prefix(clean_cmd))
        if rc != 0:
            raise RuntimeError(f"Failed to prepare remote staging directory '{remote_repo_dir}': {err.strip()}")

        scp_cmd = [
            "scp",
            "-r",
            "-P",
            str(self.config.port),
            *(["-i", self.config.key_path] if self.config.key_path else []),
            "-o",
            f"StrictHostKeyChecking={'yes' if self.config.strict_host_key_checking else 'no'}",
            *(["-o", f"UserKnownHostsFile={self.config.known_hosts_file}"] if self.config.known_hosts_file else []),
            str(local_dir),
            f"{self._target()}:{remote_root}/",
        ]
        rc, _out, err = self._run_subprocess(self._with_password_prefix(scp_cmd))
        if rc != 0:
            raise RuntimeError(f"Failed to stage test files to SSH target: {err.strip()}")

    def collect_directory(self, remote_dir: str, local_dir: Path) -> None:
        local_dir.mkdir(parents=True, exist_ok=True)
        scp_cmd = [
            "scp",
            "-r",
            "-P",
            str(self.config.port),
            *(["-i", self.config.key_path] if self.config.key_path else []),
            "-o",
            f"StrictHostKeyChecking={'yes' if self.config.strict_host_key_checking else 'no'}",
            *(["-o", f"UserKnownHostsFile={self.config.known_hosts_file}"] if self.config.known_hosts_file else []),
            f"{self._target()}:{remote_dir.rstrip('/')}/",
            str(local_dir),
        ]
        self._run_subprocess(self._with_password_prefix(scp_cmd))


class DirectTestRunner:
    def __init__(self, config_path: Path, logger: Optional[logging.Logger] = None):
        self.config_path = config_path
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self._cache_root = Path(tempfile.gettempdir()) / "bsp-test-definitions-cache"

    def run(
        self,
        resolved: ResolvedConfig,
        direct_config: Optional[DirectTestConfig],
        overrides: Optional[DirectRunOverrides] = None,
        label: str = "",
    ) -> DirectRunResult:
        cfg = self._merged_config(direct_config, overrides, resolved)
        backend = (overrides.backend if overrides and overrides.backend else "direct-local").strip() or "direct-local"
        transport = self._build_transport(cfg.transport, backend)

        output_root = Path(cfg.output_dir).expanduser() if cfg.output_dir else (Path(resolved.build_path) / "test-results")
        output_root.mkdir(parents=True, exist_ok=True)

        overall_pass = True
        suites: List[DirectTestSuiteResult] = []

        for source_index, source in enumerate(cfg.definitions):
            repo_dir = self._prepare_source_repo(source)
            def_files = self._resolve_definition_files(repo_dir, source.paths)
            if not def_files:
                source_id = source.repo_url or source.local_dir or "<unknown>"
                raise RuntimeError(f"No test-definition YAML files found for source '{source_id}'.")

            run_root = output_root / f"source-{source_index + 1}"
            run_root.mkdir(parents=True, exist_ok=True)

            repo_exec_root = repo_dir
            if isinstance(transport, _SshTransport):
                remote_root = cfg.transport.remote_workdir.rstrip("/")
                remote_source = f"{remote_root}/{repo_dir.name}"
                transport.stage_directory(repo_dir, remote_root)
                repo_exec_root = Path(remote_source)

            for def_file in def_files:
                entry_definitions = self._extract_lava_job_test_definitions(self._load_definition(def_file))
                if entry_definitions:
                    for entry_path, entry_params in entry_definitions:
                        entry_files = self._resolve_definition_files(repo_dir, [entry_path])
                        if not entry_files:
                            raise RuntimeError(
                                f"No test-definition YAML files found for LAVA job entry path "
                                f"'{entry_path}' in '{def_file}'."
                            )

                        merged_params = {k: str(v) for k, v in source.params.items()}
                        merged_params.update({k: str(v) for k, v in entry_params.items()})
                        for entry_file in entry_files:
                            rel = entry_file.relative_to(repo_dir)
                            suite_result = self._run_single_definition(
                                transport=transport,
                                suite_path=entry_file,
                                suite_rel_path=rel,
                                repo_local_root=repo_dir,
                                repo_exec_root=str(repo_exec_root),
                                params=merged_params,
                                timeout=cfg.timeout,
                                continue_on_failure=cfg.continue_on_failure,
                                output_root=run_root,
                            )
                            suites.append(suite_result)
                            if suite_result.status != "PASS":
                                overall_pass = False
                    continue

                rel = def_file.relative_to(repo_dir)
                suite_result = self._run_single_definition(
                    transport=transport,
                    suite_path=def_file,
                    suite_rel_path=rel,
                    repo_local_root=repo_dir,
                    repo_exec_root=str(repo_exec_root),
                    params=source.params,
                    timeout=cfg.timeout,
                    continue_on_failure=cfg.continue_on_failure,
                    output_root=run_root,
                )
                suites.append(suite_result)
                if suite_result.status != "PASS":
                    overall_pass = False

        if isinstance(transport, _SshTransport):
            remote_root = cfg.transport.remote_workdir.rstrip("/")
            transport.collect_directory(remote_root, output_root / "remote-logs")

        preset_info = self._extract_preset_info(resolved)
        self._write_summary(output_root, label=label, backend=backend, suites=suites, passed=overall_pass, preset_info=preset_info)
        return DirectRunResult(backend=backend, passed=overall_pass, suites=suites, preset_info=preset_info)

    def _render_job_template(
        self,
        template_text: str,
        template_path: Path,
        resolved: Optional["ResolvedConfig"],
        extra_params: Dict[str, str],
    ) -> str:
        """Render a Jinja2 job template to a YAML string.

        The template receives a context built from *resolved* (when available)
        and the caller-supplied *extra_params*.  All variables that are safe to
        include when *resolved* is ``None`` (e.g. no preset was provided) are
        still present with empty/default values so that templates can guard
        with ``{% if device_slug %}`` etc.

        Context variables
        -----------------
        ``device_slug``   – ``resolved.device.slug`` or ``""``
        ``release_slug``  – ``resolved.release.slug`` or ``""``
        ``feature_slugs`` – list of active feature slugs or ``[]``
        ``build_path``    – ``resolved.build_path`` or ``""``
        ``params``        – the *extra_params* dict (CLI ``--test-param`` values)
        """
        env = Environment(
            loader=FileSystemLoader(str(template_path.parent)),
            keep_trailing_newline=True,
        )

        device_slug: str = ""
        release_slug: str = ""
        feature_slugs: List[str] = []
        build_path: str = ""
        if resolved is not None:
            device = getattr(resolved, "device", None)
            if device is not None:
                device_slug = getattr(device, "slug", "") or ""
            release = getattr(resolved, "release", None)
            if release is not None:
                release_slug = getattr(release, "slug", "") or ""
            features = getattr(resolved, "features", []) or []
            feature_slugs = [
                getattr(f, "slug", "") for f in features if getattr(f, "slug", None)
            ]
            build_path = getattr(resolved, "build_path", "") or ""

        context: Dict[str, Any] = {
            "device_slug": device_slug,
            "release_slug": release_slug,
            "feature_slugs": feature_slugs,
            "build_path": build_path,
            "params": extra_params,
        }
        try:
            template = env.get_template(template_path.name)
            return template.render(**context)
        except TemplateError as exc:
            raise RuntimeError(
                f"Failed to render Jinja2 job template '{template_path}': {exc}"
            ) from exc

    def _merged_config(
        self,
        direct_config: Optional[DirectTestConfig],
        overrides: Optional[DirectRunOverrides],
        resolved: Optional["ResolvedConfig"] = None,
    ) -> DirectTestConfig:
        base = direct_config or DirectTestConfig()
        transport = base.transport or DirectTransportConfig()

        definitions: List[TestDefinitionSource] = [
            TestDefinitionSource(
                repo_url=s.repo_url,
                ref=s.ref,
                paths=list(s.paths),
                params=dict(s.params),
                local_dir=s.local_dir,
            )
            for s in base.definitions
        ]

        if overrides and overrides.repo_url:
            if definitions:
                definitions[0].repo_url = overrides.repo_url
            else:
                definitions = [TestDefinitionSource(repo_url=overrides.repo_url)]
        if overrides and overrides.repo_ref:
            if definitions:
                definitions[0].ref = overrides.repo_ref
        if overrides and overrides.definition_paths:
            if definitions:
                definitions[0].paths = list(overrides.definition_paths)
        if overrides and overrides.params:
            if definitions:
                merged = dict(definitions[0].params)
                merged.update({k: str(v) for k, v in overrides.params.items()})
                definitions[0].params = merged

        job_timeout_seconds: Optional[int] = None
        # Each --test-job-path is a local LAVA job YAML file.  We parse the
        # file and create one TestDefinitionSource per test-definition entry:
        #   * entries with `from: git` and a `repository` URL are cloned from
        #     that remote repository (ref = branch/revision in the entry);
        #   * entries without a repository are resolved relative to the job
        #     file's parent directory (local fallback).
        if overrides and overrides.local_job_paths:
            extra_params: Dict[str, str] = {}
            if overrides.params:
                extra_params = {k: str(v) for k, v in overrides.params.items()}
            for job_path in overrides.local_job_paths:
                abs_job = Path(job_path).expanduser().resolve()
                if not abs_job.is_file():
                    raise RuntimeError(
                        f"Local test-definition file does not exist: '{abs_job}'."
                    )
                try:
                    text = abs_job.read_text(encoding="utf-8")
                    suffix = abs_job.suffix.lower()
                    if suffix in (".jinja2", ".j2"):
                        text = self._render_job_template(text, abs_job, resolved, extra_params)
                    raw = yaml.safe_load(text) or {}
                except (OSError, yaml.YAMLError) as exc:
                    raise RuntimeError(
                        f"Failed to read LAVA job '{abs_job}': {exc}"
                    ) from exc
                for action in raw.get("actions", []):
                    if not isinstance(action, dict):
                        continue
                    test_block = action.get("test")
                    if not isinstance(test_block, dict):
                        continue
                    timeout_block = test_block.get("timeout")
                    if isinstance(timeout_block, dict):
                        minutes = timeout_block.get("minutes")
                        if minutes is not None:
                            try:
                                seconds = int(minutes) * 60
                            except (TypeError, ValueError):
                                seconds = None
                            if seconds is not None and seconds > 0:
                                job_timeout_seconds = max(job_timeout_seconds or 0, seconds)
                    for test_def in test_block.get("definitions", []):
                        if not isinstance(test_def, dict):
                            continue
                        path = test_def.get("path")
                        if not path:
                            continue
                        repository = test_def.get("repository")
                        from_type = test_def.get("from", "git" if repository else "")
                        params_raw = test_def.get("parameters", {})
                        entry_params: Dict[str, str] = (
                            {str(k): str(v) for k, v in params_raw.items()}
                            if isinstance(params_raw, dict)
                            else {}
                        )
                        merged_params = dict(extra_params)
                        merged_params.update(entry_params)
                        if from_type == "git" and repository:
                            branch = test_def.get("branch", "")
                            revision = test_def.get("revision", "")
                            ref = revision or branch or ""
                            definitions.append(
                                TestDefinitionSource(
                                    repo_url=str(repository),
                                    ref=ref,
                                    paths=[str(path)],
                                    params=merged_params,
                                )
                            )
                        else:
                            definitions.append(
                                TestDefinitionSource(
                                    local_dir=str(abs_job.parent),
                                    paths=[str(path)],
                                    params=merged_params,
                                )
                            )

        if not definitions:
            raise RuntimeError(
                "Direct test backend requires at least one test-definition source "
                "(testing.direct.definitions or --test-job-path)."
            )

        if overrides and overrides.ssh_host is not None:
            transport.host = overrides.ssh_host
        if overrides and overrides.ssh_user is not None:
            transport.user = overrides.ssh_user
        if overrides and overrides.ssh_port is not None:
            transport.port = int(overrides.ssh_port)
        if overrides and overrides.ssh_key is not None:
            transport.key_path = overrides.ssh_key
        if overrides and overrides.ssh_password is not None:
            transport.password = overrides.ssh_password
        if overrides and overrides.ssh_known_hosts_file is not None:
            transport.known_hosts_file = overrides.ssh_known_hosts_file
        if overrides and overrides.ssh_strict_host_key_checking is not None:
            transport.strict_host_key_checking = bool(overrides.ssh_strict_host_key_checking)
        if overrides and overrides.ssh_remote_workdir is not None:
            transport.remote_workdir = overrides.ssh_remote_workdir
        if overrides and overrides.ssh_serial_device is not None:
            transport.serial_device = overrides.ssh_serial_device
        if overrides and overrides.ssh_serial_baudrate is not None:
            transport.serial_baudrate = int(overrides.ssh_serial_baudrate)

        timeout = base.timeout
        if overrides and overrides.timeout is not None:
            timeout = int(overrides.timeout)
        elif overrides and overrides.local_job_paths and job_timeout_seconds is not None:
            timeout = job_timeout_seconds

        output_dir = base.output_dir
        if overrides and overrides.output_dir is not None:
            output_dir = overrides.output_dir

        return DirectTestConfig(
            definitions=definitions,
            transport=transport,
            timeout=timeout,
            output_dir=output_dir,
            continue_on_failure=base.continue_on_failure,
        )

    def _build_transport(self, transport: DirectTransportConfig, backend: str) -> _BaseTransport:
        mode = transport.mode or "local"
        if backend in ("direct-ssh", "direct-serial"):
            mode = "ssh"
        elif backend == "direct-local":
            mode = "local"
        if backend == "direct-serial" and not transport.serial_device:
            raise ValueError(
                "direct-serial backend requires a serial device "
                "(set testing.direct.transport.serial_device or --ssh-serial-device)."
            )

        if mode == "ssh":
            return _SshTransport(transport)
        return _LocalTransport()

    def _prepare_source_repo(self, source: TestDefinitionSource) -> Path:
        if source.local_dir:
            local = Path(source.local_dir).expanduser().resolve()
            if not local.is_dir():
                raise RuntimeError(
                    f"Local test-definition directory does not exist: '{local}'."
                )
            return local

        if not source.repo_url:
            raise RuntimeError("Direct test-definition source is missing repo_url.")

        self._cache_root.mkdir(parents=True, exist_ok=True)
        cache_key = hashlib.sha1(source.repo_url.encode("utf-8")).hexdigest()[:12]
        repo_dir = self._cache_root / f"repo-{cache_key}"

        if (repo_dir / ".git").exists():
            self._git(["fetch", "--all", "--tags"], cwd=repo_dir)
        else:
            self._git(["clone", source.repo_url, str(repo_dir)], cwd=self._cache_root)

        requested_ref = (source.ref or "").strip()
        if not requested_ref:
            # Follow the remote default branch tip when no ref is provided.
            target_ref = "refs/remotes/origin/HEAD"
        elif self._git_ref_exists(repo_dir, f"refs/remotes/origin/{requested_ref}"):
            # Use the fetched remote-tracking ref so branch-based refs always
            # resolve to the latest commit from origin.
            target_ref = f"refs/remotes/origin/{requested_ref}"
        else:
            target_ref = requested_ref

        self._git(["checkout", "--detach", target_ref], cwd=repo_dir)

        return repo_dir

    def _git_ref_exists(self, cwd: Path, ref: str) -> bool:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0

    def _git(self, args: List[str], cwd: Path) -> None:
        cmd = ["git", *args]
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"Git command failed: {' '.join(cmd)}\n"
                f"stdout: {proc.stdout.strip()}\n"
                f"stderr: {proc.stderr.strip()}"
            )

    def _resolve_definition_files(self, repo_dir: Path, paths: List[str]) -> List[Path]:
        search_paths = paths or ["."]
        found: List[Path] = []
        seen = set()

        for p in search_paths:
            candidate = (repo_dir / p).resolve()
            if candidate.is_file() and candidate.suffix in (".yaml", ".yml"):
                if candidate not in seen:
                    found.append(candidate)
                    seen.add(candidate)
                continue

            if candidate.is_dir():
                matches = sorted(candidate.rglob("*.yaml")) + sorted(candidate.rglob("*.yml"))
                for match in matches:
                    if match.is_file() and match not in seen:
                        found.append(match)
                        seen.add(match)
                continue

            # Glob inside repo root
            for match in sorted(repo_dir.glob(p)):
                if match.is_file() and match.suffix in (".yaml", ".yml") and match not in seen:
                    found.append(match)
                    seen.add(match)

        return found

    def _load_definition(self, path: Path) -> Dict:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise RuntimeError(f"Failed to read test-definition '{path}': {exc}") from exc

        if not isinstance(raw, dict):
            raise RuntimeError(f"Invalid test-definition format in '{path}'.")
        return raw

    def _extract_steps(self, definition: Dict) -> List[str]:
        run = definition.get("run") or {}
        steps = run.get("steps") if isinstance(run, dict) else None
        if not isinstance(steps, list):
            return []

        commands: List[str] = []
        for step in steps:
            if isinstance(step, str):
                commands.append(step)
            elif isinstance(step, dict):
                cmd = step.get("command") or step.get("run") or ""
                if cmd:
                    commands.append(str(cmd))
        return commands

    def _extract_lava_job_test_definitions(self, definition: Dict) -> List[Tuple[str, Dict[str, str]]]:
        actions = definition.get("actions")
        if not isinstance(actions, list):
            return []

        entries: List[Tuple[str, Dict[str, str]]] = []
        seen_entries = set()
        for action in actions:
            if not isinstance(action, dict):
                continue
            test_block = action.get("test")
            if not isinstance(test_block, dict):
                continue
            definitions = test_block.get("definitions")
            if not isinstance(definitions, list):
                continue

            for test_def in definitions:
                if not isinstance(test_def, dict):
                    continue
                path = test_def.get("path")
                if not path:
                    continue
                params = test_def.get("parameters")
                params_dict = {str(k): str(v) for k, v in params.items()} if isinstance(params, dict) else {}
                entry_key = (str(path), tuple(sorted(params_dict.items())))
                if entry_key in seen_entries:
                    continue
                seen_entries.add(entry_key)
                entries.append((str(path), params_dict))
        return entries

    def _expand_vars(self, text: str, params: Dict[str, str]) -> str:
        def repl_brace(match: re.Match) -> str:
            key = match.group(1)
            return str(params.get(key, match.group(0)))

        def repl_dollar(match: re.Match) -> str:
            key = match.group(1)
            return str(params.get(key, match.group(0)))

        out = _VAR_BRACE_RE.sub(repl_brace, text)
        out = _VAR_DOLLAR_RE.sub(repl_dollar, out)
        return out

    def _updated_cwd_after_step(self, current_cwd: str, command: str) -> str:
        try:
            tokens = shlex.split(command)
        except ValueError:
            return current_cwd
        if len(tokens) != 2 or tokens[0] != "cd":
            return current_cwd

        target = tokens[1]
        if target == "-":
            return current_cwd
        if os.path.isabs(target):
            return os.path.normpath(target)
        return os.path.normpath(str(Path(current_cwd) / target))

    @staticmethod
    def _parse_lava_signals(output: str) -> List[LavaSignalCase]:
        """Parse ``<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=... RESULT=pass|fail>`` lines from output."""
        results: List[LavaSignalCase] = []
        for match in _LAVA_SIGNAL_RE.finditer(output):
            results.append(LavaSignalCase(
                test_case_id=match.group(1),
                result=match.group(2).lower(),
            ))
        return results

    def _run_single_definition(
        self,
        transport: _BaseTransport,
        suite_path: Path,
        suite_rel_path: Path,
        repo_local_root: Path,
        repo_exec_root: str,
        params: Dict[str, str],
        timeout: int,
        continue_on_failure: bool,
        output_root: Path,
    ) -> DirectTestSuiteResult:
        definition = self._load_definition(suite_path)
        metadata = definition.get("metadata") if isinstance(definition.get("metadata"), dict) else {}
        suite_name = metadata.get("name") or suite_path.stem
        suite_display_name = str(suite_name)

        def_params = definition.get("params") if isinstance(definition.get("params"), dict) else {}
        merged_params = {k: str(v) for k, v in def_params.items()}
        merged_params.update({k: str(v) for k, v in params.items()})
        params_str = (
            " [" + " ".join(f"{k}={v}" for k, v in sorted(merged_params.items())) + "]"
            if merged_params
            else ""
        )

        steps = self._extract_steps(definition)
        suite_log_dir = output_root / suite_rel_path.parent / suite_rel_path.stem
        suite_log_dir.mkdir(parents=True, exist_ok=True)

        case_results: List[DirectTestCaseResult] = []
        suite_start = time.monotonic()
        suite_pass = True

        run_cwd = str(Path(repo_exec_root))

        for idx, raw_cmd in enumerate(steps, start=1):
            expanded = self._expand_vars(str(raw_cmd), merged_params)
            step_name = f"step-{idx}"
            log_file = suite_log_dir / f"{step_name}.log"
            total_steps = len(steps)
            spinner = _SPINNER_FRAMES[(idx - 1) % len(_SPINNER_FRAMES)]

            print(
                f"[direct-test] {spinner} {suite_display_name} {step_name} ({idx}/{total_steps}){params_str} running",
                flush=True,
            )

            start = time.monotonic()
            timed_out = False
            try:
                rc, stdout, stderr = transport.run(
                    expanded,
                    cwd=run_cwd,
                    env=merged_params if merged_params else None,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                rc = 124
                raw_stdout = exc.stdout if hasattr(exc, "stdout") else ""
                raw_stderr = exc.stderr if hasattr(exc, "stderr") else ""
                if isinstance(raw_stdout, bytes):
                    stdout = raw_stdout.decode("utf-8", errors="replace")
                else:
                    stdout = raw_stdout or ""
                if isinstance(raw_stderr, bytes):
                    stderr = raw_stderr.decode("utf-8", errors="replace")
                else:
                    stderr = raw_stderr or ""
                stderr = f"{stderr}\nCommand timed out".strip()
                timed_out = True
            except Exception as exc:
                rc = 1
                stdout = ""
                stderr = str(exc)

            duration = time.monotonic() - start
            status = "PASS" if rc == 0 else "FAIL"
            lava_signals = self._parse_lava_signals(stdout)
            lava_failures = sum(1 for sig in lava_signals if not sig.passed)

            if status == "PASS" and lava_failures > 0:
                status = "FAIL"
                status_mark = "❌"
                status_suffix = f" (LAVA {lava_failures} failed)"
            elif status == "PASS":
                status_mark = "✅"
                status_suffix = ""
            else:
                status_mark = "❌"
                status_suffix = ""

            if status != "PASS":
                suite_pass = False

            print(
                f"[direct-test] {status_mark} {suite_display_name} {step_name} ({idx}/{total_steps}){params_str} "
                f"{status}{status_suffix} in {duration:.2f}s",
                flush=True,
            )

            log_file.write_text(
                f"# command\n{expanded}\n\n"
                f"# return_code\n{rc}\n\n"
                f"# stdout\n{stdout}\n\n"
                f"# stderr\n{stderr}\n",
                encoding="utf-8",
            )

            case_results.append(
                DirectTestCaseResult(
                    name=step_name,
                    status=status,
                    duration=duration,
                    command=expanded,
                    params=dict(sorted(merged_params.items())),
                    log_path=str(log_file),
                    timed_out=timed_out,
                    lava_signals=lava_signals,
                    _execution_succeeded=(rc == 0),
                )
            )

            if rc == 0:
                run_cwd = self._updated_cwd_after_step(run_cwd, expanded)

            if rc != 0 and not continue_on_failure:
                # Default behavior is fail-fast within a suite; set
                # continue_on_failure to execute all remaining steps.
                break

        suite_duration = time.monotonic() - suite_start
        return DirectTestSuiteResult(
            name=suite_display_name,
            status="PASS" if suite_pass else "FAIL",
            duration=suite_duration,
            log_dir=str(suite_log_dir),
            cases=case_results,
        )

    def _extract_preset_info(self, resolved: "ResolvedConfig") -> Dict[str, str]:
        """Extract BSP preset information from a resolved config for report display."""
        info: Dict[str, str] = {}
        device = getattr(resolved, "device", None)
        release = getattr(resolved, "release", None)
        features = getattr(resolved, "features", None) or []
        if device:
            slug = getattr(device, "slug", "")
            if slug:
                info["device"] = slug
            desc = getattr(device, "description", "")
            if desc:
                info["device_description"] = desc
        if release:
            slug = getattr(release, "slug", "")
            if slug:
                info["release"] = slug
            desc = getattr(release, "description", "")
            if desc:
                info["release_description"] = desc
        if features:
            feature_slugs = ", ".join(
                getattr(f, "slug", str(f)) for f in features
            )
            if feature_slugs:
                info["features"] = feature_slugs
        return info

    def _write_summary(
        self,
        output_dir: Path,
        label: str,
        backend: str,
        suites: List[DirectTestSuiteResult],
        passed: bool,
        preset_info: Optional[Dict[str, str]] = None,
    ) -> None:
        summary: Dict[str, Any] = {
            "label": label,
            "backend": backend,
            "passed": passed,
            "suite_count": len(suites),
            "suites": [
                {
                    "name": s.name,
                    "status": s.status,
                    "duration": s.duration,
                    "log_dir": s.log_dir,
                    "cases": [
                        {
                            "name": c.name,
                            "status": c.status,
                            "duration": c.duration,
                            "command": c.command,
                            "params": c.params,
                            "log_path": c.log_path,
                            "timed_out": c.timed_out,
                            "lava_signals": [
                                {"test_case_id": sig.test_case_id, "result": sig.result}
                                for sig in c.lava_signals
                            ],
                        }
                        for c in s.cases
                    ],
                }
                for s in suites
            ],
        }
        if preset_info:
            summary["preset"] = preset_info
        (output_dir / "direct-test-summary.json").write_text(
            json.dumps(summary, indent=2),
            encoding="utf-8",
        )

        html_content = self._render_html_report(
            label=label,
            backend=backend,
            suites=suites,
            passed=passed,
            preset_info=preset_info,
        )
        html_path = output_dir / "direct-test-report.html"
        html_path.write_text(html_content, encoding="utf-8")
        self.logger.debug("HTML test report written to %s", html_path)

        self._write_pdf_report(output_dir / "direct-test-report.pdf", html_content)

    def _render_html_report(
        self,
        label: str,
        backend: str,
        suites: List[DirectTestSuiteResult],
        passed: bool,
        preset_info: Optional[Dict[str, str]] = None,
    ) -> str:
        report_context = self._build_html_report_context(suites)
        env = Environment(autoescape=True)
        tmpl = env.from_string(_HTML_REPORT_TEMPLATE)
        return tmpl.render(
            label=label,
            backend=backend,
            suites=report_context["suites"],
            report=report_context["report"],
            failures=report_context["failures"],
            passed=passed,
            preset_info=preset_info or {},
            generated_at=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        )

    @staticmethod
    def _build_html_report_context(suites: List[DirectTestSuiteResult]) -> Dict[str, Any]:
        rendered_suites: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []

        total_steps = 0
        failed_steps = 0
        timed_out_steps = 0
        total_lava_cases = 0
        failed_lava_cases = 0

        for idx, suite in enumerate(suites, start=1):
            suite_lava_total = 0
            suite_lava_failed = 0
            suite_failed_steps = 0
            suite_timed_out = 0
            rendered_cases: List[Dict[str, Any]] = []

            for case in suite.cases:
                case_lava_failed = case.lava_failed_count
                case_lava_total = len(case.lava_signals)
                if not case.execution_succeeded:
                    suite_failed_steps += 1
                if case.timed_out:
                    suite_timed_out += 1
                suite_lava_total += case_lava_total
                suite_lava_failed += case_lava_failed

                if case.timed_out:
                    issue = "Timed out"
                elif not case.execution_succeeded:
                    issue = "Command failed"
                elif case_lava_failed:
                    issue = f"LAVA failures: {case_lava_failed}"
                else:
                    issue = ""

                case_data = {
                    "name": case.name,
                    "status": case.report_status,
                    "status_class": case.report_status_class,
                    "duration": case.duration,
                    "command": case.command,
                    "params": case.params,
                    "timed_out": case.timed_out,
                    "log_path": case.log_path,
                    "lava_signals": case.lava_signals,
                    "lava_total": case_lava_total,
                    "lava_failed": case_lava_failed,
                    "issue": issue,
                    "has_details": bool(case.params or case.lava_signals),
                }
                rendered_cases.append(case_data)

                if issue:
                    failures.append(
                        {
                            "suite_name": suite.name,
                            "step_name": case.name,
                            "status": case.report_status,
                            "status_class": case.report_status_class,
                            "issue": issue,
                            "duration": case.duration,
                            "command": case.command,
                            "log_path": case.log_path,
                        }
                    )

            suite_data = {
                "id": f"suite-{idx}",
                "name": suite.name,
                "status": suite.report_status,
                "status_class": suite.report_status_class,
                "duration": suite.duration,
                "log_dir": suite.log_dir,
                "cases": rendered_cases,
                "total_steps": len(rendered_cases),
                "failed_steps": suite_failed_steps,
                "timed_out_steps": suite_timed_out,
                "lava_total": suite_lava_total,
                "lava_failed": suite_lava_failed,
                "has_issues": bool(suite_failed_steps or suite_timed_out or suite_lava_failed or suite.report_status_class != "pass"),
            }
            rendered_suites.append(suite_data)

            total_steps += len(rendered_cases)
            failed_steps += suite_failed_steps
            timed_out_steps += suite_timed_out
            total_lava_cases += suite_lava_total
            failed_lava_cases += suite_lava_failed

        rendered_suites.sort(key=lambda suite: (not suite["has_issues"], suite["name"]))
        failures.sort(key=lambda item: (item["suite_name"], item["step_name"]))

        report = {
            "total_suites": len(rendered_suites),
            "failing_suites": sum(1 for suite in rendered_suites if suite["has_issues"]),
            "total_steps": total_steps,
            "failed_steps": failed_steps,
            "timed_out_steps": timed_out_steps,
            "total_lava_cases": total_lava_cases,
            "failed_lava_cases": failed_lava_cases,
        }
        return {"suites": rendered_suites, "failures": failures, "report": report}

    def _write_pdf_report(self, pdf_path: Path, html_content: str) -> None:
        try:
            import weasyprint  # type: ignore[import]
        except ImportError:
            self.logger.debug(
                "weasyprint is not installed; skipping PDF report generation. "
                "Install it with: pip install weasyprint"
            )
            return
        try:
            weasyprint.HTML(string=html_content).write_pdf(str(pdf_path))
            self.logger.debug("PDF test report written to %s", pdf_path)
        except Exception as exc:  # pragma: no cover
            self.logger.warning("PDF report generation failed: %s", exc)
