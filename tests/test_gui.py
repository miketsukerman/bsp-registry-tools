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
import threading
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
        from bsp.gui import BspLauncherApp, _MAX_DISPLAYED_RUNNING_TASKS
        from collections import deque

        app = BspLauncherApp.__new__(BspLauncherApp)
        app._running_process = None
        app._build_task_current = 0
        app._build_task_total = 0
        app._build_running_tasks = deque(maxlen=_MAX_DISPLAYED_RUNNING_TASKS)
        app._build_warnings = 0
        app._build_errors = 0
        app._in_running_block = False
        app._progress_lock = threading.Lock()
        app._log_calls = []
        app._important_calls = []
        app._status_calls = []

        def fake_call_from_thread(fn, *a, **kw):
            fn(*a, **kw)

        app.call_from_thread = fake_call_from_thread
        app._log = lambda msg: app._log_calls.append(msg)
        app._log_important = lambda msg: app._important_calls.append(msg)
        app._set_status = lambda msg: app._status_calls.append(msg)
        app._set_cancel_button = lambda *, disabled: None
        app._refresh_progress_ui = lambda: None
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

    def test_completion_logged_to_progress_tab(self, tmp_path):
        """_log_important is called with a success message on exit code 0."""
        if not TEXTUAL_AVAILABLE:
            pytest.skip("textual not installed")

        app = self._make_minimal_app(tmp_path)
        cmd = [sys.executable, "-c", ""]
        app._stream_command(cmd)

        assert any("success" in m.lower() or "✓" in m for m in app._important_calls)

    def test_failure_logged_to_progress_tab(self, tmp_path):
        """_log_important is called with a failure message on non-zero exit."""
        if not TEXTUAL_AVAILABLE:
            pytest.skip("textual not installed")

        app = self._make_minimal_app(tmp_path)
        cmd = [sys.executable, "-c", "raise SystemExit(1)"]
        app._stream_command(cmd)

        assert any("fail" in m.lower() or "✗" in m for m in app._important_calls)


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
# bsp-launcher entry point: bsp.gui:main
# =============================================================================

class TestBspLauncherMain:
    """Tests for the bsp-launcher console script entry point (bsp.gui:main)."""

    def test_main_calls_launch_gui(self):
        """bsp-launcher with no args should call launch_gui with defaults."""
        from bsp.gui import main as gui_main

        with patch("sys.argv", ["bsp-launcher"]):
            with patch("bsp.gui.launch_gui", return_value=0) as mock_gui:
                rc = gui_main()
        assert rc == 0
        mock_gui.assert_called_once()

    def test_main_passes_registry(self, registry_file):
        """--registry arg is forwarded to launch_gui."""
        from bsp.gui import main as gui_main

        with patch("sys.argv", ["bsp-launcher", "--registry", str(registry_file)]):
            with patch("bsp.gui.launch_gui", return_value=0) as mock_gui:
                gui_main()
        _, kwargs = mock_gui.call_args
        assert kwargs.get("registry_path") == str(registry_file)

    def test_main_passes_no_update(self):
        """--no-update flag is forwarded to launch_gui."""
        from bsp.gui import main as gui_main

        with patch("sys.argv", ["bsp-launcher", "--no-update"]):
            with patch("bsp.gui.launch_gui", return_value=0) as mock_gui:
                gui_main()
        _, kwargs = mock_gui.call_args
        assert kwargs.get("no_update") is True

    def test_main_remote_default_passed_as_none(self):
        """When --remote is the default URL, launch_gui receives remote=None."""
        from bsp.gui import main as gui_main

        with patch("sys.argv", ["bsp-launcher"]):
            with patch("bsp.gui.launch_gui", return_value=0) as mock_gui:
                gui_main()
        _, kwargs = mock_gui.call_args
        assert kwargs.get("remote") is None

    def test_main_custom_remote(self):
        """Custom --remote is forwarded to launch_gui."""
        from bsp.gui import main as gui_main

        with patch("sys.argv", ["bsp-launcher", "--remote", "https://example.com/reg.git"]):
            with patch("bsp.gui.launch_gui", return_value=0) as mock_gui:
                gui_main()
        _, kwargs = mock_gui.call_args
        assert kwargs.get("remote") == "https://example.com/reg.git"


# =============================================================================
# Progress tab widget presence
# =============================================================================

