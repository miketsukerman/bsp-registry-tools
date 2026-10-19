"""
Unit tests for BSP Assistant RecommendationEngine.
"""

import concurrent.futures

import pytest

from bsp.assistant.models import UserRequirements
from bsp.assistant.recommendation_engine import RecommendationEngine


class TestRecommendationEngine:
    def test_recommend_exact_match(self, complex_engine: RecommendationEngine):
        reqs = UserRequirements(
            board_name="rsb3720",
            features=["rauc", "secure-boot"],
            release_preference="latest",
        )
        rec = complex_engine.recommend(reqs)

        assert "modular-bsp-rauc-rsb3720" in rec.preset_name
        assert "wrynose" in rec.preset_name
        assert rec.confidence_score >= 80.0
        assert len(rec.reasoning) >= 2
        assert any("rsb3720" in r.lower() or "rsb-3720" in r.lower() for r in rec.reasoning)

    def test_recommend_lts_preference(self, complex_engine: RecommendationEngine):
        reqs = UserRequirements(
            board_name="rsb3720",
            features=["rauc"],
            release_preference="lts",
        )
        rec = complex_engine.recommend(reqs)

        assert "modular-bsp-rauc-rsb3720" in rec.preset_name
        assert "scarthgap" in rec.preset_name
        assert rec.confidence_score >= 80.0

    def test_score_preset(self, complex_engine: RecommendationEngine):
        preset = complex_engine.registry_data.presets[0]  # modular-bsp-rauc-rsb3720
        reqs = UserRequirements(
            board_name="rsb3720",
            features=["rauc", "secure-boot"],
            release_preference="lts",
        )
        score = complex_engine.score_preset(preset, reqs)
        assert score > 70.0

    def test_check_compatibility_device_success(self, complex_engine: RecommendationEngine):
        report = complex_engine.check_compatibility("rsb3720", ["rauc", "secure-boot", "systemd"])
        assert report.compatible is True
        assert len(report.supported_features) == 3
        assert len(report.unsupported_features) == 0

    def test_check_compatibility_device_incompatible(self, complex_engine: RecommendationEngine):
        report = complex_engine.check_compatibility("qemuarm64", ["secure-boot"])
        assert report.compatible is False
        assert "secure-boot" in report.unsupported_features

    def test_check_compatibility_preset_success(self, complex_engine: RecommendationEngine):
        report = complex_engine.check_compatibility("modular-bsp-rauc-rsb3720", ["rauc"])
        assert report.compatible is True
        assert "rauc" in report.supported_features

    def test_find_alternatives(self, complex_engine: RecommendationEngine):
        alts = complex_engine.find_alternatives("modular-bsp-rauc-rsb3720", limit=3)
        assert isinstance(alts, list)
        assert "modular-bsp-rauc-rsb3720" not in alts

    def test_risk_assessment_stable(self, complex_engine: RecommendationEngine):
        risk = complex_engine.get_risk_assessment("modular-bsp-rauc-rsb3720")
        assert risk.risk_level == "low"
        assert risk.is_eol is False

    def test_risk_assessment_eol(self, complex_engine: RecommendationEngine):
        risk = complex_engine.get_risk_assessment("modular-bsp-rsb3720-kirkstone")
        assert risk.risk_level in ("medium", "high")
        assert risk.is_eol is True
        assert any("end-of-life" in w.lower() or "eol" in w.lower() for w in risk.warnings)

    def test_risk_assessment_dev_release(self, complex_engine: RecommendationEngine):
        risk = complex_engine.get_risk_assessment("poky-qemuarm64-master")
        assert any("development" in w.lower() or "bleeding-edge" in w.lower() for w in risk.warnings)

    def test_recommend_empty_presets_fallback(self):
        from bsp.assistant.models import RegistryData
        empty_data = RegistryData(
            presets=[],
            devices=[],
            features=[],
            releases=[],
        )
        engine = RecommendationEngine(empty_data)
        reqs = UserRequirements(board_name="unknown", features=[], release_preference="latest")
        rec = engine.recommend(reqs)
        assert rec.preset_name == "none-available"
        assert rec.confidence_score == 0.0

    def test_thread_safety_concurrent_recommendations(self, complex_engine: RecommendationEngine):
        """Verify thread-safety when handling multiple concurrent recommendation requests."""
        def make_request(board: str, feature: str):
            reqs = UserRequirements(
                board_name=board,
                features=[feature],
                release_preference="latest",
            )
            return complex_engine.recommend(reqs)

        tasks = [
            ("rsb3720", "rauc"),
            ("rom5720", "swupdate"),
            ("qemuarm64", "debug"),
            ("rsb3720", "systemd"),
        ] * 10

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(make_request, b, f) for b, f in tasks]
            results = [f.result() for f in futures]

        assert len(results) == 40
        for r in results:
            assert r.preset_name is not None
            assert r.confidence_score > 0
