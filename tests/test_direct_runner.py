"""Tests for direct test-definition execution backend."""

import json
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
    _SshTransport,
)
from bsp.models import DirectTestConfig, DirectTransportConfig, TestDefinitionSource


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
    - "printf '<LAVA_SIGNAL_TESTCASE TEST_CASE_ID=ping-gateway RESULT=pass>\\n'"
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
        assert summary["suites"][0]["cases"][0]["lava_signals"] == [
            {"test_case_id": "ping-gateway", "result": "pass"}
        ]
        assert "LAVA cases: 1" in html
        parser = _TableDataCellParser()
        parser.feed(html)
        assert parser.cells.count("ping-gateway") == 1

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
    - "echo {{GREETING}}-${TARGET} > output.txt"
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
                    ),
                    DirectTestCaseResult(
                        name="step-2",
                        status="FAIL",
                        duration=0.73,
                        command="false",
                        timed_out=False,
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
        assert "echo hello" in content

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
        assert '<div class="preset-info">' not in html

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
        assert "timed-out" in html
        assert "sleep 999" in html

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
        assert sig_json[0] == {"test_case_id": "ping-gateway", "result": "pass"}
        assert sig_json[1] == {"test_case_id": "download-a-file", "result": "fail"}

        html = (tmp_path / "out" / "direct-test-report.html").read_text(encoding="utf-8")
        assert "Overall: <span class=\"badge fail\">" in html
        assert html.count('class="badge warn"') == 2

    def test_html_report_contains_lava_signals(self, tmp_path):
        """HTML report shows LAVA signal cases as a sub-table."""
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
        assert "TEST_CASE_ID" in html
        assert "LAVA test cases" in html

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
        assert "Overall: <span class=\"badge fail\">" in html
        assert html.count('class="badge warn"') == 2
        assert (
            '<div class="card-header">\n'
            '    <span class="badge warn">\n'
            '      PASS\n'
            '    </span>\n'
            '    <h2>net-suite</h2>'
        ) in html
        step_name_index = html.index("step-1")
        step_command_index = html.index("./run-net-tests.sh")
        warn_after_step = html.find('class="badge warn"', step_name_index)
        assert warn_after_step != -1
        assert warn_after_step < step_command_index
        assert "Steps: 1" in html
        assert "LAVA cases: 2" in html


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
