"""
Unit tests for bsp.registry_writer.RegistryWriter.

Tests cover: load/save round-trip, validate, all CRUD helpers, undo, diff,
find_references, and the minimal-registry write helper used by ``bsp registry
init``.
"""

from __future__ import annotations

import pytest
from pathlib import Path

from bsp.registry_writer import RegistryWriter, ValidationIssue, _serialise_to_yaml_string
from bsp.models import (
    BspBuild,
    BspPreset,
    Device,
    Distro,
    Docker,
    Feature,
    Framework,
    Release,
    Vendor,
)


# ---------------------------------------------------------------------------
# Shared YAML fixtures
# ---------------------------------------------------------------------------

MINIMAL_YAML = """\
specification:
  version: "2.0"
registry:
  devices: []
  releases: []
  features: []
  bsp: []
"""

FULL_YAML = """\
specification:
  version: "2.0"
containers:
  test-container:
    image: "test/image:latest"
registry:
  vendors:
    - slug: acme
      name: Acme Corp
  devices:
    - slug: board-a
      description: Board A
      vendor: acme
      soc_vendor: nxp
      includes:
        - kas/board-a.yaml
  releases:
    - slug: release-1
      description: Release 1
      yocto_version: "5.0"
      includes:
        - kas/release-1.yaml
  features:
    - slug: ota
      description: OTA update
      includes:
        - kas/ota.yaml
  bsp:
    - name: board-a-release-1
      description: Board A + Release 1
      device: board-a
      release: release-1
      features:
        - ota
      build:
        container: test-container
        path: build/board-a
  distro:
    - slug: poky
      description: Poky distro
  frameworks:
    - slug: yocto
      description: Yocto Project
      vendor: Linux Foundation
"""


@pytest.fixture
def minimal_registry(tmp_path: Path) -> Path:
    p = tmp_path / "bsp-registry.yaml"
    p.write_text(MINIMAL_YAML, encoding="utf-8")
    return p


@pytest.fixture
def full_registry(tmp_path: Path) -> Path:
    p = tmp_path / "bsp-registry.yaml"
    p.write_text(FULL_YAML, encoding="utf-8")
    return p


@pytest.fixture
def writer_minimal(minimal_registry: Path) -> RegistryWriter:
    w = RegistryWriter()
    w.load(minimal_registry)
    return w


@pytest.fixture
def writer_full(full_registry: Path) -> RegistryWriter:
    w = RegistryWriter()
    w.load(full_registry)
    return w


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


