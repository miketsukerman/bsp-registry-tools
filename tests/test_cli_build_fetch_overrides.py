"""Tests for build/fetch CLI vendor-release and override forwarding."""

from unittest.mock import MagicMock, patch

import bsp
from bsp import BspManager


class TestCliBuildFetchOverrides:
    def test_build_preset_passes_vendor_release_and_override(self, registry_file):
        with patch("sys.argv", [
            "bsp",
            "--registry",
            str(registry_file),
            "build",
            "test-bsp",
            "--vendor-release",
            "imx-6.12.0",
            "--override",
            "imx-xwayland-6.6.52",
        ]):
            with patch("bsp.BspManager.build_bsp") as mock_build:
                exit_code = bsp.main()
        assert exit_code == 0
        _, kwargs = mock_build.call_args
        assert kwargs.get("vendor_release_slug") == "imx-6.12.0"
        assert kwargs.get("override_slug") == "imx-xwayland-6.6.52"

    def test_build_components_passes_vendor_release_and_override(self, registry_file):
        with patch("sys.argv", [
            "bsp",
            "--registry",
            str(registry_file),
            "build",
            "--device",
            "test-device",
            "--release",
            "test-release",
            "--vendor-release",
            "imx-6.12.0",
            "--override",
            "imx-xwayland-6.6.52",
        ]):
            with patch("bsp.BspManager.build_by_components") as mock_build:
                exit_code = bsp.main()
        assert exit_code == 0
        _, kwargs = mock_build.call_args
        assert kwargs.get("vendor_release_slug") == "imx-6.12.0"
        assert kwargs.get("override_slug") == "imx-xwayland-6.6.52"

    def test_fetch_preset_passes_vendor_release_and_override(self, registry_file):
        with patch("sys.argv", [
            "bsp",
            "--registry",
            str(registry_file),
            "fetch",
            "test-bsp",
            "--vendor-release",
            "imx-6.12.0",
            "--override",
            "imx-xwayland-6.6.52",
        ]):
            with patch("bsp.BspManager.fetch_bsp") as mock_fetch:
                exit_code = bsp.main()
        assert exit_code == 0
        _, kwargs = mock_fetch.call_args
        assert kwargs.get("vendor_release_slug") == "imx-6.12.0"
        assert kwargs.get("override_slug") == "imx-xwayland-6.6.52"

    def test_fetch_components_passes_vendor_release_and_override(self, registry_file):
        with patch("sys.argv", [
            "bsp",
            "--registry",
            str(registry_file),
            "fetch",
            "--device",
            "test-device",
            "--release",
            "test-release",
            "--vendor-release",
            "imx-6.12.0",
            "--override",
            "imx-xwayland-6.6.52",
        ]):
            with patch("bsp.BspManager.fetch_by_components") as mock_fetch:
                exit_code = bsp.main()
        assert exit_code == 0
        _, kwargs = mock_fetch.call_args
        assert kwargs.get("vendor_release_slug") == "imx-6.12.0"
        assert kwargs.get("override_slug") == "imx-xwayland-6.6.52"


class TestBspManagerBuildFetchOverrides:
    def test_build_by_components_forwards_vendor_overrides_to_resolver(self, registry_file):
        manager = BspManager(config_path=str(registry_file))
        manager.initialize()
        resolved = MagicMock()
        with patch.object(manager.resolver, "resolve", return_value=resolved) as mock_resolve:
            with patch.object(manager, "_build_resolved") as mock_build_resolved:
                manager.build_by_components(
                    "test-device",
                    "test-release",
                    vendor_release_slug="imx-6.12.0",
                    override_slug="imx-xwayland-6.6.52",
                )
        mock_resolve.assert_called_once_with(
            "test-device",
            "test-release",
            None,
            vendor_release_slug="imx-6.12.0",
            override_slug="imx-xwayland-6.6.52",
        )
        mock_build_resolved.assert_called_once()

    def test_fetch_by_components_forwards_vendor_overrides_to_resolver(self, registry_file):
        manager = BspManager(config_path=str(registry_file))
        manager.initialize()
        resolved = MagicMock()
        with patch.object(manager.resolver, "resolve", return_value=resolved) as mock_resolve:
            with patch.object(manager, "_fetch_resolved") as mock_fetch_resolved:
                manager.fetch_by_components(
                    "test-device",
                    "test-release",
                    vendor_release_slug="imx-6.12.0",
                    override_slug="imx-xwayland-6.6.52",
                )
        mock_resolve.assert_called_once_with(
            "test-device",
            "test-release",
            None,
            vendor_release_slug="imx-6.12.0",
            override_slug="imx-xwayland-6.6.52",
        )
        mock_fetch_resolved.assert_called_once()

    def test_resolve_preset_multi_passes_override_kwargs(self, registry_with_vendor_overrides_file):
        manager = BspManager(config_path=str(registry_with_vendor_overrides_file))
        manager.initialize()
        with patch.object(manager.resolver, "resolve_preset", return_value=(MagicMock(), MagicMock())) as mock_resolve:
            manager._resolve_preset_multi(
                "adv-imx8-scarthgap-imx6.6.53",
                vendor_release_slug="imx-6.12.0",
                override_slug="imx-xwayland-6.6.52",
            )
        mock_resolve.assert_called_once_with(
            "adv-imx8-scarthgap-imx6.6.53",
            extra_feature_slugs=None,
            vendor_release_slug_override="imx-6.12.0",
            override_slug_override="imx-xwayland-6.6.52",
        )


class TestResolverPresetOverridePrecedence:
    def test_resolve_preset_vendor_release_override_takes_precedence(
        self, registry_with_vendor_overrides_file
    ):
        manager = BspManager(config_path=str(registry_with_vendor_overrides_file))
        manager.initialize()
        resolved, _ = manager.resolver.resolve_preset(
            "adv-imx8-scarthgap-imx6.6.53",
            vendor_release_slug_override="imx-6.12.0",
        )
        assert any("imx-6.12.0" in item for item in resolved.kas_files)
        assert not any("imx-6.6.53" in item for item in resolved.kas_files)

    def test_resolve_preset_override_slug_override_takes_precedence(
        self, registry_with_vendor_override_slug_file
    ):
        manager = BspManager(config_path=str(registry_with_vendor_override_slug_file))
        manager.initialize()
        resolved, _ = manager.resolver.resolve_preset(
            "adv-imx8-scarthgap",
            override_slug_override="imx-xwayland-6.6.52",
        )
        assert resolved.effective_distro == "fsl-imx-xwayland"
        assert any("imx-xwayland-6.6.52" in item for item in resolved.kas_files)
