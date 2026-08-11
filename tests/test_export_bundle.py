"""
Tests for export bundle helpers (patch copying and setup script generation).
"""

import os
import stat

import pytest

from bsp import KasManager
from bsp.export_bundle import (
    DEFAULT_KAS_CONFIG_NAME,
    SETUP_SCRIPT_NAME,
    copy_patches,
    generate_setup_script,
)


@pytest.fixture
def kas_config_with_patches(tmp_dir):
    """Create a KAS configuration referencing local patch files."""
    patches_dir = tmp_dir / "patches" / "meta-foo"
    patches_dir.mkdir(parents=True)
    (patches_dir / "0001-fix.patch").write_text("--- a\n+++ b\n")
    (patches_dir / "0002-fix.patch").write_text("--- a\n+++ b\n")

    kas_content = """
header:
  version: 14

repos:
  bsp-registry:
  meta-foo:
    url: https://example.com/meta-foo.git
    refspec: main
    patches:
      fix-one:
        repo: bsp-registry
        path: patches/meta-foo/0001-fix.patch
      fix-two:
        repo: bsp-registry
        path: patches/meta-foo/0002-fix.patch
"""
    kas_path = tmp_dir / "with-patches.yaml"
    kas_path.write_text(kas_content)
    return kas_path


class TestCollectPatchFiles:
    def test_collects_patches_from_config(self, tmp_dir, kas_config_with_patches):
        manager = KasManager(
            kas_files=[str(kas_config_with_patches)],
            build_dir=str(tmp_dir / "build"),
            search_paths=[str(tmp_dir)],
        )
        patches = manager.collect_patch_files()
        assert [os.path.basename(p) for p in patches] == [
            "0001-fix.patch",
            "0002-fix.patch",
        ]

    def test_collects_patches_from_includes(self, tmp_dir, kas_config_with_patches):
        main = tmp_dir / "main.yaml"
        main.write_text(
            "header:\n  version: 14\n  includes:\n    - with-patches.yaml\n"
        )
        manager = KasManager(
            kas_files=[str(main)],
            build_dir=str(tmp_dir / "build"),
            search_paths=[str(tmp_dir)],
        )
        assert len(manager.collect_patch_files()) == 2

    def test_patches_as_list_are_supported(self, tmp_dir):
        (tmp_dir / "fix.patch").write_text("patch")
        kas_path = tmp_dir / "list.yaml"
        kas_path.write_text(
            "header:\n  version: 14\n"
            "repos:\n  meta-foo:\n    patches:\n"
            "      - repo: bsp-registry\n        path: fix.patch\n"
        )
        manager = KasManager(
            kas_files=[str(kas_path)],
            build_dir=str(tmp_dir / "build"),
            search_paths=[str(tmp_dir)],
        )
        patches = manager.collect_patch_files()
        assert len(patches) == 1
        assert patches[0].endswith("fix.patch")

    def test_missing_patch_is_skipped(self, tmp_dir):
        kas_path = tmp_dir / "missing.yaml"
        kas_path.write_text(
            "header:\n  version: 14\n"
            "repos:\n  meta-foo:\n    patches:\n"
            "      fix:\n        repo: bsp-registry\n        path: nope.patch\n"
        )
        manager = KasManager(
            kas_files=[str(kas_path)],
            build_dir=str(tmp_dir / "build"),
            search_paths=[str(tmp_dir)],
        )
        assert manager.collect_patch_files() == []

    def test_config_without_patches(self, tmp_dir, kas_config_file):
        manager = KasManager(
            kas_files=[str(kas_config_file)],
            build_dir=str(tmp_dir / "build"),
            search_paths=[str(tmp_dir)],
        )
        assert manager.collect_patch_files() == []


class TestCopyPatches:
    def test_preserves_layout_relative_to_base_dir(self, tmp_dir):
        source = tmp_dir / "patches" / "meta-foo" / "0001-fix.patch"
        source.parent.mkdir(parents=True)
        source.write_text("patch")
        export_dir = tmp_dir / "export"

        copied = copy_patches([str(source)], str(export_dir), base_dir=str(tmp_dir))

        assert [p.as_posix() for p in copied] == ["patches/meta-foo/0001-fix.patch"]
        assert (export_dir / "patches" / "meta-foo" / "0001-fix.patch").read_text() == "patch"

    def test_external_patches_land_in_patches_dir(self, tmp_dir):
        outside = tmp_dir / "outside"
        outside.mkdir()
        source = outside / "extra.patch"
        source.write_text("patch")
        base_dir = tmp_dir / "registry"
        base_dir.mkdir()
        export_dir = tmp_dir / "export"

        copy_patches([str(source)], str(export_dir), base_dir=str(base_dir))

        assert (export_dir / "patches" / "extra.patch").is_file()

    def test_missing_patch_is_ignored(self, tmp_dir):
        export_dir = tmp_dir / "export"
        export_dir.mkdir()
        assert copy_patches([str(tmp_dir / "nope.patch")], str(export_dir)) == []

    def test_duplicates_are_copied_once(self, tmp_dir):
        source = tmp_dir / "fix.patch"
        source.write_text("patch")
        export_dir = tmp_dir / "export"

        copied = copy_patches(
            [str(source), str(source)], str(export_dir), base_dir=str(tmp_dir)
        )
        assert len(copied) == 1


class TestGenerateSetupScript:
    def test_kas_script_is_executable_and_references_config(self, tmp_dir):
        export_dir = tmp_dir / "export"
        script = generate_setup_script(str(export_dir), DEFAULT_KAS_CONFIG_NAME)

        assert script.name == SETUP_SCRIPT_NAME
        content = script.read_text()
        assert DEFAULT_KAS_CONFIG_NAME in content
        assert "kas checkout" in content
        assert os.stat(script).st_mode & stat.S_IXUSR

    def test_repo_manifest_script_uses_repo_tool(self, tmp_dir):
        export_dir = tmp_dir / "export"
        script = generate_setup_script(
            str(export_dir), "manifest.xml", repo_manifest=True, label="my-bsp"
        )
        content = script.read_text()
        assert "repo init" in content
        assert "manifest.xml" in content
        assert "my-bsp" in content
