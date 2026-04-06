"""
Tests for the GUI module (bsp/gui.py).

These tests cover:
- TEXTUAL_AVAILABLE flag
- launch_gui() graceful failure when textual is missing
- BspLauncherApp composition and key functionality (using textual's test pilot)
- CLI `--gui` flag and `bsp gui` subcommand routing
"""

from unittest.mock import MagicMock, patch
import sys
from pathlib import Path

import pytest

import bsp
from bsp.gui import TEXTUAL_AVAILABLE, launch_gui


# =============================================================================
# Availability guard
# =============================================================================

class TestTextualAvailability:
    def test_textual_available_is_bool(self):
        assert isinstance(TEXTUAL_AVAILABLE, bool)

    def test_launch_gui_exported_from_package(self):
        assert hasattr(bsp, "launch_gui")
        assert callable(bsp.launch_gui)

    def test_textual_available_exported_from_package(self):
        assert hasattr(bsp, "TEXTUAL_AVAILABLE")


# =============================================================================
# launch_gui() when textual is absent
# =============================================================================

class TestLaunchGuiWithoutTextual:
    def test_returns_error_code_when_textual_missing(self, capsys):
        """launch_gui() must return 1 and print an error if textual is missing."""
        with patch("bsp.gui.TEXTUAL_AVAILABLE", False):
            rc = launch_gui()
        assert rc == 1
        captured = capsys.readouterr()
        assert "textual" in captured.err.lower()

    def test_error_message_mentions_install_hint(self, capsys):
        with patch("bsp.gui.TEXTUAL_AVAILABLE", False):
            launch_gui()
        captured = capsys.readouterr()
        assert "pip install" in captured.err


# =============================================================================
# launch_gui() when textual IS available
# =============================================================================

@pytest.mark.skipif(not TEXTUAL_AVAILABLE, reason="textual not installed")
class TestLaunchGuiWithTextual:
    def test_launch_gui_returns_zero_on_success(self):
        """launch_gui() should run the app and return 0."""
        mock_app = MagicMock()
        mock_app_instance = MagicMock()
        mock_app.return_value = mock_app_instance

        with patch("bsp.gui.BspLauncherApp", mock_app):
            rc = launch_gui(registry_path="/nonexistent/path.yaml")

        assert rc == 0
        mock_app_instance.run.assert_called_once()

    def test_launch_gui_passes_registry_path(self):
        """registry_path kwarg is forwarded to BspLauncherApp."""
        mock_app = MagicMock()
        mock_app.return_value = MagicMock()

        with patch("bsp.gui.BspLauncherApp", mock_app):
            launch_gui(registry_path="/some/path.yaml")

        _, kwargs = mock_app.call_args
        assert kwargs.get("registry_path") == "/some/path.yaml"

    def test_launch_gui_passes_remote(self):
        mock_app = MagicMock()
        mock_app.return_value = MagicMock()

        with patch("bsp.gui.BspLauncherApp", mock_app):
            launch_gui(remote="https://example.com/registry.git")

        _, kwargs = mock_app.call_args
        assert kwargs.get("remote") == "https://example.com/registry.git"

    def test_launch_gui_passes_no_update(self):
        mock_app = MagicMock()
        mock_app.return_value = MagicMock()

        with patch("bsp.gui.BspLauncherApp", mock_app):
            launch_gui(no_update=True)

        _, kwargs = mock_app.call_args
        assert kwargs.get("no_update") is True


# =============================================================================
# BspLauncherApp unit tests (textual pilot)
# =============================================================================