class TestLoadSave:
    def test_load_parses_registry(self, writer_minimal: RegistryWriter) -> None:
        assert writer_minimal.root is not None
        assert writer_minimal.root.specification.version == "2.0"

    def test_path_set_after_load(self, minimal_registry: Path,
                                 writer_minimal: RegistryWriter) -> None:
        assert writer_minimal.path == minimal_registry

    def test_load_unknown_path_raises(self, tmp_path: Path) -> None:
        w = RegistryWriter()
        with pytest.raises(SystemExit):
            w.load(tmp_path / "does-not-exist.yaml")

    def test_save_round_trip(self, writer_minimal: RegistryWriter,
                             minimal_registry: Path) -> None:
        writer_minimal.save()
        after = minimal_registry.read_text(encoding="utf-8")
        # Spec version must survive the round-trip
        assert "2.0" in after

    def test_save_creates_backup(self, writer_full: RegistryWriter,
                                 full_registry: Path) -> None:
        writer_full.save()
        backup = full_registry.with_suffix(".bak")
        assert backup.exists(), "Backup file should be created on first save"

    def test_save_backup_only_once(self, writer_full: RegistryWriter,
                                   full_registry: Path) -> None:
        writer_full.save()
        backup = full_registry.with_suffix(".bak")
        mtime1 = backup.stat().st_mtime
        writer_full.save()
        mtime2 = backup.stat().st_mtime
        assert mtime1 == mtime2, "Backup should not be overwritten on subsequent saves"

    def test_save_to_different_path(self, writer_minimal: RegistryWriter,
                                    tmp_path: Path) -> None:
        dest = tmp_path / "copy.yaml"
        writer_minimal.save(dest)
        assert dest.exists()
        assert "2.0" in dest.read_text(encoding="utf-8")

    def test_save_without_load_raises(self) -> None:
        w = RegistryWriter()
        with pytest.raises(RuntimeError, match="No registry loaded"):
            w.save(Path("/tmp/should-not-create.yaml"))

    def test_undo_stack_cleared_on_load(self, writer_full: RegistryWriter,
                                        full_registry: Path) -> None:
        writer_full.add_device(Device(
            slug="temp-dev", description="", vendor="acme", soc_vendor="nxp"
        ))
        assert len(writer_full._undo_stack) > 0
        # Reload clears the stack
        writer_full.load(full_registry)
        assert writer_full._undo_stack == []


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_valid_registry_has_no_issues(self, writer_minimal: RegistryWriter) -> None:
        issues = writer_minimal.validate()
        errors = [i for i in issues if i.level == "error"]
        assert errors == []

    def test_full_registry_valid(self, writer_full: RegistryWriter) -> None:
        issues = writer_full.validate()
        errors = [i for i in issues if i.level == "error"]
        assert errors == [], f"Unexpected errors: {errors}"

    def test_duplicate_device_slug_is_error(self, writer_minimal: RegistryWriter) -> None:
        writer_minimal.add_device(Device(
            slug="dup", description="", vendor="v", soc_vendor="s"
        ))
        # Bypass duplicate guard by directly appending to the list
        writer_minimal.root.registry.devices.append(
            Device(slug="dup", description="", vendor="v", soc_vendor="s")
        )
        issues = writer_minimal.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("dup" in i.message for i in errors)

    def test_preset_references_missing_device(self, writer_minimal: RegistryWriter) -> None:
        writer_minimal.root.registry.bsp = [
            BspPreset(name="p1", description="", device="nonexistent", release=None)
        ]
        issues = writer_minimal.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("nonexistent" in i.message for i in errors)

    def test_preset_references_missing_release(self, writer_minimal: RegistryWriter) -> None:
        writer_minimal.add_device(
            Device(slug="d1", description="", vendor="v", soc_vendor="s")
        )
        writer_minimal.root.registry.bsp = [
            BspPreset(name="p1", description="", device="d1", release="missing-rel")
        ]
        issues = writer_minimal.validate()
        errors = [i for i in issues if i.level == "error"]
        assert any("missing-rel" in i.message for i in errors)

    def test_validate_returns_issues_without_loaded_registry(self) -> None:
        w = RegistryWriter()
        issues = w.validate()
        assert any(i.level == "error" for i in issues)

    def test_issue_str(self) -> None:
        issue = ValidationIssue("error", "Something wrong")
        assert "[ERROR]" in str(issue)
        assert "Something wrong" in str(issue)

    def test_warning_for_undefined_container(self, writer_full: RegistryWriter) -> None:
        writer_full.add_preset(BspPreset(
            name="p-bad-container",
            description="",
            device="board-a",
            release="release-1",
            build=BspBuild(container="undefined-container"),
        ))
        issues = writer_full.validate()
        warnings = [i for i in issues if i.level == "warning"]
        assert any("undefined-container" in i.message for i in warnings)


# ---------------------------------------------------------------------------
# Device CRUD
# ---------------------------------------------------------------------------


class TestDeviceCrud:
    def _new_device(self, slug: str = "new-board") -> Device:
        return Device(
            slug=slug,
            description="New board",
            vendor="acme",
            soc_vendor="nxp",
        )

    def test_add_device(self, writer_minimal: RegistryWriter) -> None:
        writer_minimal.add_device(self._new_device())
        slugs = [d.slug for d in writer_minimal.root.registry.devices]
        assert "new-board" in slugs

    def test_add_device_duplicate_raises(self, writer_full: RegistryWriter) -> None:
        with pytest.raises(ValueError, match="already exists"):
            writer_full.add_device(Device(
                slug="board-a", description="", vendor="v", soc_vendor="s"
            ))

    def test_add_device_pushes_undo(self, writer_minimal: RegistryWriter) -> None:
        writer_minimal.add_device(self._new_device())
        assert len(writer_minimal._undo_stack) == 1

    def test_update_device(self, writer_full: RegistryWriter) -> None:
        writer_full.update_device("board-a", description="Updated description")
        dev = next(d for d in writer_full.root.registry.devices if d.slug == "board-a")
        assert dev.description == "Updated description"

    def test_update_device_not_found_raises(self, writer_full: RegistryWriter) -> None:
        with pytest.raises(KeyError):
            writer_full.update_device("nonexistent", description="x")

    def test_remove_device(self, writer_full: RegistryWriter) -> None:
        writer_full.remove_device("board-a")
        slugs = [d.slug for d in writer_full.root.registry.devices]
        assert "board-a" not in slugs

    def test_remove_device_not_found_raises(self, writer_full: RegistryWriter) -> None:
        with pytest.raises(KeyError):
            writer_full.remove_device("ghost")


