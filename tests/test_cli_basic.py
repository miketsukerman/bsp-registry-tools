"""
Tests for the basic bsp CLI entry point (main()) commands (v2.0).
"""

from unittest.mock import call, patch

import bsp
from bsp import BspManager, KasManager
from bsp.utils import SUPPORTED_REGISTRY_VERSION


class TestMainCli:
    def test_main_version_flag(self, capsys):
        with patch("sys.argv", ["bsp", "--version"]):
            exit_code = bsp.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "bsp-registry-tools" in captured.out
        assert SUPPORTED_REGISTRY_VERSION in captured.out

    def test_main_list_command(self, registry_file, capsys):
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "list"]):
            exit_code = bsp.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "test-bsp" in captured.out

    def test_main_list_devices_command(self, registry_file, capsys):
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "list", "devices"]):
            exit_code = bsp.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "test-device" in captured.out

    def test_main_list_releases_command(self, registry_file, capsys):
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "list", "releases"]):
            exit_code = bsp.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "test-release" in captured.out

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
            "bsp", "--registry", str(tmp_dir / "missing.yaml"), "list"
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
  version: "2.0"
containers:
  - ubuntu-22.04:
      image: "test/ubuntu-22.04:latest"
      file: Dockerfile.ubuntu
      args: []
registry:
  devices:
    - slug: test-device
      description: "Test Device"
      vendor: test-vendor
      soc_vendor: arm
      build:
        path: build/test
        container: "ubuntu-22.04"
        includes:
          - {kas_file}
  releases:
    - slug: test-release
      description: "Test Release"
  bsp:
    - name: test-bsp
      description: "Test BSP"
      device: test-device
      release: test-release
"""
        registry_file = tmp_dir / "bsp-registry.yaml"
        registry_file.write_text(registry_content)

        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file), "export", "test-bsp"
        ]):
            with patch.object(KasManager, "export_kas_config", return_value="config: data"):
                exit_code = bsp.main()
        assert exit_code == 0

    def test_main_tree_command(self, registry_file, capsys):
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "tree"]):
            exit_code = bsp.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "BSP Registry" in captured.out
        assert "test-device" in captured.out
        assert "test-release" in captured.out

    def test_main_tree_no_color_flag(self, registry_file, capsys):
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "--no-color", "tree"]):
            exit_code = bsp.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "BSP Registry" in captured.out
        assert "\x1b[" not in captured.out

    def test_main_list_no_color_flag(self, registry_file, capsys):
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "--no-color", "list"]):
            exit_code = bsp.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "test-bsp" in captured.out
        assert "\x1b[" not in captured.out

    def test_main_list_devices_no_color_flag(self, registry_file, capsys):
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "--no-color", "list", "devices"]):
            exit_code = bsp.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "test-device" in captured.out
        assert "\x1b[" not in captured.out

    def test_main_list_releases_no_color_flag(self, registry_file, capsys):
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "--no-color", "list", "releases"]):
            exit_code = bsp.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "test-release" in captured.out
        assert "\x1b[" not in captured.out

    def test_main_containers_no_color_flag(self, registry_file, capsys):
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "--no-color", "containers"]):
            exit_code = bsp.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "ubuntu-22.04" in captured.out
        assert "\x1b[" not in captured.out

    def test_main_tree_full_flag(self, registry_with_vendor_overrides_file, capsys):
        with patch("sys.argv", ["bsp", "--registry", str(registry_with_vendor_overrides_file), "--no-color", "tree", "--full"]):
            exit_code = bsp.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "vendor release" in captured.out
        assert "imx-6.6.53" in captured.out

    def test_main_tree_compact_flag(self, registry_with_vendor_overrides_file, capsys):
        with patch("sys.argv", ["bsp", "--registry", str(registry_with_vendor_overrides_file), "--no-color", "tree", "--compact"]):
            exit_code = bsp.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "vendor override:" not in captured.out
        assert "scarthgap" in captured.out

    def test_main_tree_full_and_compact_mutually_exclusive(self, registry_file, capsys):
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "tree", "--full", "--compact"]):
            exit_code = bsp.main()
        assert exit_code != 0

    def test_main_list_releases_shows_vendor_overrides(self, registry_with_vendor_overrides_file, capsys):
        with patch("sys.argv", ["bsp", "--registry", str(registry_with_vendor_overrides_file), "--no-color", "list", "releases"]):
            exit_code = bsp.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "override" in captured.out
        assert "imx-6.6.53" in captured.out

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
            "test-bsp", checkout_only=True, deploy_after_build=False, deploy_overrides={},
            build_path_override=custom_path, target=None, task=None
        )

    def test_main_build_by_components_with_path_override(self, registry_file, tmp_dir):
        """--path argument is forwarded to build_by_components() as build_path_override."""
        custom_path = str(tmp_dir / "components-build")
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "build", "--checkout", "--device", "test-device", "--release", "test-release",
            "--path", custom_path
        ]):
            with patch.object(BspManager, "build_by_components") as mock_build:
                mock_build.return_value = None
                exit_code = bsp.main()
        assert exit_code == 0
        mock_build.assert_called_once_with(
            "test-device", "test-release", [], checkout_only=True, deploy_after_build=False,
            deploy_overrides={}, build_path_override=custom_path, target=None, task=None
        )


class TestBuildCommand:
    def test_build_target_passed_to_build_bsp(self, registry_file):
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "build", "test-bsp", "--target", "my-image"
        ]):
            with patch("bsp.BspManager.build_bsp") as mock_build:
                exit_code = bsp.main()
        assert exit_code == 0
        mock_build.assert_called_once()
        _, kwargs = mock_build.call_args
        assert kwargs.get("target") == "my-image"

    def test_build_task_passed_to_build_bsp(self, registry_file):
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "build", "test-bsp", "--task", "compile"
        ]):
            with patch("bsp.BspManager.build_bsp") as mock_build:
                exit_code = bsp.main()
        assert exit_code == 0
        mock_build.assert_called_once()
        _, kwargs = mock_build.call_args
        assert kwargs.get("task") == "compile"

    def test_build_target_and_task_passed_to_build_by_components(self, registry_file):
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "build", "--device", "test-device", "--release", "test-release",
            "--target", "core-image-minimal", "--task", "configure"
        ]):
            with patch("bsp.BspManager.build_by_components") as mock_build:
                exit_code = bsp.main()
        assert exit_code == 0
        mock_build.assert_called_once()
        _, kwargs = mock_build.call_args
        assert kwargs.get("target") == "core-image-minimal"
        assert kwargs.get("task") == "configure"

    def test_build_no_target_defaults_to_none(self, registry_file):
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "build", "test-bsp"
        ]):
            with patch("bsp.BspManager.build_bsp") as mock_build:
                exit_code = bsp.main()
        assert exit_code == 0
        mock_build.assert_called_once()
        _, kwargs = mock_build.call_args
        assert kwargs.get("target") is None
        assert kwargs.get("task") is None