@pytest.mark.skipif(not TEXTUAL_AVAILABLE, reason="textual not installed")
class TestBspLauncherAppComposition:
    """
    Validate that the app composes without errors and key widgets are present.
    Uses textual's async test pilot.
    """

    async def test_app_composes(self, registry_file):
        from bsp.gui import BspLauncherApp

        app = BspLauncherApp(registry_path=str(registry_file))
        async with app.run_test(headless=True) as _:
            # Basic widget presence
            assert app.query_one("#bsp-tree") is not None
            assert app.query_one("#output-log") is not None
            assert app.query_one("#btn-build") is not None
            assert app.query_one("#btn-refresh") is not None
            # Right panel split into BSP detail and env panes
            assert app.query_one("#detail-view") is not None
            assert app.query_one("#env-view") is not None
            # Containers button must be gone
            assert len(app.query("#btn-containers")) == 0

    async def test_build_button_disabled_by_default(self, registry_file):
        from bsp.gui import BspLauncherApp
        from textual.widgets import Button

        app = BspLauncherApp(registry_path=str(registry_file))
        async with app.run_test(headless=True) as _:
            btn = app.query_one("#btn-build", Button)
            assert btn.disabled is True

    async def test_refresh_button_exists(self, registry_file):
        from bsp.gui import BspLauncherApp
        from textual.widgets import Button

        app = BspLauncherApp(registry_path=str(registry_file))
        async with app.run_test(headless=True) as _:
            btn = app.query_one("#btn-refresh", Button)
            assert btn.disabled is False

    async def test_cancel_button_disabled_by_default(self, registry_file):
        from bsp.gui import BspLauncherApp
        from textual.widgets import Button

        app = BspLauncherApp(registry_path=str(registry_file))
        async with app.run_test(headless=True) as _:
            btn = app.query_one("#btn-cancel", Button)
            assert btn.disabled is True

    async def test_flash_button_disabled_by_default(self, registry_file):
        from bsp.gui import BspLauncherApp
        from textual.widgets import Button

        app = BspLauncherApp(registry_path=str(registry_file))
        async with app.run_test(headless=True) as _:
            btn = app.query_one("#btn-flash", Button)
            assert btn.disabled is True


# =============================================================================
# BSP tree filter tests
# =============================================================================

@pytest.mark.skipif(not TEXTUAL_AVAILABLE, reason="textual not installed")
class TestBspTreeFilter:
    """Unit tests for _render_tree filter logic (no full UI needed)."""

    def _make_app_with_tree_data(self):
        """Return a BspLauncherApp whose _tree_data is pre-populated."""
        from bsp.gui import BspLauncherApp
        from textual.widgets import Tree

        app = BspLauncherApp.__new__(BspLauncherApp)

        # Minimal _tree_data mimicking the real structure:
        #   vendor: Acme (acme)
        #     device: board-a (description "Board Alpha")
        #       release: scarthgap → preset "acme-board-a-scarthgap"
        #       release: styhead  → preset "acme-board-a-styhead"
        #     device: board-b (description "Board Beta")
        #       release: scarthgap → preset "acme-board-b-scarthgap"
        #   no-vendor device: generic
        #       release: "" → preset "generic-preset"

        app._tree_data = {
            "vendor_names": {"acme": "Acme Corp"},
            "device_display": lambda slug: {
                "board-a": "Board Alpha",
                "board-b": "Board Beta",
                "generic": "generic",
            }.get(slug, slug),
            "release_labels": {
                "scarthgap": "Scarthgap",
                "styhead": "Styhead",
            },
            "vendor_device_release": {
                "acme": {
                    "board-a": {
                        "scarthgap": ["acme-board-a-scarthgap"],
                        "styhead": ["acme-board-a-styhead"],
                    },
                    "board-b": {
                        "scarthgap": ["acme-board-b-scarthgap"],
                    },
                },
            },
            "no_vendor_presets": {
                "generic": {"": ["generic-preset"]},
            },
        }

        # Provide a minimal tree widget backed by a real Tree instance,
        # using a dict-based query_one stub.
        _tree = Tree("root")
        app.query_one = lambda selector, widget_type=None: _tree

        return app

    def test_no_filter_returns_all_presets(self):
        app = self._make_app_with_tree_data()
        count = app._render_tree("")
        assert count == 4  # 3 acme + 1 generic

    def test_filter_by_vendor_name(self):
        app = self._make_app_with_tree_data()
        count = app._render_tree("Acme")
        assert count == 3  # all acme presets, no generic

    def test_filter_by_device_label(self):
        app = self._make_app_with_tree_data()
        count = app._render_tree("Alpha")
        assert count == 2  # board-a has 2 releases

    def test_filter_by_release_label(self):
        app = self._make_app_with_tree_data()
        count = app._render_tree("Styhead")
        assert count == 1  # only acme-board-a-styhead

    def test_filter_is_case_insensitive(self):
        app = self._make_app_with_tree_data()
        assert app._render_tree("styhead") == app._render_tree("Styhead")

    def test_filter_with_multiple_tokens(self):
        app = self._make_app_with_tree_data()
        count = app._render_tree("Acme Scarthgap")
        assert count == 2  # board-a-scarthgap + board-b-scarthgap

    def test_filter_matching_nothing_returns_zero(self):
        app = self._make_app_with_tree_data()
        count = app._render_tree("xyzzy")
        assert count == 0

    def test_filter_clears_tree_data_none(self):
        from bsp.gui import BspLauncherApp
        app = BspLauncherApp.__new__(BspLauncherApp)
        app._tree_data = None
        assert app._render_tree("anything") == 0