# ---------------------------------------------------------------------------
# Release CRUD
# ---------------------------------------------------------------------------


class TestReleaseCrud:
    def test_add_release(self, writer_minimal: RegistryWriter) -> None:
        writer_minimal.add_release(Release(slug="new-rel", description="New Release"))
        slugs = [r.slug for r in writer_minimal.root.registry.releases]
        assert "new-rel" in slugs

    def test_add_release_duplicate_raises(self, writer_full: RegistryWriter) -> None:
        with pytest.raises(ValueError, match="already exists"):
            writer_full.add_release(Release(slug="release-1", description="dup"))

    def test_update_release(self, writer_full: RegistryWriter) -> None:
        writer_full.update_release("release-1", description="Updated Release")
        rel = next(r for r in writer_full.root.registry.releases if r.slug == "release-1")
        assert rel.description == "Updated Release"

    def test_remove_release(self, writer_full: RegistryWriter) -> None:
        writer_full.remove_release("release-1")
        slugs = [r.slug for r in writer_full.root.registry.releases]
        assert "release-1" not in slugs

    def test_remove_release_not_found_raises(self, writer_full: RegistryWriter) -> None:
        with pytest.raises(KeyError):
            writer_full.remove_release("never-existed")


# ---------------------------------------------------------------------------
# Feature CRUD
# ---------------------------------------------------------------------------


class TestFeatureCrud:
    def test_add_feature(self, writer_minimal: RegistryWriter) -> None:
        writer_minimal.add_feature(Feature(slug="secure-boot", description="Secure Boot"))
        slugs = [f.slug for f in writer_minimal.root.registry.features]
        assert "secure-boot" in slugs

    def test_add_feature_duplicate_raises(self, writer_full: RegistryWriter) -> None:
        with pytest.raises(ValueError, match="already exists"):
            writer_full.add_feature(Feature(slug="ota", description="dup"))

    def test_update_feature(self, writer_full: RegistryWriter) -> None:
        writer_full.update_feature("ota", description="Updated OTA")
        feat = next(f for f in writer_full.root.registry.features if f.slug == "ota")
        assert feat.description == "Updated OTA"

    def test_remove_feature(self, writer_full: RegistryWriter) -> None:
        writer_full.remove_feature("ota")
        slugs = [f.slug for f in writer_full.root.registry.features]
        assert "ota" not in slugs


# ---------------------------------------------------------------------------
# Preset CRUD
# ---------------------------------------------------------------------------


class TestPresetCrud:
    def test_add_preset(self, writer_minimal: RegistryWriter) -> None:
        writer_minimal.add_preset(BspPreset(
            name="my-preset", description="My Preset",
            device="dev-1", release="rel-1",
        ))
        names = [p.name for p in writer_minimal.root.registry.bsp]
        assert "my-preset" in names

    def test_add_preset_duplicate_raises(self, writer_full: RegistryWriter) -> None:
        with pytest.raises(ValueError, match="already exists"):
            writer_full.add_preset(BspPreset(
                name="board-a-release-1", description="dup", device="board-a"
            ))

    def test_update_preset(self, writer_full: RegistryWriter) -> None:
        writer_full.update_preset("board-a-release-1", description="Updated Preset")
        preset = next(
            p for p in writer_full.root.registry.bsp if p.name == "board-a-release-1"
        )
        assert preset.description == "Updated Preset"

    def test_remove_preset(self, writer_full: RegistryWriter) -> None:
        writer_full.remove_preset("board-a-release-1")
        names = [p.name for p in (writer_full.root.registry.bsp or [])]
        assert "board-a-release-1" not in names

    def test_remove_preset_not_found_raises(self, writer_full: RegistryWriter) -> None:
        with pytest.raises(KeyError):
            writer_full.remove_preset("no-such-preset")


# ---------------------------------------------------------------------------
# Vendor CRUD
# ---------------------------------------------------------------------------


