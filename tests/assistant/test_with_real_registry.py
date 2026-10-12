"""
End-to-end integration tests using the repository's real example BSP registry.
"""

from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from bsp.assistant.cli import AssistantCLI, dispatch_assistant
from bsp.assistant.models import UserRequirements
from bsp.assistant.recommendation_engine import RecommendationEngine
from bsp.assistant.registry_parser import RegistryParser


@pytest.fixture
def real_registry_path() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    registry_file = repo_root / "examples" / "bsp-registry.yaml"
    assert registry_file.exists(), f"Real registry not found at {registry_file}"
    return registry_file


@pytest.fixture
def real_registry_data(real_registry_path: Path):
    parser = RegistryParser(str(real_registry_path))
    return parser.registry_data


@pytest.fixture
def real_engine(real_registry_data):
    return RecommendationEngine(real_registry_data)


class TestRealRegistryIntegration:
    def test_parse_real_registry(self, real_registry_data):
        assert len(real_registry_data.devices) > 0
        assert len(real_registry_data.features) > 0
        assert len(real_registry_data.presets) > 0

        # Verify QEMU devices are loaded from includes
        slugs = [d.slug for d in real_registry_data.devices]
        assert "qemuarm64" in slugs
        assert "qemuamd64" in slugs

    def test_recommendation_with_real_registry(self, real_engine: RecommendationEngine):
        reqs = UserRequirements(
            board_name="qemuarm64",
            features=["systemd"],
            release_preference="latest",
        )
        rec = real_engine.recommend(reqs)

        assert rec.preset_name is not None
        assert "qemuarm64" in rec.preset_name.lower()
        assert rec.confidence_score > 70.0
        assert len(rec.reasoning) > 0

    def test_cli_integration_with_real_registry(self, real_engine: RecommendationEngine):
        console = Console(file=StringIO(), force_terminal=True, color_system=None)
        cli = AssistantCLI(real_engine, console=console)

        ret_devs = cli.list_devices()
        assert ret_devs == 0
        out_devs = console.file.getvalue()
        assert "qemuarm64" in out_devs

        ret_feats = cli.list_features()
        assert ret_feats == 0
        out_feats = console.file.getvalue()
        assert "systemd" in out_feats

        ret_check = cli.check("qemuarm64", ["systemd"])
        assert ret_check == 0

    def test_bsp_cli_dispatch_with_real_registry(self, real_registry_path: Path):
        from bsp.bsp_manager import BspManager

        mgr = BspManager(str(real_registry_path), verbose=False)
        mgr.initialize()

        args = SimpleNamespace(
            assistant_command="discover",
            board="qemuarm64",
            features=["systemd"],
            release_pref="latest",
            release=None,
            non_interactive=True,
        )

        ret = dispatch_assistant(args, mgr)
        assert ret == 0
