"""
Tests for the ``bsp import`` manifest importer.

Covers:
* ManifestParser  — XML parsing, recursive <include> resolution, SHA pins,
                    upstream attribute, cycle detection
* _build_kas_dict — KAS YAML construction (branch / commit / layers)
* RegistryMerger  — load/save, upsert_vendor/device/release, vendor overrides
                    (flat and nested), bsp presets
* load_hints      — hints YAML loading
* _detect_codename — codename auto-detection
* ManifestImporter — create mode, merge mode, dry-run, file-exists guard,
                     hints-driven device injection, soc-vendor nesting
* _looks_like_url  — URL vs local-path discrimination
* ManifestFetcher  — clone-dir naming, fresh clone, cached-clone update,
                     --no-update bypass, missing manifest file error,
                     git failure propagation, cache clearing
* CLI             — ``bsp import`` subcommand (via sys.argv patching),
                    remote-URL routing through ManifestFetcher, --branch /
                    --manifest-file / --no-update flag forwarding
"""

import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import yaml

import bsp
from bsp.manifest_importer import (
    ImportHints,
    ManifestFetcher,
    ManifestImporter,
    ManifestParser,
    RegistryMerger,
    RepoManifest,
    _build_kas_dict,
    _detect_codename,
    _dump_yaml,
    _is_sha,
    _looks_like_url,
    _project_to_kas_repo,
    _repo_key,
    load_hints,
    ManifestProject,
)


# =============================================================================
# Helpers
# =============================================================================