class TestVendorCrud:
    def test_add_vendor(self, writer_minimal: RegistryWriter) -> None:
        writer_minimal.add_vendor(Vendor(slug="new-vendor", name="New Vendor"))
        slugs = [v.slug for v in writer_minimal.root.registry.vendors]
        assert "new-vendor" in slugs

    def test_add_vendor_duplicate_raises(self, writer_full: RegistryWriter) -> None:
        with pytest.raises(ValueError, match="already exists"):
            writer_full.add_vendor(Vendor(slug="acme", name="dup"))

    def test_update_vendor(self, writer_full: RegistryWriter) -> None:
        writer_full.update_vendor("acme", name="ACME Industries")
        vendor = next(v for v in writer_full.root.registry.vendors if v.slug == "acme")
        assert vendor.name == "ACME Industries"

    def test_remove_vendor(self, writer_full: RegistryWriter) -> None:
        writer_full.remove_vendor("acme")
        slugs = [v.slug for v in writer_full.root.registry.vendors]
        assert "acme" not in slugs


# ---------------------------------------------------------------------------
# Distro CRUD
# ---------------------------------------------------------------------------


class TestDistroCrud:
    def test_add_distro(self, writer_minimal: RegistryWriter) -> None:
        writer_minimal.add_distro(Distro(slug="isar", description="Isar"))
        slugs = [d.slug for d in writer_minimal.root.registry.distro]
        assert "isar" in slugs

    def test_add_distro_duplicate_raises(self, writer_full: RegistryWriter) -> None:
        with pytest.raises(ValueError, match="already exists"):
            writer_full.add_distro(Distro(slug="poky", description="dup"))

    def test_update_distro(self, writer_full: RegistryWriter) -> None:
        writer_full.update_distro("poky", description="Updated Poky")
        dist = next(d for d in writer_full.root.registry.distro if d.slug == "poky")
        assert dist.description == "Updated Poky"

    def test_remove_distro(self, writer_full: RegistryWriter) -> None:
        writer_full.remove_distro("poky")
        slugs = [d.slug for d in writer_full.root.registry.distro]
        assert "poky" not in slugs


# ---------------------------------------------------------------------------
# Container CRUD
# ---------------------------------------------------------------------------


class TestContainerCrud:
    def test_add_container(self, writer_full: RegistryWriter) -> None:
        writer_full.add_container("new-container", Docker(image="img:latest", file=None))
        assert "new-container" in writer_full.root.containers

    def test_add_container_duplicate_raises(self, writer_full: RegistryWriter) -> None:
        with pytest.raises(ValueError, match="already exists"):
            writer_full.add_container("test-container", Docker(image="x", file=None))

    def test_update_container(self, writer_full: RegistryWriter) -> None:
        writer_full.update_container("test-container", image="updated-image:v2")
        assert writer_full.root.containers["test-container"].image == "updated-image:v2"

    def test_remove_container(self, writer_full: RegistryWriter) -> None:
        writer_full.remove_container("test-container")
        assert "test-container" not in writer_full.root.containers

    def test_remove_container_not_found_raises(self, writer_full: RegistryWriter) -> None:
        with pytest.raises(KeyError):
            writer_full.remove_container("ghost-container")


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


class TestUndo:
    def test_undo_reverts_add_device(self, writer_minimal: RegistryWriter) -> None:
        writer_minimal.add_device(
            Device(slug="undo-me", description="", vendor="v", soc_vendor="s")
        )
        assert any(d.slug == "undo-me" for d in writer_minimal.root.registry.devices)
        result = writer_minimal.undo()
        assert result is True
        assert not any(d.slug == "undo-me" for d in writer_minimal.root.registry.devices)

    def test_undo_empty_stack_returns_false(self, writer_minimal: RegistryWriter) -> None:
        result = writer_minimal.undo()
        assert result is False

    def test_multiple_undo_steps(self, writer_minimal: RegistryWriter) -> None:
        writer_minimal.add_device(
            Device(slug="dev-1", description="", vendor="v", soc_vendor="s")
        )
        writer_minimal.add_device(
            Device(slug="dev-2", description="", vendor="v", soc_vendor="s")
        )
        writer_minimal.undo()
        slugs = [d.slug for d in writer_minimal.root.registry.devices]
        assert "dev-1" in slugs
        assert "dev-2" not in slugs
        writer_minimal.undo()
        slugs = [d.slug for d in writer_minimal.root.registry.devices]
        assert "dev-1" not in slugs

    def test_undo_reverts_update(self, writer_full: RegistryWriter) -> None:
        original_desc = next(
            d for d in writer_full.root.registry.devices if d.slug == "board-a"
        ).description
        writer_full.update_device("board-a", description="Changed")
        writer_full.undo()
        restored_desc = next(
            d for d in writer_full.root.registry.devices if d.slug == "board-a"
        ).description
        assert restored_desc == original_desc

    def test_undo_reverts_remove(self, writer_full: RegistryWriter) -> None:
        writer_full.remove_device("board-a")
        assert not any(d.slug == "board-a" for d in writer_full.root.registry.devices)
        writer_full.undo()
        assert any(d.slug == "board-a" for d in writer_full.root.registry.devices)


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


