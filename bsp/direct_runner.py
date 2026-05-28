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
from typing import Dict, List, Optional, Sequence, Tuple

import yaml
from jinja2 import Environment

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
  body { font-family: system-ui, Arial, sans-serif; background: #f4f6f9; color: #222; padding: 2rem; }
  h1 { font-size: 1.6rem; margin-bottom: 0.25rem; }
  .meta { color: #555; font-size: 0.9rem; margin-bottom: 1.5rem; }
  .badge { display: inline-block; padding: 0.25rem 0.75rem; border-radius: 4px;
           font-weight: bold; font-size: 0.85rem; }
  .pass  { background: #d4edda; color: #155724; }
  .fail  { background: #f8d7da; color: #721c24; }
  .skip  { background: #fff3cd; color: #856404; }
  .card  { background: #fff; border: 1px solid #dee2e6; border-radius: 6px;
           margin-bottom: 1.25rem; overflow: hidden; }
  .card-header { display: flex; align-items: center; gap: 0.75rem;
                 padding: 0.75rem 1rem; background: #f8f9fa;
                 border-bottom: 1px solid #dee2e6; }
  .card-header h2 { font-size: 1rem; flex: 1; }
  .card-header .duration { font-size: 0.8rem; color: #777; }
  table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
  th { text-align: left; padding: 0.5rem 1rem; background: #f1f3f5;
       border-bottom: 1px solid #dee2e6; font-weight: 600; }
  td { padding: 0.45rem 1rem; border-bottom: 1px solid #f0f0f0; }
  tr:last-child td { border-bottom: none; }
  .summary-bar { display: flex; gap: 1.5rem; padding: 0.75rem 1rem;
                 background: #e9ecef; font-size: 0.9rem; font-weight: 600; }
  .timeout-tag { font-size: 0.75rem; color: #e67e22; margin-left: 0.4rem; }
  code { font-size: 0.8rem; background: #f1f3f5; padding: 0.1rem 0.3rem;
         border-radius: 3px; word-break: break-all; }
  .lava-signals { margin: 0.25rem 0 0.25rem 1.5rem; }
  .lava-signals table { background: #fafbfc; font-size: 0.82rem; border: 1px solid #e0e0e0; border-radius: 4px; }
  .lava-signals th { background: #eff1f3; padding: 0.3rem 0.75rem; font-size: 0.8rem; }
  .lava-signals td { padding: 0.3rem 0.75rem; border-bottom: 1px solid #efefef; }
  .lava-signals tr:last-child td { border-bottom: none; }
  .lava-label { font-size: 0.72rem; color: #555; font-style: italic; margin-bottom: 0.15rem; }
</style>
</head>
<body>

<h1>BSP Test Report{% if label %}: {{ label }}{% endif %}</h1>
<p class="meta">
  Generated: {{ generated_at }} &nbsp;|&nbsp;
  Backend: <strong>{{ backend }}</strong> &nbsp;|&nbsp;
  Overall: <span class="badge {{ 'pass' if passed else 'fail' }}">
    {{ 'PASS' if passed else 'FAIL' }}
  </span>
</p>

{% for suite in suites %}
  {% set suite_pass = (suite.status | upper) == 'PASS' %}
  {% set total = suite.cases | length %}
  {% set n_pass = suite.cases | selectattr('status', 'equalto', 'PASS') | list | length %}
  {% set n_fail = total - n_pass %}
  {# Count LAVA signal cases across the suite #}
  {% set lava_total = namespace(v=0) %}
  {% set lava_pass  = namespace(v=0) %}
  {% for case in suite.cases %}
    {% for sig in case.lava_signals %}
      {% set lava_total.v = lava_total.v + 1 %}
      {% if sig.result == 'pass' %}{% set lava_pass.v = lava_pass.v + 1 %}{% endif %}
    {% endfor %}
  {% endfor %}
<div class="card">
  <div class="card-header">
    <span class="badge {{ 'pass' if suite_pass else 'fail' }}">
      {{ suite.status | upper }}
    </span>
    <h2>{{ suite.name }}</h2>
    <span class="duration">{{ "%.2f"|format(suite.duration) }}s</span>
  </div>
  <div class="summary-bar">
    <span>Steps: {{ total }}</span>
    <span style="color:#155724">Pass: {{ n_pass }}</span>
    {% if n_fail %}<span style="color:#721c24">Fail: {{ n_fail }}</span>{% endif %}
    {% if lava_total.v %}
      &nbsp;|&nbsp;
      <span>LAVA cases: {{ lava_total.v }}</span>
      <span style="color:#155724">Pass: {{ lava_pass.v }}</span>
      {% if lava_total.v - lava_pass.v %}<span style="color:#721c24">Fail: {{ lava_total.v - lava_pass.v }}</span>{% endif %}
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
      </tr>
    </thead>
    <tbody>
    {% for case in suite.cases %}
      {% set case_pass = (case.status | upper) == 'PASS' %}
      <tr>
        <td>
          {{ case.name }}
          {% if case.timed_out %}
            <span class="timeout-tag">⏱ timed-out</span>
          {% endif %}
        </td>
        <td><span class="badge {{ 'pass' if case_pass else 'fail' }}">{{ case.status | upper }}</span></td>
        <td>{{ "%.2f"|format(case.duration) }}s</td>
        <td><code>{{ case.command | e }}</code></td>
      </tr>
      {% if case.lava_signals %}
      <tr>
        <td colspan="4" style="padding: 0 0 0.5rem 0; border-bottom: 1px solid #f0f0f0;">
          <div class="lava-signals">
            <div class="lava-label">LAVA test cases reported by this step:</div>
            <table>
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
          </div>
        </td>
      </tr>
      {% endif %}
    {% endfor %}
    </tbody>
  </table>
  {% endif %}
</div>
{% endfor %}

{% if not suites %}
<p style="color:#777">No test suites were executed.</p>
{% endif %}

</body>
</html>
"""


_VAR_BRACE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_VAR_DOLLAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# LAVA signal format (case-insensitive to tolerate minor variations in test scripts):
# <LAVA_SIGNAL_TESTCASE TEST_CASE_ID=<id> RESULT=pass|fail>
_LAVA_SIGNAL_RE = re.compile(
    r"<LAVA_SIGNAL_TESTCASE\s+TEST_CASE_ID=(\S+)\s+RESULT=(pass|fail)>",
    re.IGNORECASE,
)


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
    log_path: Optional[str] = None
    timed_out: bool = False
    lava_signals: List["LavaSignalCase"] = field(default_factory=list)


@dataclass
class DirectTestSuiteResult:
    name: str
    status: str
    duration: float
    log_dir: Optional[str] = None
    cases: List[DirectTestCaseResult] = field(default_factory=list)


@dataclass
class DirectRunResult:
    backend: str
    passed: bool
    suites: List[DirectTestSuiteResult] = field(default_factory=list)


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
        mkdir_cmd = [
            "ssh",
            *self._ssh_common_options(),
            self._target(),
            f"mkdir -p {shlex.quote(remote_dir)}",
        ]
        rc, _out, err = self._run_subprocess(self._with_password_prefix(mkdir_cmd))
        if rc != 0:
            raise RuntimeError(f"Failed to create remote directory '{remote_dir}': {err.strip()}")

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
            f"{self._target()}:{remote_dir.rstrip('/')}/",
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
        cfg = self._merged_config(direct_config, overrides)
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

        self._write_summary(output_root, label=label, backend=backend, suites=suites, passed=overall_pass)
        return DirectRunResult(backend=backend, passed=overall_pass, suites=suites)

    def _merged_config(
        self,
        direct_config: Optional[DirectTestConfig],
        overrides: Optional[DirectRunOverrides],
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
                    raw = yaml.safe_load(abs_job.read_text(encoding="utf-8")) or {}
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

        target_ref = source.ref or "HEAD"
        self._git(["checkout", target_ref], cwd=repo_dir)

        return repo_dir

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

            print(
                f"[direct-test] {suite_display_name} {step_name} ({idx}/{total_steps}) running",
                flush=True,
            )

            start = time.monotonic()
            timed_out = False
            try:
                rc, stdout, stderr = transport.run(
                    expanded,
                    cwd=run_cwd,
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
            if status != "PASS":
                suite_pass = False

            print(
                f"[direct-test] {suite_display_name} {step_name} ({idx}/{total_steps}) "
                f"{status} in {duration:.2f}s",
                flush=True,
            )

            lava_signals = self._parse_lava_signals(stdout)

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
                    log_path=str(log_file),
                    timed_out=timed_out,
                    lava_signals=lava_signals,
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

    def _write_summary(
        self,
        output_dir: Path,
        label: str,
        backend: str,
        suites: List[DirectTestSuiteResult],
        passed: bool,
    ) -> None:
        summary = {
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
        (output_dir / "direct-test-summary.json").write_text(
            json.dumps(summary, indent=2),
            encoding="utf-8",
        )

        html_content = self._render_html_report(
            label=label,
            backend=backend,
            suites=suites,
            passed=passed,
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
    ) -> str:
        env = Environment(autoescape=True)
        tmpl = env.from_string(_HTML_REPORT_TEMPLATE)
        return tmpl.render(
            label=label,
            backend=backend,
            suites=suites,
            passed=passed,
            generated_at=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        )

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
