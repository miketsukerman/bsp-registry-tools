"""
Tests for export bundle helpers (patch copying and setup script generation).
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

from bsp import KasManager
from bsp.export_bundle import (
    DEFAULT_KAS_CONFIG_NAME,
    ENVIRONMENT_FILE_NAME,
    README_FILE_NAME,
    SETUP_SCRIPT_NAME,
    ExportedContainer,
    ExportedPatch,
    copy_container,
    copy_patch_entries,
    copy_patches,
    generate_setup_script,
    write_environment_file,
    write_readme,
)
from bsp.models import Docker, DockerArg, EnvironmentVariable


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


class TestCollectPatchEntries:
    def test_entries_carry_repo_and_checkout_path(self, tmp_dir, kas_config_with_patches):
        manager = KasManager(
            kas_files=[str(kas_config_with_patches)],
            build_dir=str(tmp_dir / "build"),
            search_paths=[str(tmp_dir)],
        )
        entries = manager.collect_patch_entries()

        assert [entry["repo"] for entry in entries] == ["meta-foo", "meta-foo"]
        assert [entry["repo_path"] for entry in entries] == ["meta-foo", "meta-foo"]
        assert [os.path.basename(entry["path"]) for entry in entries] == [
            "0001-fix.patch",
            "0002-fix.patch",
        ]

    def test_explicit_repo_path_is_used(self, tmp_dir):
        patch_file = tmp_dir / "0001-fix.patch"
        patch_file.write_text("--- a\n+++ b\n")
        kas_path = tmp_dir / "with-path.yaml"
        kas_path.write_text(
            """
header:
  version: 14

repos:
  bsp-registry:
  meta-foo:
    url: https://example.com/meta-foo.git
    refspec: main
    path: layers/meta-foo
    patches:
      fix-one:
        repo: bsp-registry
        path: 0001-fix.patch
