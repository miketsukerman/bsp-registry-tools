"""
Unit and integration tests for BSP Assistant CLI commands.
"""

from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rich.console import Console

from bsp.assistant.cli import AssistantCLI, dispatch_assistant, parse_feature_list
from bsp.assistant.recommendation_engine import RecommendationEngine


@pytest.fixture
def test_console():
    return Console(file=StringIO(), force_terminal=False, color_system=None, width=120)


class TestAssistantCLI:
    def test_list_devices(self, complex_engine: RecommendationEngine, test_console: Console):
        cli = AssistantCLI(complex_engine, console=test_console)
        ret = cli.list_devices()
        assert ret == 0
        output = test_console.file.getvalue()
        assert "Available Hardware Devices" in output
        assert "rsb3720" in output
        assert "rom5720" in output
        assert "qemuarm64" in output

    def test_list_features(self, complex_engine: RecommendationEngine, test_console: Console):
        cli = AssistantCLI(complex_engine, console=test_console)
        ret = cli.list_features()
        assert ret == 0
        output = test_console.file.getvalue()
        assert "Available Features" in output
        assert "rauc" in output
        assert "secure-boot" in output
        assert "systemd" in output

    def test_explain_existing_preset(self, complex_engine: RecommendationEngine, test_console: Console):
        cli = AssistantCLI(complex_engine, console=test_console)
        ret = cli.explain("modular-bsp-rauc-rsb3720")
        assert ret == 0
        output = test_console.file.getvalue()
        assert "Preset Explanation: modular-bsp-rauc-rsb3720" in output
        assert "Target Device:" in output
        assert "Risk Level:" in output

    def test_explain_missing_preset(self, complex_engine: RecommendationEngine, test_console: Console):
        cli = AssistantCLI(complex_engine, console=test_console)
        ret = cli.explain("nonexistent-preset")
        assert ret == 1
        output = test_console.file.getvalue()
        assert "not found" in output

    def test_check_compatible(self, complex_engine: RecommendationEngine, test_console: Console):
        cli = AssistantCLI(complex_engine, console=test_console)
        ret = cli.check("rsb3720", ["rauc", "secure-boot"])
        assert ret == 0
        output = test_console.file.getvalue()
        assert "Compatibility Check: rsb3720" in output
        assert "Fully Compatible" in output

    def test_check_incompatible(self, complex_engine: RecommendationEngine, test_console: Console):
        cli = AssistantCLI(complex_engine, console=test_console)
        ret = cli.check("qemuarm64", ["secure-boot"])
        assert ret == 1
        output = test_console.file.getvalue()
        assert "Compatibility Issues Detected" in output

    def test_find_feature(self, complex_engine: RecommendationEngine, test_console: Console):
        cli = AssistantCLI(complex_engine, console=test_console)
        ret = cli.find("rauc")
        assert ret == 0
        output = test_console.file.getvalue()
        assert "Presets providing feature 'rauc':" in output
        assert "modular-bsp-rauc-rsb3720" in output

    def test_suggest_preset(self, complex_engine: RecommendationEngine, test_console: Console):
        cli = AssistantCLI(complex_engine, console=test_console)
        ret = cli.suggest("rsb3720")
        assert ret == 0
        output = test_console.file.getvalue()
        assert "Recommended Preset:" in output
        assert "rsb3720" in output.lower()

    def test_discover_non_interactive(self, complex_engine: RecommendationEngine, test_console: Console):
        cli = AssistantCLI(complex_engine, console=test_console)
        ret = cli.discover_interactive(
            board="rsb3720",
            features=["rauc"],
            release_pref="latest",
            non_interactive=True,
        )
        assert ret == 0
        output = test_console.file.getvalue()
        assert "Recommended Preset:" in output
        assert "Why this choice?" in output
        assert "Ready to build?" in output

    def test_parse_feature_list(self):
        assert parse_feature_list(None) == []
        assert parse_feature_list("") == []
        assert parse_feature_list("f1, f2") == ["f1", "f2"]
        assert parse_feature_list(["f1,f2", "f3"]) == ["f1", "f2", "f3"]

    def test_dispatch_assistant(self, complex_parser):
        mgr = SimpleNamespace(registry=complex_parser._raw_root)
        args = SimpleNamespace(
            assistant_command="list-devices",
        )
        ret = dispatch_assistant(args, mgr)
        assert ret == 0

        args_check = SimpleNamespace(
            assistant_command="check",
            board="rsb3720",
            features=["rauc"],
            release=None,
        )
        assert dispatch_assistant(args_check, mgr) == 0

        args_find = SimpleNamespace(
            assistant_command="find",
            feature="rauc",
            release=None,
        )
        assert dispatch_assistant(args_find, mgr) == 0

        args_suggest = SimpleNamespace(
            assistant_command="suggest",
            board="rsb3720",
        )
        assert dispatch_assistant(args_suggest, mgr) == 0
