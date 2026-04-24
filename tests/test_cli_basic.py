"""
Tests for the basic bsp CLI entry point (main()) commands.
"""

from unittest.mock import patch

import bsp
from bsp import BspManager, KasManager


class TestMainCli:
    def test_main_list_command(self, registry_file, capsys):
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "list"]):
            exit_code = bsp.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "test-bsp" in captured.out

    def test_main_containers_command(self, registry_file, capsys):
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "containers"]):
            exit_code = bsp.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "ubuntu-22.04" in captured.out

    def test_main_no_command_exits(self):
        with patch("sys.argv", ["bsp"]):
            exit_code = bsp.main()
        assert exit_code != 0

    def test_main_missing_registry_exits(self, tmp_dir):
        with patch("sys.argv", [
            "bsp", "--registry", str(tmp_dir / "missing.yml"), "list"
        ]):
            exit_code = bsp.main()
        assert exit_code != 0

    def test_main_keyboard_interrupt(self, registry_file):
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "list"]):
            with patch.object(BspManager, "initialize", side_effect=KeyboardInterrupt):
                exit_code = bsp.main()
        assert exit_code == 130

    def test_main_export_command_to_stdout(self, tmp_dir, capsys):
        kas_file = tmp_dir / "test.yml"
        kas_file.write_text("header:\n  version: 14\nmachine: qemuarm64\n")
        registry_content = f"""
specification:
  version: "1.0"
registry:
  bsp:
    - name: test-bsp
      description: "Test BSP"
      build:
        path: build/test
        environment:
          container: "ubuntu-22.04"
        configuration:
          - {kas_file}
containers:
  - ubuntu-22.04:
      image: "test/ubuntu-22.04:latest"
      file: Dockerfile.ubuntu
      args: []
"""
        registry_file = tmp_dir / "bsp-registry.yml"
        registry_file.write_text(registry_content)

        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file), "export", "test-bsp"
        ]):
            with patch.object(KasManager, "export_kas_config", return_value="config: data"):
                exit_code = bsp.main()
        assert exit_code == 0

    def test_main_build_with_path_override(self, registry_file, tmp_dir):
        """--path argument is forwarded to build_bsp() as build_path_override."""
        custom_path = str(tmp_dir / "my-custom-build")
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "build", "--checkout", "--path", custom_path, "test-bsp"
        ]):
            with patch.object(BspManager, "build_bsp") as mock_build_bsp:
                mock_build_bsp.return_value = None
                exit_code = bsp.main()
        assert exit_code == 0
        mock_build_bsp.assert_called_once_with(
            "test-bsp", checkout_only=True, build_path_override=custom_path, features=[]
        )

    def test_main_build_with_single_feature(self, registry_file, tmp_dir):
        """--feature argument is forwarded to build_bsp() as a list."""
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "build", "test-bsp", "--feature", "kas/feature-a.yml"
        ]):
            with patch.object(BspManager, "build_bsp") as mock_build_bsp:
                mock_build_bsp.return_value = None
                exit_code = bsp.main()
        assert exit_code == 0
        mock_build_bsp.assert_called_once_with(
            "test-bsp", checkout_only=False, build_path_override=None,
            features=["kas/feature-a.yml"]
        )

    def test_main_build_with_multiple_features(self, registry_file, tmp_dir):
        """Multiple --feature arguments are collected into a list."""
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "build", "test-bsp",
            "--feature", "kas/feature-a.yml",
            "--feature", "kas/feature-b.yml"
        ]):
            with patch.object(BspManager, "build_bsp") as mock_build_bsp:
                mock_build_bsp.return_value = None
                exit_code = bsp.main()
        assert exit_code == 0
        mock_build_bsp.assert_called_once_with(
            "test-bsp", checkout_only=False, build_path_override=None,
            features=["kas/feature-a.yml", "kas/feature-b.yml"]
        )

    def test_main_build_without_feature_defaults_to_empty(self, registry_file):
        """build without --feature passes an empty features list."""
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "build", "test-bsp"
        ]):
            with patch.object(BspManager, "build_bsp") as mock_build_bsp:
                mock_build_bsp.return_value = None
                exit_code = bsp.main()
        assert exit_code == 0
        mock_build_bsp.assert_called_once_with(
            "test-bsp", checkout_only=False, build_path_override=None, features=[]
        )