# =============================================================================
# Filter widget present in composed app
# =============================================================================

@pytest.mark.skipif(not TEXTUAL_AVAILABLE, reason="textual not installed")
class TestFilterWidgetPresence:
    """Verify that the filter Input is composed into the left panel."""

    async def test_filter_input_present(self, registry_file):
        from bsp.gui import BspLauncherApp
        from textual.widgets import Input

        app = BspLauncherApp(registry_path=str(registry_file))
        async with app.run_test(headless=True) as _:
            widget = app.query_one("#filter-input", Input)
            assert widget is not None

    async def test_filter_input_starts_empty(self, registry_file):
        from bsp.gui import BspLauncherApp
        from textual.widgets import Input

        app = BspLauncherApp(registry_path=str(registry_file))
        async with app.run_test(headless=True) as _:
            widget = app.query_one("#filter-input", Input)
            assert widget.value == ""


# =============================================================================
# BuildTargetScreen tests
# =============================================================================

@pytest.mark.skipif(not TEXTUAL_AVAILABLE, reason="textual not installed")
class TestBuildTargetScreen:
    """Tests for the BuildTargetScreen dialog."""

    async def test_build_target_screen_compose(self, registry_file):
        """BuildTargetScreen can be pushed and contains expected widgets."""
        from bsp.gui import BspLauncherApp, BuildTargetScreen
        from textual.widgets import Button, Checkbox

        pushed_screen = BuildTargetScreen("my-bsp", "/tmp/build")
        app = BspLauncherApp(registry_path=str(registry_file))
        async with app.run_test(headless=True) as pilot:
            await app.push_screen(pushed_screen)
            await pilot.pause(0.1)
            # After pushing, app.screen IS the modal screen
            modal = app.screen
            assert modal.query_one("#build-confirm", Button) is not None
            assert modal.query_one("#build-cancel", Button) is not None
            assert modal.query_one("#opt-clean", Checkbox) is not None
            assert modal.query_one("#opt-checkout-only", Checkbox) is not None

    async def test_build_target_cancel_dismisses_none(self, registry_file):
        """Pressing Cancel dismisses the BuildTargetScreen with None."""
        from bsp.gui import BspLauncherApp, BuildTargetScreen
        from textual.widgets import Button

        results = []
        app = BspLauncherApp(registry_path=str(registry_file))

        async with app.run_test(headless=True) as pilot:
            await app.push_screen(
                BuildTargetScreen("my-bsp", "/tmp/build"),
                lambda v: results.append(v),
            )
            await pilot.pause()
            await pilot.click("#build-cancel")
            await pilot.pause()

        assert results == [None]

    async def test_build_target_confirm_returns_options(self, registry_file):
        """Pressing Build dismisses with a dict containing build options."""
        from bsp.gui import BspLauncherApp, BuildTargetScreen

        results = []
        app = BspLauncherApp(registry_path=str(registry_file))

        async with app.run_test(headless=True) as pilot:
            await app.push_screen(
                BuildTargetScreen("my-bsp", "/tmp/build"),
                lambda v: results.append(v),
            )
            await pilot.pause()
            await pilot.click("#build-confirm")
            await pilot.pause()

        assert len(results) == 1
        assert isinstance(results[0], dict)
        assert "clean" in results[0]
        assert "checkout_only" in results[0]


# =============================================================================
# _resolve_build_path tests
# =============================================================================

@pytest.mark.skipif(not TEXTUAL_AVAILABLE, reason="textual not installed")
class TestResolveBuildPath:
    """Tests for BspLauncherApp._resolve_build_path."""

    def test_returns_empty_string_without_manager(self, registry_file):
        from bsp.gui import BspLauncherApp

        app = BspLauncherApp(registry_path=str(registry_file))
        # _bsp_manager is None before mount
        result = app._resolve_build_path("any-bsp")
        assert result == ""


