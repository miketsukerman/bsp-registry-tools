"""
Tests for RegistryWriter in bsp/registry_writer.py.
"""

import copy
import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from bsp.registry_writer import RegistryWriter, ValidationIssue, SUPPORTED_REGISTRY_VERSION


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_V2_REGISTRY = {
    "specification": {"version": "2.0"},
    "containers": {
        "ubuntu-22.04": {"image": "test/ubuntu-22.04:latest", "file": "Dockerfile"},
    },
    "registry": {
        "devices": [
            {
                "slug": "test-device",
                "description": "Test Device",
                "vendor": "test-vendor",
                "soc_vendor": "test-soc",
            }
        ],
        "releases": [
            {
                "slug": "test-release",
                "description": "Test Release",
            }
        ],
        "features": [
            {
                "slug": "test-feature",
                "description": "Test Feature",
            }
        ],
        "bsp": [
            {
                "name": "test-preset",
                "description": "Test Preset",
                "device": "test-device",
                "release": "test-release",
                "features": [],
            }
        ],
        "vendors": [
            {
                "slug": "test-vendor",
                "name": "Test Vendor",
            }
        ],
        "distro": [],
        "frameworks": [],
    },
}


@pytest.fixture
def registry_file(tmp_path):
    """Write a minimal valid v2 registry file and return its path."""
    path = tmp_path / "bsp-registry.yaml"
    with open(path, "w") as fh:
        yaml.dump(copy.deepcopy(MINIMAL_V2_REGISTRY), fh, default_flow_style=False,
                  sort_keys=False)
    return path


@pytest.fixture
def writer(registry_file):
    """Return a RegistryWriter loaded with the minimal v2 registry."""
    w = RegistryWriter()
    w.load(registry_file)
    return w


# ===========================================================================
# I/O tests
# ===========================================================================

class TestLoadAndSave:
    def test_load_and_save_roundtrip(self, registry_file, tmp_path):
        w = RegistryWriter()
        w.load(registry_file)
        out = tmp_path / "out.yaml"
        w.save(out)
        with open(out) as fh:
            loaded = yaml.safe_load(fh)
        assert loaded["specification"]["version"] == "2.0"
        assert loaded["registry"]["devices"][0]["slug"] == "test-device"

    def test_save_default_path(self, registry_file):
        w = RegistryWriter()
        w.load(registry_file)
        # Mutate, then save without passing a path
        w._data["registry"]["devices"][0]["description"] = "Changed"
        w.save()
        with open(registry_file) as fh:
            reloaded = yaml.safe_load(fh)
        assert reloaded["registry"]["devices"][0]["description"] == "Changed"

    def test_atomic_save_removes_tmp(self, registry_file):
        w = RegistryWriter()
        w.load(registry_file)
        w.save()
        tmp = registry_file.with_suffix(registry_file.suffix + ".tmp")
        assert not tmp.exists()

    def test_backup_created_on_save(self, registry_file):
        w = RegistryWriter()
        w.load(registry_file)
        w.save()
        bak = registry_file.with_suffix(registry_file.suffix + ".bak")
        assert bak.exists()

    def test_save_raises_without_path(self):
        w = RegistryWriter()
        w._data = copy.deepcopy(MINIMAL_V2_REGISTRY)
        with pytest.raises(RuntimeError, match="No path set"):
            w.save()


# ===========================================================================
# Undo tests
# ===========================================================================

class TestUndo:
    def test_undo_after_add_device(self, writer, registry_file):
        initial_count = len(writer.show_device())
        writer.add_device(slug="new-dev", description="New", vendor="v", soc_vendor="s")
        assert len(writer.show_device()) == initial_count + 1
        writer.undo()
        assert len(writer.show_device()) == initial_count

    def test_undo_without_history_raises(self, writer):
        with pytest.raises(RuntimeError, match="Nothing to undo"):
            writer.undo()

    def test_undo_saves_to_disk(self, writer, registry_file):
        writer.add_device(slug="tmp-dev", description="Tmp", vendor="v", soc_vendor="s")
        writer.save()
        writer.undo()
        with open(registry_file) as fh:
            reloaded = yaml.safe_load(fh)
        slugs = [d["slug"] for d in reloaded["registry"]["devices"]]
        assert "tmp-dev" not in slugs


