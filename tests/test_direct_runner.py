"""Tests for direct test-definition execution backend."""

import subprocess
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
        assert "[direct-test] smoke-suite step-1 (1/2) running" in captured.out
        assert "[direct-test] smoke-suite step-1 (1/2) PASS in" in captured.out
        assert "[direct-test] smoke-suite step-2 (2/2) running" in captured.out
        assert "[direct-test] smoke-suite step-2 (2/2) PASS in" in captured.out

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

        assert result.passed is True
        assert len(result.suites) == 1
        suite = result.suites[0]
        assert suite.name == "network-suite"
        assert len(suite.cases) == 1
        step = suite.cases[0]
        assert len(step.lava_signals) == 2
        assert step.lava_signals[0].test_case_id == "ping-gateway"
        assert step.lava_signals[0].result == "pass"
        assert step.lava_signals[1].test_case_id == "download-a-file"
        assert step.lava_signals[1].result == "fail"

        # Verify JSON output contains lava_signals
        import json
        summary = json.loads((tmp_path / "out" / "direct-test-summary.json").read_text())
        sig_json = summary["suites"][0]["cases"][0]["lava_signals"]
        assert len(sig_json) == 2
        assert sig_json[0] == {"test_case_id": "ping-gateway", "result": "pass"}
        assert sig_json[1] == {"test_case_id": "download-a-file", "result": "fail"}

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

        import pytest
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
