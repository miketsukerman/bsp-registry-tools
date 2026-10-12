"""
Unit tests for BSP Assistant data models.
"""

import pytest

from bsp.assistant.models import (
    CompatibilityMatrix,
    CompatibilityReport,
    DeviceInfo,
    FeatureInfo,
    PresetInfo,
    Recommendation,
    RegistryData,
    RiskAssessment,
    UserRequirements,
)


class TestPresetInfo:
    def test_creation_and_attributes(self):
        preset = PresetInfo(
            name="test-preset",
            description="Test description",
            device="rsb3720",
            releases=["scarthgap", "wrynose"],
            features=["rauc", "secure-boot"],
            stability_status="stable",
            tested_combinations=True,
        )
        assert preset.name == "test-preset"
        assert preset.description == "Test description"
        assert preset.device == "rsb3720"
        assert preset.releases == ["scarthgap", "wrynose"]
        assert preset.features == ["rauc", "secure-boot"]
        assert preset.stability_status == "stable"
        assert preset.tested_combinations is True

    def test_serialization(self):
        preset = PresetInfo(
            name="test-preset",
            description="Test",
            device="rsb3720",
            releases=["scarthgap"],
            features=["rauc"],
            stability_status="development",
            tested_combinations=False,
        )
        d = preset.to_dict()
        assert d["name"] == "test-preset"
        assert d["stability_status"] == "development"
        assert d["tested_combinations"] is False

        restored = PresetInfo.from_dict(d)
        assert restored.name == preset.name
        assert restored.releases == preset.releases
        assert restored.features == preset.features


class TestDeviceInfo:
    def test_creation_and_serialization(self):
        device = DeviceInfo(
            slug="rsb3720",
            name="Advantech RSB-3720",
            vendor="nxp",
            soc_family="imx8mp",
            supported_features=["rauc", "secure-boot"],
        )
        assert device.slug == "rsb3720"
        assert device.vendor == "nxp"
        assert device.soc_family == "imx8mp"
        assert "rauc" in device.supported_features

        d = device.to_dict()
        restored = DeviceInfo.from_dict(d)
        assert restored.slug == device.slug
        assert restored.supported_features == device.supported_features


class TestFeatureInfo:
    def test_creation_and_serialization(self):
        feat = FeatureInfo(
            slug="rauc",
            name="RAUC OTA",
            description="Robust Auto-Update Controller",
            compatible_vendors=["nxp", "qemu"],
            compatible_releases=["scarthgap", "wrynose"],
        )
        assert feat.slug == "rauc"
        assert "nxp" in feat.compatible_vendors
        assert "scarthgap" in feat.compatible_releases

        d = feat.to_dict()
        restored = FeatureInfo.from_dict(d)
        assert restored.slug == feat.slug
        assert restored.compatible_vendors == feat.compatible_vendors


class TestUserRequirements:
    def test_defaults_and_options(self):
        req = UserRequirements(
            board_name="rsb3720",
            features=["rauc"],
            release_preference="latest",
        )
        assert req.board_name == "rsb3720"
        assert req.features == ["rauc"]
        assert req.release_preference == "latest"
        assert req.release_version is None

        d = req.to_dict()
        restored = UserRequirements.from_dict(d)
        assert restored.board_name == "rsb3720"
        assert restored.release_preference == "latest"


class TestRecommendation:
    def test_creation_and_serialization(self):
        rec = Recommendation(
            preset_name="modular-bsp-rauc-rsb3720-wrynose",
            confidence_score=95.0,
            reasoning=["Board matches", "Features match"],
            alternatives=["modular-bsp-rauc-rsb3720-scarthgap"],
            known_issues=[],
            build_time_estimate="2-3 hours",
        )
        assert rec.preset_name == "modular-bsp-rauc-rsb3720-wrynose"
        assert rec.confidence_score == 95.0
        assert len(rec.reasoning) == 2
        assert len(rec.alternatives) == 1
        assert rec.build_time_estimate == "2-3 hours"

        d = rec.to_dict()
        restored = Recommendation.from_dict(d)
        assert restored.confidence_score == 95.0
        assert restored.preset_name == rec.preset_name


class TestRiskAssessment:
    def test_creation_and_serialization(self):
        risk = RiskAssessment(
            preset_name="test-preset",
            risk_level="high",
            warnings=["EOL release", "Untested combination"],
            is_tested=False,
            is_eol=True,
        )
        assert risk.risk_level == "high"
        assert risk.is_eol is True
        assert len(risk.warnings) == 2

        d = risk.to_dict()
        restored = RiskAssessment.from_dict(d)
        assert restored.risk_level == "high"
        assert restored.is_eol is True


class TestCompatibilityReport:
    def test_creation_and_serialization(self):
        report = CompatibilityReport(
            preset_or_board="rsb3720",
            compatible=True,
            supported_features=["rauc", "systemd"],
            unsupported_features=[],
            reasons=["All features supported"],
        )
        assert report.compatible is True
        assert len(report.supported_features) == 2

        d = report.to_dict()
        restored = CompatibilityReport.from_dict(d)
        assert restored.compatible is True
        assert restored.supported_features == ["rauc", "systemd"]


class TestCompatibilityMatrix:
    def test_creation(self):
        matrix = CompatibilityMatrix(
            device_features={"rsb3720": ["rauc", "systemd"]},
            feature_vendors={"rauc": ["nxp"]},
            feature_releases={"rauc": ["scarthgap"]},
        )
        assert "rauc" in matrix.device_features["rsb3720"]
        assert matrix.feature_vendors["rauc"] == ["nxp"]