# =============================================================================
# Build log file saving
# =============================================================================

class TestStreamCommandLogFile:
    """Tests for the log-file-writing behaviour of _stream_command."""

    def _make_minimal_app(self, tmp_path):
        """Return a BspLauncherApp instance with all UI calls stubbed out."""
        from bsp.gui import BspLauncherApp

        app = BspLauncherApp.__new__(BspLauncherApp)
        app._running_process = None
        app._log_calls = []
        app._status_calls = []

        def fake_call_from_thread(fn, *a, **kw):
            fn(*a, **kw)

        app.call_from_thread = fake_call_from_thread
        app._log = lambda msg: app._log_calls.append(msg)
        app._set_status = lambda msg: app._status_calls.append(msg)
        app._set_cancel_button = lambda *, disabled: None
        return app

    def test_log_file_created_on_successful_command(self, tmp_path):
        """A successful command writes output to the log file."""
        if not TEXTUAL_AVAILABLE:
            pytest.skip("textual not installed")

        log_path = str(tmp_path / "build.log")
        app = self._make_minimal_app(tmp_path)

        cmd = [sys.executable, "-c", "print('hello from build')"]
        app._stream_command(cmd, log_file=log_path)

        assert Path(log_path).exists()
        content = Path(log_path).read_text()
        assert "hello from build" in content

    def test_completion_logged_to_output_log(self, tmp_path):
        """A success message is written to the output log on exit code 0."""
        if not TEXTUAL_AVAILABLE:
            pytest.skip("textual not installed")

        app = self._make_minimal_app(tmp_path)
        cmd = [sys.executable, "-c", ""]
        app._stream_command(cmd)

        assert any("success" in m.lower() for m in app._log_calls)

    def test_failure_logged_to_output_log(self, tmp_path):
        """A failure message is written to the output log on non-zero exit."""
        if not TEXTUAL_AVAILABLE:
            pytest.skip("textual not installed")

        app = self._make_minimal_app(tmp_path)
        cmd = [sys.executable, "-c", "raise SystemExit(1)"]
        app._stream_command(cmd)

        assert any("exited with code" in m.lower() for m in app._log_calls)


# =============================================================================
# _list_removable_drives utility
# =============================================================================

class TestListRemovableDrives:
    """Tests for the _list_removable_drives() helper."""

    def test_returns_list(self):
        from bsp.gui import _list_removable_drives
        result = _list_removable_drives()
        assert isinstance(result, list)

    def test_each_entry_is_two_tuple(self):
        from bsp.gui import _list_removable_drives
        for item in _list_removable_drives():
            assert isinstance(item, tuple) and len(item) == 2

    def test_each_path_starts_with_dev(self):
        from bsp.gui import _list_removable_drives
        for path, _label in _list_removable_drives():
            assert path.startswith("/dev/"), f"Unexpected path: {path}"

    def test_empty_list_on_unknown_platform(self, monkeypatch):
        import bsp.gui as gui_mod
        monkeypatch.setattr(gui_mod.platform, "system", lambda: "Windows")
        result = gui_mod._list_removable_drives()
        assert result == []

    def test_linux_removable_drive_parsed(self, tmp_path, monkeypatch):
        """Linux sysfs parsing includes drives marked removable=1 with model/size."""
        import bsp.gui as gui_mod

        # Build a minimal /sys/block/sda structure
        sda = tmp_path / "sda"
        sda.mkdir()
        (sda / "removable").write_text("1\n")
        # 2 097 152 sectors × 512 B = 1 GiB
        (sda / "size").write_text("2097152\n")
        (sda / "device").mkdir()
        (sda / "device" / "model").write_text("FakeUSBDrive\n")

        monkeypatch.setattr(gui_mod.platform, "system", lambda: "Linux")
        # Redirect the /sys/block path lookup to tmp_path
        monkeypatch.setattr(gui_mod, "Path", lambda p: tmp_path if p == "/sys/block" else __builtins__["__import__"]("pathlib").Path(p))

        result = gui_mod._list_removable_drives()
        assert len(result) == 1
        path, label = result[0]
        assert path == "/dev/sda"
        assert "FakeUSBDrive" in label
        assert "GB" in label or "MB" in label


