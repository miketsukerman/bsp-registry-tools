"""Tests for direct test-definition execution backend."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bsp.direct_runner import DirectRunOverrides, DirectTestRunner, _SshTransport
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