class TestDiff:
    def test_no_diff_when_unchanged(self, writer_full: RegistryWriter) -> None:
        # Save first so on-disk matches in-memory
        writer_full.save()
        diff = writer_full.diff()
        assert diff == ""

    def test_diff_detects_change(self, writer_full: RegistryWriter) -> None:
        writer_full.save()  # sync on-disk
        writer_full.update_device("board-a", description="CHANGED")
        diff = writer_full.diff()
        assert diff != ""
        assert "CHANGED" in diff

    def test_diff_on_unloaded_writer_is_empty(self) -> None:
        w = RegistryWriter()
        assert w.diff() == ""


# ---------------------------------------------------------------------------
# find_references
# ---------------------------------------------------------------------------


class TestFindReferences:
    def test_device_referenced_by_preset(self, writer_full: RegistryWriter) -> None:
        refs = writer_full.find_references("device", "board-a")
        assert any("board-a-release-1" in r for r in refs)

    def test_release_referenced_by_preset(self, writer_full: RegistryWriter) -> None:
        refs = writer_full.find_references("release", "release-1")
        assert any("board-a-release-1" in r for r in refs)

    def test_feature_referenced_by_preset(self, writer_full: RegistryWriter) -> None:
        refs = writer_full.find_references("feature", "ota")
        assert any("board-a-release-1" in r for r in refs)

    def test_vendor_referenced_by_device(self, writer_full: RegistryWriter) -> None:
        refs = writer_full.find_references("vendor", "acme")
        assert any("board-a" in r for r in refs)

    def test_container_referenced_by_preset(self, writer_full: RegistryWriter) -> None:
        refs = writer_full.find_references("container", "test-container")
        assert any("board-a-release-1" in r for r in refs)

    def test_no_references_for_unknown(self, writer_full: RegistryWriter) -> None:
        refs = writer_full.find_references("device", "ghost-device")
        assert refs == []

    def test_no_references_when_not_loaded(self) -> None:
        w = RegistryWriter()
        refs = w.find_references("device", "any")
        assert refs == []


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


class TestSerialisationHelpers:
    def test_serialise_to_yaml_contains_version(self) -> None:
        data = {"specification": {"version": "2.0"}, "registry": {}}
        yaml_str = _serialise_to_yaml_string(data)
        assert "2.0" in yaml_str

    def test_round_trip_preserves_devices(self, writer_full: RegistryWriter,
                                          full_registry: Path) -> None:
        writer_full.save()
        w2 = RegistryWriter()
        w2.load(full_registry)
        slugs = [d.slug for d in w2.root.registry.devices]
        assert "board-a" in slugs

    def test_round_trip_preserves_presets(self, writer_full: RegistryWriter,
                                          full_registry: Path) -> None:
        writer_full.save()
        w2 = RegistryWriter()
        w2.load(full_registry)
        names = [p.name for p in (w2.root.registry.bsp or [])]
        assert "board-a-release-1" in names

    def test_round_trip_preserves_containers(self, writer_full: RegistryWriter,
                                              full_registry: Path) -> None:
        writer_full.save()
        w2 = RegistryWriter()
        w2.load(full_registry)
        assert "test-container" in (w2.root.containers or {})

    def test_add_and_save_persists_device(self, writer_minimal: RegistryWriter,
                                           minimal_registry: Path) -> None:
        writer_minimal.add_device(
            Device(slug="persisted", description="Persisted", vendor="v", soc_vendor="s")
        )
        writer_minimal.save()
        w2 = RegistryWriter()
        w2.load(minimal_registry)
        slugs = [d.slug for d in w2.root.registry.devices]
        assert "persisted" in slugs
