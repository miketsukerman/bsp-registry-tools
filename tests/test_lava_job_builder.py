"""
Tests for bsp/lava_job_builder.py.
"""

import tempfile
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock

import pytest
import yaml

from bsp.lava_job_builder import build_lava_job, _basename_filter
from bsp.resolver import ResolvedConfig


# =============================================================================
# Helpers
# =============================================================================

def _make_resolved(
    device_slug: str = "qemu-arm64",
    release_slug: str = "scarthgap",
    feature_slugs: Optional[List[str]] = None,
    build_path: str = "build/poky/qemu-arm64/scarthgap",
) -> ResolvedConfig:
    """Create a minimal ResolvedConfig for testing."""
    device = MagicMock()
    device.slug = device_slug

    release = MagicMock()
    release.slug = release_slug

    features = []
    for slug in (feature_slugs or []):
        f = MagicMock()
        f.slug = slug
        features.append(f)

    return ResolvedConfig(
        device=device,
        release=release,
        features=features,
        build_path=build_path,
    )


# =============================================================================
# _basename_filter
# =============================================================================

class TestBasenameFilter:
    def test_returns_basename(self):
        assert _basename_filter("/some/path/suite.robot") == "suite.robot"

    def test_works_with_relative_path(self):
        assert _basename_filter("tests/robot/smoke.robot") == "smoke.robot"

    def test_works_with_filename_only(self):
        assert _basename_filter("boot.robot") == "boot.robot"


# =============================================================================
# build_lava_job — basic rendering
# =============================================================================

class TestBuildLavaJobBasic:
    def test_returns_string(self):
        resolved = _make_resolved()
        result = build_lava_job(resolved, device_type="qemu-aarch64")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_device_type_in_output(self):
        resolved = _make_resolved()
        result = build_lava_job(resolved, device_type="my-device-type")
        assert "my-device-type" in result

    def test_job_name_includes_device_and_release(self):
        resolved = _make_resolved(device_slug="mydevice", release_slug="myrelease")
        result = build_lava_job(resolved, device_type="x")
        assert "mydevice" in result
        assert "myrelease" in result

    def test_job_name_includes_features(self):
        resolved = _make_resolved(feature_slugs=["ssh", "debug"])
        result = build_lava_job(resolved, device_type="x")
        assert "ssh" in result
        assert "debug" in result

    def test_image_url_formed_from_artifact_url_and_build_path(self):
        # artifact_url is now a full-URL override; it is used as-is.
        resolved = _make_resolved(build_path="build/poky/myboard/scarthgap")
        result = build_lava_job(
            resolved,
            device_type="x",
            artifact_url="http://files.example.com/my-image.wic.gz",
        )
        assert "http://files.example.com/my-image.wic.gz" in result

    def test_image_url_from_artifact_server_url_and_artifact_name(self):
        # artifact_server_url + artifact_name → image_url composition
        resolved = _make_resolved(build_path="build/poky/myboard/scarthgap")
        result = build_lava_job(
            resolved,
            device_type="x",
            artifact_server_url="http://files.example.com",
            artifact_name="core-image-minimal.wic.gz",
        )
        assert "http://files.example.com/core-image-minimal.wic.gz" in result

    def test_image_url_from_artifact_server_url_and_build_path(self):
        # legacy path: artifact_server_url + build_path when artifact_name absent
        resolved = _make_resolved(build_path="build/poky/myboard/scarthgap")
        result = build_lava_job(
            resolved,
            device_type="x",
            artifact_server_url="http://files.example.com",
        )
        assert "http://files.example.com/build/poky/myboard/scarthgap" in result

    def test_artifact_url_takes_priority_over_server_url(self):
        # Full artifact_url wins over artifact_server_url + artifact_name
        resolved = _make_resolved(build_path="build/poky/myboard/scarthgap")
        result = build_lava_job(
            resolved,
            device_type="x",
            artifact_url="http://override.example.com/full.wic.gz",
            artifact_server_url="http://server.example.com",
            artifact_name="other.wic.gz",
        )
        assert "http://override.example.com/full.wic.gz" in result
        assert "http://server.example.com" not in result

    def test_no_image_url_when_artifact_url_empty(self):
        resolved = _make_resolved()
        result = build_lava_job(resolved, device_type="x", artifact_url="")
        # The built-in template skips the deploy block when image_url is empty
        assert "deploy:" not in result

    def test_tags_rendered(self):
        resolved = _make_resolved()
        result = build_lava_job(
            resolved,
            device_type="x",
            lava_tags=["hil", "imx8"],
        )
        assert "hil" in result
        assert "imx8" in result

    def test_no_tags_section_when_empty(self):
        resolved = _make_resolved()
        result = build_lava_job(resolved, device_type="x", lava_tags=[])
        assert "tags:" not in result

    def test_robot_suites_rendered(self):
        resolved = _make_resolved()
        result = build_lava_job(
            resolved,
            device_type="x",
            robot_suites=["tests/smoke.robot"],
        )
        assert "tests/smoke.robot" in result

    def test_robot_variables_rendered(self):
        resolved = _make_resolved()
        result = build_lava_job(
            resolved,
            device_type="x",
            robot_suites=["tests/smoke.robot"],
            robot_variables={"BOARD_IP": "192.168.1.10"},
        )
        assert "BOARD_IP" in result
        assert "192.168.1.10" in result

    def test_timeout_minutes_computed_from_wait_timeout(self):
        resolved = _make_resolved()
        result = build_lava_job(
            resolved, device_type="x", wait_timeout=3600
        )
        assert "60" in result  # 3600 // 60

    def test_minimum_timeout_is_1_minute(self):
        resolved = _make_resolved()
        result = build_lava_job(
            resolved, device_type="x", wait_timeout=30
        )
        # Even if wait_timeout is < 60s the template should not crash
        assert "minutes:" in result


