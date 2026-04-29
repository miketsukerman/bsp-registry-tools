"""
CLI-level tests for ``bsp registry`` subcommand group.
"""

import copy
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import bsp.cli as bsp_cli


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

MINIMAL_REGISTRY_YAML = """\
specification:
  version: "2.0"
containers:
  ubuntu-22.04:
    image: test/ubuntu-22.04:latest
    file: Dockerfile
registry:
  devices:
    - slug: test-device
      description: Test Device
      vendor: test-vendor
      soc_vendor: test-soc
  releases:
    - slug: test-release
      description: Test Release
  features:
    - slug: test-feature
      description: Test Feature
  bsp:
    - name: test-preset
      description: Test Preset
      device: test-device
      release: test-release
      features: []
  vendors:
    - slug: test-vendor
      name: Test Vendor
  distro: []
  frameworks: []
"""


@pytest.fixture
def registry_file(tmp_path):
    path = tmp_path / "bsp-registry.yaml"
    path.write_text(MINIMAL_REGISTRY_YAML)
    return path


# ---------------------------------------------------------------------------
# registry init
# ---------------------------------------------------------------------------

class TestRegistryInit:
    def test_registry_init_creates_file(self, tmp_path, registry_file):
        out = tmp_path / "new-reg.yaml"
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "init", "--output", str(out),
        ]):
            exit_code = bsp_cli.main()
        assert exit_code == 0
        assert out.exists()

    def test_registry_init_file_contents_valid(self, tmp_path, registry_file):
        out = tmp_path / "new-reg.yaml"
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "init", "--output", str(out),
        ]):
            bsp_cli.main()
        with open(out) as fh:
            data = yaml.safe_load(fh)
        assert data["specification"]["version"] == "2.0"
        assert "registry" in data

    def test_registry_init_fails_if_exists(self, tmp_path, registry_file):
        out = tmp_path / "exists.yaml"
        out.write_text("foo: bar")
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "init", "--output", str(out),
        ]):
            exit_code = bsp_cli.main()
        assert exit_code != 0

    def test_registry_init_force_overwrites(self, tmp_path, registry_file):
        out = tmp_path / "exists.yaml"
        out.write_text("foo: bar")
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "init", "--output", str(out), "--force",
        ]):
            exit_code = bsp_cli.main()
        assert exit_code == 0
        with open(out) as fh:
            data = yaml.safe_load(fh)
        assert data["specification"]["version"] == "2.0"


# ---------------------------------------------------------------------------
# registry validate
# ---------------------------------------------------------------------------

class TestRegistryValidate:
    def test_registry_validate_ok(self, registry_file, capsys):
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file), "registry", "validate",
        ]):
            exit_code = bsp_cli.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "valid" in captured.out.lower()

    def test_registry_validate_bad_version(self, registry_file, capsys):
        bad_yaml = MINIMAL_REGISTRY_YAML.replace('version: "2.0"', 'version: "1.0"')
        registry_file.write_text(bad_yaml)
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file), "registry", "validate",
        ]):
            exit_code = bsp_cli.main()
        assert exit_code != 0
        captured = capsys.readouterr()
        assert "[ERROR]" in captured.out

    def test_registry_validate_broken_device_ref(self, registry_file, capsys):
        data = yaml.safe_load(MINIMAL_REGISTRY_YAML)
        data["registry"]["bsp"][0]["device"] = "nonexistent"
        registry_file.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False)
        )
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file), "registry", "validate",
        ]):
            exit_code = bsp_cli.main()
        assert exit_code != 0


# ---------------------------------------------------------------------------
# registry diff
# ---------------------------------------------------------------------------

class TestRegistryDiff:
    def test_registry_diff_identical(self, registry_file, capsys):
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "diff", str(registry_file),
        ]):
            exit_code = bsp_cli.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_registry_diff_changed(self, registry_file, tmp_path, capsys):
        other = tmp_path / "other.yaml"
        data = yaml.safe_load(MINIMAL_REGISTRY_YAML)
        data["registry"]["devices"][0]["description"] = "Changed description"
        other.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "diff", str(other),
        ]):
            exit_code = bsp_cli.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "---" in captured.out
        assert "+++" in captured.out


# ---------------------------------------------------------------------------
# registry add / show — device
# ---------------------------------------------------------------------------

class TestRegistryAddDevice:
    def test_add_device_then_show(self, registry_file, capsys):
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "add", "device",
            "--slug", "my-board",
            "--description", "My Board",
            "--vendor", "acme",
            "--soc-vendor", "nxp",
        ]):
            exit_code = bsp_cli.main()
        assert exit_code == 0

        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "show", "device", "--slug", "my-board",
        ]):
            exit_code = bsp_cli.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "my-board" in captured.out

    def test_add_device_with_includes(self, registry_file, capsys):
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "add", "device",
            "--slug", "board2",
            "--description", "Board 2",
            "--vendor", "acme",
            "--soc-vendor", "nxp",
            "--includes", "kas/board2.yaml", "kas/extra.yaml",
        ]):
            exit_code = bsp_cli.main()
        assert exit_code == 0
        with open(registry_file) as fh:
            data = yaml.safe_load(fh)
        dev = next(d for d in data["registry"]["devices"] if d["slug"] == "board2")
        assert "kas/board2.yaml" in dev["includes"]


# ---------------------------------------------------------------------------
# registry edit — device
# ---------------------------------------------------------------------------