# ===========================================================================
# Scaffolding tests
# ===========================================================================

class TestInitRegistry:
    def test_init_registry_creates_valid_file(self, tmp_path):
        out = tmp_path / "new-registry.yaml"
        RegistryWriter.init_registry(out)
        w = RegistryWriter()
        w.load(out)
        issues = w.validate()
        errors = [i for i in issues if i.severity == "error"]
        assert not errors

    def test_init_registry_raises_if_exists(self, tmp_path):
        out = tmp_path / "existing.yaml"
        out.write_text("foo: bar")
        with pytest.raises(FileExistsError):
            RegistryWriter.init_registry(out)

    def test_init_registry_force_overwrites(self, tmp_path):
        out = tmp_path / "existing.yaml"
        out.write_text("foo: bar")
        RegistryWriter.init_registry(out, force=True)
        with open(out) as fh:
            data = yaml.safe_load(fh)
        assert data["specification"]["version"] == SUPPORTED_REGISTRY_VERSION

    def test_init_registry_creates_parent_dirs(self, tmp_path):
        out = tmp_path / "sub" / "dir" / "reg.yaml"
        RegistryWriter.init_registry(out)
        assert out.exists()


# ===========================================================================
# Validation tests
# ===========================================================================

class TestValidation:
    def test_validate_ok_on_good_registry(self, writer):
        issues = writer.validate()
        errors = [i for i in issues if i.severity == "error"]
        assert not errors

    def test_validate_detects_wrong_version(self, writer):
        writer._data["specification"]["version"] = "1.0"
        issues = writer.validate()
        paths = [i.path for i in issues]
        assert "specification.version" in paths
        assert any(i.severity == "error" for i in issues)

    def test_validate_detects_missing_device_field(self, writer):
        writer._data["registry"]["devices"][0].pop("vendor")
        issues = writer.validate()
        assert any("vendor" in i.path for i in issues)
        assert any(i.severity == "error" for i in issues)

    def test_validate_detects_duplicate_device_slugs(self, writer):
        dup = copy.deepcopy(writer._data["registry"]["devices"][0])
        writer._data["registry"]["devices"].append(dup)
        issues = writer.validate()
        assert any("Duplicate" in i.message for i in issues)

    def test_validate_detects_broken_preset_device_ref(self, writer):
        writer._data["registry"]["bsp"][0]["device"] = "nonexistent-device"
        issues = writer.validate()
        assert any("nonexistent-device" in i.message for i in issues)

    def test_validate_detects_broken_preset_release_ref(self, writer):
        writer._data["registry"]["bsp"][0]["release"] = "bad-release"
        issues = writer.validate()
        assert any("bad-release" in i.message for i in issues)

    def test_validate_detects_broken_preset_feature_ref(self, writer):
        writer._data["registry"]["bsp"][0]["features"] = ["missing-feature"]
        issues = writer.validate()
        assert any("missing-feature" in i.message for i in issues)

    def test_validate_detects_missing_release_in_preset(self, writer):
        writer._data["registry"]["bsp"][0].pop("release", None)
        writer._data["registry"]["bsp"][0].pop("releases", None)
        issues = writer.validate()
        assert any("release" in i.path for i in issues)

    def test_validation_issue_str(self):
        issue = ValidationIssue(severity="error", path="registry.devices[0].slug",
                                message="Required field 'slug' is missing.")
        assert "[ERROR]" in str(issue)
        assert "slug" in str(issue)

    def test_validate_device_vendor_warning_when_vendors_defined(self, writer):
        writer._data["registry"]["devices"][0]["vendor"] = "unknown-vendor"
        issues = writer.validate()
        warnings = [i for i in issues if i.severity == "warning"]
        assert any("unknown-vendor" in i.message for i in warnings)


# ===========================================================================
# Diff tests
# ===========================================================================

class TestDiff:
    def test_diff_identical_files(self, registry_file):
        w = RegistryWriter()
        w.load(registry_file)
        result = w.diff(registry_file)
        assert result == ""

    def test_diff_returns_unified_diff(self, registry_file, tmp_path):
        w = RegistryWriter()
        w.load(registry_file)
        # Create a modified version
        other = tmp_path / "other.yaml"
        data = copy.deepcopy(w._data)
        data["registry"]["devices"][0]["description"] = "Modified description"
        with open(other, "w") as fh:
            yaml.dump(data, fh, default_flow_style=False, sort_keys=False)
        result = w.diff(other)
        assert "---" in result
        assert "+++" in result
        assert "Modified description" in result

    def test_diff_raises_on_missing_other_file(self, writer, tmp_path):
        with pytest.raises(FileNotFoundError):
            writer.diff(tmp_path / "nonexistent.yaml")