# =============================================================================
# CLI routing: --gui flag and 'bsp gui' subcommand
# =============================================================================

class TestCliGuiRouting:
    """Ensure CLI correctly routes --gui flag and 'bsp gui' to launch_gui()."""

    def _mock_launch(self, *args, **kwargs):
        """Callable that records calls and returns 0."""
        self._called_with = kwargs
        return 0

    def test_bsp_gui_subcommand(self, registry_file):
        """``bsp gui`` should invoke launch_gui."""
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "gui"]):
            with patch("bsp.gui.launch_gui", return_value=0) as mock_gui:
                rc = bsp.main()
        assert rc == 0
        mock_gui.assert_called_once()

    def test_bsp_gui_flag(self, registry_file):
        """``bsp --gui`` should invoke launch_gui."""
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "--gui", "list"]):
            with patch("bsp.gui.launch_gui", return_value=0) as mock_gui:
                rc = bsp.main()
        assert rc == 0
        mock_gui.assert_called_once()

    def test_no_command_prints_help_and_exits_nonzero(self):
        """Running ``bsp`` with no command should exit with non-zero code."""
        with patch("sys.argv", ["bsp"]):
            rc = bsp.main()
        assert rc != 0

    def test_gui_subcommand_passes_registry(self, registry_file):
        """Registry path is forwarded to launch_gui when using 'bsp gui'."""
        with patch("sys.argv", ["bsp", "--registry", str(registry_file), "gui"]):
            with patch("bsp.gui.launch_gui", return_value=0) as mock_gui:
                bsp.main()
        _, kwargs = mock_gui.call_args
        assert kwargs.get("registry_path") == str(registry_file)


# =============================================================================
# cli_runner module
# =============================================================================

class TestCliRunnerModule:
    def test_cli_runner_importable(self):
        import bsp.cli_runner  # noqa: F401 — just ensure it imports cleanly


# =============================================================================
# bsp-explorer entry point: bsp.gui:main
# =============================================================================

class TestBspLauncherMain:
    """Tests for the bsp-explorer console script entry point (bsp.gui:main)."""

    def test_main_calls_launch_gui(self):
        """bsp-explorer with no args should call launch_gui with defaults."""
        from bsp.gui import main as gui_main

        with patch("sys.argv", ["bsp-explorer"]):
            with patch("bsp.gui.launch_gui", return_value=0) as mock_gui:
                rc = gui_main()
        assert rc == 0
        mock_gui.assert_called_once()

    def test_main_passes_registry(self, registry_file):
        """--registry arg is forwarded to launch_gui."""
        from bsp.gui import main as gui_main

        with patch("sys.argv", ["bsp-explorer", "--registry", str(registry_file)]):
            with patch("bsp.gui.launch_gui", return_value=0) as mock_gui:
                gui_main()
        _, kwargs = mock_gui.call_args
        assert kwargs.get("registry_path") == str(registry_file)

    def test_main_passes_no_update(self):
        """--no-update flag is forwarded to launch_gui."""
        from bsp.gui import main as gui_main

        with patch("sys.argv", ["bsp-explorer", "--no-update"]):
            with patch("bsp.gui.launch_gui", return_value=0) as mock_gui:
                gui_main()
        _, kwargs = mock_gui.call_args
        assert kwargs.get("no_update") is True

    def test_main_remote_default_passed_as_none(self):
        """When --remote is the default URL, launch_gui receives remote=None."""
        from bsp.gui import main as gui_main

        with patch("sys.argv", ["bsp-explorer"]):
            with patch("bsp.gui.launch_gui", return_value=0) as mock_gui:
                gui_main()
        _, kwargs = mock_gui.call_args
        assert kwargs.get("remote") is None

    def test_main_custom_remote(self):
        """Custom --remote is forwarded to launch_gui."""
        from bsp.gui import main as gui_main

        with patch("sys.argv", ["bsp-explorer", "--remote", "https://example.com/reg.git"]):
            with patch("bsp.gui.launch_gui", return_value=0) as mock_gui:
                gui_main()
        _, kwargs = mock_gui.call_args
        assert kwargs.get("remote") == "https://example.com/reg.git"


# =============================================================================
# _list_removable_drives utility
# =============================================================================

