"""Tests for direct test-definition execution backend."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bsp.direct_runner import DirectRunOverrides, DirectTestRunner, DirectTestSuiteResult, DirectTestCaseResult, _SshTransport
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