# ===========================================================================
# CRUD — Device
# ===========================================================================

class TestDeviceCrud:
    def test_add_device_roundtrip(self, writer, registry_file):
        writer.add_device(slug="new-board", description="New Board",
                          vendor="acme", soc_vendor="nxp",
                          soc_family="imx8", architecture="arm64",
                          includes=["kas/new.yaml"])
        writer.save()
        w2 = RegistryWriter()
        w2.load(registry_file)
        dev = w2.show_device("new-board")
        assert dev["description"] == "New Board"
        assert dev["soc_family"] == "imx8"
        assert dev["architecture"] == "arm64"
        assert dev["includes"] == ["kas/new.yaml"]

    def test_add_device_duplicate_raises(self, writer):
        with pytest.raises(ValueError, match="already exists"):
            writer.add_device(slug="test-device", description="Dup",
                              vendor="v", soc_vendor="s")

    def test_edit_device(self, writer):
        writer.edit_device("test-device", description="Updated description")
        assert writer.show_device("test-device")["description"] == "Updated description"

    def test_edit_device_none_values_ignored(self, writer):
        original_vendor = writer.show_device("test-device")["vendor"]
        writer.edit_device("test-device", vendor=None)
        assert writer.show_device("test-device")["vendor"] == original_vendor

    def test_edit_device_not_found_raises(self, writer):
        with pytest.raises(KeyError):
            writer.edit_device("no-such-device", description="x")

    def test_remove_device(self, writer):
        writer.remove_device("test-device")
        devices = writer.show_device()
        assert not any(d["slug"] == "test-device" for d in devices)

    def test_remove_device_warns_about_preset_reference(self, writer, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            writer.remove_device("test-device")
        assert "test-preset" in caplog.text

    def test_remove_device_not_found_raises(self, writer):
        with pytest.raises(KeyError):
            writer.remove_device("no-such-device")

    def test_show_device_all(self, writer):
        result = writer.show_device()
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_show_device_single(self, writer):
        result = writer.show_device("test-device")
        assert isinstance(result, dict)
        assert result["slug"] == "test-device"

    def test_show_device_not_found_raises(self, writer):
        with pytest.raises(KeyError):
            writer.show_device("nope")


# ===========================================================================
# CRUD — Release
# ===========================================================================

class TestReleaseCrud:
    def test_add_release_roundtrip(self, writer, registry_file):
        writer.add_release(slug="nanbield", description="Yocto 4.3",
                           yocto_version="4.3",
                           includes=["kas/nanbield.yaml"])
        writer.save()
        w2 = RegistryWriter()
        w2.load(registry_file)
        rel = w2.show_release("nanbield")
        assert rel["yocto_version"] == "4.3"

    def test_add_release_duplicate_raises(self, writer):
        with pytest.raises(ValueError):
            writer.add_release(slug="test-release", description="Dup")

    def test_edit_release(self, writer):
        writer.edit_release("test-release", description="Updated")
        assert writer.show_release("test-release")["description"] == "Updated"

    def test_remove_release(self, writer):
        writer.remove_release("test-release")
        assert not any(r["slug"] == "test-release" for r in writer.show_release())

    def test_remove_release_warns_about_preset_reference(self, writer, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            writer.remove_release("test-release")
        assert "test-preset" in caplog.text


# ===========================================================================
# CRUD — Feature
# ===========================================================================

class TestFeatureCrud:
    def test_add_feature_roundtrip(self, writer, registry_file):
        writer.add_feature(slug="ota", description="OTA update",
                           includes=["kas/ota.yaml"])
        writer.save()
        w2 = RegistryWriter()
        w2.load(registry_file)
        feat = w2.show_feature("ota")
        assert feat["includes"] == ["kas/ota.yaml"]

    def test_add_feature_duplicate_raises(self, writer):
        with pytest.raises(ValueError):
            writer.add_feature(slug="test-feature", description="Dup")

    def test_edit_feature(self, writer):
        writer.edit_feature("test-feature", description="New description")
        assert writer.show_feature("test-feature")["description"] == "New description"

    def test_remove_feature(self, writer):
        writer.remove_feature("test-feature")
        assert not any(f["slug"] == "test-feature" for f in writer.show_feature())


# ===========================================================================
# CRUD — Preset
# ===========================================================================

class TestPresetCrud:
    def test_add_preset_roundtrip(self, writer, registry_file):
        writer.add_preset(name="my-preset", description="My Preset",
                          device="test-device", release="test-release",
                          features=["test-feature"])
        writer.save()
        w2 = RegistryWriter()
        w2.load(registry_file)
        preset = w2.show_preset("my-preset")
        assert preset["device"] == "test-device"
        assert "test-feature" in preset["features"]

    def test_add_preset_duplicate_raises(self, writer):
        with pytest.raises(ValueError):
            writer.add_preset(name="test-preset", description="Dup",
                              device="test-device", release="test-release")

    def test_edit_preset(self, writer):
        writer.edit_preset("test-preset", description="Edited")
        assert writer.show_preset("test-preset")["description"] == "Edited"

    def test_remove_preset(self, writer):
        writer.remove_preset("test-preset")
        assert not any(p["name"] == "test-preset" for p in writer.show_preset())


# ===========================================================================
# CRUD — Container
# ===========================================================================

class TestContainerCrud:
    def test_add_container_roundtrip(self, writer, registry_file):
        writer.add_container(name="debian-12", image="debian:12",
                             file="Dockerfile.debian")
        writer.save()
        w2 = RegistryWriter()
        w2.load(registry_file)
        containers = w2.show_container()
        assert "debian-12" in containers

    def test_add_container_duplicate_raises(self, writer):
        with pytest.raises(ValueError):
            writer.add_container(name="ubuntu-22.04", image="test/ubuntu:latest")

    def test_edit_container(self, writer):
        writer.edit_container("ubuntu-22.04", image="test/ubuntu-22.04:v2")
        result = writer.show_container("ubuntu-22.04")
        assert result["ubuntu-22.04"]["image"] == "test/ubuntu-22.04:v2"

    def test_remove_container(self, writer):
        writer.remove_container("ubuntu-22.04")
        containers = writer.show_container()
        assert "ubuntu-22.04" not in containers

    def test_remove_container_not_found_raises(self, writer):
        with pytest.raises(KeyError):
            writer.remove_container("nonexistent")


# ===========================================================================
# References
# ===========================================================================

class TestFindReferences:
    def test_find_device_references(self, writer):
        refs = writer.find_references("device", "test-device")
        assert any("test-preset" in r for r in refs)

    def test_find_release_references(self, writer):
        refs = writer.find_references("release", "test-release")
        assert any("test-preset" in r for r in refs)

    def test_find_feature_references(self, writer):
        writer._data["registry"]["bsp"][0]["features"] = ["test-feature"]
        refs = writer.find_references("feature", "test-feature")
        assert any("test-preset" in r for r in refs)

    def test_no_references(self, writer):
        refs = writer.find_references("device", "unknown")
        assert refs == []


# ===========================================================================
# Git helpers
# ===========================================================================

class TestGitHelpers:
    def test_git_stage_no_repo_does_not_raise(self, writer, registry_file):
        import subprocess
        with patch("subprocess.run",
                   side_effect=subprocess.CalledProcessError(128, "git")):
            # Should swallow the error gracefully
            writer.git_stage(registry_file)

    def test_git_commit_no_repo_does_not_raise(self, writer, registry_file):
        import subprocess
        with patch("subprocess.run",
                   side_effect=subprocess.CalledProcessError(128, "git")):
            writer.git_commit("test commit", registry_file)

    def test_git_stage_called_with_correct_args(self, writer, registry_file):
        with patch("subprocess.run") as mock_run:
            writer.git_stage(registry_file)
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "git"
        assert call_args[1] == "add"

    def test_git_stage_no_path_warns(self, caplog):
        import logging
        w = RegistryWriter()
        with caplog.at_level(logging.WARNING):
            w.git_stage()
        assert "no path set" in caplog.text.lower()
