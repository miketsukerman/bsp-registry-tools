"""Tests for direct test-definition execution backend."""

import json
import logging
import subprocess
from html.parser import HTMLParser

import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bsp.direct_runner import (
    DirectRunOverrides,
    DirectTestRunner,
    DirectTestSuiteResult,
    DirectTestCaseResult,
    LavaSignalCase,
    _LocalTransport,
    _SshTransport,
)
from bsp.models import DirectTestConfig, DirectTransportConfig, TestDefinitionSource
from bsp.requirements_catalog import (
    clear_catalog_url_cache,
    discover_catalog,
    humanize_test_case_id,
    inline_catalog,
    is_catalog_url,
    load_catalog_file,
    load_catalog_url,
    raw_catalog_url,
)


class _TableDataCellParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._in_td = False
        self._current = []
        self.cells = []

    def handle_starttag(self, tag, attrs):
        if tag == "td":
            self._in_td = True
            self._current = []

    def handle_data(self, data):
        if self._in_td:
            self._current.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self._in_td:
            self.cells.append("".join(self._current).strip())
            self._in_td = False
            self._current = []


def _init_git_repo(repo_dir: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "init",
        ],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )


def _git_commit_all(repo_dir: Path, message: str) -> None:
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            message,
        ],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )


def _git_output(repo_dir: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo_dir, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


class TestSourceRepoRefresh:
    def test_refreshes_cached_repo_to_latest_remote_branch_commit(self, tmp_path):
        remote_repo = tmp_path / "remote-defs"
        remote_repo.mkdir()
        tracked_file = remote_repo / "version.txt"
        tracked_file.write_text("v1\n", encoding="utf-8")
        _init_git_repo(remote_repo)
        branch = _git_output(remote_repo, "rev-parse", "--abbrev-ref", "HEAD")

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        source = TestDefinitionSource(repo_url=str(remote_repo), ref=branch, paths=["."])

        cached_repo = runner._prepare_source_repo(source)
        assert (cached_repo / "version.txt").read_text(encoding="utf-8") == "v1\n"

        tracked_file.write_text("v2\n", encoding="utf-8")
        _git_commit_all(remote_repo, "update")

        cached_repo = runner._prepare_source_repo(source)
        assert (cached_repo / "version.txt").read_text(encoding="utf-8") == "v2\n"

    def test_refreshes_cached_repo_when_no_ref_is_provided(self, tmp_path):
        remote_repo = tmp_path / "remote-defs"
        remote_repo.mkdir()
        tracked_file = remote_repo / "version.txt"
        tracked_file.write_text("v1\n", encoding="utf-8")
        _init_git_repo(remote_repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        source = TestDefinitionSource(repo_url=str(remote_repo), paths=["."])

        cached_repo = runner._prepare_source_repo(source)
        assert (cached_repo / "version.txt").read_text(encoding="utf-8") == "v1\n"

        tracked_file.write_text("v2\n", encoding="utf-8")
        _git_commit_all(remote_repo, "update")

        cached_repo = runner._prepare_source_repo(source)
        assert (cached_repo / "version.txt").read_text(encoding="utf-8") == "v2\n"


class TestDirectRunnerLocal:
    def test_runs_lava_job_test_definitions_section(self, tmp_path):
        repo = tmp_path / "defs-repo"
        defs_dir = repo / "defs"
        jobs_dir = repo / "jobs"
        defs_dir.mkdir(parents=True)
        jobs_dir.mkdir(parents=True)
        (defs_dir / "smoke.yaml").write_text(
            """
metadata:
  name: smoke-suite
run:
  steps:
    - "echo smoke"
""",
            encoding="utf-8",
        )
        (defs_dir / "net.yaml").write_text(
            """
metadata:
  name: net-suite
run:
  steps:
    - "echo net"
""",
            encoding="utf-8",
        )
        (jobs_dir / "job.yaml").write_text(
            """
actions:
  - deploy:
      to: tftp
  - test:
      definitions:
        - repository: https://example.com/test-definitions.git
          from: git
          path: defs/smoke.yaml
        - repository: https://example.com/test-definitions.git
          from: git
          path: defs/net.yaml
""",
            encoding="utf-8",
        )
        _init_git_repo(repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        cfg = DirectTestConfig(
            definitions=[
                TestDefinitionSource(
                    repo_url=repo.as_uri(),
                    paths=["jobs/job.yaml"],
                )
            ],
            timeout=20,
        )
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=cfg,
            overrides=DirectRunOverrides(backend="direct-local", output_dir=str(tmp_path / "out")),
            label="local",
        )

        assert result.passed is True
        assert [suite.name for suite in result.suites] == ["smoke-suite", "net-suite"]

    def test_duplicate_lava_job_definitions_run_once(self, tmp_path):
        repo = tmp_path / "defs-repo"
        defs_dir = repo / "defs"
        jobs_dir = repo / "jobs"
        defs_dir.mkdir(parents=True)
        jobs_dir.mkdir(parents=True)
        (defs_dir / "net.yaml").write_text(
            r"""
metadata:
  name: network-suite
run:
  steps:
    - "printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ping-gateway RESULT=pass>\n'"
""",
            encoding="utf-8",
        )
        (jobs_dir / "job.yaml").write_text(
            """
actions:
  - test:
      definitions:
        - path: defs/net.yaml
          parameters:
            TARGET: net
  - test:
      definitions:
        - path: defs/net.yaml
          parameters:
            TARGET: net
""",
            encoding="utf-8",
        )
        _init_git_repo(repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        cfg = DirectTestConfig(
            definitions=[TestDefinitionSource(repo_url=repo.as_uri(), paths=["jobs/job.yaml"])],
            timeout=20,
        )
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=cfg,
            overrides=DirectRunOverrides(backend="direct-local", output_dir=str(tmp_path / "out")),
            label="dedupe",
        )

        assert result.passed is True
        assert len(result.suites) == 1
        assert result.suites[0].name == "network-suite"
        assert len(result.suites[0].cases) == 1
        assert len(result.suites[0].cases[0].lava_signals) == 1

        summary = json.loads((tmp_path / "out" / "direct-test-summary.json").read_text(encoding="utf-8"))
        html = (tmp_path / "out" / "direct-test-report.html").read_text(encoding="utf-8")
        signals_json = summary["suites"][0]["cases"][0]["lava_signals"]
        assert len(signals_json) == 1
        assert signals_json[0]["test_case_id"] == "ping-gateway"
        assert signals_json[0]["result"] == "pass"
        assert "LAVA cases: <strong>1</strong>" in html
        parser = _TableDataCellParser()
        parser.feed(html)
        # The test case is reported exactly once per report table: the
        # cross-suite requirements matrix and the suite's own case table.  The
        # cell also carries the folded description text.
        assert sum(1 for cell in parser.cells if cell.startswith("ping-gateway")) == 2

    def test_lava_job_definition_parameters_override_source_params(self, tmp_path):
        repo = tmp_path / "defs-repo"
        defs_dir = repo / "defs"
        jobs_dir = repo / "jobs"
        defs_dir.mkdir(parents=True)
        jobs_dir.mkdir(parents=True)
        (defs_dir / "param.yaml").write_text(
            """
metadata:
  name: param-suite
run:
  steps:
    - "test ${SOURCE_ONLY} = base"
    - "test ${SHARED} = entry"
    - "test ${ENTRY_ONLY} = extra"
""",
            encoding="utf-8",
        )
        (jobs_dir / "job.yaml").write_text(
            """
actions:
  - test:
      definitions:
        - path: defs/param.yaml
          parameters:
            SHARED: entry
            ENTRY_ONLY: extra
""",
            encoding="utf-8",
        )
        _init_git_repo(repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        cfg = DirectTestConfig(
            definitions=[
                TestDefinitionSource(
                    repo_url=repo.as_uri(),
                    paths=["jobs/job.yaml"],
                    params={"SOURCE_ONLY": "base", "SHARED": "source"},
                )
            ],
            timeout=20,
        )
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=cfg,
            overrides=DirectRunOverrides(backend="direct-local", output_dir=str(tmp_path / "out")),
            label="local",
        )

        assert result.passed is True
        assert len(result.suites) == 1
        assert result.suites[0].name == "param-suite"

    def test_local_job_yaml_timeout_minutes_override_default_timeout(self, tmp_path):
        base = tmp_path / "project"
        defs_dir = base / "defs"
        defs_dir.mkdir(parents=True)
        (defs_dir / "smoke.yaml").write_text(
            """
metadata:
  name: smoke-suite
run:
  steps:
    - "echo hello"
""",
            encoding="utf-8",
        )
        job_file = base / "job.yaml"
        job_file.write_text(
            """
actions:
  - test:
      timeout:
        minutes: 50
      definitions:
        - path: defs/smoke.yaml
""",
            encoding="utf-8",
        )

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))
        seen_timeouts = []

        def _fake_run(self, command, cwd, env=None, timeout=None):
            seen_timeouts.append(timeout)
            return 0, "ok\n", ""

        with patch.object(_LocalTransport, "run", autospec=True, side_effect=_fake_run):
            result = runner.run(
                resolved=resolved,
                direct_config=None,
                overrides=DirectRunOverrides(
                    backend="direct-local",
                    local_job_paths=[str(job_file)],
                    output_dir=str(tmp_path / "out"),
                ),
                label="local-job",
            )

        assert result.passed is True
        assert seen_timeouts == [3000]

    def test_emits_step_progress_to_stdout(self, tmp_path, capsys):
        repo = tmp_path / "defs-repo"
        defs_dir = repo / "defs"
        defs_dir.mkdir(parents=True)
        (defs_dir / "smoke.yaml").write_text(
            """
metadata:
  name: smoke-suite
run:
  steps:
    - "echo first"
    - "echo second"
""",
            encoding="utf-8",
        )
        _init_git_repo(repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        cfg = DirectTestConfig(
            definitions=[
                TestDefinitionSource(
                    repo_url=repo.as_uri(),
                    paths=["defs/smoke.yaml"],
                )
            ],
            timeout=20,
        )
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=cfg,
            overrides=DirectRunOverrides(backend="direct-local", output_dir=str(tmp_path / "out")),
            label="local",
        )

        captured = capsys.readouterr()
        assert result.passed is True
        assert "[direct-test] ⠋ smoke-suite step-1 (1/2) running" in captured.out
        assert "[direct-test] ✅ smoke-suite step-1 (1/2) PASS in" in captured.out
        assert "[direct-test] ⠙ smoke-suite step-2 (2/2) running" in captured.out
        assert "[direct-test] ✅ smoke-suite step-2 (2/2) PASS in" in captured.out

    def test_emits_failed_status_when_lava_cases_have_failures(self, tmp_path, capsys):
        repo = tmp_path / "defs-repo"
        defs_dir = repo / "defs"
        defs_dir.mkdir(parents=True)
        (defs_dir / "lava-mixed.yaml").write_text(
            """
metadata:
  name: lava-mixed
run:
  steps:
    - "printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=case-a RESULT=pass>\\n<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=case-b RESULT=fail>\\n'"
""",
            encoding="utf-8",
        )
        _init_git_repo(repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        cfg = DirectTestConfig(
            definitions=[
                TestDefinitionSource(
                    repo_url=repo.as_uri(),
                    paths=["defs/lava-mixed.yaml"],
                )
            ],
            timeout=20,
        )
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=cfg,
            overrides=DirectRunOverrides(backend="direct-local", output_dir=str(tmp_path / "out")),
            label="local",
        )

        captured = capsys.readouterr()
        assert result.passed is False
        assert "[direct-test] ❌ lava-mixed step-1 (1/1) FAIL (LAVA 1 failed) in" in captured.out

    def test_emits_green_pass_when_all_lava_cases_pass(self, tmp_path, capsys):
        repo = tmp_path / "defs-repo"
        defs_dir = repo / "defs"
        defs_dir.mkdir(parents=True)
        (defs_dir / "lava-green.yaml").write_text(
            """
metadata:
  name: lava-green
run:
  steps:
    - "printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=case-a RESULT=pass>\\n<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=case-b RESULT=pass>\\n'"
""",
            encoding="utf-8",
        )
        _init_git_repo(repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        cfg = DirectTestConfig(
            definitions=[
                TestDefinitionSource(
                    repo_url=repo.as_uri(),
                    paths=["defs/lava-green.yaml"],
                )
            ],
            timeout=20,
        )
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=cfg,
            overrides=DirectRunOverrides(backend="direct-local", output_dir=str(tmp_path / "out")),
            label="local",
        )

        captured = capsys.readouterr()
        assert result.passed is True
        assert "[direct-test] ✅ lava-green step-1 (1/1) PASS in" in captured.out

    def test_emits_params_in_step_log_lines(self, tmp_path, capsys):
        """Merged params are shown in the running and result log lines."""
        repo = tmp_path / "defs-repo"
        defs_dir = repo / "defs"
        defs_dir.mkdir(parents=True)
        (defs_dir / "param-suite.yaml").write_text(
            """
metadata:
  name: param-suite
params:
  GREETING: hello
run:
  steps:
    - "echo ok"
""",
            encoding="utf-8",
        )
        _init_git_repo(repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        cfg = DirectTestConfig(
            definitions=[
                TestDefinitionSource(
                    repo_url=repo.as_uri(),
                    paths=["defs/param-suite.yaml"],
                    params={"TARGET": "board"},
                )
            ],
            timeout=20,
        )
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=cfg,
            overrides=DirectRunOverrides(backend="direct-local", output_dir=str(tmp_path / "out")),
            label="local",
        )

        captured = capsys.readouterr()
        assert result.passed is True
        assert "[GREETING=hello TARGET=board]" in captured.out
        assert "[direct-test] ⠋ param-suite step-1 (1/1) [GREETING=hello TARGET=board] running" in captured.out
        assert "[direct-test] ✅ param-suite step-1 (1/1) [GREETING=hello TARGET=board] PASS in" in captured.out

    def test_runs_definition_set_locally(self, tmp_path):
        repo = tmp_path / "defs-repo"
        defs_dir = repo / "defs"
        defs_dir.mkdir(parents=True)
        (defs_dir / "smoke.yaml").write_text(
            """
metadata:
  name: smoke-suite
params:
  GREETING: hello
run:
  steps:
    - "echo {GREETING}-${TARGET} > output.txt"
    - "test -f output.txt"
""",
            encoding="utf-8",
        )
        _init_git_repo(repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        cfg = DirectTestConfig(
            definitions=[
                TestDefinitionSource(
                    repo_url=repo.as_uri(),
                    paths=["defs"],
                    params={"TARGET": "world"},
                )
            ],
            timeout=20,
        )
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=cfg,
            overrides=DirectRunOverrides(backend="direct-local", output_dir=str(tmp_path / "out")),
            label="local",
        )

        assert result.passed is True
        assert len(result.suites) == 1
        assert result.suites[0].name == "smoke-suite"
        assert (tmp_path / "out" / "direct-test-summary.json").exists()

    def test_params_exported_as_env_vars(self, tmp_path):
        """Parameters from the test-definition source must be available as shell env vars."""
        repo = tmp_path / "defs-repo"
        defs_dir = repo / "defs"
        defs_dir.mkdir(parents=True)
        out_file = tmp_path / "out" / "env_out.txt"
        # The step reads $CAN_COUNT from the environment (no template substitution).
        (defs_dir / "env.yaml").write_text(
            f"""
metadata:
  name: env-suite
run:
  steps:
    - "echo $CAN_COUNT > {out_file}"
""",
            encoding="utf-8",
        )
        _init_git_repo(repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        cfg = DirectTestConfig(
            definitions=[
                TestDefinitionSource(
                    repo_url=repo.as_uri(),
                    paths=["defs"],
                    params={"CAN_COUNT": "2"},
                )
            ],
            timeout=20,
        )
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=cfg,
            overrides=DirectRunOverrides(backend="direct-local", output_dir=str(tmp_path / "out")),
        )

        assert result.passed is True
        assert out_file.read_text(encoding="utf-8").strip() == "2"

    def test_ssh_transport_passes_env_to_remote(self):
        """_SshTransport.run() must include exported env vars in the remote command."""
        config = DirectTransportConfig(host="board.local", user="root", port=22)
        transport = _SshTransport(config)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            transport.run(
                "test_script.sh",
                cwd="/tmp/work",
                env={"CAN_COUNT": "2", "CAN0_DEV": "can0"},
            )

        call_args = mock_run.call_args[0][0]
        remote_cmd = call_args[-1]  # bash -lc '...'
        assert "CAN_COUNT" in remote_cmd
        assert "CAN0_DEV" in remote_cmd
        assert "export" in remote_cmd

    def test_resolves_multiple_definition_files(self, tmp_path):
        repo = tmp_path / "defs-repo"
        defs_dir = repo / "defs"
        nested = defs_dir / "nested"
        nested.mkdir(parents=True)
        (defs_dir / "one.yaml").write_text("run:\n  steps: []\n", encoding="utf-8")
        (nested / "two.yml").write_text("run:\n  steps: []\n", encoding="utf-8")
        _init_git_repo(repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        files = runner._resolve_definition_files(repo, ["defs"])
        assert len(files) == 2
        assert files[0].name == "one.yaml"
        assert files[1].name == "two.yml"

    def test_runs_steps_from_repository_root(self, tmp_path):
        repo = tmp_path / "defs-repo"
        defs_dir = repo / "defs"
        defs_dir.mkdir(parents=True)
        (defs_dir / "smoke.yaml").write_text(
            """
run:
  steps:
    - "cd ./defs && test -f smoke.yaml"
""",
            encoding="utf-8",
        )
        _init_git_repo(repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        cfg = DirectTestConfig(
            definitions=[
                TestDefinitionSource(
                    repo_url=repo.as_uri(),
                    paths=["defs/smoke.yaml"],
                )
            ],
            timeout=20,
        )
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=cfg,
            overrides=DirectRunOverrides(backend="direct-local", output_dir=str(tmp_path / "out")),
            label="local",
        )

        assert result.passed is True

    def test_persists_cwd_after_cd_step(self, tmp_path):
        repo = tmp_path / "defs-repo"
        defs_dir = repo / "defs"
        defs_dir.mkdir(parents=True)
        (defs_dir / "smoke.sh").write_text(
            "#!/usr/bin/env bash\necho smoke-ok\n",
            encoding="utf-8",
        )
        (defs_dir / "smoke.sh").chmod(0o755)
        (defs_dir / "smoke.yaml").write_text(
            """
run:
  steps:
    - "cd ./defs"
    - "./smoke.sh -s False -t 'pwd, uname -a'"
""",
            encoding="utf-8",
        )
        _init_git_repo(repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        cfg = DirectTestConfig(
            definitions=[
                TestDefinitionSource(
                    repo_url=repo.as_uri(),
                    paths=["defs/smoke.yaml"],
                )
            ],
            timeout=20,
        )
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=cfg,
            overrides=DirectRunOverrides(backend="direct-local", output_dir=str(tmp_path / "out")),
            label="local",
        )

        assert result.passed is True


class TestSshTransport:
    def test_builds_ssh_command_with_password_and_serial(self):
        cfg = DirectTransportConfig(
            mode="ssh",
            host="dut.local",
            user="root",
            port=2222,
            key_path="/tmp/key",
            strict_host_key_checking=False,
            known_hosts_file="/tmp/known_hosts",
            serial_device="/dev/ttyUSB0",
            serial_baudrate=115200,
        )
        cfg.password = "pw"
        transport = _SshTransport(cfg)

        with patch("bsp.direct_runner.shutil.which", return_value="/usr/bin/sshpass"), \
                patch("bsp.direct_runner.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            rc, _out, _err = transport.run("echo ok", cwd="/tmp/work")

        assert rc == 0
        cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(cmd)
        assert cmd[0:3] == ["sshpass", "-p", "pw"]
        assert "ssh" in cmd_str
        assert "StrictHostKeyChecking=no" in cmd_str
        assert "UserKnownHostsFile=/tmp/known_hosts" in cmd_str
        assert "ProxyCommand=socat - FILE:/dev/ttyUSB0,raw,echo=0,b115200" in cmd_str
        assert "root@dut.local" in cmd_str

    def test_stage_directory_cleans_remote_repo_before_copy(self):
        cfg = DirectTransportConfig(
            mode="ssh",
            host="dut.local",
            user="root",
            port=2222,
            strict_host_key_checking=True,
        )
        transport = _SshTransport(cfg)

        with patch("bsp.direct_runner.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            transport.stage_directory(Path("/tmp/repo-b305ecb10ee5"), "/tmp/bsp-direct-tests")

        assert mock_run.call_count == 2

        prepare_cmd = mock_run.call_args_list[0][0][0]
        prepare_cmd_str = " ".join(prepare_cmd)
        assert "ssh" in prepare_cmd_str
        assert "root@dut.local" in prepare_cmd_str
        assert "mkdir -p /tmp/bsp-direct-tests" in prepare_cmd_str
        assert "rm -rf /tmp/bsp-direct-tests/repo-b305ecb10ee5" in prepare_cmd_str

        scp_cmd = mock_run.call_args_list[1][0][0]
        scp_cmd_str = " ".join(scp_cmd)
        assert "scp" in scp_cmd_str
        assert "/tmp/repo-b305ecb10ee5" in scp_cmd_str
        assert "root@dut.local:/tmp/bsp-direct-tests/" in scp_cmd_str


class TestDirectRunnerBackendSelection:
    def test_direct_serial_uses_ssh_transport(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        transport_cfg = DirectTransportConfig(mode="local", serial_device="/dev/ttyUSB0")
        transport = runner._build_transport(transport_cfg, "direct-serial")
        assert isinstance(transport, _SshTransport)

    def test_direct_serial_requires_serial_device(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        transport_cfg = DirectTransportConfig(mode="ssh")
        try:
            runner._build_transport(transport_cfg, "direct-serial")
            assert False, "Expected ValueError for missing serial device"
        except ValueError as exc:
            assert "direct-serial backend requires a serial device" in str(exc)


class TestReportGeneration:
    """Tests for HTML and PDF test report generation."""

    def _make_suites(self):
        return [
            DirectTestSuiteResult(
                name="smoke-suite",
                status="PASS",
                duration=1.23,
                log_dir="/tmp/logs/smoke-suite",
                cases=[
                    DirectTestCaseResult(
                        name="step-1",
                        status="PASS",
                        duration=0.5,
                        command="echo hello",
                        params={"GREETING": "hello", "TARGET": "board"},
                        log_path="/tmp/logs/smoke-suite/step-1.log",
                        lava_signals=[LavaSignalCase(test_case_id="ping-gateway", result="pass")],
                    ),
                    DirectTestCaseResult(
                        name="step-2",
                        status="FAIL",
                        duration=0.73,
                        command="false",
                        timed_out=False,
                        log_path="/tmp/logs/smoke-suite/step-2.log",
                        lava_signals=[LavaSignalCase(test_case_id="download-a-file", result="fail")],
                        _execution_succeeded=True,
                    ),
                ],
            ),
            DirectTestSuiteResult(
                name="net-suite",
                status="FAIL",
                duration=0.1,
                log_dir="/tmp/logs/net-suite",
                cases=[],
            ),
        ]

    def test_html_report_written(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        runner._write_summary(
            output_dir=tmp_path,
            label="ci-run",
            backend="direct-local",
            suites=self._make_suites(),
            passed=False,
        )
        html_path = tmp_path / "direct-test-report.html"
        assert html_path.exists(), "HTML report file should be created"

    def test_html_report_contains_expected_content(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        runner._write_summary(
            output_dir=tmp_path,
            label="ci-run",
            backend="direct-local",
            suites=self._make_suites(),
            passed=False,
        )
        content = (tmp_path / "direct-test-report.html").read_text(encoding="utf-8")
        assert "smoke-suite" in content
        assert "net-suite" in content
        assert "direct-local" in content
        assert "ci-run" in content
        assert "FAIL" in content
        assert "ping-gateway" in content
        assert "download-a-file" in content
        assert "Aggregate summary" in content
        assert "Failures first" in content
        assert "Suite navigation" in content
        assert "Test Case" in content
        assert "echo hello" not in content
        assert "href=\"file:///tmp/logs/smoke-suite/step-1.log\"" not in content

    def test_html_report_failure_section_lists_failed_lava_case_ids(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        runner._write_summary(
            output_dir=tmp_path,
            label="ci-run",
            backend="direct-local",
            suites=self._make_suites(),
            passed=False,
        )
        content = (tmp_path / "direct-test-report.html").read_text(encoding="utf-8")
        assert "Failures first" in content
        assert "download-a-file" in content
        assert "Command failed" not in content
        assert "step-2" not in content

    def test_html_report_is_valid_html(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        runner._write_summary(
            output_dir=tmp_path,
            label="test",
            backend="direct-local",
            suites=self._make_suites(),
            passed=True,
        )
        content = (tmp_path / "direct-test-report.html").read_text(encoding="utf-8")
        assert content.strip().startswith("<!DOCTYPE html>")
        assert "</html>" in content

    def test_render_html_report_pass(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        html = runner._render_html_report(
            label="my-label",
            backend="direct-ssh",
            suites=self._make_suites(),
            passed=True,
        )
        assert "my-label" in html
        assert "direct-ssh" in html
        assert "PASS" in html

    def test_render_html_report_with_preset_info(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        html = runner._render_html_report(
            label="my-label",
            backend="direct-local",
            suites=self._make_suites(),
            passed=True,
            preset_info={
                "device": "epc-r9000",
                "device_description": "EPCR9000 board",
                "release": "scarthgap",
                "release_description": "Yocto 5.0",
                "features": "wifi, bt",
            },
        )
        assert "epc-r9000" in html
        assert "EPCR9000 board" in html
        assert "scarthgap" in html
        assert "Yocto 5.0" in html
        assert "wifi, bt" in html

    def test_render_html_report_without_preset_info(self, tmp_path):
        """Preset info section is omitted when not provided."""
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        html = runner._render_html_report(
            label="no-preset",
            backend="direct-local",
            suites=[],
            passed=True,
        )
        assert "Preset info" not in html

    def test_json_summary_includes_preset_info(self, tmp_path):
        import json as _json

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        runner._write_summary(
            output_dir=tmp_path,
            label="ci-run",
            backend="direct-local",
            suites=self._make_suites(),
            passed=False,
            preset_info={"device": "epc-r9000", "release": "scarthgap"},
        )
        summary = _json.loads((tmp_path / "direct-test-summary.json").read_text(encoding="utf-8"))
        assert summary["preset"]["device"] == "epc-r9000"
        assert summary["preset"]["release"] == "scarthgap"
        assert summary["suites"][0]["cases"][0]["params"] == {
            "GREETING": "hello",
            "TARGET": "board",
        }

    def test_json_summary_omits_preset_key_when_no_info(self, tmp_path):
        import json as _json

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        runner._write_summary(
            output_dir=tmp_path,
            label="ci-run",
            backend="direct-local",
            suites=[],
            passed=True,
        )
        summary = _json.loads((tmp_path / "direct-test-summary.json").read_text(encoding="utf-8"))
        assert "preset" not in summary

    def test_extract_preset_info_from_resolved(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        device = SimpleNamespace(slug="epc-r9000", description="EPCR9000 board")
        release = SimpleNamespace(slug="scarthgap", description="Yocto 5.0")
        feature1 = SimpleNamespace(slug="wifi")
        feature2 = SimpleNamespace(slug="bt")
        resolved = SimpleNamespace(
            build_path=str(tmp_path),
            device=device,
            release=release,
            features=[feature1, feature2],
        )
        info = runner._extract_preset_info(resolved)
        assert info["device"] == "epc-r9000"
        assert info["device_description"] == "EPCR9000 board"
        assert info["release"] == "scarthgap"
        assert info["release_description"] == "Yocto 5.0"
        assert info["features"] == "wifi, bt"

    def test_extract_preset_info_no_device_or_release(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path))
        info = runner._extract_preset_info(resolved)
        assert info == {}

    def test_integration_html_report_includes_preset_info(self, tmp_path):
        """Integration: full run embeds device/release info in the HTML report."""
        repo = tmp_path / "defs-repo"
        defs_dir = repo / "defs"
        defs_dir.mkdir(parents=True)
        (defs_dir / "smoke.yaml").write_text(
            "run:\n  steps:\n    - echo hi\n",
            encoding="utf-8",
        )
        _init_git_repo(repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        cfg = DirectTestConfig(
            definitions=[TestDefinitionSource(repo_url=repo.as_uri(), paths=["defs"])],
            timeout=20,
        )
        device = SimpleNamespace(slug="epc-r9000", description="EPCR9000 board")
        release = SimpleNamespace(slug="scarthgap", description="Yocto 5.0")
        resolved = SimpleNamespace(
            build_path=str(tmp_path / "build"),
            device=device,
            release=release,
            features=[],
        )
        runner.run(
            resolved=resolved,
            direct_config=cfg,
            overrides=DirectRunOverrides(backend="direct-local", output_dir=str(tmp_path / "out")),
            label="integration",
        )

        html = (tmp_path / "out" / "direct-test-report.html").read_text(encoding="utf-8")
        assert "epc-r9000" in html
        assert "scarthgap" in html

        import json as _json
        summary = _json.loads((tmp_path / "out" / "direct-test-summary.json").read_text(encoding="utf-8"))
        assert summary["preset"]["device"] == "epc-r9000"
        assert summary["preset"]["release"] == "scarthgap"

    def test_integration_reports_include_case_params(self, tmp_path):
        import json as _json

        repo = tmp_path / "defs-repo"
        defs_dir = repo / "defs"
        defs_dir.mkdir(parents=True)
        (defs_dir / "param-suite.yaml").write_text(
            """
metadata:
  name: param-suite
params:
  GREETING: hello
run:
  steps:
    - "echo ok"
""",
            encoding="utf-8",
        )
        _init_git_repo(repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        cfg = DirectTestConfig(
            definitions=[
                TestDefinitionSource(
                    repo_url=repo.as_uri(),
                    paths=["defs/param-suite.yaml"],
                    params={"TARGET": "board"},
                )
            ],
            timeout=20,
        )
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        runner.run(
            resolved=resolved,
            direct_config=cfg,
            overrides=DirectRunOverrides(backend="direct-local", output_dir=str(tmp_path / "out")),
            label="integration",
        )

        html = (tmp_path / "out" / "direct-test-report.html").read_text(encoding="utf-8")
        assert "Parameters used by this step" not in html
        assert "GREETING" not in html
        assert "hello" not in html
        assert "TARGET" not in html
        assert "board" not in html
        assert "No LAVA test cases reported for this suite." in html

        summary = _json.loads((tmp_path / "out" / "direct-test-summary.json").read_text(encoding="utf-8"))
        assert summary["suites"][0]["cases"][0]["params"] == {
            "GREETING": "hello",
            "TARGET": "board",
        }

    def test_render_html_report_timed_out_case(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        suites = [
            DirectTestSuiteResult(
                name="timeout-suite",
                status="FAIL",
                duration=30.0,
                cases=[
                    DirectTestCaseResult(
                        name="long-step",
                        status="FAIL",
                        duration=30.0,
                        command="sleep 999",
                        timed_out=True,
                    )
                ],
            )
        ]
        html = runner._render_html_report(
            label="timeout-test",
            backend="direct-local",
            suites=suites,
            passed=False,
        )
        assert "Execution timed out before all LAVA test cases were reported." in html
        assert "No LAVA test cases reported for this suite." in html
        assert "sleep 999" not in html

    def test_html_report_folds_logs_of_failed_cases(self, tmp_path):
        """Failed test cases carry their step log, collapsed by default."""
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        suites = [
            DirectTestSuiteResult(
                name="net-suite",
                status="FAIL",
                duration=1.0,
                cases=[
                    DirectTestCaseResult(
                        name="step-1",
                        status="FAIL",
                        duration=0.5,
                        command="./run-net-tests.sh",
                        log_text="# stdout\ndownload failed: timeout\n",
                        lava_signals=[
                            LavaSignalCase(test_case_id="ping-gateway", result="pass"),
                            LavaSignalCase(test_case_id="download-a-file", result="fail"),
                        ],
                        _execution_succeeded=False,
                    )
                ],
            )
        ]
        html = runner._render_html_report(
            label="test",
            backend="direct-local",
            suites=suites,
            passed=False,
        )
        assert '<details class="log-fold">' in html
        # <details> without "open" renders collapsed.
        assert "<details class=\"log-fold\" open" not in html
        assert "download failed: timeout" in html

    def test_html_report_omits_logs_for_passed_cases(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        suites = [
            DirectTestSuiteResult(
                name="net-suite",
                status="PASS",
                duration=1.0,
                cases=[
                    DirectTestCaseResult(
                        name="step-1",
                        status="PASS",
                        duration=0.5,
                        command="./run-net-tests.sh",
                        log_text="# stdout\neverything is fine\n",
                        lava_signals=[LavaSignalCase(test_case_id="ping-gateway", result="pass")],
                        _execution_succeeded=True,
                    )
                ],
            )
        ]
        html = runner._render_html_report(
            label="test",
            backend="direct-local",
            suites=suites,
            passed=True,
        )
        assert '<details class="log-fold">' not in html
        assert "everything is fine" not in html

    def test_report_log_excerpt_read_from_log_file_and_truncated(self, tmp_path):
        log_file = tmp_path / "step-1.log"
        log_file.write_text("\n".join(f"line-{i}" for i in range(500)), encoding="utf-8")
        case = DirectTestCaseResult(
            name="step-1",
            status="FAIL",
            duration=0.1,
            command="./run.sh",
            log_path=str(log_file),
        )
        excerpt = DirectTestRunner._case_log_excerpt(case)
        assert "line-0" not in excerpt
        assert "line-499" in excerpt
        assert "log truncated" in excerpt

    def test_report_log_excerpt_missing_log_file(self, tmp_path):
        case = DirectTestCaseResult(
            name="step-1",
            status="FAIL",
            duration=0.1,
            command="./run.sh",
            log_path=str(tmp_path / "does-not-exist.log"),
        )
        assert DirectTestRunner._case_log_excerpt(case) == ""

    def test_pdf_skipped_when_weasyprint_missing(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        with patch.dict("sys.modules", {"weasyprint": None}):
            runner._write_pdf_report(tmp_path / "out.pdf", "<html></html>")
        assert not (tmp_path / "out.pdf").exists()

    def test_pdf_written_when_weasyprint_available(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        mock_wp = MagicMock()
        mock_html_instance = MagicMock()
        mock_wp.HTML.return_value = mock_html_instance
        with patch.dict("sys.modules", {"weasyprint": mock_wp}):
            runner._write_pdf_report(tmp_path / "out.pdf", "<html></html>")
        mock_wp.HTML.assert_called_once_with(string="<html></html>")
        mock_html_instance.write_pdf.assert_called_once_with(str(tmp_path / "out.pdf"))

    def test_integration_html_report_from_full_run(self, tmp_path):
        """Integration: full run generates HTML report alongside JSON summary."""
        repo = tmp_path / "defs-repo"
        defs_dir = repo / "defs"
        defs_dir.mkdir(parents=True)
        (defs_dir / "smoke.yaml").write_text(
            "run:\n  steps:\n    - echo hi\n",
            encoding="utf-8",
        )
        _init_git_repo(repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        cfg = DirectTestConfig(
            definitions=[TestDefinitionSource(repo_url=repo.as_uri(), paths=["defs"])],
            timeout=20,
        )
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))
        runner.run(
            resolved=resolved,
            direct_config=cfg,
            overrides=DirectRunOverrides(backend="direct-local", output_dir=str(tmp_path / "out")),
            label="integration",
        )

        assert (tmp_path / "out" / "direct-test-report.html").exists()
        assert (tmp_path / "out" / "direct-test-summary.json").exists()


class TestLavaSignalParsing:
    """Tests for LAVA_SIGNAL_TESTCASE parsing from step output."""

    def test_parse_single_pass_signal(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        output = "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ping-gateway RESULT=pass>"
        signals = runner._parse_lava_signals(output)
        assert len(signals) == 1
        assert signals[0].test_case_id == "ping-gateway"
        assert signals[0].result == "pass"
        assert signals[0].passed is True

    def test_parse_single_fail_signal(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        output = "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=print-routing-tables RESULT=fail>"
        signals = runner._parse_lava_signals(output)
        assert len(signals) == 1
        assert signals[0].test_case_id == "print-routing-tables"
        assert signals[0].result == "fail"
        assert signals[0].passed is False

    def test_parse_multiple_signals(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        output = (
            "some preamble output\n"
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=print-network-statistics RESULT=fail>\n"
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=list-all-network-interfaces RESULT=pass>\n"
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ip-link-loopback-up RESULT=pass>\n"
            "some trailing output\n"
        )
        signals = runner._parse_lava_signals(output)
        assert len(signals) == 3
        assert signals[0].test_case_id == "print-network-statistics"
        assert signals[0].result == "fail"
        assert signals[1].test_case_id == "list-all-network-interfaces"
        assert signals[1].result == "pass"
        assert signals[2].test_case_id == "ip-link-loopback-up"
        assert signals[2].result == "pass"

    def test_parse_full_sample_output(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        output = (
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=print-network-statistics RESULT=fail>\n"
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=list-all-network-interfaces RESULT=pass>\n"
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=print-routing-tables RESULT=fail>\n"
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ip-link-loopback-up RESULT=pass>\n"
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=route-dump-after-ip-link-loopback-up RESULT=fail>\n"
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ip-link-interface-up RESULT=fail>\n"
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ip-link-interface-down RESULT=fail>\n"
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=Dynamic-Host-Configuration-Protocol-Client-dhclient-v RESULT=fail>\n"
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=print-routing-tables-after-dhclient-request RESULT=fail>\n"
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ping-gateway RESULT=fail>\n"
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=download-a-file RESULT=pass>\n"
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=download-a-file-md5 RESULT=fail>\n"
        )
        signals = runner._parse_lava_signals(output)
        assert len(signals) == 12
        passing = [s for s in signals if s.result == "pass"]
        failing = [s for s in signals if s.result == "fail"]
        assert len(passing) == 3
        assert len(failing) == 9
        assert signals[7].test_case_id == "Dynamic-Host-Configuration-Protocol-Client-dhclient-v"

    def test_parse_empty_output(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        assert runner._parse_lava_signals("") == []
        assert runner._parse_lava_signals("no signals here") == []

    def test_lava_signals_in_case_result_and_json(self, tmp_path):
        """Integration: LAVA signals emitted by a step appear in case results and JSON."""
        repo = tmp_path / "defs-repo"
        defs_dir = repo / "defs"
        defs_dir.mkdir(parents=True)
        # Step echoes two LAVA signals to stdout
        (defs_dir / "net.yaml").write_text(
            "metadata:\n  name: network-suite\n"
            "run:\n  steps:\n"
            "    - \"printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ping-gateway RESULT=pass>\\n"
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=download-a-file RESULT=fail>\\n'\"\n",
            encoding="utf-8",
        )
        _init_git_repo(repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        cfg = DirectTestConfig(
            definitions=[TestDefinitionSource(repo_url=repo.as_uri(), paths=["defs/net.yaml"])],
            timeout=20,
        )
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))
        result = runner.run(
            resolved=resolved,
            direct_config=cfg,
            overrides=DirectRunOverrides(backend="direct-local", output_dir=str(tmp_path / "out")),
            label="lava-integration",
        )

        assert result.passed is False
        assert len(result.suites) == 1
        suite = result.suites[0]
        assert suite.name == "network-suite"
        assert suite.status == "FAIL"
        assert len(suite.cases) == 1
        step = suite.cases[0]
        assert step.status == "FAIL"
        assert len(step.lava_signals) == 2
        assert step.lava_signals[0].test_case_id == "ping-gateway"
        assert step.lava_signals[0].result == "pass"
        assert step.lava_signals[1].test_case_id == "download-a-file"
        assert step.lava_signals[1].result == "fail"

        # Verify JSON output contains lava_signals
        import json
        summary = json.loads((tmp_path / "out" / "direct-test-summary.json").read_text())
        assert summary["passed"] is False
        assert summary["suites"][0]["status"] == "FAIL"
        assert summary["suites"][0]["cases"][0]["status"] == "FAIL"
        sig_json = summary["suites"][0]["cases"][0]["lava_signals"]
        assert len(sig_json) == 2
        assert sig_json[0]["test_case_id"] == "ping-gateway"
        assert sig_json[0]["result"] == "pass"
        assert sig_json[1]["test_case_id"] == "download-a-file"
        assert sig_json[1]["result"] == "fail"

        html = (tmp_path / "out" / "direct-test-report.html").read_text(encoding="utf-8")
        assert "Overall:" in html
        assert "badge fail" in html
        assert html.count('class="badge warn"') >= 1
        assert "ping-gateway" in html
        assert "download-a-file" in html
        assert "step-1" not in html

    def test_html_report_contains_lava_signals(self, tmp_path):
        """HTML report shows only suite-level LAVA signal cases."""
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        suites = [
            DirectTestSuiteResult(
                name="net-suite",
                status="PASS",
                duration=1.0,
                cases=[
                    DirectTestCaseResult(
                        name="step-1",
                        status="PASS",
                        duration=0.5,
                        command="./run-net-tests.sh",
                        lava_signals=[
                            LavaSignalCase(test_case_id="ping-gateway", result="pass"),
                            LavaSignalCase(test_case_id="download-a-file", result="fail"),
                        ],
                    )
                ],
            )
        ]
        html = runner._render_html_report(
            label="test",
            backend="direct-local",
            suites=suites,
            passed=True,
        )
        assert "ping-gateway" in html
        assert "download-a-file" in html
        assert "Test Case" in html
        assert "step-1" not in html
        assert "./run-net-tests.sh" not in html

    def test_html_report_omits_case_params(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        suites = [
            DirectTestSuiteResult(
                name="param-suite",
                status="PASS",
                duration=1.0,
                cases=[
                    DirectTestCaseResult(
                        name="step-1",
                        status="PASS",
                        duration=0.5,
                        command="./run-tests.sh",
                        params={"TARGET": "board-a", "GREETING": "hello"},
                    )
                ],
            )
        ]
        html = runner._render_html_report(
            label="test",
            backend="direct-local",
            suites=suites,
            passed=True,
        )
        assert "Parameters used by this step" not in html
        assert "TARGET" not in html
        assert "board-a" not in html
        assert "GREETING" not in html
        assert "hello" not in html
        assert "No LAVA test cases reported for this suite." in html

    def test_html_report_marks_lava_failures_as_yellow_pass(self, tmp_path):
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        suites = [
            DirectTestSuiteResult(
                name="net-suite",
                status="FAIL",
                duration=1.0,
                cases=[
                    DirectTestCaseResult(
                        name="step-1",
                        status="FAIL",
                        duration=0.5,
                        command="./run-net-tests.sh",
                        _execution_succeeded=True,
                        lava_signals=[
                            LavaSignalCase(test_case_id="ping-gateway", result="pass"),
                            LavaSignalCase(test_case_id="download-a-file", result="fail"),
                        ],
                    )
                ],
            )
        ]
        html = runner._render_html_report(
            label="test",
            backend="direct-local",
            suites=suites,
            passed=False,
        )
        assert "Overall:" in html
        assert "badge fail" in html
        assert html.count('class="badge warn"') >= 1
        assert "EXEC PASS / LAVA FAIL" in html
        assert "LAVA cases: <strong>2</strong>" in html
        assert "download-a-file" in html
        assert "step-1" not in html
        assert "./run-net-tests.sh" not in html


class TestLocalJobPath:
    """Tests for --test-job-path (local LAVA job YAML without a git repo URL)."""

    def test_runs_local_job_yaml_without_repo_url(self, tmp_path):
        """LAVA job YAML in a local directory should execute without --test-repo-url."""
        base = tmp_path / "project"
        defs_dir = base / "defs"
        defs_dir.mkdir(parents=True)

        (defs_dir / "smoke.yaml").write_text(
            """
metadata:
  name: smoke-suite
run:
  steps:
    - "echo hello"
""",
            encoding="utf-8",
        )
        (base / "job.yaml").write_text(
            """
actions:
  - test:
      definitions:
        - path: defs/smoke.yaml
""",
            encoding="utf-8",
        )

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=None,
            overrides=DirectRunOverrides(
                backend="direct-local",
                local_job_paths=[str(base / "job.yaml")],
                output_dir=str(tmp_path / "out"),
            ),
            label="local-job",
        )

        assert result.passed is True
        assert [s.name for s in result.suites] == ["smoke-suite"]

    def test_local_job_yaml_resolves_definitions_relative_to_job_dir(self, tmp_path):
        """Definitions in a local job YAML are resolved relative to the job file's parent."""
        base = tmp_path / "project"
        defs_dir = base / "defs"
        defs_dir.mkdir(parents=True)

        (defs_dir / "net.yaml").write_text(
            """
metadata:
  name: net-suite
run:
  steps:
    - "echo net-ok"
""",
            encoding="utf-8",
        )
        job_file = base / "job.yaml"
        job_file.write_text(
            """
actions:
  - test:
      definitions:
        - path: defs/net.yaml
""",
            encoding="utf-8",
        )

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=None,
            overrides=DirectRunOverrides(
                backend="direct-local",
                local_job_paths=[str(job_file)],
                output_dir=str(tmp_path / "out"),
            ),
            label="local-job",
        )

        assert result.passed is True
        assert result.suites[0].name == "net-suite"

    def test_local_job_yaml_params_override_source_params(self, tmp_path):
        """Entry-level parameters in the job YAML override source-level params."""
        base = tmp_path / "project"
        defs_dir = base / "defs"
        defs_dir.mkdir(parents=True)

        (defs_dir / "param.yaml").write_text(
            """
metadata:
  name: param-suite
run:
  steps:
    - "test ${SHARED} = entry"
    - "test ${EXTRA} = override"
""",
            encoding="utf-8",
        )
        job_file = base / "job.yaml"
        job_file.write_text(
            """
actions:
  - test:
      definitions:
        - path: defs/param.yaml
          parameters:
            SHARED: entry
            EXTRA: override
""",
            encoding="utf-8",
        )

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=None,
            overrides=DirectRunOverrides(
                backend="direct-local",
                local_job_paths=[str(job_file)],
                params={"SHARED": "source", "EXTRA": "source-extra"},
                output_dir=str(tmp_path / "out"),
            ),
            label="local-job",
        )

        assert result.passed is True
        assert result.suites[0].name == "param-suite"

    def test_lava_job_parameters_substitute_single_brace_vars(self, tmp_path):
        """LAVA job `parameters:` must override `{VAR}` placeholders in test steps.

        Regression test for the bug where DISTRO_VER from the job file's
        parameters: block was silently ignored because _VAR_BRACE_RE matched
        Jinja2-style {{ VAR }} instead of Lava-Test-style {VAR}.  The test
        script would then use the default from params: instead of the job value.
        """
        base = tmp_path / "project"
        defs_dir = base / "defs"
        defs_dir.mkdir(parents=True)
        out_file = tmp_path / "out" / "distro_ver.txt"

        # Simulate context.yaml-style definition: default DISTRO_VER is the
        # bare release list; the LAVA job overrides it with a .*() regex.
        (defs_dir / "context.yaml").write_text(
            f"""
metadata:
  name: context-suite
params:
  DISTRO_VER: scarthgap|styhead|walnascar
run:
  steps:
    - "printf '%s\\n' '{{DISTRO_VER}}' > {out_file}"
""",
            encoding="utf-8",
        )
        job_file = base / "job.yaml"
        job_file.write_text(
            """
actions:
  - test:
      definitions:
        - path: defs/context.yaml
          parameters:
            DISTRO_VER: ".*(scarthgap|styhead|walnascar|whinlatter|wrynose)"
""",
            encoding="utf-8",
        )

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=None,
            overrides=DirectRunOverrides(
                backend="direct-local",
                local_job_paths=[str(job_file)],
                output_dir=str(tmp_path / "out"),
            ),
            label="distro-ver",
        )

        assert result.passed is True
        # The file must contain the job-parameter value, not the default.
        written = out_file.read_text(encoding="utf-8").strip()
        assert written == ".*(scarthgap|styhead|walnascar|whinlatter|wrynose)"

    def test_multiple_local_job_paths(self, tmp_path):
        """Multiple --test-job-path values execute all referenced suites."""
        base = tmp_path / "project"
        defs_dir = base / "defs"
        defs_dir.mkdir(parents=True)

        for name in ("alpha", "beta"):
            (defs_dir / f"{name}.yaml").write_text(
                f"""
metadata:
  name: {name}-suite
run:
  steps:
    - "echo {name}"
""",
                encoding="utf-8",
            )

        for name in ("job-a", "job-b"):
            suite_name = "alpha" if name == "job-a" else "beta"
            (base / f"{name}.yaml").write_text(
                f"""
actions:
  - test:
      definitions:
        - path: defs/{suite_name}.yaml
""",
                encoding="utf-8",
            )

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=None,
            overrides=DirectRunOverrides(
                backend="direct-local",
                local_job_paths=[
                    str(base / "job-a.yaml"),
                    str(base / "job-b.yaml"),
                ],
                output_dir=str(tmp_path / "out"),
            ),
            label="local-job",
        )

        assert result.passed is True
        assert {s.name for s in result.suites} == {"alpha-suite", "beta-suite"}

    def test_nonexistent_local_job_dir_raises(self, tmp_path):
        """Passing a path whose parent does not exist raises RuntimeError."""
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        with pytest.raises(RuntimeError, match="does not exist"):
            runner.run(
                resolved=resolved,
                direct_config=None,
                overrides=DirectRunOverrides(
                    backend="direct-local",
                    local_job_paths=[str(tmp_path / "nonexistent" / "job.yaml")],
                    output_dir=str(tmp_path / "out"),
                ),
                label="local-job",
            )

    def test_local_job_yaml_git_repo_definitions(self, tmp_path):
        """Entries with `from: git` and `repository` are cloned and run from that repo."""
        # Create a local git repo acting as the remote test-definitions repository
        remote_repo = tmp_path / "remote-defs"
        defs_dir = remote_repo / "automated" / "linux"
        defs_dir.mkdir(parents=True)
        (defs_dir / "smoke.yaml").write_text(
            """
metadata:
  name: remote-smoke-suite
run:
  steps:
    - "echo remote-ok"
""",
            encoding="utf-8",
        )
        _init_git_repo(remote_repo)

        # Write a LAVA job YAML that references the local git repo as if it were remote
        job_file = tmp_path / "job.yaml"
        job_file.write_text(
            f"""
actions:
  - test:
      definitions:
        - repository: {remote_repo}
          from: git
          path: automated/linux/smoke.yaml
          name: smoke
          parameters:
            TIMEOUT: "30"
""",
            encoding="utf-8",
        )

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=None,
            overrides=DirectRunOverrides(
                backend="direct-local",
                local_job_paths=[str(job_file)],
                output_dir=str(tmp_path / "out"),
            ),
            label="git-job",
        )

        assert result.passed is True
        assert result.suites[0].name == "remote-smoke-suite"

    def test_local_job_yaml_mixed_git_and_local_definitions(self, tmp_path):
        """A job YAML mixing `from: git` and local-path entries runs both."""
        # Remote repo (git)
        remote_repo = tmp_path / "remote-defs"
        (remote_repo / "tests").mkdir(parents=True)
        (remote_repo / "tests" / "remote.yaml").write_text(
            """
metadata:
  name: remote-suite
run:
  steps:
    - "echo remote"
""",
            encoding="utf-8",
        )
        _init_git_repo(remote_repo)

        # Local test definition (no repository)
        local_dir = tmp_path / "local-defs"
        local_dir.mkdir()
        (local_dir / "local.yaml").write_text(
            """
metadata:
  name: local-suite
run:
  steps:
    - "echo local"
""",
            encoding="utf-8",
        )

        job_file = local_dir / "job.yaml"
        job_file.write_text(
            f"""
actions:
  - test:
      definitions:
        - repository: {remote_repo}
          from: git
          path: tests/remote.yaml
          name: remote
        - path: local.yaml
""",
            encoding="utf-8",
        )

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=None,
            overrides=DirectRunOverrides(
                backend="direct-local",
                local_job_paths=[str(job_file)],
                output_dir=str(tmp_path / "out"),
            ),
            label="mixed-job",
        )

        assert result.passed is True
        assert {s.name for s in result.suites} == {"remote-suite", "local-suite"}

    def test_local_job_yaml_git_entry_params_merged(self, tmp_path):
        """Parameters from `from: git` entries are merged: entry params override CLI params."""
        remote_repo = tmp_path / "remote-defs"
        remote_repo.mkdir()
        (remote_repo / "param.yaml").write_text(
            """
metadata:
  name: param-suite
run:
  steps:
    - "test ${CLI_PARAM} = cli-value"
    - "test ${ENTRY_PARAM} = entry-value"
""",
            encoding="utf-8",
        )
        _init_git_repo(remote_repo)

        job_file = tmp_path / "job.yaml"
        job_file.write_text(
            f"""
actions:
  - test:
      definitions:
        - repository: {remote_repo}
          from: git
          path: param.yaml
          name: param
          parameters:
            ENTRY_PARAM: entry-value
""",
            encoding="utf-8",
        )

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=None,
            overrides=DirectRunOverrides(
                backend="direct-local",
                local_job_paths=[str(job_file)],
                params={"CLI_PARAM": "cli-value", "ENTRY_PARAM": "cli-override"},
                output_dir=str(tmp_path / "out"),
            ),
            label="git-params",
        )

        assert result.passed is True
        assert result.suites[0].name == "param-suite"


class TestJinja2JobPath:
    """Tests for Jinja2 template support in --test-job-path."""

    def test_jinja2_template_rendered_before_yaml_parse(self, tmp_path):
        """A .jinja2 job file is rendered as Jinja2 before being parsed as YAML."""
        base = tmp_path / "project"
        defs_dir = base / "defs"
        defs_dir.mkdir(parents=True)

        (defs_dir / "smoke.yaml").write_text(
            """
metadata:
  name: smoke-suite
run:
  steps:
    - "echo hello"
""",
            encoding="utf-8",
        )
        job_file = base / "job.jinja2"
        job_file.write_text(
            """
actions:
  - test:
      definitions:
        - path: defs/smoke.yaml
""",
            encoding="utf-8",
        )

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=None,
            overrides=DirectRunOverrides(
                backend="direct-local",
                local_job_paths=[str(job_file)],
                output_dir=str(tmp_path / "out"),
            ),
            label="jinja2-job",
        )

        assert result.passed is True
        assert result.suites[0].name == "smoke-suite"

    def test_j2_extension_also_rendered(self, tmp_path):
        """A .j2 job file is rendered as Jinja2 before being parsed as YAML."""
        base = tmp_path / "project"
        defs_dir = base / "defs"
        defs_dir.mkdir(parents=True)

        (defs_dir / "net.yaml").write_text(
            """
metadata:
  name: net-suite
run:
  steps:
    - "echo net-ok"
""",
            encoding="utf-8",
        )
        job_file = base / "job.j2"
        job_file.write_text(
            """
actions:
  - test:
      definitions:
        - path: defs/net.yaml
""",
            encoding="utf-8",
        )

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=None,
            overrides=DirectRunOverrides(
                backend="direct-local",
                local_job_paths=[str(job_file)],
                output_dir=str(tmp_path / "out"),
            ),
            label="j2-job",
        )

        assert result.passed is True
        assert result.suites[0].name == "net-suite"

    def test_jinja2_template_uses_resolved_context(self, tmp_path):
        """Template variables (device_slug, release_slug, etc.) are available."""
        base = tmp_path / "project"
        defs_dir = base / "defs"
        defs_dir.mkdir(parents=True)

        # Suite that checks its name was set from the template rendering
        (defs_dir / "smoke.yaml").write_text(
            """
metadata:
  name: smoke-suite
run:
  steps:
    - "echo hello"
""",
            encoding="utf-8",
        )
        # Template uses device_slug and release_slug in a comment (no-op for YAML)
        # but conditionally includes a definition based on feature_slugs
        job_file = base / "job.jinja2"
        job_file.write_text(
            """
# device={{ device_slug }} release={{ release_slug }}
actions:
  - test:
      definitions:
        - path: defs/smoke.yaml
""",
            encoding="utf-8",
        )

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")

        device = SimpleNamespace(slug="qemu-arm64")
        release = SimpleNamespace(slug="scarthgap")
        resolved = SimpleNamespace(
            build_path=str(tmp_path / "build"),
            device=device,
            release=release,
            features=[],
        )

        result = runner.run(
            resolved=resolved,
            direct_config=None,
            overrides=DirectRunOverrides(
                backend="direct-local",
                local_job_paths=[str(job_file)],
                output_dir=str(tmp_path / "out"),
            ),
            label="jinja2-context",
        )

        assert result.passed is True
        assert result.suites[0].name == "smoke-suite"

    def test_jinja2_template_params_available(self, tmp_path):
        """CLI --test-param values are available as ``params`` in the template."""
        base = tmp_path / "project"
        defs_dir = base / "defs"
        defs_dir.mkdir(parents=True)

        (defs_dir / "param.yaml").write_text(
            """
metadata:
  name: param-suite
run:
  steps:
    - "echo hello"
""",
            encoding="utf-8",
        )
        # Template selects a definition path using a param value
        job_file = base / "job.jinja2"
        job_file.write_text(
            """
actions:
  - test:
      definitions:
        - path: defs/{{ params.get('SUITE', 'param') }}.yaml
""",
            encoding="utf-8",
        )

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=None,
            overrides=DirectRunOverrides(
                backend="direct-local",
                local_job_paths=[str(job_file)],
                params={"SUITE": "param"},
                output_dir=str(tmp_path / "out"),
            ),
            label="jinja2-params",
        )

        assert result.passed is True
        assert result.suites[0].name == "param-suite"

    def test_jinja2_conditional_definitions(self, tmp_path):
        """Jinja2 conditionals in a job template are evaluated correctly."""
        base = tmp_path / "project"
        defs_dir = base / "defs"
        defs_dir.mkdir(parents=True)

        (defs_dir / "smoke.yaml").write_text(
            """
metadata:
  name: smoke-suite
run:
  steps:
    - "echo hello"
""",
            encoding="utf-8",
        )
        (defs_dir / "extra.yaml").write_text(
            """
metadata:
  name: extra-suite
run:
  steps:
    - "echo extra"
""",
            encoding="utf-8",
        )
        # Template conditionally adds an extra suite based on a feature slug
        job_file = base / "job.jinja2"
        job_file.write_text(
            """
actions:
  - test:
      definitions:
        - path: defs/smoke.yaml
{% if 'extra' in feature_slugs %}
        - path: defs/extra.yaml
{% endif %}
""",
            encoding="utf-8",
        )

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")

        device = SimpleNamespace(slug="qemu-arm64")
        release = SimpleNamespace(slug="scarthgap")
        extra_feature = SimpleNamespace(slug="extra")
        resolved = SimpleNamespace(
            build_path=str(tmp_path / "build"),
            device=device,
            release=release,
            features=[extra_feature],
        )

        result = runner.run(
            resolved=resolved,
            direct_config=None,
            overrides=DirectRunOverrides(
                backend="direct-local",
                local_job_paths=[str(job_file)],
                output_dir=str(tmp_path / "out"),
            ),
            label="jinja2-conditional",
        )

        assert result.passed is True
        assert [s.name for s in result.suites] == ["smoke-suite", "extra-suite"]

    def test_jinja2_template_syntax_error_raises_runtime_error(self, tmp_path):
        """A Jinja2 syntax error in the job template raises RuntimeError."""
        base = tmp_path / "project"
        base.mkdir(parents=True)

        job_file = base / "bad.jinja2"
        job_file.write_text("{{ unclosed_block", encoding="utf-8")

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        with pytest.raises(RuntimeError, match="Failed to render Jinja2 job template"):
            runner.run(
                resolved=resolved,
                direct_config=None,
                overrides=DirectRunOverrides(
                    backend="direct-local",
                    local_job_paths=[str(job_file)],
                    output_dir=str(tmp_path / "out"),
                ),
            )


class TestSuiteFilter:
    """Tests for --test-suite (suites=...) filtering of LAVA job entries."""

    @staticmethod
    def _write_job_with_named_suites(base):
        defs_dir = base / "defs"
        defs_dir.mkdir(parents=True)
        for suite in ("alpha", "beta", "gamma"):
            (defs_dir / f"{suite}.yaml").write_text(
                f"""
metadata:
  name: {suite}-suite
run:
  steps:
    - "echo {suite}-ok"
""",
                encoding="utf-8",
            )
        job_file = base / "job.yaml"
        job_file.write_text(
            """
actions:
  - test:
      definitions:
        - path: defs/alpha.yaml
          name: adv-alpha
        - path: defs/beta.yaml
          name: adv-beta
        - path: defs/gamma.yaml
          name: adv-gamma
""",
            encoding="utf-8",
        )
        return job_file

    def test_single_suite_selected(self, tmp_path):
        """Only the requested suite from the job YAML is executed."""
        job_file = self._write_job_with_named_suites(tmp_path / "project")

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=None,
            overrides=DirectRunOverrides(
                backend="direct-local",
                local_job_paths=[str(job_file)],
                suites=["adv-beta"],
                output_dir=str(tmp_path / "out"),
            ),
            label="suite-filter",
        )

        assert result.passed is True
        assert [s.name for s in result.suites] == ["beta-suite"]

    def test_multiple_suites_selected(self, tmp_path):
        """--test-suite is repeatable and selects each matching entry."""
        job_file = self._write_job_with_named_suites(tmp_path / "project")

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=None,
            overrides=DirectRunOverrides(
                backend="direct-local",
                local_job_paths=[str(job_file)],
                suites=["adv-alpha", "adv-gamma"],
                output_dir=str(tmp_path / "out"),
            ),
            label="suite-filter",
        )

        assert result.passed is True
        assert [s.name for s in result.suites] == ["alpha-suite", "gamma-suite"]

    def test_no_filter_runs_all_suites(self, tmp_path):
        """Without --test-suite every job entry still runs (regression guard)."""
        job_file = self._write_job_with_named_suites(tmp_path / "project")

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=None,
            overrides=DirectRunOverrides(
                backend="direct-local",
                local_job_paths=[str(job_file)],
                output_dir=str(tmp_path / "out"),
            ),
            label="suite-filter",
        )

        assert result.passed is True
        assert [s.name for s in result.suites] == ["alpha-suite", "beta-suite", "gamma-suite"]

    def test_unmatched_suite_lists_available_names(self, tmp_path):
        """An unmatched --test-suite raises an error listing available suites."""
        job_file = self._write_job_with_named_suites(tmp_path / "project")

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        with pytest.raises(RuntimeError) as exc_info:
            runner.run(
                resolved=resolved,
                direct_config=None,
                overrides=DirectRunOverrides(
                    backend="direct-local",
                    local_job_paths=[str(job_file)],
                    suites=["missing"],
                    output_dir=str(tmp_path / "out"),
                ),
            )

        message = str(exc_info.value)
        assert "No test suites matched" in message
        assert "adv-alpha" in message
        assert "adv-beta" in message
        assert "adv-gamma" in message

    def test_filter_skips_unselected_git_repositories(self, tmp_path):
        """Only the git repository of the selected suite is cloned."""
        remote_repo = tmp_path / "remote-defs"
        (remote_repo / "tests").mkdir(parents=True)
        (remote_repo / "tests" / "remote.yaml").write_text(
            """
metadata:
  name: remote-suite
run:
  steps:
    - "echo remote-ok"
""",
            encoding="utf-8",
        )
        _init_git_repo(remote_repo)

        job_file = tmp_path / "job.yaml"
        job_file.write_text(
            f"""
actions:
  - test:
      definitions:
        - repository: {remote_repo}
          from: git
          path: tests/remote.yaml
          name: adv-remote
        - repository: {tmp_path / "does-not-exist"}
          from: git
          path: tests/other.yaml
          name: adv-other
""",
            encoding="utf-8",
        )

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=None,
            overrides=DirectRunOverrides(
                backend="direct-local",
                local_job_paths=[str(job_file)],
                suites=["adv-remote"],
                output_dir=str(tmp_path / "out"),
            ),
            label="suite-filter-git",
        )

        assert result.passed is True
        assert [s.name for s in result.suites] == ["remote-suite"]

    def test_entry_name_used_when_definition_has_no_metadata_name(self, tmp_path):
        """The LAVA entry name becomes the suite name when metadata.name is absent."""
        base = tmp_path / "project"
        defs_dir = base / "defs"
        defs_dir.mkdir(parents=True)
        (defs_dir / "context.yaml").write_text(
            """
run:
  steps:
    - "echo context-ok"
""",
            encoding="utf-8",
        )
        job_file = base / "job.yaml"
        job_file.write_text(
            """
actions:
  - test:
      definitions:
        - path: defs/context.yaml
          name: adv-context
""",
            encoding="utf-8",
        )

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))
        output_dir = tmp_path / "out"

        result = runner.run(
            resolved=resolved,
            direct_config=None,
            overrides=DirectRunOverrides(
                backend="direct-local",
                local_job_paths=[str(job_file)],
                suites=["adv-context"],
                output_dir=str(output_dir),
            ),
            label="suite-name",
        )

        assert result.passed is True
        assert [s.name for s in result.suites] == ["adv-context"]

        summary = json.loads((output_dir / "direct-test-summary.json").read_text(encoding="utf-8"))
        assert [s["name"] for s in summary["suites"]] == ["adv-context"]

    def test_filter_applies_to_job_yaml_from_definition_paths(self, tmp_path):
        """Job YAMLs reached via --test-definition-path honour the suite filter."""
        base = tmp_path / "repo"
        defs_dir = base / "defs"
        defs_dir.mkdir(parents=True)
        for suite in ("one", "two"):
            (defs_dir / f"{suite}.yaml").write_text(
                f"""
metadata:
  name: {suite}-suite
run:
  steps:
    - "echo {suite}-ok"
""",
                encoding="utf-8",
            )
        (base / "job.yaml").write_text(
            """
actions:
  - test:
      definitions:
        - path: defs/one.yaml
          name: adv-one
        - path: defs/two.yaml
          name: adv-two
""",
            encoding="utf-8",
        )

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        resolved = SimpleNamespace(build_path=str(tmp_path / "build"))

        result = runner.run(
            resolved=resolved,
            direct_config=DirectTestConfig(
                definitions=[TestDefinitionSource(local_dir=str(base), paths=["job.yaml"])],
            ),
            overrides=DirectRunOverrides(
                backend="direct-local",
                suites=["adv-two"],
                output_dir=str(tmp_path / "out"),
            ),
            label="suite-filter-defpath",
        )

        assert result.passed is True
        assert [s.name for s in result.suites] == ["two-suite"]


class TestLavaSignalAttributeParsing:
    """Tests for extended ``<LAVA_SIGNAL_TESTCASE ...>`` attribute parsing."""

    def test_legacy_two_field_signal(self):
        signals = DirectTestRunner._parse_lava_signals(
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ping RESULT=pass>"
        )
        assert len(signals) == 1
        assert signals[0].test_case_id == "ping"
        assert signals[0].result == "pass"
        assert signals[0].passed is True
        assert signals[0].extra == {}

    def test_measurement_and_units(self):
        signals = DirectTestRunner._parse_lava_signals(
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=cpu0 RESULT=pass MEASUREMENT=1600000 UNITS=Hz>"
        )
        assert signals[0].measurement == "1600000"
        assert signals[0].units == "Hz"
        assert signals[0].report_result == "PASS (1600000 Hz)"

    def test_quoted_values_with_spaces(self):
        signals = DirectTestRunner._parse_lava_signals(
            '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=disk RESULT=pass '
            'DESCRIPTION="The specified disk shall be readable">'
        )
        assert signals[0].description == "The specified disk shall be readable"

    def test_requirement_id_attribute(self):
        signals = DirectTestRunner._parse_lava_signals(
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=L-CPU-MODEL-x RESULT=pass REQUIREMENT_ID=L-CPU-MODEL>"
        )
        assert signals[0].requirement_id == "L-CPU-MODEL"

    def test_unknown_attributes_are_preserved(self):
        signals = DirectTestRunner._parse_lava_signals(
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=x RESULT=pass CUSTOM=value>"
        )
        assert signals[0].extra == {"CUSTOM": "value"}

    def test_skip_result_is_not_a_failure(self):
        signals = DirectTestRunner._parse_lava_signals(
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=x RESULT=skip>"
        )
        assert signals[0].not_run is True
        assert signals[0].failed is False
        assert signals[0].passed is False

    def test_manual_result(self):
        signals = DirectTestRunner._parse_lava_signals(
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=x RESULT=manual>"
        )
        assert signals[0].is_manual is True
        assert signals[0].failed is False
        assert signals[0].report_result == "MANUAL"

    def test_signal_without_result_is_ignored(self):
        assert DirectTestRunner._parse_lava_signals("<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=x>") == []


class TestRequirementCatalog:
    """Tests for requirement catalogue loading and resolution."""

    def test_mapping_catalog_file(self, tmp_path):
        path = tmp_path / "requirements.yaml"
        path.write_text(
            "L-CPU-MODEL:\n"
            "  description: The Linux Kernel shall enumerate the specified CPU Model\n"
            "  specification: Cortex-A53\n"
            "  version: 1\n"
            "  category: CPU\n",
            encoding="utf-8",
        )
        catalog = load_catalog_file(path)
        entry = catalog.get("L-CPU-MODEL")
        assert entry.description == "The Linux Kernel shall enumerate the specified CPU Model"
        assert entry.specification_for() == "Cortex-A53"
        assert entry.version == "1"
        assert entry.category == "CPU"

    def test_list_catalog_with_requirements_wrapper(self, tmp_path):
        path = tmp_path / "requirements.yaml"
        path.write_text(
            "requirements:\n"
            "  - id: L-DISK-DEV\n"
            "    description: The Linux Kernel shall enumerate the specified disk device\n"
            "    category: DISK\n",
            encoding="utf-8",
        )
        catalog = load_catalog_file(path)
        assert catalog.get("L-DISK-DEV").category == "DISK"

    def test_malformed_catalog_is_skipped(self, tmp_path):
        path = tmp_path / "requirements.yaml"
        path.write_text("this: [is, not: valid\n", encoding="utf-8")
        assert len(load_catalog_file(path)) == 0

    def test_missing_catalog_file_yields_empty_catalog(self, tmp_path):
        catalog = discover_catalog(tmp_path)
        assert len(catalog) == 0

    def test_discover_conventional_catalog(self, tmp_path):
        (tmp_path / "requirements.yaml").write_text("L-X:\n  description: X\n", encoding="utf-8")
        assert len(discover_catalog(tmp_path)) == 1

    def test_discover_explicit_path(self, tmp_path):
        (tmp_path / "custom.yaml").write_text("L-Y:\n  description: Y\n", encoding="utf-8")
        catalog = discover_catalog(tmp_path, explicit_paths=["custom.yaml"])
        assert catalog.get("L-Y").description == "Y"

    def test_longest_prefix_resolution_with_instance(self, tmp_path):
        path = tmp_path / "requirements.yaml"
        path.write_text(
            "L-CPU-FREQ-SCALING:\n  description: base\n"
            "L-CPU-FREQ-SCALING-MAX:\n"
            "  description: The CPU should match the specified maximum scaling frequency\n"
            "  specification:\n    cpu0: 1600000\n    cpu1: 1500000\n",
            encoding="utf-8",
        )
        catalog = load_catalog_file(path)
        entry, instance = catalog.resolve("L-CPU-FREQ-SCALING-MAX-cpu1")
        assert entry.id == "L-CPU-FREQ-SCALING-MAX"
        assert instance == "cpu1"
        assert entry.specification_for(instance) == "1500000"

    def test_explicit_requirement_id_wins_over_prefix(self, tmp_path):
        path = tmp_path / "requirements.yaml"
        path.write_text("L-A:\n  description: A\nL-A-B:\n  description: AB\n", encoding="utf-8")
        catalog = load_catalog_file(path)
        entry, instance = catalog.resolve("L-A-B-c", requirement_id="L-A")
        assert entry.id == "L-A"
        assert instance == "B-c"

    def test_inline_catalog_from_definition(self):
        catalog = inline_catalog(
            {"metadata": {"test_cases": {"L-X": {"description": "inline description"}}}}
        )
        assert catalog.get("L-X").description == "inline description"

    def test_humanize_test_case_id(self):
        assert humanize_test_case_id("ping-gateway") == "Ping gateway"

    def test_verifies_and_remarks_are_loaded(self, tmp_path):
        path = tmp_path / "requirements.yaml"
        path.write_text(
            "L-CPU-MODEL:\n"
            "  description: The Linux Kernel shall enumerate the specified CPU Model\n"
            "  verifies: 'lscpu reports the model name configured for the device'\n"
            "  remarks: Requires a booted userspace\n",
            encoding="utf-8",
        )
        entry = load_catalog_file(path).get("L-CPU-MODEL")
        assert entry.verifies == "lscpu reports the model name configured for the device"
        assert entry.remarks == "Requires a booted userspace"

    def test_purpose_is_an_alias_for_description(self, tmp_path):
        path = tmp_path / "requirements.yaml"
        path.write_text("L-X:\n  purpose: Why this case exists\n", encoding="utf-8")
        assert load_catalog_file(path).get("L-X").description == "Why this case exists"

    def test_string_only_entry_keeps_working(self, tmp_path):
        path = tmp_path / "requirements.yaml"
        path.write_text("L-X: A plain description\n", encoding="utf-8")
        entry = load_catalog_file(path).get("L-X")
        assert entry.description == "A plain description"
        assert entry.verifies == ""
        assert entry.remarks == ""

    def test_malformed_verifies_value_is_stringified(self, tmp_path):
        path = tmp_path / "requirements.yaml"
        path.write_text(
            "L-X:\n  description: X\n  verifies:\n    - first check\n    - second check\n",
            encoding="utf-8",
        )
        assert load_catalog_file(path).get("L-X").verifies == "first check second check"


class _FakeResponse:
    def __init__(self, text="", error=None):
        self.text = text
        self._error = error

    def raise_for_status(self):
        if self._error is not None:
            raise self._error


class TestRemoteRequirementCatalog:
    """Tests for requirement catalogues referenced by an http(s) URL."""

    _CATALOG_URL = (
        "https://github.com/miketsukerman/modular-bsp-test-definitions/"
        "blob/main/requirements.yaml"
    )
    _RAW_URL = (
        "https://raw.githubusercontent.com/miketsukerman/"
        "modular-bsp-test-definitions/main/requirements.yaml"
    )
    _CATALOG_TEXT = (
        "requirements:\n"
        "  L-CAN-DEV:\n"
        "    description: The configured CAN network interface exists on the target.\n"
        "    verifies: Runs `ip addr show <iface>` and requires it to succeed.\n"
        "    category: CAN\n"
        "    version: 1\n"
    )

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        clear_catalog_url_cache()
        yield
        clear_catalog_url_cache()

    def _patch_requests(self, monkeypatch, response, calls=None):
        def _get(url, timeout=None):
            if calls is not None:
                calls.append((url, timeout))
            return response

        monkeypatch.setattr("requests.get", _get)

    def test_is_catalog_url(self):
        assert is_catalog_url(self._CATALOG_URL) is True
        assert is_catalog_url("http://example.com/requirements.yaml") is True
        assert is_catalog_url("requirements.yaml") is False
        assert is_catalog_url("/etc/requirements.yaml") is False

    def test_github_blob_url_is_converted_to_raw(self):
        assert raw_catalog_url(self._CATALOG_URL) == self._RAW_URL
        assert raw_catalog_url(self._CATALOG_URL.replace("/blob/", "/raw/")) == self._RAW_URL

    def test_non_github_url_is_left_unchanged(self):
        url = "https://example.com/catalogs/requirements.yaml"
        assert raw_catalog_url(url) == url

    def test_download_catalog(self, monkeypatch):
        calls = []
        self._patch_requests(monkeypatch, _FakeResponse(self._CATALOG_TEXT), calls)
        catalog = load_catalog_url(self._CATALOG_URL)
        entry = catalog.get("L-CAN-DEV")
        assert entry.description == "The configured CAN network interface exists on the target."
        assert entry.category == "CAN"
        # The GitHub web URL is fetched as raw content, not as an HTML page.
        assert calls[0][0] == self._RAW_URL

    def test_download_is_cached_per_run(self, monkeypatch):
        calls = []
        self._patch_requests(monkeypatch, _FakeResponse(self._CATALOG_TEXT), calls)
        load_catalog_url(self._CATALOG_URL)
        load_catalog_url(self._CATALOG_URL)
        assert len(calls) == 1

    def test_failing_download_yields_empty_catalog(self, monkeypatch, caplog):
        self._patch_requests(monkeypatch, _FakeResponse(error=RuntimeError("404")))
        with caplog.at_level(logging.WARNING):
            catalog = load_catalog_url(self._CATALOG_URL)
        assert len(catalog) == 0
        assert any("Failed to download requirement catalogue" in r.getMessage() for r in caplog.records)

    def test_failing_download_is_not_cached(self, monkeypatch):
        calls = []
        self._patch_requests(monkeypatch, _FakeResponse(error=RuntimeError("boom")), calls)
        load_catalog_url(self._CATALOG_URL)
        self._patch_requests(monkeypatch, _FakeResponse(self._CATALOG_TEXT), calls)
        assert len(load_catalog_url(self._CATALOG_URL)) == 1

    def test_discover_catalog_accepts_url(self, monkeypatch, tmp_path):
        self._patch_requests(monkeypatch, _FakeResponse(self._CATALOG_TEXT))
        catalog = discover_catalog(tmp_path, explicit_paths=[self._CATALOG_URL])
        assert catalog.get("L-CAN-DEV").version == "1"

    def test_local_catalog_overrides_remote_entry(self, monkeypatch, tmp_path):
        self._patch_requests(monkeypatch, _FakeResponse(self._CATALOG_TEXT))
        (tmp_path / "local.yaml").write_text(
            "L-CAN-DEV:\n  description: Board specific text\n", encoding="utf-8"
        )
        catalog = discover_catalog(
            tmp_path, explicit_paths=[self._CATALOG_URL, "local.yaml"]
        )
        assert catalog.get("L-CAN-DEV").description == "Board specific text"

    def test_remote_descriptions_reach_the_report(self, monkeypatch, tmp_path):
        self._patch_requests(monkeypatch, _FakeResponse(self._CATALOG_TEXT))
        definition = (
            "metadata:\n  name: can-suite\n"
            "run:\n  steps:\n"
            "    - \"printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=L-CAN-DEV-can0 RESULT=pass>\\n'\"\n"
        )
        repo = tmp_path / "defs"
        repo.mkdir()
        (repo / "suite.yaml").write_text(definition, encoding="utf-8")
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        cfg = DirectTestConfig(
            definitions=[TestDefinitionSource(local_dir=str(repo), paths=["suite.yaml"])],
            timeout=30,
        )
        result = runner.run(
            resolved=SimpleNamespace(build_path=str(tmp_path / "build")),
            direct_config=cfg,
            overrides=DirectRunOverrides(
                backend="direct-local",
                output_dir=str(tmp_path / "out"),
                requirement_catalog_paths=[self._CATALOG_URL],
            ),
            label="remote-catalog",
        )
        row = DirectTestRunner._build_html_report_context(result.suites)["requirements"][0]
        assert row["description_source"] == "catalogue"
        assert row["description"] == "The configured CAN network interface exists on the target."
        assert row["category"] == "CAN"


class TestCatalogEnrichedReport:
    """Tests for report rows enriched by the requirement catalogue."""

    def _run(self, tmp_path, definition_text, catalog_text=None, params=None, catalogs=None):
        repo = tmp_path / "defs-repo"
        defs_dir = repo / "defs"
        defs_dir.mkdir(parents=True)
        (defs_dir / "suite.yaml").write_text(definition_text, encoding="utf-8")
        if catalog_text is not None:
            (repo / "requirements.yaml").write_text(catalog_text, encoding="utf-8")
        _init_git_repo(repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        cfg = DirectTestConfig(
            definitions=[
                TestDefinitionSource(
                    repo_url=repo.as_uri(),
                    paths=["defs/suite.yaml"],
                    params=params or {},
                )
            ],
            timeout=30,
        )
        return runner.run(
            resolved=SimpleNamespace(build_path=str(tmp_path / "build")),
            direct_config=cfg,
            overrides=DirectRunOverrides(
                backend="direct-local",
                output_dir=str(tmp_path / "out"),
                requirement_catalog_paths=catalogs,
            ),
            label="catalog-run",
        )

    _DEFINITION = (
        "metadata:\n  name: cpu-suite\n"
        "run:\n  steps:\n"
        "    - \"printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=L-CPU-FREQ-SCALING-MAX-cpu0 "
        "RESULT=pass MEASUREMENT=1600000>\\n'\"\n"
    )
    _CATALOG = (
        "L-CPU-FREQ-SCALING-MAX:\n"
        "  description: The CPU should match the specified maximum scaling frequency\n"
        "  specification:\n    cpu0: 1600000\n"
        "  version: 1\n"
        "  category: CPU\n"
    )

    def test_catalog_metadata_reaches_html_and_json(self, tmp_path):
        result = self._run(tmp_path, self._DEFINITION, self._CATALOG)
        assert result.passed is True

        signal = result.suites[0].cases[0].lava_signals[0]
        assert signal.requirement_id == "L-CPU-FREQ-SCALING-MAX"
        assert signal.description == "The CPU should match the specified maximum scaling frequency"
        assert signal.specification == "1600000"
        assert signal.version == "1"
        assert signal.category == "CPU"
        assert signal.report_result == "PASS (1600000)"

        html = (tmp_path / "out" / "direct-test-report.html").read_text(encoding="utf-8")
        assert "Requirement Id" in html
        assert "<th>Description</th>" not in html
        assert "<summary>L-CPU-FREQ-SCALING-MAX-cpu0</summary>" in html
        assert "Parameters" in html
        assert "Version" in html
        assert "The CPU should match the specified maximum scaling frequency" in html
        assert "PASS (1600000)" in html
        assert "Requirements, specification, and verification" in html
        assert "Categories" in html

        summary = json.loads((tmp_path / "out" / "direct-test-summary.json").read_text(encoding="utf-8"))
        sig_json = summary["suites"][0]["cases"][0]["lava_signals"][0]
        assert sig_json["requirement_id"] == "L-CPU-FREQ-SCALING-MAX"
        assert sig_json["category"] == "CPU"
        assert sig_json["verification_status"] == "PASS (1600000)"
        # Existing keys keep their original meaning.
        assert sig_json["test_case_id"] == "L-CPU-FREQ-SCALING-MAX-cpu0"
        assert sig_json["result"] == "pass"
        assert summary["report"]["passed_lava_cases"] == 1
        assert summary["categories"][0]["category"] == "CPU"
        assert summary["requirements"][0]["requirement_id"] == "L-CPU-FREQ-SCALING-MAX"

    def test_explicit_catalog_path_option(self, tmp_path):
        repo = tmp_path / "defs-repo"
        defs_dir = repo / "defs"
        defs_dir.mkdir(parents=True)
        (defs_dir / "suite.yaml").write_text(self._DEFINITION, encoding="utf-8")
        (repo / "custom-reqs.yaml").write_text(self._CATALOG, encoding="utf-8")
        _init_git_repo(repo)

        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        result = runner.run(
            resolved=SimpleNamespace(build_path=str(tmp_path / "build")),
            direct_config=DirectTestConfig(
                definitions=[TestDefinitionSource(repo_url=repo.as_uri(), paths=["defs/suite.yaml"])],
            ),
            overrides=DirectRunOverrides(
                backend="direct-local",
                output_dir=str(tmp_path / "out"),
                requirement_catalog_paths=["custom-reqs.yaml"],
            ),
            label="catalog-run",
        )
        assert result.suites[0].cases[0].lava_signals[0].category == "CPU"

    def test_inline_metadata_overrides_shared_catalog(self, tmp_path):
        definition = (
            "metadata:\n  name: cpu-suite\n"
            "  test_cases:\n"
            "    L-CPU-FREQ-SCALING-MAX:\n"
            "      description: inline description\n"
            "run:\n  steps:\n"
            "    - \"printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=L-CPU-FREQ-SCALING-MAX-cpu0 RESULT=pass>\\n'\"\n"
        )
        result = self._run(tmp_path, definition, self._CATALOG)
        assert result.suites[0].cases[0].lava_signals[0].description == "inline description"

    def test_signal_attribute_overrides_catalog(self, tmp_path):
        definition = (
            "metadata:\n  name: cpu-suite\n"
            "run:\n  steps:\n"
            "    - \"printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=L-CPU-FREQ-SCALING-MAX-cpu0 "
            "RESULT=pass DESCRIPTION=\\\"signal wins\\\">\\n'\"\n"
        )
        result = self._run(tmp_path, definition, self._CATALOG)
        assert result.suites[0].cases[0].lava_signals[0].description == "signal wins"

    def test_description_falls_back_to_humanized_id(self, tmp_path):
        definition = (
            "metadata:\n  name: net-suite\n"
            "run:\n  steps:\n"
            "    - \"printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ping-gateway RESULT=pass>\\n'\"\n"
        )
        result = self._run(tmp_path, definition)
        # No catalogue metadata is recorded on the signal itself, ...
        assert result.suites[0].cases[0].lava_signals[0].description == ""
        # ... but the report row still shows a readable description.
        rows = DirectTestRunner._build_html_report_context(result.suites)["requirements"]
        assert rows[0]["description"] == "Ping gateway"
        assert rows[0]["has_description"] is False

    def test_suite_description_is_reported_on_the_suite(self, tmp_path):
        definition = (
            "metadata:\n  name: net-suite\n  description: Networking checks\n"
            "run:\n  steps:\n"
            "    - \"printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ping-gateway RESULT=pass>\\n'\"\n"
        )
        result = self._run(tmp_path, definition)
        # The suite description describes the suite, not its test cases.
        assert result.suites[0].description == "Networking checks"
        assert result.suites[0].cases[0].lava_signals[0].description == ""

        html = (tmp_path / "out" / "direct-test-report.html").read_text(encoding="utf-8")
        assert '<p class="suite-description">Networking checks</p>' in html

        summary = json.loads(
            (tmp_path / "out" / "direct-test-summary.json").read_text(encoding="utf-8")
        )
        assert summary["suites"][0]["description"] == "Networking checks"

    def test_params_reach_report_rows(self, tmp_path):
        definition = (
            "metadata:\n  name: net-suite\n"
            "params:\n  BOARD: default\n"
            "run:\n  steps:\n"
            "    - \"printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ping-gateway RESULT=pass>\\n'\"\n"
        )
        result = self._run(tmp_path, definition, params={"BOARD_IP": "10.0.0.1"})
        signal = result.suites[0].cases[0].lava_signals[0]
        assert signal.params == {"BOARD": "default", "BOARD_IP": "10.0.0.1"}
        html = (tmp_path / "out" / "direct-test-report.html").read_text(encoding="utf-8")
        assert "BOARD_IP=10.0.0.1" in html

    def test_optional_columns_hidden_without_metadata(self, tmp_path):
        definition = (
            "metadata:\n  name: net-suite\n"
            "run:\n  steps:\n"
            "    - \"printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ping-gateway RESULT=pass>\\n'\"\n"
        )
        self._run(tmp_path, definition)
        html = (tmp_path / "out" / "direct-test-report.html").read_text(encoding="utf-8")
        assert "Requirement Id" not in html
        assert "Req. version" not in html
        assert "Categories" not in html

    def test_description_is_folded_into_test_case_cell(self, tmp_path):
        definition = (
            "metadata:\n  name: net-suite\n"
            "run:\n  steps:\n"
            "    - \"printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ping-gateway RESULT=pass>\\n'\"\n"
        )
        self._run(tmp_path, definition)
        html = (tmp_path / "out" / "direct-test-report.html").read_text(encoding="utf-8")
        # The description has no column of its own; it expands from the test
        # case id cell.  Derived text keeps the muted style that marks it.
        assert "<th>Description</th>" not in html
        assert "<summary>ping-gateway</summary>" in html
        assert "Ping gateway" in html
        assert 'class="desc-derived"' in html

    def test_description_source_precedence(self, tmp_path):
        catalog = "ping-gateway:\n  description: catalogue text\n"
        signal_definition = (
            "metadata:\n  name: net-suite\n"
            "run:\n  steps:\n"
            "    - \"printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ping-gateway RESULT=pass "
            "DESCRIPTION=\\\"signal text\\\">\\n'\"\n"
        )
        plain_definition = (
            "metadata:\n  name: net-suite\n"
            "run:\n  steps:\n"
            "    - \"printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ping-gateway RESULT=pass>\\n'\"\n"
        )

        def _row(result):
            return DirectTestRunner._build_html_report_context(result.suites)["requirements"][0]

        row = _row(self._run(tmp_path / "signal", signal_definition, catalog))
        assert (row["description"], row["description_source"]) == ("signal text", "signal")

        row = _row(self._run(tmp_path / "catalog", plain_definition, catalog))
        assert (row["description"], row["description_source"]) == ("catalogue text", "catalogue")

        row = _row(self._run(tmp_path / "derived", plain_definition))
        assert (row["description"], row["description_source"]) == ("Ping gateway", "derived")

    def test_verifies_and_remarks_reach_report_and_summary(self, tmp_path):
        catalog = (
            "ping-gateway:\n"
            "  description: The device shall reach its default gateway\n"
            "  verifies: 'ping -c1 the gateway address exits with status 0'\n"
            "  remarks: Needs a configured network\n"
        )
        definition = (
            "metadata:\n  name: net-suite\n"
            "run:\n  steps:\n"
            "    - \"printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ping-gateway RESULT=pass>\\n'\"\n"
        )
        result = self._run(tmp_path, definition, catalog)
        signal = result.suites[0].cases[0].lava_signals[0]
        assert signal.verifies == "ping -c1 the gateway address exits with status 0"
        assert signal.remarks == "Needs a configured network"

        html = (tmp_path / "out" / "direct-test-report.html").read_text(encoding="utf-8")
        assert "Verifies: ping -c1 the gateway address exits with status 0" in html
        assert "Remarks: Needs a configured network" in html

        summary = json.loads(
            (tmp_path / "out" / "direct-test-summary.json").read_text(encoding="utf-8")
        )
        signal_json = summary["suites"][0]["cases"][0]["lava_signals"][0]
        assert signal_json["verifies"] == "ping -c1 the gateway address exits with status 0"
        assert signal_json["remarks"] == "Needs a configured network"
        assert signal_json["description_source"] == "catalogue"

    def test_signal_attributes_override_verifies_and_remarks(self, tmp_path):
        catalog = (
            "ping-gateway:\n  description: d\n  verifies: catalogue verifies\n"
            "  remarks: catalogue remarks\n"
        )
        definition = (
            "metadata:\n  name: net-suite\n"
            "run:\n  steps:\n"
            "    - \"printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ping-gateway RESULT=pass "
            "VERIFIES=\\\"signal verifies\\\" REMARKS=\\\"signal remarks\\\">\\n'\"\n"
        )
        signal = self._run(tmp_path, definition, catalog).suites[0].cases[0].lava_signals[0]
        assert signal.verifies == "signal verifies"
        assert signal.remarks == "signal remarks"

    def test_step_description_reaches_report_and_summary(self, tmp_path):
        definition = (
            "metadata:\n  name: net-suite\n"
            "run:\n  steps:\n"
            "    - command: \"true\"\n"
            "      description: Bring the network interface up\n"
        )
        result = self._run(tmp_path, definition)
        assert result.suites[0].cases[0].description == "Bring the network interface up"

        html = (tmp_path / "out" / "direct-test-report.html").read_text(encoding="utf-8")
        assert "Executed steps (1)" in html
        assert "Bring the network interface up" in html

        summary = json.loads(
            (tmp_path / "out" / "direct-test-summary.json").read_text(encoding="utf-8")
        )
        assert summary["suites"][0]["cases"][0]["description"] == "Bring the network interface up"

    def test_step_description_from_metadata_steps(self, tmp_path):
        definition = (
            "metadata:\n  name: net-suite\n"
            "  steps:\n    step-1: Bring the network interface up\n"
            "run:\n  steps:\n    - \"true\"\n"
        )
        result = self._run(tmp_path, definition)
        assert result.suites[0].cases[0].description == "Bring the network interface up"

    def test_undescribed_steps_are_not_listed(self, tmp_path):
        definition = (
            "metadata:\n  name: net-suite\n"
            "run:\n  steps:\n    - \"true\"\n"
        )
        self._run(tmp_path, definition)
        html = (tmp_path / "out" / "direct-test-report.html").read_text(encoding="utf-8")
        assert "Executed steps" not in html

    def test_description_coverage_kpi_and_warning(self, tmp_path, caplog):
        catalog = "described-case:\n  description: A described test case\n"
        definition = (
            "metadata:\n  name: net-suite\n"
            "run:\n  steps:\n"
            "    - \"printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=described-case RESULT=pass>\\n"
            "<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ping-gateway RESULT=pass>\\n'\"\n"
        )
        with caplog.at_level(logging.WARNING):
            result = self._run(tmp_path, definition, catalog)

        context = DirectTestRunner._build_html_report_context(result.suites)
        assert context["report"]["described_lava_cases"] == 1
        assert context["report"]["total_lava_cases"] == 2
        assert context["undescribed_cases"] == ["ping-gateway"]

        html = (tmp_path / "out" / "direct-test-report.html").read_text(encoding="utf-8")
        assert "Described tests" in html

        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("ping-gateway" in message for message in warnings)


class TestReportContextAggregation:
    """Unit tests for the report context builder aggregation logic."""

    @staticmethod
    def _suite(signals):
        return DirectTestSuiteResult(
            name="suite",
            status="PASS",
            duration=1.0,
            cases=[
                DirectTestCaseResult(
                    name="step-1",
                    status="PASS",
                    duration=1.0,
                    command="run",
                    lava_signals=signals,
                )
            ],
        )

    def test_counts_by_result_state(self):
        suites = [
            self._suite(
                [
                    LavaSignalCase(test_case_id="a", result="pass"),
                    LavaSignalCase(test_case_id="b", result="fail"),
                    LavaSignalCase(test_case_id="c", result="skip"),
                    LavaSignalCase(test_case_id="d", result="manual", manual=True),
                ]
            )
        ]
        report = DirectTestRunner._build_html_report_context(suites)["report"]
        assert report["total_lava_cases"] == 4
        assert report["passed_lava_cases"] == 1
        assert report["failed_lava_cases"] == 1
        assert report["not_run_lava_cases"] == 1
        assert report["manual_lava_cases"] == 1

    def test_category_rollup_all_ok(self):
        suites = [self._suite([LavaSignalCase(test_case_id="a", result="pass", category="CPU")])]
        categories = DirectTestRunner._build_html_report_context(suites)["categories"]
        assert categories == [
            {
                "category": "CPU",
                "status": "All automated tests OK",
                "status_class": "pass",
                "remarks": "",
                "total": 1,
                "failed": 0,
                "manual": 0,
            }
        ]

    def test_category_rollup_errors_and_manual_remark(self):
        suites = [
            self._suite(
                [
                    LavaSignalCase(test_case_id="a", result="fail", category="RTC"),
                    LavaSignalCase(test_case_id="b", result="manual", manual=True, category="AUDIO"),
                ]
            )
        ]
        categories = DirectTestRunner._build_html_report_context(suites)["categories"]
        by_name = {row["category"]: row for row in categories}
        assert by_name["RTC"]["status"] == "Errors found"
        assert by_name["RTC"]["status_class"] == "fail"
        assert by_name["AUDIO"]["status"] == "All automated tests OK"
        assert by_name["AUDIO"]["remarks"] == "Has manual tests"

    def test_description_is_escaped_in_html(self, tmp_path):
        suites = [
            self._suite(
                [
                    LavaSignalCase(
                        test_case_id="a",
                        result="pass",
                        description="tags <b> & ampersands",
                    )
                ]
            )
        ]
        runner = DirectTestRunner(config_path=tmp_path / "registry.yaml")
        html = runner._render_html_report(
            label="esc", backend="direct-local", suites=suites, passed=True
        )
        assert "tags &lt;b&gt; &amp; ampersands" in html
        assert "tags <b> & ampersands" not in html