class TestRegistryEditDevice:
    def test_edit_device_description(self, registry_file, capsys):
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "edit", "device",
            "--slug", "test-device",
            "--description", "Updated Description",
        ]):
            exit_code = bsp_cli.main()
        assert exit_code == 0

        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "show", "device", "--slug", "test-device",
        ]):
            bsp_cli.main()
        captured = capsys.readouterr()
        assert "Updated Description" in captured.out

    def test_edit_nonexistent_device_fails(self, registry_file, capsys):
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "edit", "device",
            "--slug", "does-not-exist",
            "--description", "x",
        ]):
            exit_code = bsp_cli.main()
        assert exit_code != 0


# ---------------------------------------------------------------------------
# registry remove — device
# ---------------------------------------------------------------------------

class TestRegistryRemoveDevice:
    def test_remove_device(self, registry_file, capsys):
        # First add a device we can remove without affecting presets
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "add", "device",
            "--slug", "disposable",
            "--description", "Disposable",
            "--vendor", "x",
            "--soc-vendor", "y",
        ]):
            bsp_cli.main()

        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "remove", "device",
            "--slug", "disposable",
        ]):
            exit_code = bsp_cli.main()
        assert exit_code == 0

        with open(registry_file) as fh:
            data = yaml.safe_load(fh)
        slugs = [d["slug"] for d in data["registry"]["devices"]]
        assert "disposable" not in slugs

    def test_remove_nonexistent_device_fails(self, registry_file, capsys):
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "remove", "device",
            "--slug", "nope",
        ]):
            exit_code = bsp_cli.main()
        assert exit_code != 0


# ---------------------------------------------------------------------------
# registry add / edit / remove — release (round-trip)
# ---------------------------------------------------------------------------

class TestRegistryReleaseCrud:
    def test_add_edit_remove_release(self, registry_file):
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "add", "release",
            "--slug", "nanbield",
            "--description", "Yocto 4.3",
            "--yocto-version", "4.3",
        ]):
            assert bsp_cli.main() == 0

        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "edit", "release",
            "--slug", "nanbield",
            "--description", "Yocto 4.3 LTS",
        ]):
            assert bsp_cli.main() == 0

        with open(registry_file) as fh:
            data = yaml.safe_load(fh)
        rel = next(r for r in data["registry"]["releases"] if r["slug"] == "nanbield")
        assert rel["description"] == "Yocto 4.3 LTS"

        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "remove", "release",
            "--slug", "nanbield",
        ]):
            assert bsp_cli.main() == 0

        with open(registry_file) as fh:
            data = yaml.safe_load(fh)
        slugs = [r["slug"] for r in data["registry"]["releases"]]
        assert "nanbield" not in slugs


# ---------------------------------------------------------------------------
# registry show — list all
# ---------------------------------------------------------------------------

class TestRegistryShowAll:
    def test_show_all_devices(self, registry_file, capsys):
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "show", "device",
        ]):
            exit_code = bsp_cli.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "test-device" in captured.out

    def test_show_all_releases(self, registry_file, capsys):
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "show", "release",
        ]):
            exit_code = bsp_cli.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "test-release" in captured.out


# ---------------------------------------------------------------------------
# registry add -- git-stage / git-commit flags (mocked subprocess)
# ---------------------------------------------------------------------------

class TestRegistryGitFlags:
    def test_add_with_git_stage(self, registry_file):
        with patch("subprocess.run") as mock_run:
            with patch("sys.argv", [
                "bsp", "--registry", str(registry_file),
                "registry", "add", "feature",
                "--slug", "ota",
                "--description", "OTA update",
                "--git-stage",
            ]):
                exit_code = bsp_cli.main()
        assert exit_code == 0
        # subprocess.run should have been called for git add
        assert mock_run.called
        calls = [list(c[0][0]) for c in mock_run.call_args_list]
        assert any(c[0] == "git" and c[1] == "add" for c in calls)

    def test_add_with_git_commit(self, registry_file):
        with patch("subprocess.run") as mock_run:
            with patch("sys.argv", [
                "bsp", "--registry", str(registry_file),
                "registry", "add", "feature",
                "--slug", "secure-boot",
                "--description", "Secure Boot",
                "--git-commit", "Add secure-boot feature",
            ]):
                exit_code = bsp_cli.main()
        assert exit_code == 0
        calls = [list(c[0][0]) for c in mock_run.call_args_list]
        assert any(c[0] == "git" and c[1] == "commit" for c in calls)


# ---------------------------------------------------------------------------
# Missing registry file
# ---------------------------------------------------------------------------

class TestMissingRegistryFile:
    def test_registry_command_missing_registry_errors(self, tmp_path, capsys):
        missing = tmp_path / "no-such-file.yaml"
        with patch("sys.argv", [
            "bsp", "--registry", str(missing),
            "registry", "validate",
        ]):
            exit_code = bsp_cli.main()
        assert exit_code != 0


# ---------------------------------------------------------------------------
# container CRUD via CLI
# ---------------------------------------------------------------------------

class TestRegistryContainerCrud:
    def test_add_container_then_show(self, registry_file, capsys):
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "add", "container",
            "--name", "debian-12",
            "--image", "debian:12",
        ]):
            exit_code = bsp_cli.main()
        assert exit_code == 0

        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "show", "container", "--name", "debian-12",
        ]):
            exit_code = bsp_cli.main()
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "debian-12" in captured.out

    def test_remove_container(self, registry_file):
        with patch("sys.argv", [
            "bsp", "--registry", str(registry_file),
            "registry", "remove", "container",
            "--name", "ubuntu-22.04",
        ]):
            exit_code = bsp_cli.main()
        assert exit_code == 0
        with open(registry_file) as fh:
            data = yaml.safe_load(fh)
        assert "ubuntu-22.04" not in (data.get("containers") or {})