@pytest.mark.skipif(not TEXTUAL_AVAILABLE, reason="textual not installed")
class TestProgressTabWidgets:
    """Verify that the progress-tab widgets are composed into the app."""

    async def test_progress_widgets_present(self, registry_file):
        """All progress-tab widgets are accessible by ID after composition."""
        from bsp.gui import BspLauncherApp

        app = BspLauncherApp(registry_path=str(registry_file))
        async with app.run_test(headless=True) as _:
            assert app.query_one("#log-tabs") is not None
            assert app.query_one("#progress-status") is not None
            assert app.query_one("#build-progress") is not None
            assert app.query_one("#progress-log") is not None
            # Raw log tab must still be present
            assert app.query_one("#output-log") is not None

    async def test_progress_tab_is_default_active(self, registry_file):
        """The Progress tab is the first (default active) tab."""
        from bsp.gui import BspLauncherApp
        from textual.widgets import TabbedContent

        app = BspLauncherApp(registry_path=str(registry_file))
        async with app.run_test(headless=True) as _:
            tabs = app.query_one("#log-tabs", TabbedContent)
            assert tabs.active == "tab-progress"


# =============================================================================
# BitBake progress parsing
# =============================================================================

@pytest.mark.skipif(not TEXTUAL_AVAILABLE, reason="textual not installed")
class TestBitBakeProgressParsing:
    """Unit tests for _parse_and_update_progress line classification."""

    def _make_app(self):
        """Return a minimal BspLauncherApp with all UI calls stubbed."""
        from bsp.gui import BspLauncherApp, _MAX_DISPLAYED_RUNNING_TASKS
        from collections import deque

        app = BspLauncherApp.__new__(BspLauncherApp)
        app._build_task_current = 0
        app._build_task_total = 0
        app._build_running_tasks = deque(maxlen=_MAX_DISPLAYED_RUNNING_TASKS)
        app._build_warnings = 0
        app._build_errors = 0
        app._in_running_block = False
        app._progress_lock = threading.Lock()
        app._important_calls = []
        app._refresh_calls = []

        app.call_from_thread = lambda fn, *a, **kw: fn(*a, **kw)
        app._log_important = lambda msg: app._important_calls.append(msg)
        app._refresh_progress_ui = lambda: app._refresh_calls.append(True)
        return app

    def _assert_important_contains(self, app, keyword: str) -> None:
        """Assert that at least one progress-log message contains *keyword*."""
        assert any(keyword in m for m in app._important_calls), (
            f"{keyword!r} not found in important_calls: {app._important_calls}"
        )

    def test_parses_task_count_line(self):
        app = self._make_app()
        app._parse_and_update_progress(
            "Currently  3 running tasks (19 of 2411):"
        )
        assert app._build_task_current == 19
        assert app._build_task_total == 2411
        assert app._in_running_block is True
        assert list(app._build_running_tasks) == []

    def test_entering_running_block_clears_previous_tasks(self):
        app = self._make_app()
        app._build_running_tasks = ["old-task do_compile"]
        app._parse_and_update_progress(
            # BitBake uses the plural form "tasks" even for a single running task.
            "Currently  1 running tasks (5 of 100):"
        )
        assert list(app._build_running_tasks) == []

    def test_parses_running_task_line(self):
        app = self._make_app()
        app._in_running_block = True
        app._parse_and_update_progress(
            "0: python3-native-3.11.1-r0 do_fetch (pid 12345)"
        )
        assert len(app._build_running_tasks) == 1
        assert "python3-native-3.11.1-r0" in app._build_running_tasks[0]
        assert "do_fetch" in app._build_running_tasks[0]

    def test_non_task_line_ends_running_block(self):
        app = self._make_app()
        app._in_running_block = True
        app._parse_and_update_progress("Some unrelated NOTE line")
        assert app._in_running_block is False

    def test_multiple_running_tasks_accumulated(self):
        app = self._make_app()
        app._in_running_block = True
        app._parse_and_update_progress("0: pkg-a-1.0-r0 do_compile (pid 100)")
        app._parse_and_update_progress("1: pkg-b-2.0-r0 do_fetch (pid 101)")
        assert len(app._build_running_tasks) == 2

    def test_counts_warnings(self):
        app = self._make_app()
        app._parse_and_update_progress("WARNING: File not found")
        assert app._build_warnings == 1
        self._assert_important_contains(app, "WARNING")

    def test_counts_multiple_warnings(self):
        app = self._make_app()
        app._parse_and_update_progress("WARNING: First warning")
        app._parse_and_update_progress("WARNING: Second warning")
        assert app._build_warnings == 2

    def test_counts_errors(self):
        app = self._make_app()
        app._parse_and_update_progress("ERROR: Task failed")
        assert app._build_errors == 1
        self._assert_important_contains(app, "ERROR")

    def test_counts_fatal_as_error(self):
        app = self._make_app()
        app._parse_and_update_progress("FATAL: Build exploded")
        assert app._build_errors == 1
        self._assert_important_contains(app, "FATAL")

    def test_tasks_summary_logged_to_progress_tab(self):
        app = self._make_app()
        app._parse_and_update_progress(
            "NOTE: Tasks Summary: Attempted 450 tasks of which 449 didn't need to be rerun"
        )
        assert any("Tasks Summary" in m for m in app._important_calls)

    def test_build_phase_preparing_runqueue_logged(self):
        """NOTE: Preparing RunQueue is forwarded to the progress-log."""
        app = self._make_app()
        app._parse_and_update_progress("NOTE: Preparing RunQueue")
        assert any("Preparing RunQueue" in m for m in app._important_calls)

    def test_build_phase_executing_tasks_logged(self):
        """NOTE: Executing Tasks is forwarded to the progress-log."""
        app = self._make_app()
        app._parse_and_update_progress("NOTE: Executing Tasks")
        assert any("Executing Tasks" in m for m in app._important_calls)

    def test_kas_info_line_logged_to_progress_tab(self):
        """KAS INFO log lines are forwarded to the progress-log."""
        app = self._make_app()
        app._parse_and_update_progress("INFO     kas: Running bitbake ...")
        assert any("Running bitbake" in m for m in app._important_calls)

    def test_kas_info_config_file_logged(self):
        """KAS INFO log lines about config files are forwarded."""
        app = self._make_app()
        app._parse_and_update_progress("INFO     kas: Using KAS project config files: /path")
        assert any("Using KAS project config files" in m for m in app._important_calls)

    def test_unrecognised_line_ignored(self):
        app = self._make_app()
        app._parse_and_update_progress("Loading cache: 100%|#####| Time: 0:00:01")
        assert app._build_warnings == 0
        assert app._build_errors == 0
        assert app._important_calls == []

    def test_refresh_called_on_task_count(self):
        app = self._make_app()
        app._parse_and_update_progress("Currently  2 running tasks (10 of 50):")
        assert app._refresh_calls

    def test_refresh_called_on_warning(self):
        app = self._make_app()
        app._parse_and_update_progress("WARNING: something")
        assert app._refresh_calls

    # ── Non-interactive/piped-mode tests ──────────────────────────────────────

    def test_noninteractive_running_task_sets_progress(self):
        """NOTE: Running task X of Y updates task counts."""
        app = self._make_app()
        app._parse_and_update_progress(
            "NOTE: Running task 42 of 2411 (ID: 2, /path/recipe.bb, do_fetch) [cpu: 0.00s]"
        )
        assert app._build_task_current == 42
        assert app._build_task_total == 2411
        assert app._refresh_calls

    def test_noninteractive_noexec_task_sets_progress(self):
        """NOTE: Running noexec task X of Y also updates task counts."""
        app = self._make_app()
        app._parse_and_update_progress(
            "NOTE: Running noexec task 1 of 500 (/path/recipe.bb, do_build) [cpu: 0.00s]"
        )
        assert app._build_task_current == 1
        assert app._build_task_total == 500

    def test_noninteractive_progress_advances(self):
        """Successive Running task lines advance the counter."""
        app = self._make_app()
        app._parse_and_update_progress(
            "NOTE: Running task 10 of 100 (ID: 10, /path/recipe.bb, do_fetch)"
        )
        app._parse_and_update_progress(
            "NOTE: Running noexec task 11 of 100 (/path/recipe.bb, do_build)"
        )
        assert app._build_task_current == 11
        assert app._build_task_total == 100

    def test_noninteractive_active_recipe_tracked(self):
        """NOTE: recipe do_task: msg adds to the running tasks list."""
        app = self._make_app()
        app._parse_and_update_progress("NOTE: glibc-2.35-r0 do_compile: Compiling glibc")
        assert any("glibc-2.35-r0" in t for t in app._build_running_tasks)
        assert any("do_compile" in t for t in app._build_running_tasks)
        assert app._refresh_calls

    def test_noninteractive_multiple_active_recipes_accumulated(self):
        """Multiple recipe NOTE lines accumulate in the running task list."""
        app = self._make_app()
        app._parse_and_update_progress("NOTE: glibc-2.35-r0 do_compile: Compiling")
        app._parse_and_update_progress("NOTE: python3-native-3.11.9-r0 do_fetch: Fetching")
        assert len(app._build_running_tasks) == 2

    def test_noninteractive_active_recipe_rolling_window(self):
        """Running tasks list is capped at _MAX_DISPLAYED_RUNNING_TASKS."""
        from bsp.gui import _MAX_DISPLAYED_RUNNING_TASKS
        app = self._make_app()
        for i in range(_MAX_DISPLAYED_RUNNING_TASKS + 3):
            app._parse_and_update_progress(f"NOTE: pkg-{i}-1.0-r0 do_compile: msg")
        assert len(app._build_running_tasks) <= _MAX_DISPLAYED_RUNNING_TASKS

    def test_noninteractive_duplicate_recipe_not_added_twice(self):
        """Duplicate recipe+task entries are not added to the running list."""
        app = self._make_app()
        app._parse_and_update_progress("NOTE: glibc-2.35-r0 do_compile: start")
        app._parse_and_update_progress("NOTE: glibc-2.35-r0 do_compile: progress")
        count = list(app._build_running_tasks).count("glibc-2.35-r0 do_compile")
        assert count == 1, f"Expected 1 occurrence, found {count}"

    def test_active_recipe_does_not_match_generic_note_lines(self):
        """NOTE lines without do_task format are NOT captured as active recipes."""
        app = self._make_app()
        app._parse_and_update_progress("NOTE: Preparing RunQueue")
        app._parse_and_update_progress("NOTE: Executing Tasks")
        assert list(app._build_running_tasks) == []
        assert app._build_warnings == 0

    def test_ansi_codes_stripped_before_parsing(self):
        """ANSI escape sequences are stripped so patterns still match."""
        app = self._make_app()
        # Running task line wrapped in ANSI bold
        app._parse_and_update_progress(
            "\x1b[1mNOTE: Running task 7 of 200\x1b[0m (ID: 7, /path, do_fetch)"
        )
        assert app._build_task_current == 7
        assert app._build_task_total == 200

    def test_ansi_codes_stripped_for_warning(self):
        """ANSI-wrapped WARNING lines still increment the warning counter."""
        app = self._make_app()
        app._parse_and_update_progress("\x1b[33mWARNING: Something deprecated\x1b[0m")
        assert app._build_warnings == 1

    def test_carriage_return_prefix_stripped(self):
        """Bare \\r before NOTE: is stripped so the pattern matches."""
        app = self._make_app()
        app._parse_and_update_progress("\rNOTE: Running task 9 of 300")
        assert app._build_task_current == 9
        assert app._build_task_total == 300

    def test_ansi_erase_and_cr_stripped(self):
        """\\x1b[K\\r prefix (BitBake line-clear pattern) is stripped."""
        app = self._make_app()
        app._parse_and_update_progress("\x1b[K\rNOTE: Running task 3 of 50")
        assert app._build_task_current == 3
        assert app._build_task_total == 50

    def test_ansi_cursor_up_erase_and_cr_stripped(self):
        """Full VT100 cursor-up+erase+CR prefix is stripped before matching."""
        app = self._make_app()
        app._parse_and_update_progress(
            "\x1b[1A\x1b[K\rNOTE: Running task 20 of 400"
        )
        assert app._build_task_current == 20
        assert app._build_task_total == 400

    def test_cr_prefix_warning_still_counted(self):
        """\\r-prefixed WARNING: lines still increment the warning counter."""
        app = self._make_app()
        app._parse_and_update_progress("\rWARNING: Something bad happened")
        assert app._build_warnings == 1


# =============================================================================
# _reset_build_progress
# =============================================================================

@pytest.mark.skipif(not TEXTUAL_AVAILABLE, reason="textual not installed")
class TestResetBuildProgress:
    """Verify that _reset_build_progress resets all tracking state."""

    async def test_reset_clears_progress_state(self, registry_file):
        """_reset_build_progress zeroes counters and clears running tasks."""
        from bsp.gui import BspLauncherApp

        app = BspLauncherApp(registry_path=str(registry_file))
        async with app.run_test(headless=True) as _:
            # Simulate some accumulated progress (lock is already initialised)
            with app._progress_lock:
                app._build_task_current = 42
                app._build_task_total = 100
                app._build_running_tasks.append("pkg do_compile")
                app._build_warnings = 3
                app._build_errors = 1
                app._in_running_block = True

            app._reset_build_progress()

            assert app._build_task_current == 0
            assert app._build_task_total == 0
            assert list(app._build_running_tasks) == []
            assert app._build_warnings == 0
            assert app._build_errors == 0
            assert app._in_running_block is False