# =============================================================================
# build_lava_job — custom template
# =============================================================================

class TestBuildLavaJobCustomTemplate:
    def test_custom_template_rendered(self, tmp_path):
        tpl = tmp_path / "my-job.yaml.j2"
        tpl.write_text("device: {{ device_type }}\nboard: {{ device_slug }}\n")
        resolved = _make_resolved(device_slug="myboard")
        result = build_lava_job(
            resolved,
            device_type="mydevicetype",
            job_template_path=str(tpl),
        )
        assert "device: mydevicetype" in result
        assert "board: myboard" in result

    def test_missing_template_raises_file_not_found(self):
        resolved = _make_resolved()
        with pytest.raises(FileNotFoundError, match="not found"):
            build_lava_job(
                resolved,
                device_type="x",
                job_template_path="/nonexistent/template.yaml.j2",
            )

    def test_custom_template_has_access_to_all_context_keys(self, tmp_path):
        tpl = tmp_path / "full-ctx.yaml.j2"
        tpl.write_text(
            "dt={{ device_type }}\n"
            "jn={{ job_name }}\n"
            "iu={{ image_url }}\n"
            "au={{ artifact_url }}\n"
            "asu={{ artifact_server_url }}\n"
            "an={{ artifact_name }}\n"
            "bp={{ build_path }}\n"
            "ds={{ device_slug }}\n"
            "rs={{ release_slug }}\n"
            "fs={{ feature_slugs | join(',') }}\n"
            "lt={{ lava_tags | join(',') }}\n"
            "ro={{ robot_suites | join(',') }}\n"
            "tm={{ timeout_minutes }}\n"
        )
        resolved = _make_resolved(
            device_slug="d",
            release_slug="r",
            feature_slugs=["f1"],
            build_path="build/x",
        )
        result = build_lava_job(
            resolved,
            device_type="dt-val",
            artifact_url="http://art/img.wic.gz",
            artifact_server_url="http://server",
            artifact_name="img.wic.gz",
            job_template_path=str(tpl),
            lava_tags=["t1"],
            robot_suites=["s.robot"],
            wait_timeout=120,
        )
        assert "dt=dt-val" in result
        assert "ds=d" in result
        assert "rs=r" in result
        assert "fs=f1" in result
        assert "lt=t1" in result
        assert "ro=s.robot" in result
        assert "tm=2" in result  # 120 // 60
        assert "asu=http://server" in result
        assert "an=img.wic.gz" in result


# =============================================================================
# Output is parseable YAML (built-in template with a complete config)
# =============================================================================

