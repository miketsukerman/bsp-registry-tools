"""
Tests for build-manifest path sanitisation helpers.
"""

from pathlib import Path

import pytest

from bsp.manifest_paths import ManifestPathSanitizer


@pytest.fixture
def sanitizer(tmp_path):
    registry_root = tmp_path / "registry"
    build_root = tmp_path / "registry" / "build" / "qemu"
    home = tmp_path / "home"
    for path in (registry_root, build_root, home):
        path.mkdir(parents=True, exist_ok=True)
    return ManifestPathSanitizer(
        registry_root=registry_root,
        build_root=build_root,
        home=home,
    )


class TestRelativize:
    def test_path_under_registry_root(self, sanitizer, tmp_path):
        value = str(tmp_path / "registry" / "kas" / "device.yaml")
        assert sanitizer.relativize(value) == "kas/device.yaml"

    def test_path_under_build_root_prefers_deepest_anchor(self, sanitizer, tmp_path):
        value = str(tmp_path / "registry" / "build" / "qemu" / "conf" / "local.conf")
        assert sanitizer.relativize(value) == "conf/local.conf"

    def test_anchor_itself_becomes_dot(self, sanitizer, tmp_path):
        assert sanitizer.relativize(str(tmp_path / "registry")) == "."

    def test_path_under_home(self, sanitizer, tmp_path):
        value = str(tmp_path / "home" / ".cache" / "yocto")
        assert sanitizer.relativize(value) == "${HOME}/.cache/yocto"

    def test_tilde_path_is_expanded_against_home(self, sanitizer, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        assert sanitizer.relativize("~/downloads") == "${HOME}/downloads"

    def test_path_outside_all_anchors(self, sanitizer):
        assert sanitizer.relativize("/opt/toolchains/gcc") == "<external>/gcc"

    def test_relative_path_is_unchanged(self, sanitizer):
        assert sanitizer.relativize("kas/device.yaml") == "kas/device.yaml"

    def test_non_path_values_are_unchanged(self, sanitizer):
        assert sanitizer.relativize("") == ""
        assert sanitizer.relativize(None) is None
        assert sanitizer.relativize(42) == 42

    def test_relativize_to_registry_ignores_build_anchor(self, sanitizer, tmp_path):
        value = str(tmp_path / "registry" / "build" / "qemu")
        assert sanitizer.relativize_to_registry(value) == "build/qemu"

    def test_relativize_to_registry_outside_registry(self, sanitizer, tmp_path):
        value = str(tmp_path / "elsewhere" / "build-dir")
        assert sanitizer.relativize_to_registry(value) == "<external>/build-dir"

    def test_missing_anchors_fall_back_to_placeholders(self):
        plain = ManifestPathSanitizer(home=Path("/nonexistent-home"))
        assert plain.relativize("/opt/data/file.txt") == "<external>/file.txt"


class TestScrubText:
    def test_whole_value_path_is_relativized(self, sanitizer, tmp_path):
        value = str(tmp_path / "registry" / "conf" / "site.conf")
        assert sanitizer.scrub_text(value) == "conf/site.conf"

    def test_local_conf_line_with_quoted_absolute_path(self, sanitizer, tmp_path):
        line = f'DL_DIR = "{tmp_path / "registry" / "downloads"}"'
        assert sanitizer.scrub_text(line) == 'DL_DIR = "${registry}/downloads"'

    def test_local_conf_line_with_external_path(self, sanitizer):
        assert sanitizer.scrub_text('SSTATE_DIR = "/opt/yocto/sstate"') == \
            'SSTATE_DIR = "<external>/sstate"'

    def test_runtime_args_mount_host_path_is_scrubbed(self, sanitizer, tmp_path):
        args = f"--ipc host -v {tmp_path / 'home' / 'dl'}:/downloads:ro"
        assert sanitizer.scrub_text(args) == "--ipc host -v ${HOME}/dl:/downloads:ro"

    def test_runtime_args_external_mount_host_path_is_scrubbed(self, sanitizer):
        assert sanitizer.scrub_text("-v /srv/cache/sstate:/sstate:ro") == \
            "-v <external>/sstate:/sstate:ro"

    def test_assignment_without_quotes_is_scrubbed(self, sanitizer):
        assert sanitizer.scrub_text("DL_DIR=/opt/yocto/downloads") == \
            "DL_DIR=<external>/downloads"

    def test_text_without_paths_is_unchanged(self, sanitizer):
        assert sanitizer.scrub_text("--network host") == "--network host"

    def test_non_string_values_are_unchanged(self, sanitizer):
        assert sanitizer.scrub_text(None) is None
        assert sanitizer.scrub_text(True) is True


class TestScrubArgv:
    def test_program_name_is_reduced_to_basename(self, sanitizer, tmp_path):
        argv = ["/home/user/.venv/bin/bsp", "build", "preset"]
        assert sanitizer.scrub_argv(argv) == ["bsp", "build", "preset"]

    def test_absolute_arguments_are_relativized(self, sanitizer, tmp_path):
        argv = ["/usr/local/bin/bsp", "--path", str(tmp_path / "registry" / "build" / "qemu")]
        assert sanitizer.scrub_argv(argv) == ["bsp", "--path", "."]

    def test_empty_argv(self, sanitizer):
        assert sanitizer.scrub_argv([]) == []
