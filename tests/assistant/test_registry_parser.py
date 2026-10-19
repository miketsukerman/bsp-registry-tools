"""
Unit tests for BSP Assistant RegistryParser.
"""

from pathlib import Path

import pytest

from bsp.assistant.cache import RegistryCache
from bsp.assistant.registry_parser import RegistryParser


class TestRegistryParser:
    def test_load_minimal_registry(self, minimal_registry_path: Path):
        parser = RegistryParser(str(minimal_registry_path))
        data = parser.registry_data
        assert len(data.presets) == 1
        assert len(data.devices) == 1
        assert len(data.features) == 2
        assert len(data.releases) == 1
        assert data.presets[0].name == "test-preset-a"

    def test_load_complex_registry(self, complex_parser: RegistryParser):
        data = complex_parser.registry_data
        assert len(data.devices) == 3
        assert len(data.features) == 5
        assert len(data.presets) == 4
        assert "scarthgap" in data.releases

    def test_extract_presets(self, complex_parser: RegistryParser):
        presets = complex_parser.extract_presets()
        preset_names = [p.name for p in presets]
        assert "modular-bsp-rauc-rsb3720" in preset_names
        assert "modular-bsp-rsb3720-kirkstone" in preset_names

        rauc_preset = next(p for p in presets if p.name == "modular-bsp-rauc-rsb3720")
        assert "scarthgap" in rauc_preset.releases
        assert "wrynose" in rauc_preset.releases
        assert "rauc" in rauc_preset.features
        assert "secure-boot" in rauc_preset.features

    def test_extract_devices(self, complex_parser: RegistryParser):
        devices = complex_parser.extract_devices()
        dev_map = {d.slug: d for d in devices}
        assert "rsb3720" in dev_map
        assert dev_map["rsb3720"].vendor == "nxp"
        assert dev_map["rsb3720"].soc_family == "imx8mp"
        assert "rauc" in dev_map["rsb3720"].supported_features

    def test_extract_features(self, complex_parser: RegistryParser):
        features = complex_parser.extract_features()
        feat_map = {f.slug: f for f in features}
        assert "rauc" in feat_map
        assert feat_map["rauc"].compatible_vendors == ["nxp"]
        assert "scarthgap" in feat_map["rauc"].compatible_releases

    def test_compatibility_matrix(self, complex_parser: RegistryParser):
        matrix = complex_parser.get_compatibility_matrix()
        assert "rauc" in matrix.device_features["rsb3720"]
        assert "nxp" in matrix.feature_vendors["rauc"]
        assert "scarthgap" in matrix.feature_releases["rauc"]

    def test_find_presets_by_device(self, complex_parser: RegistryParser):
        rsb_presets = complex_parser.find_presets_by_device("rsb3720")
        assert len(rsb_presets) >= 2
        names = [p.name for p in rsb_presets]
        assert "modular-bsp-rauc-rsb3720" in names
        assert "modular-bsp-rsb3720-kirkstone" in names

        # Test case insensitive and normalized matching
        rsb_presets_upper = complex_parser.find_presets_by_device("RSB-3720")
        assert len(rsb_presets_upper) == len(rsb_presets)

    def test_find_presets_by_feature(self, complex_parser: RegistryParser):
        rauc_presets = complex_parser.find_presets_by_feature("rauc")
        assert len(rauc_presets) == 1
        assert rauc_presets[0].name == "modular-bsp-rauc-rsb3720"

        systemd_presets = complex_parser.find_presets_by_feature("systemd")
        assert len(systemd_presets) == 4

    def test_caching_behavior(self, minimal_registry_path: Path):
        cache = RegistryCache()
        p1 = RegistryParser(str(minimal_registry_path), cache=cache)
        d1 = p1.registry_data

        p2 = RegistryParser(str(minimal_registry_path), cache=cache)
        d2 = p2.registry_data

        assert d1 is d2
        assert cache.has(str(minimal_registry_path))
        cache.clear()
        assert not cache.has(str(minimal_registry_path))

    def test_file_not_found(self):
        parser = RegistryParser()
        with pytest.raises(FileNotFoundError):
            parser.load_registry("/non/existent/path/bsp-registry.yaml")

    def test_unloaded_registry_data_raises_value_error(self):
        parser = RegistryParser()
        with pytest.raises(ValueError):
            _ = parser.registry_data