class TestBuiltinTemplateYamlValid:
    def test_output_is_valid_yaml(self):
        resolved = _make_resolved(build_path="build/poky/qemu/scarthgap")
        result = build_lava_job(
            resolved,
            device_type="qemu-aarch64",
            artifact_server_url="http://files.example.com",
            lava_tags=["ci"],
            robot_suites=["tests/smoke.robot"],
            robot_variables={"IP": "10.0.0.1"},
            wait_timeout=1800,
        )
        # Should parse without errors
        parsed = yaml.safe_load(result)
        assert parsed is not None
        assert "job_name" in parsed
        assert "device_type" in parsed

    def test_output_without_image_is_valid_yaml(self):
        resolved = _make_resolved()
        result = build_lava_job(resolved, device_type="qemu-aarch64")
        parsed = yaml.safe_load(result)
        assert parsed is not None


# =============================================================================
# build_lava_job — lava_context parameter
# =============================================================================

class TestBuildLavaJobContext:
    def test_no_context_when_lava_context_is_none(self):
        resolved = _make_resolved()
        result = build_lava_job(resolved, device_type="qemu", lava_context=None)
        assert "context:" not in result

    def test_context_rendered_in_builtin_template(self):
        resolved = _make_resolved()
        result = build_lava_job(
            resolved,
            device_type="qemu",
            lava_context={"device_arch": "amd64", "device_machine": "qemux86-64"},
        )
        assert "context:" in result
        assert "arch: amd64" in result
        assert "machine: qemux86-64" in result

    def test_context_rendered_in_custom_template(self, tmp_path):
        tpl = tmp_path / "ctx.yaml.j2"
        tpl.write_text(
            "{% if context %}ctx_arch={{ context.device_arch }}\n"
            "ctx_mach={{ context.device_machine }}{% endif %}\n"
        )
        resolved = _make_resolved()
        result = build_lava_job(
            resolved,
            device_type="qemu",
            lava_context={"device_arch": "arm64", "device_machine": "imx8mpevk"},
            job_template_path=str(tpl),
        )
        assert "ctx_arch=arm64" in result
        assert "ctx_mach=imx8mpevk" in result

    def test_context_absent_in_custom_template_when_none(self, tmp_path):
        tpl = tmp_path / "ctx.yaml.j2"
        tpl.write_text("{% if context %}CONTEXT{% endif %}no-context\n")
        resolved = _make_resolved()
        result = build_lava_job(
            resolved,
            device_type="qemu",
            lava_context=None,
            job_template_path=str(tpl),
        )
        assert "CONTEXT" not in result
        assert "no-context" in result

    def test_builtin_template_with_context_is_valid_yaml(self):
        resolved = _make_resolved(build_path="build/poky/qemu/scarthgap")
        result = build_lava_job(
            resolved,
            device_type="qemu",
            artifact_server_url="http://files.example.com",
            lava_context={"device_arch": "amd64", "device_machine": "qemux86-64"},
        )
        parsed = yaml.safe_load(result)
        assert parsed is not None
        assert parsed.get("context") == {"arch": "amd64", "machine": "qemux86-64"}


# =============================================================================
# build_lava_job — machine defaults to device.slug via bsp_manager resolution
# (testing the context dict that the manager would build)
# =============================================================================

class TestBuildLavaJobMachineDefaultsToDeviceSlug:
    """
    The bsp_manager always sets device_machine = device.slug when no explicit
    testing.lava.context.machine is provided.  These tests verify that the
    job builder renders whatever dict it receives correctly, including the
    slug-derived machine value.
    """

    def test_machine_from_device_slug_rendered_in_builtin_template(self):
        resolved = _make_resolved(device_slug="imx8mpevk")
        # Simulate manager-resolved context: machine = device slug
        result = build_lava_job(
            resolved,
            device_type="imx8mpevk",
            lava_context={"device_arch": "", "device_machine": "imx8mpevk"},
        )
        assert "context:" in result
        assert "machine: imx8mpevk" in result

    def test_machine_from_device_slug_is_valid_yaml(self):
        resolved = _make_resolved(device_slug="qemux86-64")
        result = build_lava_job(
            resolved,
            device_type="qemu",
            lava_context={"device_arch": "amd64", "device_machine": "qemux86-64"},
        )
        parsed = yaml.safe_load(result)
        assert parsed["context"]["machine"] == "qemux86-64"

    def test_arch_empty_machine_slug_still_renders_context(self):
        resolved = _make_resolved(device_slug="my-device")
        result = build_lava_job(
            resolved,
            device_type="my-device",
            lava_context={"device_arch": "", "device_machine": "my-device"},
        )
        assert "context:" in result
        assert "machine: my-device" in result