"""
        )
        manager = KasManager(
            kas_files=[str(kas_path)],
            build_dir=str(tmp_dir / "build"),
            search_paths=[str(tmp_dir)],
        )
        entries = manager.collect_patch_entries()
        assert entries[0]["repo_path"] == "layers/meta-foo"


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


class TestCopyPatchEntries:
    def test_repository_information_is_kept(self, tmp_dir):
        source = tmp_dir / "patches" / "meta-foo" / "0001-fix.patch"
        source.parent.mkdir(parents=True)
        source.write_text("--- a\n+++ b\n")

        copied = copy_patch_entries(
            [{"repo": "meta-foo", "repo_path": "layers/meta-foo", "path": str(source)}],
            str(tmp_dir / "export"),
            base_dir=str(tmp_dir),
        )

        assert len(copied) == 1
        assert copied[0].path.as_posix() == "patches/meta-foo/0001-fix.patch"
        assert copied[0].repo == "meta-foo"
        assert copied[0].repo_path == "layers/meta-foo"

    def test_repo_path_defaults_to_repo_name(self, tmp_dir):
        source = tmp_dir / "0001-fix.patch"
        source.write_text("--- a\n+++ b\n")

        copied = copy_patch_entries(
            [{"repo": "meta-foo", "path": str(source)}],
            str(tmp_dir / "export"),
            base_dir=str(tmp_dir),
        )
        assert copied[0].repo_path == "meta-foo"


class TestCopyContainer:
    def test_dockerfile_is_copied_into_container_dir(self, tmp_dir):
        (tmp_dir / "Dockerfile.ubuntu").write_text("FROM ubuntu\n")
        export_dir = tmp_dir / "export"

        exported = copy_container(
            Docker(
                image="test/ubuntu:latest",
                file="Dockerfile.ubuntu",
                args=[DockerArg("DISTRO", "ubuntu:22.04")],
                privileged=True,
                runtime_args="--net=host",
            ),
            str(export_dir),
            base_dir=str(tmp_dir),
        )

        assert exported is not None
        assert exported.image == "test/ubuntu:latest"
        assert exported.dockerfile.as_posix() == "container/Dockerfile.ubuntu"
        assert exported.args == [("DISTRO", "ubuntu:22.04")]
        assert exported.privileged is True
        assert exported.runtime_args == "--net=host"
        assert (export_dir / "container" / "Dockerfile.ubuntu").read_text() == "FROM ubuntu\n"

    def test_prebuilt_image_without_dockerfile(self, tmp_dir):
        exported = copy_container(
            Docker(image="test/ubuntu:latest", file=None),
            str(tmp_dir / "export"),
        )
        assert exported is not None
        assert exported.image == "test/ubuntu:latest"
        assert exported.dockerfile is None

    def test_missing_dockerfile_is_skipped(self, tmp_dir):
        exported = copy_container(
            Docker(image="test/ubuntu:latest", file="nope.Dockerfile"),
            str(tmp_dir / "export"),
            base_dir=str(tmp_dir),
        )
        assert exported is not None
        assert exported.dockerfile is None

    def test_no_container(self, tmp_dir):
        assert copy_container(None, str(tmp_dir / "export")) is None

    def test_container_without_image_and_file(self, tmp_dir):
        assert copy_container(Docker(image=None, file=None), str(tmp_dir / "export")) is None


class TestWriteEnvironmentFile:
    def test_variables_are_written_with_defaults(self, tmp_dir):
        export_dir = tmp_dir / "export"
        path = write_environment_file(
            [
                EnvironmentVariable("DL_DIR", "/tmp/downloads"),
                EnvironmentVariable("SSTATE_DIR", "/tmp/sstate"),
            ],
            str(export_dir),
            label="my-bsp",
        )

        assert path.name == ENVIRONMENT_FILE_NAME
        content = path.read_text()
        assert ': ${DL_DIR:="/tmp/downloads"}' in content
        assert "export SSTATE_DIR" in content

    def test_env_placeholders_become_shell_references(self, tmp_dir):
        path = write_environment_file(
            [EnvironmentVariable("GITCONFIG_FILE", "$ENV{HOME}/.gitconfig")],
            str(tmp_dir / "export"),
        )
        assert ': ${GITCONFIG_FILE:="${HOME}/.gitconfig"}' in path.read_text()

    def test_later_variables_win(self, tmp_dir):
        path = write_environment_file(
            [
                EnvironmentVariable("DL_DIR", "/global"),
                EnvironmentVariable("DL_DIR", "/named"),
            ],
            str(tmp_dir / "export"),
        )
        content = path.read_text()
        assert "/named" in content
        assert "/global" not in content

    def test_no_variables_writes_nothing(self, tmp_dir):
        export_dir = tmp_dir / "export"
        assert write_environment_file([], str(export_dir)) is None
        assert not (export_dir / ENVIRONMENT_FILE_NAME).exists()


class TestSetupScriptContainerAndEnvironment:
    def test_container_image_is_built_and_used(self, tmp_dir):
        export_dir = tmp_dir / "export"
        container = ExportedContainer(
            image="test/ubuntu:latest",
            dockerfile=Path("container") / "Dockerfile.ubuntu",
            args=[("DISTRO", "ubuntu:22.04")],
            privileged=True,
        )
        script = generate_setup_script(
            str(export_dir),
            DEFAULT_KAS_CONFIG_NAME,
            container=container,
            environment_file=ENVIRONMENT_FILE_NAME,
        )

        content = script.read_text()
        assert "kas-container" in content
        assert 'KAS_CONTAINER_IMAGE:-test/ubuntu:latest' in content
        assert "container/Dockerfile.ubuntu" in content
        assert '--build-arg "DISTRO=ubuntu:22.04"' in content
        assert "--isar" in content
        assert ENVIRONMENT_FILE_NAME in content

    def test_script_without_container_uses_native_kas(self, tmp_dir):
        script = generate_setup_script(str(tmp_dir / "export"), DEFAULT_KAS_CONFIG_NAME)
        content = script.read_text()
        assert "kas-container" not in content
        assert "KAS_CONTAINER_IMAGE" not in content
        assert ENVIRONMENT_FILE_NAME not in content

    def test_repo_script_sources_environment(self, tmp_dir):
        script = generate_setup_script(
            str(tmp_dir / "export"),
            "manifest.xml",
            repo_manifest=True,
            environment_file=ENVIRONMENT_FILE_NAME,
        )
        assert f'. "$SCRIPT_DIR/{ENVIRONMENT_FILE_NAME}"' in script.read_text()

    def test_generated_scripts_are_valid_posix_shell(self, tmp_dir):
        export_dir = tmp_dir / "export"
        write_environment_file(
            [EnvironmentVariable("DL_DIR", '$ENV{HOME}/dl "quoted"')],
            str(export_dir),
        )
        script = generate_setup_script(
            str(export_dir),
            DEFAULT_KAS_CONFIG_NAME,
            container=ExportedContainer(
                image="test/ubuntu:latest",
                dockerfile=Path("container") / "Dockerfile.ubuntu",
                runtime_args='--net=host -e X="y"',
            ),
            environment_file=ENVIRONMENT_FILE_NAME,
        )
        for path in (script, export_dir / ENVIRONMENT_FILE_NAME):
            assert subprocess.run(["sh", "-n", str(path)]).returncode == 0


class TestRepoManifestPatches:
    def test_patches_are_applied_after_repo_sync(self, tmp_dir):
        script = generate_setup_script(
            str(tmp_dir / "export"),
            "manifest.xml",
            repo_manifest=True,
            patches=[
                ExportedPatch(
                    path=Path("patches/meta-foo/0001-fix.patch"),
                    repo="meta-foo",
                    repo_path="layers/meta-foo",
                )
            ],
        )
        content = script.read_text()

        assert "apply_patch()" in content
        assert 'apply_patch "layers/meta-foo" "patches/meta-foo/0001-fix.patch"' in content
        assert content.index("repo sync") < content.index("apply_patch()")
        assert "apply --reverse --check" in content

    def test_no_patch_block_without_patches(self, tmp_dir):
        script = generate_setup_script(
            str(tmp_dir / "export"), "manifest.xml", repo_manifest=True
        )
        assert "apply_patch" not in script.read_text()

    def test_kas_script_does_not_apply_patches(self, tmp_dir):
        script = generate_setup_script(
            str(tmp_dir / "export"),
            DEFAULT_KAS_CONFIG_NAME,
            patches=[ExportedPatch(path=Path("patches/0001-fix.patch"), repo="meta-foo")],
        )
        assert "apply_patch" not in script.read_text()

    def test_repo_script_with_patches_is_valid_posix_shell(self, tmp_dir):
        script = generate_setup_script(
            str(tmp_dir / "export"),
            "manifest.xml",
            repo_manifest=True,
            patches=[
                ExportedPatch(
                    path=Path('patches/quo"ted.patch'),
                    repo="meta-foo",
                    repo_path="layers/meta-foo",
                )
            ],
        )
        assert subprocess.run(["sh", "-n", str(script)]).returncode == 0


class TestWriteReadme:
    def test_readme_documents_bundle_contents(self, tmp_dir):
        export_dir = tmp_dir / "export"
        readme = write_readme(
            str(export_dir),
            DEFAULT_KAS_CONFIG_NAME,
            label="my-bsp - description",
            patches=[Path("patches/meta-foo/0001-fix.patch")],
            container=ExportedContainer(
                image="test/ubuntu:latest",
                dockerfile=Path("container") / "Dockerfile.ubuntu",
                args=[("DISTRO", "ubuntu:22.04")],
                privileged=True,
                runtime_args="--net=host",
            ),
            environment_file=ENVIRONMENT_FILE_NAME,
        )

        assert readme.name == README_FILE_NAME
        content = readme.read_text()
        assert content.startswith("# my-bsp - description")
        assert DEFAULT_KAS_CONFIG_NAME in content
        assert "`patches/meta-foo/`" in content
        assert "container/Dockerfile.ubuntu" in content
        assert ENVIRONMENT_FILE_NAME in content
        assert SETUP_SCRIPT_NAME in content
        assert "kas-container" in content
        assert "test/ubuntu:latest" in content
        assert "DISTRO=ubuntu:22.04" in content
        assert "--net=host" in content

    def test_readme_without_extras_mentions_plain_kas(self, tmp_dir):
        readme = write_readme(str(tmp_dir / "export"), DEFAULT_KAS_CONFIG_NAME)
        content = readme.read_text()
        assert "kas-container" not in content
        assert ENVIRONMENT_FILE_NAME not in content
        assert "Build container" not in content
        assert SETUP_SCRIPT_NAME in content

    def test_readme_without_setup_script_documents_manual_commands(self, tmp_dir):
        readme = write_readme(
            str(tmp_dir / "export"),
            DEFAULT_KAS_CONFIG_NAME,
            setup_script=False,
        )
        content = readme.read_text()
        assert SETUP_SCRIPT_NAME not in content
        assert f"kas checkout {DEFAULT_KAS_CONFIG_NAME}" in content

    def test_repo_manifest_readme_documents_patch_application(self, tmp_dir):
        readme = write_readme(
            str(tmp_dir / "export"),
            "manifest.xml",
            repo_manifest=True,
            patches=[
                ExportedPatch(
                    path=Path("patches/0001-fix.patch"),
                    repo="meta-foo",
                    repo_path="layers/meta-foo",
                )
            ],
        )
        content = readme.read_text()
        assert "patches/" in content
        assert "git apply" in content

    def test_repo_manifest_readme_mentions_repo_tool(self, tmp_dir):
        readme = write_readme(
            str(tmp_dir / "export"),
            "manifest.xml",
            repo_manifest=True,
            label="android-bsp",
        )
        content = readme.read_text()
        assert "android-bsp" in content
        assert "manifest.xml" in content
        assert "repo" in content
        assert "kas" not in content