def _write(path: Path, content: str) -> Path:
    """Write *content* to *path*, creating parent dirs, return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def _manifest(tmp_path: Path, xml: str, name: str = "default.xml") -> Path:
    """Write an XML manifest to *tmp_path/<name>* and return its path."""
    return _write(tmp_path / name, xml)


# =============================================================================
# _is_sha
# =============================================================================

class TestIsSha:
    def test_sha1_lower(self):
        assert _is_sha("a" * 40)

    def test_sha1_upper(self):
        assert _is_sha("A" * 40)

    def test_sha256(self):
        assert _is_sha("b" * 64)

    def test_branch_name_is_not_sha(self):
        assert not _is_sha("scarthgap")

    def test_short_hash_is_not_sha(self):
        assert not _is_sha("deadbeef")

    def test_none_is_not_sha(self):
        assert not _is_sha(None)


# =============================================================================
# ManifestParser
# =============================================================================

class TestManifestParserSimple:
    def test_single_project_with_remote(self, tmp_path):
        m = _manifest(tmp_path, """
            <manifest>
              <remote name="github" fetch="https://github.com/org"/>
              <default revision="scarthgap" remote="github"/>
              <project name="meta-layer" path="sources/meta-layer"/>
            </manifest>
        """)
        manifest = ManifestParser().parse(m)
        assert len(manifest.projects) == 1
        p = manifest.projects[0]
        assert p.name == "meta-layer"
        assert p.path == "sources/meta-layer"
        assert p.url == "https://github.com/org/meta-layer"
        assert p.revision == "scarthgap"
        assert manifest.default_revision == "scarthgap"

    def test_project_overrides_revision(self, tmp_path):
        m = _manifest(tmp_path, """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <default revision="main" remote="r"/>
              <project name="poky" revision="scarthgap"/>
            </manifest>
        """)
        manifest = ManifestParser().parse(m)
        assert manifest.projects[0].revision == "scarthgap"

    def test_project_sha_revision(self, tmp_path):
        sha = "a" * 40
        m = _manifest(tmp_path, f"""
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <project name="repo" revision="{sha}" upstream="main"/>
            </manifest>
        """)
        manifest = ManifestParser().parse(m)
        p = manifest.projects[0]
        assert p.revision == sha
        assert p.upstream == "main"
        assert _is_sha(p.revision)

    def test_project_without_path_defaults_to_name(self, tmp_path):
        m = _manifest(tmp_path, """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <project name="my/layer"/>
            </manifest>
        """)
        manifest = ManifestParser().parse(m)
        assert manifest.projects[0].path == "my/layer"

    def test_multiple_remotes(self, tmp_path):
        m = _manifest(tmp_path, """
            <manifest>
              <remote name="yocto" fetch="https://git.yoctoproject.org"/>
              <remote name="oe"    fetch="https://github.com/openembedded"/>
              <default remote="yocto" revision="scarthgap"/>
              <project name="poky"/>
              <project name="meta-openembedded" remote="oe"/>
            </manifest>
        """)
        manifest = ManifestParser().parse(m)
        assert manifest.projects[0].url == "https://git.yoctoproject.org/poky"
        assert manifest.projects[1].url == "https://github.com/openembedded/meta-openembedded"

    def test_direct_url_attribute(self, tmp_path):
        m = _manifest(tmp_path, """
            <manifest>
              <project name="special" url="https://custom.host/special.git" path="src/special"/>
            </manifest>
        """)
        manifest = ManifestParser().parse(m)
        assert manifest.projects[0].url == "https://custom.host/special.git"

    def test_copyfile_and_linkfile(self, tmp_path):
        m = _manifest(tmp_path, """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <project name="layer">
                <copyfile src="setup-env" dest="setup-env"/>
                <linkfile src="README"    dest="README.md"/>
              </project>
            </manifest>
        """)
        manifest = ManifestParser().parse(m)
        p = manifest.projects[0]
        assert ("setup-env", "setup-env") in p.copyfiles
        assert ("README", "README.md") in p.linkfiles

    def test_groups_parsed(self, tmp_path):
        m = _manifest(tmp_path, """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <project name="layer" groups="bsp,optional"/>
            </manifest>
        """)
        manifest = ManifestParser().parse(m)
        assert manifest.projects[0].groups == ["bsp", "optional"]

    def test_remote_with_trailing_slash_stripped(self, tmp_path):
        m = _manifest(tmp_path, """
            <manifest>
              <remote name="r" fetch="https://example.com/"/>
              <default remote="r"/>
              <project name="meta-foo"/>
            </manifest>
        """)
        manifest = ManifestParser().parse(m)
        assert manifest.projects[0].url == "https://example.com/meta-foo"

    def test_project_missing_name_skipped(self, tmp_path):
        m = _manifest(tmp_path, """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <project path="some/path"/>
              <project name="good"/>
            </manifest>
        """)
        manifest = ManifestParser().parse(m)
        assert len(manifest.projects) == 1
        assert manifest.projects[0].name == "good"


class TestManifestParserIncludes:
    def test_include_merges_projects(self, tmp_path):
        _write(tmp_path / "base.xml", """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <project name="base-layer"/>
            </manifest>
        """)
        m = _manifest(tmp_path, """
            <manifest>
              <include name="base.xml"/>
              <remote name="r" fetch="https://example.com"/>
              <project name="extra-layer"/>
            </manifest>
        """)
        manifest = ManifestParser().parse(m)
        names = [p.name for p in manifest.projects]
        assert "base-layer" in names
        assert "extra-layer" in names

    def test_include_adopts_default_revision(self, tmp_path):
        _write(tmp_path / "base.xml", """
            <manifest>
              <default revision="kirkstone"/>
            </manifest>
        """)
        m = _manifest(tmp_path, """
            <manifest>
              <include name="base.xml"/>
              <remote name="r" fetch="https://example.com"/>
              <project name="meta-x"/>
            </manifest>
        """)
        manifest = ManifestParser().parse(m)
        assert manifest.projects[0].revision == "kirkstone"

    def test_local_default_overrides_included_default(self, tmp_path):
        _write(tmp_path / "base.xml", """
            <manifest>
              <default revision="kirkstone"/>
            </manifest>
        """)
        m = _manifest(tmp_path, """
            <manifest>
              <include name="base.xml"/>
              <default revision="scarthgap"/>
              <remote name="r" fetch="https://example.com"/>
              <project name="meta-x"/>
            </manifest>
        """)
        manifest = ManifestParser().parse(m)
        assert manifest.projects[0].revision == "scarthgap"

    def test_nested_includes(self, tmp_path):
        _write(tmp_path / "a.xml", """
            <manifest>
              <include name="b.xml"/>
              <remote name="r" fetch="https://example.com"/>
              <project name="layer-a"/>
            </manifest>
        """)
        _write(tmp_path / "b.xml", """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <project name="layer-b"/>
            </manifest>
        """)
        m = _manifest(tmp_path, """
            <manifest>
              <include name="a.xml"/>
            </manifest>
        """)
        manifest = ManifestParser().parse(m)
        names = [p.name for p in manifest.projects]
        assert "layer-a" in names
        assert "layer-b" in names

    def test_cycle_detection(self, tmp_path):
        _write(tmp_path / "loop.xml", """
            <manifest>
              <include name="default.xml"/>
            </manifest>
        """)
        m = _manifest(tmp_path, """
            <manifest>
              <include name="loop.xml"/>
            </manifest>
        """)
        # Should not raise; cycle is silently skipped.
        manifest = ManifestParser().parse(m)
        assert isinstance(manifest, RepoManifest)

    def test_parse_invalid_xml_raises_value_error(self, tmp_path):
        bad = _write(tmp_path / "bad.xml", "<manifest><unclosed>")
        with pytest.raises(ValueError, match="Cannot parse manifest"):
            ManifestParser().parse(bad)

    def test_wrong_root_element_raises_value_error(self, tmp_path):
        bad = _write(tmp_path / "bad.xml", "<repo><project name='x'/></repo>")
        with pytest.raises(ValueError, match="Root element must be"):
            ManifestParser().parse(bad)


# =============================================================================
# _detect_codename
# =============================================================================

class TestDetectCodename:
    def test_from_default_revision(self):
        manifest = RepoManifest(default_revision="scarthgap")
        assert _detect_codename(manifest) == "scarthgap"

    def test_from_project_branch_revision(self):
        manifest = RepoManifest(projects=[
            ManifestProject(
                name="poky", path="layers/poky",
                url="https://example.com/poky",
                revision="lf-scarthgap-6.6.52",
            )
        ])
        assert _detect_codename(manifest) == "scarthgap"

    def test_from_project_upstream(self):
        manifest = RepoManifest(projects=[
            ManifestProject(
                name="poky", path="layers/poky",
                url="https://example.com/poky",
                revision="a" * 40,
                upstream="kirkstone",
            )
        ])
        assert _detect_codename(manifest) == "kirkstone"

    def test_sha_only_returns_none(self):
        manifest = RepoManifest(projects=[
            ManifestProject(
                name="p", path="p", url="u",
                revision="a" * 40,
            )
        ])
        assert _detect_codename(manifest) is None

    def test_no_revisions_returns_none(self):
        manifest = RepoManifest()
        assert _detect_codename(manifest) is None


# =============================================================================
# KAS generation
# =============================================================================

class TestProjectToKasRepo:
    def test_branch_revision(self):
        p = ManifestProject(
            name="poky", path="layers/poky",
            url="https://git.yoctoproject.org/poky",
            revision="scarthgap",
        )
        repo = _project_to_kas_repo(p, default_branch=None)
        assert repo["url"] == "https://git.yoctoproject.org/poky"
        assert repo["path"] == "layers/poky"
        assert repo["branch"] == "scarthgap"
        assert "commit" not in repo

    def test_sha_revision_emits_commit(self):
        sha = "b" * 40
        p = ManifestProject(
            name="meta-imx", path="layers/meta-imx",
            url="https://github.com/nxp-imx/meta-imx",
            revision=sha,
        )
        repo = _project_to_kas_repo(p, default_branch="scarthgap")
        assert repo["commit"] == sha
        assert "branch" not in repo

    def test_sha_plus_upstream_emits_both(self):
        sha = "c" * 40
        p = ManifestProject(
            name="meta-imx", path="layers/meta-imx",
            url="https://github.com/nxp-imx/meta-imx",
            revision=sha,
            upstream="scarthgap-6.6.52",
        )
        repo = _project_to_kas_repo(p, default_branch=None)
        assert repo["commit"] == sha
        assert repo["branch"] == "scarthgap-6.6.52"

    def test_no_revision_uses_default_branch(self):
        p = ManifestProject(
            name="meta-x", path="layers/meta-x",
            url="https://example.com/meta-x",
            revision=None,
        )
        repo = _project_to_kas_repo(p, default_branch="scarthgap")
        assert repo["branch"] == "scarthgap"
        assert "commit" not in repo

    def test_known_layers_poky(self):
        p = ManifestProject(
            name="yocto/poky", path="layers/poky",
            url="https://git.yoctoproject.org/poky",
        )
        repo = _project_to_kas_repo(p, default_branch=None)
        # _repo_key → "poky" → known layers
        assert "meta" in repo["layers"]
        assert "meta-poky" in repo["layers"]

    def test_known_layers_bitbake_disabled(self):
        p = ManifestProject(
            name="bitbake", path="layers/bitbake",
            url="https://github.com/openembedded/bitbake",
        )
        repo = _project_to_kas_repo(p, default_branch=None)
        assert repo["layers"]["bitbake"] == "disabled"

    def test_unknown_layer_defaults_to_basename(self):
        p = ManifestProject(
            name="meta-custom", path="sources/meta-custom",
            url="https://example.com/meta-custom",
        )
        repo = _project_to_kas_repo(p, default_branch=None)
        assert "meta-custom" in repo["layers"]

    def test_none_layer_value_serialises_as_bare_key(self):
        p = ManifestProject(
            name="meta-custom", path="layers/meta-custom",
            url="https://example.com",
        )
        repo = _project_to_kas_repo(p, default_branch=None)
        dumped = _dump_yaml({"layers": repo["layers"]})
        # None should become a bare key (no 'null')
        assert "null" not in dumped
        assert "meta-custom:" in dumped


class TestBuildKasDict:
    def test_basic(self):
        projects = [
            ManifestProject(
                name="poky", path="layers/poky",
                url="https://git.yoctoproject.org/poky",
                revision="scarthgap",
            ),
        ]
        kas = _build_kas_dict(projects, default_branch="scarthgap")
        assert kas["header"]["version"] == 14
        assert "poky" in kas["repos"]

    def test_skip_names_excludes_projects(self):
        projects = [
            ManifestProject(name="keep", path="layers/keep", url="u1"),
            ManifestProject(name="skip-me", path="layers/skip-me", url="u2"),
        ]
        kas = _build_kas_dict(projects, default_branch=None, skip_names={"skip-me"})
        assert "keep" in kas["repos"]
        assert "skip-me" not in kas["repos"]

    def test_duplicate_path_basenames_first_wins(self):
        projects = [
            ManifestProject(name="org/layer", path="a/layer", url="u1", revision="main"),
            ManifestProject(name="other/layer", path="b/layer", url="u2", revision="dev"),
        ]
        kas = _build_kas_dict(projects, default_branch=None)
        # Both have basename "layer", only first should appear
        assert kas["repos"]["layer"]["url"] == "u1"


# =============================================================================
# load_hints
# =============================================================================

class TestLoadHints:
    def test_no_file_returns_empty(self):
        hints = load_hints(Path("/nonexistent/path.yml"))
        assert hints.projects == {}
        assert hints.devices == []

    def test_none_path_returns_empty(self):
        hints = load_hints(None)
        assert hints.projects == {}

    def test_loads_projects_and_devices(self, tmp_path):
        hints_file = _write(tmp_path / "hints.yml", """
            projects:
              meta-secret:
                role: skip
              meta-board:
                role: device
                slug: myboard
                vendor: myvendor
            devices:
              - slug: myboard
                description: My Board
                vendor: myvendor
                soc_vendor: nxp
        """)
        hints = load_hints(hints_file)
        assert hints.projects["meta-secret"]["role"] == "skip"
        assert hints.projects["meta-board"]["slug"] == "myboard"
        assert hints.devices[0]["slug"] == "myboard"


# =============================================================================
# RegistryMerger
# =============================================================================

class TestRegistryMerger:
    @pytest.fixture
    def merger(self):
        return RegistryMerger()

    def test_load_nonexistent_returns_skeleton(self, merger, tmp_path):
        data = merger.load(tmp_path / "nonexistent.yml")
        assert data["specification"]["version"] == "2.0"
        assert "registry" in data

    def test_load_existing_file(self, merger, tmp_path):
        reg = tmp_path / "bsp-registry.yml"
        reg.write_text(yaml.safe_dump({"specification": {"version": "2.0"}, "registry": {"vendors": []}}))
        data = merger.load(reg)
        assert data["registry"]["vendors"] == []

    def test_upsert_vendor_adds(self, merger):
        data = {"registry": {}}
        assert merger.upsert_vendor(data, "nxp") is True
        assert data["registry"]["vendors"][0]["slug"] == "nxp"

    def test_upsert_vendor_skips_duplicate(self, merger):
        data = {"registry": {"vendors": [{"slug": "nxp", "name": "NXP"}]}}
        assert merger.upsert_vendor(data, "nxp") is False
        assert len(data["registry"]["vendors"]) == 1

    def test_upsert_device_adds(self, merger):
        data = {"registry": {}}
        device = {"slug": "myboard", "vendor": "myvendor"}
        assert merger.upsert_device(data, device) is True
        assert data["registry"]["devices"][0]["slug"] == "myboard"

    def test_upsert_device_skips_duplicate(self, merger):
        existing = {"slug": "myboard", "vendor": "myvendor"}
        data = {"registry": {"devices": [existing]}}
        assert merger.upsert_device(data, {"slug": "myboard"}) is False

    def test_upsert_release_creates_new(self, merger):
        data = {"registry": {}}
        rel = merger.upsert_release(
            data, slug="scarthgap",
            description="Yocto 5.0",
            includes=["yocto/releases/scarthgap.yml"],
        )
        assert rel["slug"] == "scarthgap"
        assert rel in data["registry"]["releases"]

    def test_upsert_release_returns_existing(self, merger):
        data = {"registry": {"releases": [{"slug": "scarthgap", "description": "Old"}]}}
        rel = merger.upsert_release(
            data, "scarthgap", "New description", includes=[]
        )
        assert rel["description"] == "Old"   # not overwritten

    def test_add_vendor_override_flat(self, merger):
        release = {"slug": "scarthgap"}
        added = merger.add_vendor_override(
            release,
            vendor_slug="advantech",
            vendor_release_slug="imx-6.6.52",
            vendor_release_description="IMX 6.6.52",
            kas_include_path="vendors/advantech/imx-6.6.52-scarthgap.yml",
        )
        assert added is True
        vo = release["vendor_overrides"][0]
        assert vo["vendor"] == "advantech"
        vr = vo["releases"][0]
        assert vr["slug"] == "imx-6.6.52"
        assert vr["includes"] == ["vendors/advantech/imx-6.6.52-scarthgap.yml"]

    def test_add_vendor_override_flat_duplicate(self, merger):
        release = {
            "slug": "scarthgap",
            "vendor_overrides": [{
                "vendor": "advantech",
                "releases": [{"slug": "imx-6.6.52", "includes": ["existing.yml"]}],
            }],
        }
        added = merger.add_vendor_override(
            release,
            vendor_slug="advantech",
            vendor_release_slug="imx-6.6.52",
            vendor_release_description="",
            kas_include_path="new.yml",
        )
        assert added is False

    def test_add_vendor_override_nested_soc_vendor(self, merger):
        release = {"slug": "scarthgap"}
        added = merger.add_vendor_override(
            release,
            vendor_slug="advantech",
            vendor_release_slug="imx-6.6.52",
            vendor_release_description="",
            kas_include_path="vendors/advantech/nxp/imx-6.6.52-scarthgap.yml",
            soc_vendor_slug="nxp",
            distro_slug="fsl-imx-xwayland",
        )
        assert added is True
        vo = release["vendor_overrides"][0]
        assert vo["vendor"] == "advantech"
        svo = vo["soc_vendors"][0]
        assert svo["vendor"] == "nxp"
        assert svo["distro"] == "fsl-imx-xwayland"
        assert svo["releases"][0]["slug"] == "imx-6.6.52"

    def test_add_vendor_override_nested_duplicate(self, merger):
        release = {
            "slug": "scarthgap",
            "vendor_overrides": [{
                "vendor": "advantech",
                "soc_vendors": [{
                    "vendor": "nxp",
                    "releases": [{"slug": "imx-6.6.52", "includes": ["x.yml"]}],
                }],
            }],
        }
        added = merger.add_vendor_override(
            release,
            vendor_slug="advantech",
            vendor_release_slug="imx-6.6.52",
            vendor_release_description="",
            kas_include_path="y.yml",
            soc_vendor_slug="nxp",
        )
        assert added is False

    def test_upsert_bsp_preset(self, merger):
        data = {"registry": {}}
        preset = {"name": "myboard-scarthgap", "device": "myboard", "release": "scarthgap"}
        assert merger.upsert_bsp_preset(data, preset) is True
        assert merger.upsert_bsp_preset(data, preset) is False

    def test_save_and_reload(self, merger, tmp_path):
        reg = tmp_path / "bsp-registry.yml"
        data = {"specification": {"version": "2.0"}, "registry": {"vendors": [{"slug": "nxp"}]}}
        merger.save(reg, data)
        loaded = merger.load(reg)
        assert loaded["registry"]["vendors"][0]["slug"] == "nxp"


# =============================================================================
# ManifestImporter integration
# =============================================================================

class TestManifestImporterCreate:
    def test_create_generates_kas_and_registry(self, tmp_path):
        m = _manifest(tmp_path / "manifest", """
            <manifest>
              <remote name="r" fetch="https://github.com/nxp-imx"/>
              <default revision="scarthgap"/>
              <project name="meta-imx" path="layers/meta-imx"/>
            </manifest>
        """)
        result = ManifestImporter().run(
            manifest_path=m,
            output_dir=tmp_path / "out",
            vendor_slug="testvendor",
            vendor_release_slug="test-release",
        )
        out = tmp_path / "out"
        assert len(result.kas_files) == 1
        kas_rel, _ = result.kas_files[0]
        kas_abs = out / kas_rel
        assert kas_abs.exists()
        registry = out / "bsp-registry.yml"
        assert registry.exists()
        reg_data = yaml.safe_load(registry.read_text())
        assert reg_data["specification"]["version"] == "2.0"
        releases = reg_data["registry"]["releases"]
        assert any(r["slug"] == "scarthgap" for r in releases)

    def test_create_errors_if_registry_exists(self, tmp_path):
        m = _manifest(tmp_path / "manifest", """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <project name="layer"/>
            </manifest>
        """)
        out = tmp_path / "out"
        out.mkdir()
        (out / "bsp-registry.yml").write_text("specification:\n  version: '2.0'\n")
        with pytest.raises(FileExistsError):
            ManifestImporter().run(
                manifest_path=m,
                output_dir=out,
                merge=False,
            )

    def test_kas_path_flat_vendor(self, tmp_path):
        m = _manifest(tmp_path / "manifest", """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <default revision="scarthgap"/>
              <project name="meta-layer"/>
            </manifest>
        """)
        result = ManifestImporter().run(
            manifest_path=m,
            output_dir=tmp_path / "out",
            vendor_slug="myvendor",
            vendor_release_slug="my-bsp-1.0",
        )
        kas_rel, _ = result.kas_files[0]
        assert kas_rel == "vendors/myvendor/my-bsp-1.0-scarthgap.yml"

    def test_kas_path_nested_soc_vendor(self, tmp_path):
        m = _manifest(tmp_path / "manifest", """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <default revision="kirkstone"/>
              <project name="meta-layer"/>
            </manifest>
        """)
        result = ManifestImporter().run(
            manifest_path=m,
            output_dir=tmp_path / "out",
            vendor_slug="advantech",
            soc_vendor_slug="nxp",
            vendor_release_slug="imx-5.15.52",
        )
        kas_rel, _ = result.kas_files[0]
        assert kas_rel == "vendors/advantech/nxp/imx-5.15.52-kirkstone.yml"
        out = tmp_path / "out"
        reg_data = yaml.safe_load((out / "bsp-registry.yml").read_text())
        release = reg_data["registry"]["releases"][0]
        vo = release["vendor_overrides"][0]
        assert vo["vendor"] == "advantech"
        svo = vo["soc_vendors"][0]
        assert svo["vendor"] == "nxp"

    def test_sha_preserved_in_kas(self, tmp_path):
        sha = "d" * 40
        m = _manifest(tmp_path / "manifest", f"""
            <manifest>
              <remote name="r" fetch="https://github.com/nxp-imx"/>
              <default revision="scarthgap"/>
              <project name="meta-imx" revision="{sha}" upstream="scarthgap-6.6.52"/>
            </manifest>
        """)
        result = ManifestImporter().run(
            manifest_path=m,
            output_dir=tmp_path / "out",
        )
        kas_rel, kas_yaml = result.kas_files[0]
        kas_abs = tmp_path / "out" / kas_rel
        kas_data = yaml.safe_load(kas_abs.read_text())
        repo = kas_data["repos"]["meta-imx"]
        assert repo["commit"] == sha
        assert repo["branch"] == "scarthgap-6.6.52"

    def test_default_vendor_slug_is_imported(self, tmp_path):
        m = _manifest(tmp_path / "manifest", """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <project name="layer"/>
            </manifest>
        """)
        result = ManifestImporter().run(
            manifest_path=m,
            output_dir=tmp_path / "out",
        )
        kas_rel, _ = result.kas_files[0]
        assert kas_rel.startswith("vendors/imported/")


class TestManifestImporterMerge:
    def test_merge_adds_to_existing_registry(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        existing = {
            "specification": {"version": "2.0"},
            "registry": {
                "vendors": [{"slug": "existing-vendor", "name": "Existing"}],
                "releases": [],
            },
        }
        (out / "bsp-registry.yml").write_text(_dump_yaml(existing))

        m = _manifest(tmp_path / "manifest", """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <default revision="scarthgap"/>
              <project name="layer"/>
            </manifest>
        """)
        ManifestImporter().run(
            manifest_path=m,
            output_dir=out,
            vendor_slug="new-vendor",
            vendor_release_slug="release-1",
            merge=True,
        )
        reg_data = yaml.safe_load((out / "bsp-registry.yml").read_text())
        vendor_slugs = [v["slug"] for v in reg_data["registry"]["vendors"]]
        assert "existing-vendor" in vendor_slugs
        assert "new-vendor" in vendor_slugs
        assert any(r["slug"] == "scarthgap" for r in reg_data["registry"]["releases"])

    def test_merge_idempotent(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        (out / "bsp-registry.yml").write_text(
            _dump_yaml({"specification": {"version": "2.0"}, "registry": {}})
        )
        m = _manifest(tmp_path / "manifest", """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <default revision="scarthgap"/>
              <project name="layer"/>
            </manifest>
        """)
        kwargs = dict(
            manifest_path=m, output_dir=out,
            vendor_slug="v", vendor_release_slug="r1", merge=True,
        )
        ManifestImporter().run(**kwargs)
        ManifestImporter().run(**kwargs)
        reg_data = yaml.safe_load((out / "bsp-registry.yml").read_text())
        # Should not have duplicated entries
        assert len(reg_data["registry"]["releases"]) == 1
        release = reg_data["registry"]["releases"][0]
        assert len(release["vendor_overrides"][0]["releases"]) == 1


class TestManifestImporterDryRun:
    def test_dry_run_creates_no_files(self, tmp_path, capsys):
        m = _manifest(tmp_path / "manifest", """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <default revision="scarthgap"/>
              <project name="layer"/>
            </manifest>
        """)
        out = tmp_path / "out"
        ManifestImporter().run(
            manifest_path=m,
            output_dir=out,
            vendor_slug="v",
            vendor_release_slug="r1",
            dry_run=True,
        )
        assert not out.exists() or not (out / "bsp-registry.yml").exists()


class TestManifestImporterHints:
    def test_hints_skip_excludes_project_from_kas(self, tmp_path):
        hints_file = _write(tmp_path / "hints.yml", """
            projects:
              meta-secret:
                role: skip
        """)
        m = _manifest(tmp_path / "manifest", """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <project name="meta-layer"/>
              <project name="meta-secret"/>
            </manifest>
        """)
        result = ManifestImporter().run(
            manifest_path=m,
            output_dir=tmp_path / "out",
            vendor_slug="v",
            hints_path=hints_file,
        )
        _, kas_yaml = result.kas_files[0]
        assert "meta-secret" not in kas_yaml

    def test_hints_devices_injected_into_registry(self, tmp_path):
        hints_file = _write(tmp_path / "hints.yml", """
            devices:
              - slug: myboard
                description: My Board
                vendor: myvendor
                soc_vendor: nxp
                includes:
                  - vendors/myvendor/nxp/machine/myboard.yml
        """)
        m = _manifest(tmp_path / "manifest", """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <project name="layer"/>
            </manifest>
        """)
        ManifestImporter().run(
            manifest_path=m,
            output_dir=tmp_path / "out",
            vendor_slug="myvendor",
            hints_path=hints_file,
        )
        reg_data = yaml.safe_load(
            (tmp_path / "out" / "bsp-registry.yml").read_text()
        )
        devices = reg_data["registry"].get("devices", [])
        assert any(d["slug"] == "myboard" for d in devices)

    def test_hints_device_not_duplicated_on_merge(self, tmp_path):
        hints_file = _write(tmp_path / "hints.yml", """
            devices:
              - slug: myboard
                vendor: v
        """)
        out = tmp_path / "out"
        out.mkdir()
        (out / "bsp-registry.yml").write_text(_dump_yaml({
            "specification": {"version": "2.0"},
            "registry": {"devices": [{"slug": "myboard", "vendor": "v"}]},
        }))
        m = _manifest(tmp_path / "manifest", """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <project name="layer"/>
            </manifest>
        """)
        ManifestImporter().run(
            manifest_path=m, output_dir=out,
            vendor_slug="v", merge=True, hints_path=hints_file,
        )
        reg_data = yaml.safe_load((out / "bsp-registry.yml").read_text())
        devices = reg_data["registry"]["devices"]
        assert len([d for d in devices if d["slug"] == "myboard"]) == 1


class TestManifestImporterCodename:
    def test_release_slug_override(self, tmp_path):
        m = _manifest(tmp_path / "manifest", """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <project name="layer"/>
            </manifest>
        """)
        result = ManifestImporter().run(
            manifest_path=m,
            output_dir=tmp_path / "out",
            release_slug="mycodename",
        )
        kas_rel, _ = result.kas_files[0]
        assert "mycodename" in kas_rel

    def test_distro_in_vendor_override(self, tmp_path):
        m = _manifest(tmp_path / "manifest", """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <default revision="scarthgap"/>
              <project name="layer"/>
            </manifest>
        """)
        ManifestImporter().run(
            manifest_path=m,
            output_dir=tmp_path / "out",
            vendor_slug="adv",
            vendor_release_slug="imx-bsp",
            distro_slug="fsl-imx-xwayland",
        )
        reg_data = yaml.safe_load(
            (tmp_path / "out" / "bsp-registry.yml").read_text()
        )
        release = reg_data["registry"]["releases"][0]
        vo = release["vendor_overrides"][0]
        assert vo.get("distro") == "fsl-imx-xwayland"


# =============================================================================
# CLI integration
# =============================================================================

class TestCliImportCommand:
    def test_import_subcommand_creates_files(self, tmp_path):
        m = _manifest(tmp_path / "manifest", """
            <manifest>
              <remote name="r" fetch="https://github.com/nxp-imx"/>
              <default revision="scarthgap"/>
              <project name="meta-imx" path="layers/meta-imx"/>
            </manifest>
        """)
        out = tmp_path / "out"
        with patch("sys.argv", [
            "bsp", "import", str(m),
            "--output-dir", str(out),
            "--vendor", "testvendor",
            "--vendor-release", "test-1.0",
        ]):
            exit_code = bsp.main()
        assert exit_code == 0
        assert (out / "bsp-registry.yml").exists()

    def test_import_nonexistent_manifest(self, tmp_path):
        with patch("sys.argv", [
            "bsp", "import", str(tmp_path / "missing.xml"),
            "--output-dir", str(tmp_path / "out"),
        ]):
            exit_code = bsp.main()
        assert exit_code != 0

    def test_import_merge_flag(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        (out / "bsp-registry.yml").write_text(
            _dump_yaml({"specification": {"version": "2.0"}, "registry": {}})
        )
        m = _manifest(tmp_path / "manifest", """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <default revision="scarthgap"/>
              <project name="layer"/>
            </manifest>
        """)
        with patch("sys.argv", [
            "bsp", "import", str(m),
            "--output-dir", str(out),
            "--vendor", "v",
            "--merge",
        ]):
            exit_code = bsp.main()
        assert exit_code == 0
        reg_data = yaml.safe_load((out / "bsp-registry.yml").read_text())
        assert "releases" in reg_data["registry"]

    def test_import_dry_run_no_files(self, tmp_path):
        m = _manifest(tmp_path / "manifest", """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <project name="layer"/>
            </manifest>
        """)
        out = tmp_path / "out"
        with patch("sys.argv", [
            "bsp", "import", str(m),
            "--output-dir", str(out),
            "--dry-run",
        ]):
            exit_code = bsp.main()
        assert exit_code == 0
        assert not (out / "bsp-registry.yml").exists()

    def test_import_creates_fails_if_registry_exists(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        (out / "bsp-registry.yml").write_text("existing content\n")
        m = _manifest(tmp_path / "manifest", """
            <manifest>
              <remote name="r" fetch="https://example.com"/>
              <project name="layer"/>
            </manifest>
        """)
        with patch("sys.argv", [
            "bsp", "import", str(m), "--output-dir", str(out),
        ]):
            exit_code = bsp.main()
        assert exit_code != 0

    def test_import_with_soc_vendor_and_distro(self, tmp_path):
        m = _manifest(tmp_path / "manifest", """
            <manifest>
              <remote name="r" fetch="https://github.com/nxp-imx"/>
              <default revision="scarthgap"/>
              <project name="meta-imx"/>
            </manifest>
        """)
        out = tmp_path / "out"
        with patch("sys.argv", [
            "bsp", "import", str(m),
            "--output-dir", str(out),
            "--vendor", "advantech",
            "--soc-vendor", "nxp",
            "--vendor-release", "imx-6.6.52-2.2.0",
            "--distro", "fsl-imx-xwayland",
        ]):
            exit_code = bsp.main()
        assert exit_code == 0
        reg_data = yaml.safe_load((out / "bsp-registry.yml").read_text())
        release = next(
            r for r in reg_data["registry"]["releases"]
            if r["slug"] == "scarthgap"
        )
        vo = release["vendor_overrides"][0]
        assert vo["vendor"] == "advantech"
        svo = vo["soc_vendors"][0]
        assert svo["vendor"] == "nxp"
        assert svo["distro"] == "fsl-imx-xwayland"


# =============================================================================
# _looks_like_url
# =============================================================================

class TestLooksLikeUrl:
    def test_https_is_url(self):
        assert _looks_like_url("https://github.com/nxp-imx/imx-manifest")

    def test_http_is_url(self):
        assert _looks_like_url("http://example.com/repo.git")

    def test_git_scheme_is_url(self):
        assert _looks_like_url("git://example.com/repo")

    def test_ssh_scheme_is_url(self):
        assert _looks_like_url("ssh://git@github.com/org/repo.git")

    def test_git_at_is_url(self):
        assert _looks_like_url("git@github.com:org/repo.git")

    def test_local_path_is_not_url(self):
        assert not _looks_like_url("/home/user/manifest/default.xml")

    def test_relative_path_is_not_url(self):
        assert not _looks_like_url("default.xml")

    def test_windows_path_is_not_url(self):
        assert not _looks_like_url(r"C:\manifests\default.xml")


# =============================================================================
# ManifestFetcher unit tests  (git calls are always mocked)
# =============================================================================

class TestManifestFetcher:
    """All git subprocess calls are replaced with no-op mocks."""

    @pytest.fixture
    def fetcher(self, tmp_path):
        return ManifestFetcher(cache_dir=tmp_path / "cache")

    # ── _clone_dir determinism ─────────────────────────────────────────────

    def test_clone_dir_is_deterministic(self, fetcher):
        d1 = fetcher._clone_dir("https://example.com/repo", "main")
        d2 = fetcher._clone_dir("https://example.com/repo", "main")
        assert d1 == d2

    def test_clone_dir_differs_by_branch(self, fetcher):
        d1 = fetcher._clone_dir("https://example.com/repo", "main")
        d2 = fetcher._clone_dir("https://example.com/repo", "dev")
        assert d1 != d2

    def test_clone_dir_differs_by_url(self, fetcher):
        d1 = fetcher._clone_dir("https://example.com/repo-a", "main")
        d2 = fetcher._clone_dir("https://example.com/repo-b", "main")
        assert d1 != d2

    def test_clone_dir_contains_repo_name(self, fetcher):
        d = fetcher._clone_dir("https://github.com/nxp-imx/imx-manifest", "main")
        assert "imx-manifest" in d.name

    def test_clone_dir_strips_dot_git(self, fetcher):
        d = fetcher._clone_dir("https://example.com/repo.git", "main")
        assert ".git" not in d.name

    # ── Fresh clone ────────────────────────────────────────────────────────

    def test_fetch_clones_when_not_cached(self, fetcher, tmp_path):
        url = "https://example.com/repo"
        branch = "main"
        clone_dir = fetcher._clone_dir(url, branch)

        # Simulate git clone creating the repo with default.xml
        def fake_run(cmd):
            clone_dir.mkdir(parents=True, exist_ok=True)
            (clone_dir / ".git").mkdir()
            (clone_dir / "default.xml").write_text("<manifest/>")

        with patch.object(fetcher, "_run", side_effect=fake_run) as mock_run:
            result = fetcher.fetch(url, branch=branch)

        mock_run.assert_called_once()
        assert result == clone_dir / "default.xml"

    def test_fetch_uses_specified_manifest_file(self, fetcher, tmp_path):
        url = "https://example.com/repo"
        branch = "scarthgap"
        clone_dir = fetcher._clone_dir(url, branch)

        def fake_run(cmd):
            clone_dir.mkdir(parents=True, exist_ok=True)
            (clone_dir / ".git").mkdir()
            (clone_dir / "custom.xml").write_text("<manifest/>")

        with patch.object(fetcher, "_run", side_effect=fake_run):
            result = fetcher.fetch(url, branch=branch, manifest_file="custom.xml")

        assert result.name == "custom.xml"

    def test_fetch_raises_if_manifest_file_missing(self, fetcher, tmp_path):
        url = "https://example.com/repo"
        branch = "main"
        clone_dir = fetcher._clone_dir(url, branch)

        def fake_run(cmd):
            clone_dir.mkdir(parents=True, exist_ok=True)
            (clone_dir / ".git").mkdir()
            # deliberately do NOT create default.xml

        with patch.object(fetcher, "_run", side_effect=fake_run):
            with pytest.raises(ValueError, match="not found in cloned repo"):
                fetcher.fetch(url, branch=branch)

    # ── Update (already cached) ────────────────────────────────────────────

    def test_fetch_updates_existing_clone(self, fetcher, tmp_path):
        url = "https://example.com/repo"
        branch = "main"
        clone_dir = fetcher._clone_dir(url, branch)

        # Pre-create a "cached" clone
        clone_dir.mkdir(parents=True)
        (clone_dir / ".git").mkdir()
        (clone_dir / "default.xml").write_text("<manifest/>")

        with patch.object(fetcher, "_run") as mock_run, \
             patch.object(fetcher, "_update") as mock_update:
            result = fetcher.fetch(url, branch=branch, update=True)

        mock_update.assert_called_once_with(clone_dir, branch)
        mock_run.assert_not_called()

    def test_fetch_skips_update_when_no_update(self, fetcher, tmp_path):
        url = "https://example.com/repo"
        branch = "main"
        clone_dir = fetcher._clone_dir(url, branch)

        clone_dir.mkdir(parents=True)
        (clone_dir / ".git").mkdir()
        (clone_dir / "default.xml").write_text("<manifest/>")

        with patch.object(fetcher, "_run") as mock_run, \
             patch.object(fetcher, "_update") as mock_update:
            fetcher.fetch(url, branch=branch, update=False)

        mock_update.assert_not_called()
        mock_run.assert_not_called()

    # ── _run error propagation ─────────────────────────────────────────────

    def test_run_raises_runtime_error_on_failure(self, fetcher):
        import subprocess
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                128, ["git", "clone", "url"], stderr="fatal: repo not found"
            )
            with pytest.raises(RuntimeError, match="Git command failed"):
                fetcher._run(["git", "clone", "https://example.com/repo", "/tmp/x"])

    # ── clear_cache ────────────────────────────────────────────────────────

    def test_clear_cache_removes_directory(self, fetcher, tmp_path):
        url = "https://example.com/repo"
        branch = "main"
        clone_dir = fetcher._clone_dir(url, branch)
        clone_dir.mkdir(parents=True)
        (clone_dir / ".git").mkdir()

        fetcher.clear_cache(url, branch)
        assert not clone_dir.exists()

    def test_clear_cache_noop_if_not_cached(self, fetcher):
        # Should not raise
        fetcher.clear_cache("https://example.com/nevercloned", "main")


# =============================================================================
# CLI — remote URL support
# =============================================================================

class TestCliImportRemoteUrl:
    """Test that a remote Git URL is routed through ManifestFetcher."""

    def _make_simple_manifest(self, path: Path) -> None:
        path.write_text(
            "<manifest>"
            "<remote name='r' fetch='https://example.com'/>"
            "<default revision='scarthgap'/>"
            "<project name='meta-layer'/>"
            "</manifest>"
        )

    def test_url_triggers_fetcher(self, tmp_path):
        """When MANIFEST is a URL the fetcher is called, not a local open."""
        out = tmp_path / "out"
        manifest_xml = tmp_path / "default.xml"
        self._make_simple_manifest(manifest_xml)

        with patch(
            "bsp.manifest_importer.ManifestFetcher.fetch",
            return_value=manifest_xml,
        ) as mock_fetch, \
        patch("sys.argv", [
            "bsp", "import",
            "https://github.com/nxp-imx/imx-manifest",
            "--branch", "imx-linux-scarthgap",
            "--manifest-file", "imx-6.6.52-2.2.0.xml",
            "--output-dir", str(out),
            "--vendor", "advantech",
            "--soc-vendor", "nxp",
            "--vendor-release", "imx-6.6.52-2.2.0",
        ]):
            exit_code = bsp.main()

        assert exit_code == 0
        mock_fetch.assert_called_once_with(
            url="https://github.com/nxp-imx/imx-manifest",
            branch="imx-linux-scarthgap",
            manifest_file="imx-6.6.52-2.2.0.xml",
            update=True,
        )

    def test_local_path_does_not_trigger_fetcher(self, tmp_path):
        """When MANIFEST is a local path the fetcher is NOT called."""
        out = tmp_path / "out"
        manifest_xml = tmp_path / "default.xml"
        self._make_simple_manifest(manifest_xml)

        with patch(
            "bsp.manifest_importer.ManifestFetcher.fetch"
        ) as mock_fetch, \
        patch("sys.argv", [
            "bsp", "import", str(manifest_xml),
            "--output-dir", str(out),
        ]):
            exit_code = bsp.main()

        assert exit_code == 0
        mock_fetch.assert_not_called()

    def test_no_update_flag_passed_to_fetcher(self, tmp_path):
        out = tmp_path / "out"
        manifest_xml = tmp_path / "default.xml"
        self._make_simple_manifest(manifest_xml)

        with patch(
            "bsp.manifest_importer.ManifestFetcher.fetch",
            return_value=manifest_xml,
        ) as mock_fetch, \
        patch("sys.argv", [
            "bsp", "import",
            "https://github.com/nxp-imx/imx-manifest",
            "--no-update",
            "--output-dir", str(out),
        ]):
            bsp.main()

        _, kwargs = mock_fetch.call_args
        assert kwargs.get("update") is False

    def test_url_default_branch_is_main(self, tmp_path):
        out = tmp_path / "out"
        manifest_xml = tmp_path / "default.xml"
        self._make_simple_manifest(manifest_xml)

        with patch(
            "bsp.manifest_importer.ManifestFetcher.fetch",
            return_value=manifest_xml,
        ) as mock_fetch, \
        patch("sys.argv", [
            "bsp", "import",
            "https://example.com/repo",
            "--output-dir", str(out),
        ]):
            bsp.main()

        _, kwargs = mock_fetch.call_args
        assert kwargs.get("branch") == "main"

    def test_url_default_manifest_file_is_default_xml(self, tmp_path):
        out = tmp_path / "out"
        manifest_xml = tmp_path / "default.xml"
        self._make_simple_manifest(manifest_xml)

        with patch(
            "bsp.manifest_importer.ManifestFetcher.fetch",
            return_value=manifest_xml,
        ) as mock_fetch, \
        patch("sys.argv", [
            "bsp", "import",
            "https://example.com/repo",
            "--output-dir", str(out),
        ]):
            bsp.main()

        _, kwargs = mock_fetch.call_args
        assert kwargs.get("manifest_file") == "default.xml"

    def test_fetcher_error_returns_nonzero(self, tmp_path):
        out = tmp_path / "out"
        with patch(
            "bsp.manifest_importer.ManifestFetcher.fetch",
            side_effect=RuntimeError("git clone failed"),
        ), \
        patch("sys.argv", [
            "bsp", "import",
            "https://example.com/broken-repo",
            "--output-dir", str(out),
        ]):
            exit_code = bsp.main()

        assert exit_code != 0
