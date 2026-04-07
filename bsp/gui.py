"""
GUI launcher for BSP Registry Manager using textual TUI framework.

This module provides an interactive terminal-based GUI for managing
Advantech Board Support Packages (BSPs). It offers a visual alternative
to the CLI with real-time log output, BSP selection, and action buttons.
"""

from __future__ import annotations

import argparse
import datetime
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    from textual import on
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
    from textual.screen import ModalScreen
    from textual.widgets import (
        Button,
        Checkbox,
        Footer,
        Header,
        Input,
        Label,
        ListItem,
        ListView,
        ProgressBar,
        Static,
        TextArea,
        Tree,
    )
    TEXTUAL_AVAILABLE = True
except ImportError:
    TEXTUAL_AVAILABLE = False


# =============================================================================
# BitBake / KAS output parsing helpers
# =============================================================================

# Matches BitBake non-TTY progress lines:
#   "NOTE: Running task 42 of 1234"
#   "NOTE: Running [noexec] task 42 of 1234"
_RE_BUILD_TASK = re.compile(r"NOTE: Running (?:\[noexec\] )?task (\d+) of (\d+)")

# Matches bmaptool percentage progress lines:
#   "bmaptool: info: 50% done, …"
_RE_BMAP_PERCENT = re.compile(r"(\d+)% done")


# =============================================================================
# Utility: enumerate removable / flashable block devices
# =============================================================================

def _list_removable_drives() -> List[Tuple[str, str]]:
    """
    Return a list of ``(device_path, display_label)`` tuples for block
    devices that are suitable flash targets (removable USB drives, SD cards,
    eMMC controllers).

    Supports Linux (via ``/sys/block``) and macOS (via ``diskutil``).
    Returns an empty list on unsupported platforms or when discovery fails.
    """
    drives: List[Tuple[str, str]] = []

    if platform.system() == "Linux":
        sys_block = Path("/sys/block")
        if not sys_block.exists():
            return drives

        for dev in sorted(sys_block.iterdir()):
            name = dev.name
            # Skip virtual / loop / device-mapper / zram devices
            if (name.startswith("loop") or name.startswith("dm-")
                    or name.startswith("zram") or name.startswith("ram")):
                continue

            removable_file = dev / "removable"
            is_removable = (
                removable_file.exists()
                and removable_file.read_text().strip() == "1"
            )
            # eMMC parent devices (mmcblkN, not mmcblkNpM partitions)
            is_mmc = bool(re.match(r"^mmcblk\d+$", name))

            if not (is_removable or is_mmc):
                continue

            # Size (sectors × 512 bytes)
            size_str = ""
            size_file = dev / "size"
            if size_file.exists():
                try:
                    sectors = int(size_file.read_text().strip())
                    size_bytes = sectors * 512
                    if size_bytes >= 1_073_741_824:
                        size_str = f"{size_bytes / 1_073_741_824:.1f} GB"
                    elif size_bytes > 0:
                        size_str = f"{size_bytes / 1_048_576:.0f} MB"
                except ValueError:
                    pass

            # Model name from sysfs
            model = ""
            for model_path in (dev / "device" / "model", dev / "device" / "name"):
                if model_path.exists():
                    model = model_path.read_text().strip()
                    break

            dev_path = f"/dev/{name}"
            parts = [x for x in [model, size_str] if x]
            label = f"{dev_path}  ({', '.join(parts)})" if parts else dev_path
            drives.append((dev_path, label))

    elif platform.system() == "Darwin":
        try:
            import plistlib
            result = subprocess.run(
                ["diskutil", "list", "-plist", "external"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                data = plistlib.loads(result.stdout)
                for disk in data.get("AllDisksAndPartitions", []):
                    dev_id = disk.get("DeviceIdentifier", "")
                    if not dev_id:
                        continue
                    dev_path = f"/dev/{dev_id}"
                    size_bytes = disk.get("Size", 0)
                    size_str = ""
                    if size_bytes >= 1_073_741_824:
                        size_str = f"{size_bytes / 1_073_741_824:.1f} GB"
                    elif size_bytes > 0:
                        size_str = f"{size_bytes / 1_048_576:.0f} MB"
                    label = f"{dev_path}  ({size_str})" if size_str else dev_path
                    drives.append((dev_path, label))
        except Exception:
            pass

    return drives

# =============================================================================
# Guard for missing textual dependency
# =============================================================================


def launch_gui(
    registry_path: Optional[str] = None,
    remote: Optional[str] = None,
    branch: Optional[str] = None,
    no_update: bool = False,
) -> int:
    """
    Launch the BSP Registry GUI application.

    Args:
        registry_path: Optional path to a local BSP registry file.
        remote: Optional remote git URL for the registry.
        branch: Optional branch for the remote registry.
        no_update: If True, skip updating the cached remote registry.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    if not TEXTUAL_AVAILABLE:
        print(
            "Error: The 'textual' package is required for the GUI.\n"
            "Install it with:  pip install 'bsp-registry-tools[gui]'",
            file=sys.stderr,
        )
        return 1

    app = BspLauncherApp(
        registry_path=registry_path,
        remote=remote,
        branch=branch,
        no_update=no_update,
    )
    result = app.run()

    # Shell action: the TUI exited cleanly — now exec the interactive shell in
    # the restored terminal, replacing the current process.
    if isinstance(result, tuple) and result[0] == "shell":
        bsp_name = result[1]
        cmd = [sys.executable, "-m", "bsp.cli_runner"]
        if registry_path:
            cmd += ["--registry", registry_path]
        if remote:
            cmd += ["--remote", remote]
        if branch:
            cmd += ["--branch", branch]
        if no_update:
            cmd.append("--no-update")
        cmd += ["shell", bsp_name]
        os.execv(sys.executable, cmd)

    return 0


def main() -> int:
    """
    Entry point for the ``bsp-explorer`` console script.

    Parses command-line arguments and launches the BSP Registry GUI.

    Returns:
        Exit code (0 for success, non-zero for errors).
    """
    from .registry_fetcher import DEFAULT_REMOTE_URL, DEFAULT_BRANCH

    parser = argparse.ArgumentParser(
        prog="bsp-explorer",
        description="BSP Registry Explorer — interactive TUI for Advantech BSP management",
    )
    parser.add_argument(
        "--registry", "-r",
        default=None,
        metavar="REGISTRY",
        help="BSP registry file (local path; skips remote fetch)",
    )
    parser.add_argument(
        "--remote",
        default=DEFAULT_REMOTE_URL,
        metavar="URL",
        help="Remote registry git URL (default: %(default)s)",
    )
    parser.add_argument(
        "--branch",
        default=DEFAULT_BRANCH,
        metavar="BRANCH",
        help="Remote registry branch (default: %(default)s)",
    )
    parser.add_argument(
        "--no-update",
        dest="no_update",
        action="store_true",
        help="Skip updating the cached registry clone",
    )

    args = parser.parse_args()

    return launch_gui(
        registry_path=args.registry,
        remote=args.remote if args.remote != DEFAULT_REMOTE_URL else None,
        branch=args.branch if args.branch != DEFAULT_BRANCH else None,
        no_update=args.no_update,
    )


def main_web() -> int:
    """
    Entry point for the ``bsp-explorer-web`` console script.

    Delegates to ``textual serve bsp-explorer [args]`` so the BSP Explorer
    TUI can be accessed from a browser via Textual's built-in web server.

    Accepts the same registry flags as ``bsp-explorer`` plus optional
    ``--host`` / ``--port`` to control where the HTTP server listens.

    Returns:
        Exit code (0 for success, non-zero for errors).  The function normally
        replaces the current process via :func:`os.execvp` and never returns.
    """
    import shlex
    from .registry_fetcher import DEFAULT_REMOTE_URL, DEFAULT_BRANCH

    parser = argparse.ArgumentParser(
        prog="bsp-explorer-web",
        description=(
            "BSP Registry Explorer — serve in the browser via 'textual serve'.\n"
            "Requires the 'textual-serve' extra: pip install 'textual[serve]'"
        ),
    )
    parser.add_argument(
        "--registry", "-r",
        default=None,
        metavar="REGISTRY",
        help="BSP registry file (local path; skips remote fetch)",
    )
    parser.add_argument(
        "--remote",
        default=DEFAULT_REMOTE_URL,
        metavar="URL",
        help="Remote registry git URL (default: %(default)s)",
    )
    parser.add_argument(
        "--branch",
        default=DEFAULT_BRANCH,
        metavar="BRANCH",
        help="Remote registry branch (default: %(default)s)",
    )
    parser.add_argument(
        "--no-update",
        dest="no_update",
        action="store_true",
        help="Skip updating the cached registry clone",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        metavar="HOST",
        help="Host for the web server (default: 0.0.0.0 — all interfaces)",
    )
    parser.add_argument(
        "--port",
        default=8000,
        type=int,
        metavar="PORT",
        help="Port for the web server (default: 8000)",
    )

    args = parser.parse_args()

    # Build the bsp-explorer command that textual serve will run.
    bsp_cmd: list[str] = ["bsp-explorer"]
    if args.registry:
        bsp_cmd += ["--registry", args.registry]
    if args.remote != DEFAULT_REMOTE_URL:
        bsp_cmd += ["--remote", args.remote]
    if args.branch != DEFAULT_BRANCH:
        bsp_cmd += ["--branch", args.branch]
    if args.no_update:
        bsp_cmd.append("--no-update")

    textual_argv = [
        "textual", "serve",
        shlex.join(bsp_cmd),
        "--host", args.host,
        "--port", str(args.port),
    ]

    os.execvp("textual", textual_argv)
    return 0  # unreachable


# =============================================================================
# Textual TUI Application (only defined when textual is available)
# =============================================================================

if TEXTUAL_AVAILABLE:

    class ConfirmScreen(ModalScreen[bool]):
        """A simple yes/no confirmation dialog."""

        DEFAULT_CSS = """
        ConfirmScreen {
            align: center middle;
        }
        ConfirmScreen > Container {
            background: $surface;
            border: thick $primary;
            padding: 1 2;
            width: 50;
            height: 10;
        }
        ConfirmScreen Label {
            width: 100%;
            text-align: center;
            padding: 1 0;
        }
        ConfirmScreen Horizontal {
            align: center middle;
            height: 3;
        }
        ConfirmScreen Button {
            margin: 0 2;
        }
        """

        def __init__(self, message: str) -> None:
            super().__init__()
            self._message = message

        def compose(self) -> ComposeResult:
            with Container():
                yield Label(self._message)
                with Horizontal():
                    yield Button("Yes", variant="success", id="confirm-yes")
                    yield Button("No", variant="error", id="confirm-no")

        @on(Button.Pressed, "#confirm-yes")
        def on_yes(self) -> None:
            self.dismiss(True)

        @on(Button.Pressed, "#confirm-no")
        def on_no(self) -> None:
            self.dismiss(False)

    class FlashScreen(ModalScreen):
        """A dialog that prompts for an image and a target block device to flash."""

        DEFAULT_CSS = """
        FlashScreen {
            align: center middle;
        }
        FlashScreen > Container {
            background: $surface;
            border: thick $warning;
            padding: 1 2;
            width: 72;
            height: auto;
            max-height: 40;
        }
        FlashScreen Label {
            width: 100%;
            text-align: center;
            padding: 1 0;
        }
        FlashScreen #images-label {
            text-align: left;
            color: $text-muted;
            padding: 0;
        }
        FlashScreen #image-list {
            height: auto;
            max-height: 8;
            border: solid $warning-darken-2;
            margin: 0 0 1 0;
        }
        FlashScreen #no-images-label {
            color: $error;
            text-style: italic;
            padding: 0 0 1 0;
        }
        FlashScreen #drives-label {
            text-align: left;
            color: $text-muted;
            padding: 0;
        }
        FlashScreen #drive-list {
            height: auto;
            max-height: 6;
            border: solid $primary-darken-2;
            margin: 0 0 1 0;
        }
        FlashScreen #no-drives-label {
            color: $text-muted;
            text-style: italic;
            padding: 0 0 1 0;
        }
        FlashScreen Input {
            width: 100%;
            margin: 0 0 1 0;
        }
        FlashScreen Horizontal {
            align: center middle;
            height: 3;
        }
        FlashScreen Button {
            margin: 0 2;
        }
        """

        def __init__(self, bsp_name: str, images: List[Path]) -> None:
            super().__init__()
            self._bsp_name = bsp_name
            self._images = images
            self._drives = _list_removable_drives()
            self._selected_image: Optional[Path] = images[0] if images else None

        def _make_image_items(self) -> List[ListItem]:
            """Build the list of image ListItem widgets for the ListView."""
            return [ListItem(Label(img.name), name=str(img)) for img in self._images]

        def _make_drive_items(self) -> List[ListItem]:
            """Build the list of drive ListItem widgets for the ListView."""
            return [ListItem(Label(label), name=path) for path, label in self._drives]

        def compose(self) -> ComposeResult:
            with Container():
                yield Label(f"Flash '{self._bsp_name}' — Select image and target device:")
                if self._images:
                    yield Label("Available images:", id="images-label")
                    yield ListView(*self._make_image_items(), id="image-list")
                else:
                    yield Label(
                        "No images found — build the BSP first.",
                        id="no-images-label",
                    )
                if self._drives:
                    yield Label("Removable drives detected:", id="drives-label")
                    yield ListView(*self._make_drive_items(), id="drive-list")
                else:
                    yield Label(
                        "No removable drives detected — enter path manually.",
                        id="no-drives-label",
                    )
                yield Input(
                    placeholder="/dev/sda or /dev/mmcblk0",
                    id="flash-target-input",
                )
                with Horizontal():
                    yield Button(
                        "⚡ Flash",
                        variant="warning",
                        id="flash-confirm",
                        disabled=not self._images,
                    )
                    yield Button("Cancel", variant="default", id="flash-cancel")

        @on(ListView.Selected, "#image-list")
        def on_image_selected(self, event: ListView.Selected) -> None:
            """Record the image chosen from the list."""
            path = event.item.name
            if path:
                self._selected_image = Path(path)

        @on(ListView.Selected, "#drive-list")
        def on_drive_selected(self, event: ListView.Selected) -> None:
            """Populate the input field when a drive is chosen from the list."""
            path = event.item.name
            if path:
                self.query_one("#flash-target-input", Input).value = path

        @on(Button.Pressed, "#flash-confirm")
        def on_flash(self) -> None:
            target = self.query_one("#flash-target-input", Input).value.strip()
            if target and self._selected_image:
                self.dismiss((str(self._selected_image), target))
            else:
                self.dismiss(None)

        @on(Button.Pressed, "#flash-cancel")
        def on_flash_cancel(self) -> None:
            self.dismiss(None)

    class BuildTargetScreen(ModalScreen):
        """Dialog that shows build options before launching a build."""

        DEFAULT_CSS = """
        BuildTargetScreen {
            align: center middle;
        }
        BuildTargetScreen > Container {
            background: $surface;
            border: thick $success;
            padding: 1 2;
            width: 64;
            height: auto;
        }
        BuildTargetScreen Label {
            width: 100%;
            text-align: center;
            padding: 1 0;
        }
        BuildTargetScreen #info-label {
            color: $text-muted;
            text-style: italic;
            text-align: left;
            padding: 0 0 1 0;
        }
        BuildTargetScreen #target-label {
            text-align: left;
            padding: 0 0 0 0;
        }
        BuildTargetScreen Input {
            width: 100%;
            margin: 0 0 1 0;
        }
        BuildTargetScreen Checkbox {
            margin: 0 0 1 0;
        }
        BuildTargetScreen Horizontal {
            align: center middle;
            height: 3;
        }
        BuildTargetScreen Button {
            margin: 0 2;
        }
        """

        def __init__(self, bsp_name: str, build_path: str) -> None:
            super().__init__()
            self._bsp_name = bsp_name
            self._build_path = build_path

        def compose(self) -> ComposeResult:
            with Container():
                yield Label(f"▶ Build '{self._bsp_name}'")
                if self._build_path:
                    yield Label(
                        f"Build path: {self._build_path}", id="info-label"
                    )
                yield Label("KAS target — BitBake recipe/image (optional):", id="target-label")
                yield Input(
                    placeholder="e.g. core-image-minimal",
                    id="opt-target",
                )
                yield Checkbox("Clean build (--clean)", id="opt-clean")
                yield Checkbox(
                    "Checkout only — no build (--checkout)",
                    id="opt-checkout-only",
                )
                yield Checkbox("Verbose output (--verbose)", id="opt-verbose")
                with Horizontal():
                    yield Button("▶ Build", variant="success", id="build-confirm")
                    yield Button("Cancel", variant="default", id="build-cancel")

        @on(Button.Pressed, "#build-confirm")
        def on_build(self) -> None:
            target = self.query_one("#opt-target", Input).value.strip()
            if target and not re.fullmatch(r"[\w][\w\-\.]*", target):
                self.app.bell()
                self.notify(
                    "Target must contain only letters, digits, hyphens, underscores, or dots.",
                    severity="error",
                )
                return
            clean = self.query_one("#opt-clean", Checkbox).value
            checkout_only = self.query_one("#opt-checkout-only", Checkbox).value
            verbose = self.query_one("#opt-verbose", Checkbox).value
            self.dismiss({
                "target": target,
                "clean": clean,
                "checkout_only": checkout_only,
                "verbose": verbose,
            })

        @on(Button.Pressed, "#build-cancel")
        def on_build_cancel(self) -> None:
            self.dismiss(None)

    class CopyableTextArea(TextArea):
        """Read-only TextArea that still handles Ctrl+C for copying selected text.

        Textual's TextArea returns False from check_consume_key() for *all* keys
        when read_only=True, which causes Ctrl+C to bubble up to the App where it
        triggers the "help_quit" notification instead of copying. Overriding
        check_consume_key() to allow Ctrl+C (and Super+C) through fixes this.
        """

        def check_consume_key(self, key: str, character: str | None = None) -> bool:
            if key in ("ctrl+c", "super+c"):
                return True
            return super().check_consume_key(key, character)

    class BspLauncherApp(App):
        """
        BSP Registry Explorer — interactive TUI for Advantech BSP management.

        Features:
        - Browse and select BSPs from the registry
        - View BSP details (description, OS info, build configuration)
        - Launch builds, shell sessions, and config exports
        - Real-time output log with automatic scrolling
        - Registry refresh (remote pull or local reload)
        """

        TITLE = "BSP Registry Explorer"
        SUB_TITLE = "Advantech Board Support Package Manager"
        # Maximum seconds between two clicks to be considered a double-click
        _DOUBLE_CLICK_THRESHOLD: float = 0.5

        CSS = """
        /* ── Layout ─────────────────────────────────────────── */
        #main-layout {
            height: 1fr;
        }

        #left-panel {
            width: 1fr;
            border: round $primary;
            padding: 0 1;
        }

        #right-panel {
            width: 1fr;
            border: round $primary;
            padding: 0 1;
        }

        #bsp-detail-pane {
            height: 1fr;
            border-bottom: solid $primary-darken-2;
        }

        #env-pane {
            height: 1fr;
            border-bottom: solid $primary-darken-2;
        }

        #env-label {
            color: $accent;
            text-style: bold;
        }

        #artifacts-pane {
            height: 1fr;
        }

        #artifacts-label {
            color: $accent;
            text-style: bold;
        }

        #artifacts-path {
            color: $text-muted;
            padding: 0 1;
            height: auto;
        }

        #artifacts-view {
            height: 1fr;
        }

        /* ── Registry bar ────────────────────────────────────── */
        #registry-bar {
            height: 3;
            align: left middle;
            background: $surface;
            border-bottom: solid $primary-darken-2;
            padding: 0 1;
        }

        #registry-label {
            width: 1fr;
            color: $text-muted;
        }

        #btn-refresh {
            min-width: 12;
        }

        /* ── BSP tree ────────────────────────────────────────── */
        #filter-input {
            height: 3;
            margin: 0 0 1 0;
        }

        #bsp-tree {
            height: 1fr;
        }

        /* ── Detail panel ────────────────────────────────────── */
        #detail-view {
            height: 1fr;
            padding: 0 1;
        }

        .detail-key {
            color: $accent;
            text-style: bold;
        }

        /* ── Action buttons ──────────────────────────────────── */
        #action-bar {
            height: 3;
            align: left middle;
            background: $surface;
            border-top: solid $primary-darken-2;
            padding: 0 1;
        }

        #action-bar Button {
            margin-right: 1;
        }

        /* ── Log panel ───────────────────────────────────────── */
        #log-panel {
            height: 30%;
            border: round $primary;
        }

        #log-panel.log-fullscreen {
            height: 1fr;
        }

        #progress-row {
            height: 1;
            display: none;
        }

        #progress-row.visible {
            display: block;
        }

        #progress-status {
            width: auto;
            padding-right: 1;
            content-align: left middle;
        }

        #build-progress {
            width: 1fr;
        }

        #output-log {
            height: 1fr;
        }

        /* ── Status bar ──────────────────────────────────────── */
        #status-bar {
            height: 1;
            background: $primary-darken-3;
            padding: 0 1;
            color: $text-muted;
        }
        """

        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("r", "refresh", "Refresh"),
            Binding("b", "build", "Build"),
            Binding("s", "shell", "Shell"),
            Binding("e", "export_config", "Export"),
            Binding("f", "flash", "Flash"),
            Binding("l", "toggle_log", "Log"),
            Binding("c", "cancel", "Cancel", show=False),
            Binding("x", "cancel", "Cancel", show=False),
        ]

        def __init__(
            self,
            registry_path: Optional[str] = None,
            remote: Optional[str] = None,
            branch: Optional[str] = None,
            no_update: bool = False,
        ) -> None:
            super().__init__()
            self._registry_path = registry_path
            self._remote = remote
            self._branch = branch
            self._no_update = no_update
            self._bsp_manager = None
            self._selected_bsp_name: Optional[str] = None
            self._running_process: Optional[subprocess.Popen] = None
            self._is_loading: bool = False
            self._load_lock = threading.Lock()
            # Cached data for re-filtering without reloading the registry
            self._tree_data: Optional[dict] = None
            # Double-click tracking for build artifacts panel
            self._artifact_last_click_time: float = 0.0
            self._artifact_last_click_item: Optional[str] = None

        # ── Compose ──────────────────────────────────────────────

        def compose(self) -> ComposeResult:
            yield Header()

            # Registry path bar
            with Horizontal(id="registry-bar"):
                yield Label("Registry: (loading…)", id="registry-label")
                yield Button("⟳ Refresh", id="btn-refresh", variant="primary")

            # Middle: BSP list | BSP detail
            with Horizontal(id="main-layout"):
                with Vertical(id="left-panel"):
                    yield Label("BSP Presets", classes="detail-key")
                    yield Input(
                        placeholder="Filter by vendor, device or release…",
                        id="filter-input",
                    )
                    yield Tree("Vendors", id="bsp-tree")

                with Vertical(id="right-panel"):
                    with Vertical(id="bsp-detail-pane"):
                        yield Label("BSP Details", classes="detail-key")
                        yield ScrollableContainer(
                            Static("Select a BSP from the list.", id="detail-view"),
                            id="detail-scroll",
                        )
                    with Vertical(id="env-pane"):
                        yield Label("Build Environment", id="env-label")
                        yield ScrollableContainer(
                            Static("", id="env-view"),
                            id="env-scroll",
                        )
                    with Vertical(id="artifacts-pane"):
                        yield Label("Build Artifacts", id="artifacts-label")
                        yield Static("", id="artifacts-path")
                        yield ListView(id="artifacts-view")

            # Action buttons
            with Horizontal(id="action-bar"):
                yield Button("▶ Build", id="btn-build", variant="success", disabled=True)
                yield Button("$ Shell", id="btn-shell", variant="default", disabled=True)
                yield Button("↑ Export", id="btn-export", variant="default", disabled=True)
                yield Button("⚡ Flash", id="btn-flash", variant="warning", disabled=True)
                yield Button("✕ Cancel", id="btn-cancel", variant="error", disabled=True)

            # Output log panel
            with Vertical(id="log-panel"):
                with Horizontal(id="progress-row"):
                    yield Static("", id="progress-status")
                    yield ProgressBar(id="build-progress", total=100, show_eta=False)
                yield CopyableTextArea(
                    id="output-log",
                    read_only=True,
                    soft_wrap=True,
                    show_line_numbers=False,
                )

            yield Static("", id="status-bar")
            yield Footer()

        # ── Lifecycle ────────────────────────────────────────────

        def on_mount(self) -> None:
            """Initialize BSP tree and load the registry."""
            tree = self.query_one("#bsp-tree", Tree)
            tree.root.expand()
            self._load_registry_async()

        # ── Registry loading ─────────────────────────────────────

        def _load_registry_async(self) -> None:
            """Spawn a thread to load the registry without blocking the UI."""
            with self._load_lock:
                if self._is_loading:
                    self._log("[yellow]Registry load already in progress[/yellow]")
                    return
                self._is_loading = True
            self._set_status("Loading registry…")
            thread = threading.Thread(target=self._load_registry, daemon=True)
            thread.start()

        def _load_registry(self) -> None:
            """Load the BSP registry (runs in background thread)."""
            try:
                # Import here to avoid circular imports at module level
                from .bsp_manager import BspManager
                from .registry_fetcher import (
                    DEFAULT_REMOTE_URL,
                    DEFAULT_BRANCH,
                    RegistryFetcher,
                )

                registry_path = self._registry_path

                if registry_path is None:
                    local_defaults = ["bsp-registry.yaml", "bsp-registry.yml"]
                    local_registry = next(
                        (name for name in local_defaults if Path(name).is_file()), None
                    )
                    if local_registry is not None:
                        registry_path = local_registry
                    else:
                        fetcher = RegistryFetcher()
                        registry_path = str(
                            fetcher.fetch_registry(
                                repo_url=self._remote or DEFAULT_REMOTE_URL,
                                branch=self._branch or DEFAULT_BRANCH,
                                update=not self._no_update,
                            )
                        )

                bsp_manager = BspManager(registry_path)
                bsp_manager.initialize()

                self._bsp_manager = bsp_manager
                self.call_from_thread(self._populate_bsp_tree, registry_path)

            except SystemExit:
                self.call_from_thread(
                    self._log, "[red]Failed to load registry (see logs for details)[/red]"
                )
                self.call_from_thread(self._set_status, "Error: registry load failed")
            except Exception as exc:
                self.call_from_thread(
                    self._log, f"[red]Error loading registry: {exc}[/red]"
                )
                self.call_from_thread(self._set_status, f"Error: {exc}")
            finally:
                with self._load_lock:
                    self._is_loading = False

        def _populate_bsp_tree(self, registry_path: str) -> None:
            """Rebuild the BSP tree with Vendor → Device → Release → Preset hierarchy."""
            from .models import Device as DeviceModel
            from .registry_fetcher import DEFAULT_REMOTE_URL, DEFAULT_BRANCH

            tree = self.query_one("#bsp-tree", Tree)
            tree.clear()

            # Build registry label showing path only; URL/branch goes to subtitle
            remote_url = self._remote or DEFAULT_REMOTE_URL
            branch = self._branch or DEFAULT_BRANCH
            registry_label_text = f"Registry: {registry_path}"
            if self._registry_path:
                subtitle = registry_path
            else:
                subtitle = f"{remote_url} @ {branch}"

            label = self.query_one("#registry-label", Label)
            label.update(registry_label_text)

            # Update subtitle to include URL + branch
            self.sub_title = subtitle

            if not (self._bsp_manager and self._bsp_manager.model
                    and self._bsp_manager.resolver):
                self._set_status("No BSPs found in registry")
                self._log("[yellow]No BSPs found in registry[/yellow]")
                return

            registry = self._bsp_manager.model.registry

            # vendor_slug → display name
            vendor_names: dict[str, str] = {
                v.slug: (v.name or v.slug) for v in (registry.vendors or [])
            }
            # device_slug → Device object
            devices: dict[str, DeviceModel] = {
                d.slug: d for d in (registry.devices or [])
            }
            # release_slug → display label (description or slug)
            release_labels: dict[str, str] = {
                r.slug: (r.description or r.slug)
                for r in (registry.releases or [])
            }

            def device_display(device_slug: str) -> str:
                d: Optional[DeviceModel] = devices.get(device_slug)
                return (d.description if d and d.description else device_slug)

            # Use the resolver so multi-release presets are expanded into
            # concrete entries (e.g. poky-qemuarm64 → poky-qemuarm64-scarthgap,
            # poky-qemuarm64-styhead) each carrying their single release slug.
            expanded_presets = self._bsp_manager.resolver.list_presets()

            # vendor_slug → device_slug → release_slug → [full preset name]
            vendor_device_release: dict[str, dict[str, dict[str, list]]] = {}
            no_vendor_presets: dict[str, dict[str, list]] = {}

            for bsp in expanded_presets:
                device = devices.get(bsp.device)
                vendor_slug = device.vendor if device else None
                release_slug = bsp.release or ""

                if vendor_slug:
                    vd = vendor_device_release.setdefault(vendor_slug, {})
                    dr = vd.setdefault(bsp.device, {})
                    dr.setdefault(release_slug, []).append(bsp.name)
                else:
                    dr = no_vendor_presets.setdefault(bsp.device, {})
                    dr.setdefault(release_slug, []).append(bsp.name)

            # Cache the registry data so the filter can re-render without
            # reloading everything from disk.
            self._tree_data = {
                "vendor_names": vendor_names,
                "device_display": device_display,
                "release_labels": release_labels,
                "vendor_device_release": vendor_device_release,
                "no_vendor_presets": no_vendor_presets,
            }

            filter_text = self.query_one("#filter-input", Input).value
            preset_count = self._render_tree(filter_text)

            if preset_count:
                self._set_status(f"Loaded {preset_count} BSP preset(s)")
                self._log(f"[green]Registry loaded:[/green] {preset_count} BSP(s) available")
            else:
                self._set_status("No BSPs found in registry")
                self._log("[yellow]No BSPs found in registry[/yellow]")

        def _render_tree(self, filter_text: str = "") -> int:
            """Rebuild the tree widget contents applying *filter_text*.

            Each token in *filter_text* (whitespace-separated) must appear in
            at least one of the vendor name, device label, or release label for
            a preset to be included.  Returns the number of visible presets.
            """
            if self._tree_data is None:
                return 0

            tokens = [t.lower() for t in filter_text.split() if t]
            vendor_names = self._tree_data["vendor_names"]
            device_display = self._tree_data["device_display"]
            release_labels = self._tree_data["release_labels"]
            vendor_device_release = self._tree_data["vendor_device_release"]
            no_vendor_presets = self._tree_data["no_vendor_presets"]

            tree = self.query_one("#bsp-tree", Tree)
            tree.clear()

            preset_count = 0

            def _matches(vendor_label: str, device_slug: str, rel_slug: str) -> bool:
                """Return True if every token matches vendor, device, or release."""
                if not tokens:
                    return True
                dev_label = device_display(device_slug).lower()
                rel_label = release_labels.get(rel_slug, rel_slug).lower()
                searchable = f"{vendor_label.lower()} {dev_label} {rel_label}"
                return all(tok in searchable for tok in tokens)

            def _add_device_subtree(parent_node, vendor_label: str,
                                    device_slug: str,
                                    release_map: dict[str, list]) -> int:
                """Add a device sub-tree, skipping releases that don't match.

                Returns the number of preset leaves added.
                """
                dev_label = device_display(device_slug)
                dev_node = None
                added = 0
                for rel_slug, names in sorted(release_map.items()):
                    if not _matches(vendor_label, device_slug, rel_slug):
                        continue
                    if dev_node is None:
                        dev_node = parent_node.add(
                            f"[green]{dev_label}[/green]", expand=True
                        )
                    rel_label = release_labels.get(rel_slug, rel_slug)
                    rel_node = dev_node.add(
                        f"[italic cyan]{rel_label}[/italic cyan]", expand=True
                    )
                    for preset_name in sorted(names):
                        rel_node.add_leaf(
                            f"[dim white]{preset_name}[/dim white]",
                            data=preset_name,
                        )
                        added += 1
                return added

            # Add vendor → device → release → preset sub-trees.
            # Pre-check each vendor to avoid inserting an empty vendor node.
            for vendor_slug, device_map in sorted(vendor_device_release.items()):
                display_name = vendor_names.get(vendor_slug, vendor_slug.capitalize())
                # Check whether any device/release under this vendor passes the filter
                has_match = any(
                    _matches(display_name, dev_slug, rel_slug)
                    for dev_slug, release_map in device_map.items()
                    for rel_slug in release_map
                )
                if not has_match:
                    continue
                vendor_node = tree.root.add(
                    f"[bold yellow]{display_name}[/bold yellow]", expand=True
                )
                for device_slug, release_map in sorted(device_map.items()):
                    preset_count += _add_device_subtree(
                        vendor_node, display_name, device_slug, release_map
                    )

            # Devices that had no matching vendor go under "Other"
            other_label = "Other"
            has_other = any(
                _matches(other_label, dev_slug, rel_slug)
                for dev_slug, release_map in no_vendor_presets.items()
                for rel_slug in release_map
            )
            if no_vendor_presets and has_other:
                other_node = tree.root.add(
                    f"[bold yellow]{other_label}[/bold yellow]", expand=True
                )
                for device_slug, release_map in sorted(no_vendor_presets.items()):
                    preset_count += _add_device_subtree(
                        other_node, other_label, device_slug, release_map
                    )

            return preset_count

        @on(Input.Changed, "#filter-input")
        def on_filter_changed(self, event: Input.Changed) -> None:
            """Re-render the BSP tree whenever the filter text changes."""
            if self._tree_data is None:
                return
            count = self._render_tree(event.value)
            if count:
                self._set_status(f"Showing {count} BSP preset(s)")
            else:
                self._set_status("No BSPs match filter")

        # ── BSP selection ────────────────────────────────────────

        @on(Tree.NodeSelected, "#bsp-tree")
        def on_bsp_selected(self, event: Tree.NodeSelected) -> None:
            """Show details for the selected BSP preset and enable action buttons."""
            bsp_name = event.node.data
            if bsp_name is None:
                # Vendor or device node — nothing to select
                return
            self._selected_bsp_name = bsp_name
            self._show_bsp_details(bsp_name)

            # Enable action buttons
            for btn_id in ("#btn-build", "#btn-shell", "#btn-export", "#btn-flash"):
                self.query_one(btn_id, Button).disabled = False

        @on(ListView.Selected, "#artifacts-view")
        def on_artifact_selected(self, event: ListView.Selected) -> None:
            """Open the flash dialog when a flash image in the artifacts panel is double-clicked."""
            item_name = event.item.name
            if not item_name:
                return  # placeholder item ("No artifacts found")

            now = time.monotonic()
            if (
                now - self._artifact_last_click_time < self._DOUBLE_CLICK_THRESHOLD
                and item_name == self._artifact_last_click_item
            ):
                # Double-click detected — open flash dialog for this image
                image_path = Path(item_name)
                is_flash_image = any(
                    image_path.name.endswith(s)
                    for s in (".wic", ".wic.gz", ".wic.bz2", ".wic.xz", ".wic.zst", ".img")
                )
                if is_flash_image and self._selected_bsp_name:
                    bsp_name = self._selected_bsp_name

                    def _on_result(result: Optional[tuple]) -> None:
                        if result:
                            img_path, target = result
                            self._run_bsp_command(
                                "flash", bsp_name,
                                "--target", target,
                                "--image", img_path,
                                show_progress=True,
                            )

                    self.push_screen(
                        FlashScreen(bsp_name, [image_path]), _on_result
                    )

            self._artifact_last_click_time = now
            self._artifact_last_click_item = item_name

        def _show_bsp_details(self, bsp_name: str) -> None:
            """Render BSP details (top) and build environment (bottom) in the right panel."""
            if not self._bsp_manager:
                return

            # Search raw registry first (single-release presets).
            # For expanded multi-release names (e.g. poky-qemuarm64-scarthgap)
            # fall back to the resolver's expanded list.
            bsp = next(
                (b for b in (self._bsp_manager.model.registry.bsp or [])
                 if b.name == bsp_name),
                None,
            )
            if bsp is None and self._bsp_manager.resolver:
                bsp = next(
                    (p for p in self._bsp_manager.resolver.list_presets()
                     if p.name == bsp_name),
                    None,
                )

            if bsp is None:
                return

            # ── Top pane: BSP details ──────────────────────────────
            lines = [
                f"[bold cyan]Name:[/bold cyan]        {bsp.name}",
                f"[bold cyan]Description:[/bold cyan] {bsp.description}",
            ]

            lines.append(f"[bold cyan]Build Path:[/bold cyan]  {bsp.build.path}")

            # Bitbake targets
            if bsp.targets:
                lines.append(f"[bold cyan]Targets:[/bold cyan]     {', '.join(bsp.targets)}")

            self.query_one("#detail-view", Static).update("\n".join(lines))

            # ── Bottom pane: build environment ─────────────────────
            env_lines: list[str] = []

            # Shared helpers for this BSP
            registry = self._bsp_manager.model.registry
            release_slug = bsp.release or (bsp.releases[0] if bsp.releases else None)
            release_obj = next(
                (r for r in (registry.releases or []) if r.slug == release_slug),
                None,
            ) if release_slug else None

            # Container name (from preset's build section or resolved env)
            container_name: Optional[str] = None
            if bsp.build and bsp.build.container:
                container_name = bsp.build.container
            elif release_obj:
                try:
                    named_env = self._bsp_manager.resolver.get_named_environment(
                        release_obj
                    )
                    if named_env and named_env.container:
                        container_name = named_env.container
                except Exception:
                    pass

            # Resolve the container entry to get the image name
            if container_name:
                try:
                    containers = getattr(self._bsp_manager.model, "containers", None) or {}
                    container_cfg = containers.get(container_name)
                    if container_cfg and getattr(container_cfg, "image", None):
                        env_lines.append(
                            f"[bold cyan]Container:[/bold cyan]  {container_name}"
                            f"  [dim]({container_cfg.image})[/dim]"
                        )
                    else:
                        env_lines.append(
                            f"[bold cyan]Container:[/bold cyan]  {container_name}"
                        )
                except Exception:
                    env_lines.append(
                        f"[bold cyan]Container:[/bold cyan]  {container_name}"
                    )

            # Named environment and its variables
            named_env = None
            if release_obj:
                try:
                    named_env = self._bsp_manager.resolver.get_named_environment(
                        release_obj
                    )
                    if named_env:
                        env_name = release_obj.environment or "default"
                        env_lines.append(
                            f"[bold cyan]Environment:[/bold cyan] {env_name}"
                        )
                except Exception:
                    pass
            else:
                # No matched release — try to show "default" environment directly
                try:
                    envs = getattr(self._bsp_manager.model, "environments", None) or {}
                    if envs:
                        env_name = "default"
                        named_env = envs.get(env_name)
                        if named_env:
                            env_lines.append(
                                f"[bold cyan]Environment:[/bold cyan] {env_name}"
                            )
                except Exception:
                    pass

            # Show named environment variables
            if named_env:
                try:
                    from .environment import EnvironmentManager
                    if named_env.variables:
                        env_lines.append("[bold cyan]Variables:[/bold cyan]")
                        expanded = EnvironmentManager(named_env.variables).get_environment_dict()
                        for var in named_env.variables:
                            env_lines.append(
                                f"  [dim]{var.name}[/dim] = {expanded.get(var.name, var.value)}"
                            )
                except Exception:
                    pass

            # Global environment variables
            try:
                global_env = getattr(self._bsp_manager.model, "environment", None)
                if global_env and getattr(global_env, "variables", None):
                    env_lines.append("[bold cyan]Global Vars:[/bold cyan]")
                    from .environment import EnvironmentManager
                    expanded = EnvironmentManager(global_env.variables).get_environment_dict()
                    for var in global_env.variables:
                        env_lines.append(
                            f"  [dim]{var.name}[/dim] = {expanded.get(var.name, var.value)}"
                        )
            except Exception:
                pass

            if not env_lines:
                env_lines.append("[dim]No build environment configuration[/dim]")

            self.query_one("#env-view", Static).update("\n".join(env_lines))

            # ── Bottom pane: build artifacts ────────────────────────
            self._update_artifacts_pane(bsp_name)

        def _scan_build_artifacts(self, bsp_name: str) -> List[Path]:
            """Return a sorted list of build artifacts for *bsp_name*.

            Searches the Yocto deploy/images directory for:
            - Flash images: ``*.wic``, ``*.wic.gz``, ``*.wic.bz2``, ``*.wic.xz``,
              ``*.wic.zst``, ``*.img``
            - Kernel images: ``uImage``, ``uImage-*``, ``zImage``, ``zImage-*``,
              ``Image``, ``Image-*``, ``fitImage``, ``fitImage-*``

            Returns an empty list if the build directory does not exist yet.
            """
            if not self._bsp_manager:
                return []
            try:
                deploy_dir = self._bsp_manager._get_deploy_dir(bsp_name)
            except Exception:
                return []

            if not deploy_dir.exists():
                return []

            candidates: List[Path] = []
            # Flash images
            for pattern in ("*.wic", "*.wic.gz", "*.wic.bz2", "*.wic.xz", "*.wic.zst", "*.img"):
                candidates.extend(deploy_dir.rglob(pattern))
            # Filter out bmap sidecar files
            candidates = [f for f in candidates if ".bmap" not in f.name]

            # Kernel images (exact names and name-* variants)
            for kname in ("uImage", "zImage", "Image", "fitImage", "vmlinuz", "bzImage"):
                # exact name
                for p in deploy_dir.rglob(kname):
                    candidates.append(p)
                # name-<variant> patterns (e.g. uImage-5.15.0-yocto-standard)
                for p in deploy_dir.rglob(f"{kname}-*"):
                    # skip symlinks to keep the list clean
                    if not p.is_symlink():
                        candidates.append(p)

            # Deduplicate, skip symlinks for non-kernel files too
            seen: Set[str] = set()
            unique: List[Path] = []
            for f in candidates:
                key = str(f.resolve())
                if key not in seen:
                    seen.add(key)
                    unique.append(f)

            # Sort: wic/img first by mtime desc, then kernels by mtime desc
            stat_cache: Dict[Path, os.stat_result] = {}
            for f in unique:
                try:
                    stat_cache[f] = f.stat()
                except OSError:
                    pass

            def _sort_key(f: Path):
                is_kernel = f.name.startswith(
                    ("uImage", "zImage", "Image", "fitImage", "vmlinuz", "bzImage")
                )
                mtime = -stat_cache[f].st_mtime if f in stat_cache else 0
                return (1 if is_kernel else 0, mtime)

            unique.sort(key=_sort_key)
            return unique

        def _update_artifacts_pane(self, bsp_name: str) -> None:
            """Populate the Build Artifacts pane for the given BSP."""
            artifacts = self._scan_build_artifacts(bsp_name)
            artifacts_list = self.query_one("#artifacts-view", ListView)
            path_widget = self.query_one("#artifacts-path", Static)

            # Always show the build path
            build_path = self._resolve_build_path(bsp_name)
            if build_path:
                path_widget.update(f"[dim]Path: {build_path}[/dim]")
            else:
                path_widget.update("")

            artifacts_list.clear()

            if not artifacts:
                artifacts_list.append(
                    ListItem(Label("[dim]No build artifacts found[/dim]"), name="")
                )
                return

            for f in artifacts:
                try:
                    st = f.stat()
                    size_bytes = st.st_size
                    if size_bytes >= 1_073_741_824:
                        size_str = f"{size_bytes / 1_073_741_824:.1f} GB"
                    elif size_bytes >= 1_048_576:
                        size_str = f"{size_bytes / 1_048_576:.1f} MB"
                    elif size_bytes >= 1024:
                        size_str = f"{size_bytes / 1024:.0f} KB"
                    else:
                        size_str = f"{size_bytes} B"
                    mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                    if any(f.name.endswith(s) for s in (".wic", ".wic.gz", ".wic.bz2", ".wic.xz", ".wic.zst", ".img")):
                        icon = "💾"
                    else:
                        icon = "🐧"
                    label_text = f"{icon} {f.name}  {size_str}  {mtime}"
                    artifacts_list.append(ListItem(Label(label_text), name=str(f)))
                except OSError:
                    artifacts_list.append(ListItem(Label(f"[dim]{f.name}[/dim]"), name=str(f)))

        @on(Button.Pressed, "#btn-refresh")
        def on_refresh(self) -> None:
            self.action_refresh()

        @on(Button.Pressed, "#btn-build")
        def on_build(self) -> None:
            self.action_build()

        @on(Button.Pressed, "#btn-shell")
        def on_shell(self) -> None:
            self.action_shell()

        @on(Button.Pressed, "#btn-export")
        def on_export(self) -> None:
            self.action_export_config()

        @on(Button.Pressed, "#btn-flash")
        def on_flash(self) -> None:
            self.action_flash()

        @on(Button.Pressed, "#btn-cancel")
        def on_cancel(self) -> None:
            self.action_cancel()

        # ── Keybinding actions ───────────────────────────────────

        def action_toggle_log(self) -> None:
            """Toggle the log panel between normal (30%) and full-screen height."""
            log_panel = self.query_one("#log-panel")
            expanded = log_panel.has_class("log-fullscreen")
            for widget_id in ("#registry-bar", "#main-layout", "#action-bar"):
                self.query_one(widget_id).display = expanded
            log_panel.toggle_class("log-fullscreen")

        def action_refresh(self) -> None:
            """Reload the BSP registry."""
            self._bsp_manager = None
            self._selected_bsp_name = None
            for btn_id in ("#btn-build", "#btn-shell", "#btn-export", "#btn-flash", "#btn-cancel"):
                self.query_one(btn_id, Button).disabled = True
            self.query_one("#detail-view", Static).update("Select a BSP from the list.")
            self.query_one("#env-view", Static).update("")
            self.query_one("#artifacts-path", Static).update("")
            self.query_one("#artifacts-view", ListView).clear()
            tree = self.query_one("#bsp-tree", Tree)
            tree.clear()
            self._load_registry_async()

        def action_build(self) -> None:
            """Open the build target dialog, then build the selected BSP."""
            if not self._selected_bsp_name:
                self._log("[yellow]No BSP selected[/yellow]")
                return

            # Resolve the build path for this preset so the dialog can show it
            # and the log saver knows where to write the log file.
            build_path = self._resolve_build_path(self._selected_bsp_name)

            def _on_options(options: Optional[dict]) -> None:
                if options is None:
                    return
                # --verbose is a top-level bsp flag (bsp --verbose build …)
                pre_args: list[str] = []
                if options.get("verbose"):
                    pre_args.append("--verbose")
                cmd_args = ["build", self._selected_bsp_name]
                if options.get("target"):
                    cmd_args.extend(["--target", options["target"]])
                if options.get("clean"):
                    cmd_args.append("--clean")
                if options.get("checkout_only"):
                    cmd_args.append("--checkout")
                log_file = (
                    str(Path(build_path) / f"bsp-build-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.log") if build_path else None
                )
                self._run_bsp_command(*pre_args, *cmd_args, log_file=log_file, show_progress=True)

            self.push_screen(
                BuildTargetScreen(self._selected_bsp_name, build_path),
                _on_options,
            )

        def action_shell(self) -> None:
            """Exit the TUI and open an interactive shell for the selected BSP."""
            if not self._selected_bsp_name:
                self._log("[yellow]No BSP selected[/yellow]")
                return
            # Exit the TUI — launch_gui() will exec 'bsp shell <name>' in the
            # restored terminal, replacing the current process.
            self.exit(("shell", self._selected_bsp_name))

        def action_export_config(self) -> None:
            """Export configuration for the selected BSP."""
            if not self._selected_bsp_name:
                self._log("[yellow]No BSP selected[/yellow]")
                return
            self._run_bsp_command("export", self._selected_bsp_name)

        def action_flash(self) -> None:
            """Open a dialog to flash the selected BSP image to a target device."""
            if not self._selected_bsp_name:
                self._log("[yellow]No BSP selected[/yellow]")
                return

            # Enumerate available images from the deploy directory
            images: List[Path] = []
            if self._bsp_manager:
                try:
                    images = self._bsp_manager.list_flash_images(self._selected_bsp_name)
                except Exception as exc:
                    self._log(f"[yellow]Could not enumerate flash images: {exc}[/yellow]")

            def _on_result(result: Optional[tuple]) -> None:
                if result:
                    image_path, target = result
                    self._run_bsp_command(
                        "flash", self._selected_bsp_name,
                        "--target", target,
                        "--image", image_path,
                        show_progress=True,
                    )

            self.push_screen(FlashScreen(self._selected_bsp_name, images), _on_result)

        def action_cancel(self) -> None:
            """Terminate the currently running build/export command."""
            proc = self._running_process
            if proc is None:
                self._log("[yellow]No command is currently running[/yellow]")
                return
            try:
                # Kill the entire process group so child processes (e.g. kas)
                # don't keep the stdout pipe open and block the stream loop.
                if hasattr(os, "killpg"):
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except (OSError, ProcessLookupError):
                        proc.terminate()
                else:
                    proc.terminate()
                self._log("[yellow]Cancelling running command…[/yellow]")
                self._set_status("Cancelling…")
            except OSError:
                pass

        # ── CLI subprocess helper ────────────────────────────────

        def _resolve_build_path(self, bsp_name: str) -> str:
            """
            Return the resolved build path for *bsp_name*, or an empty string
            if it cannot be determined (e.g. no build.path configured).
            """
            if not self._bsp_manager:
                return ""
            try:
                # Try the raw registry entry first
                bsp = next(
                    (b for b in (self._bsp_manager.model.registry.bsp or [])
                     if b.name == bsp_name),
                    None,
                )
                if bsp is None and self._bsp_manager.resolver:
                    bsp = next(
                        (p for p in self._bsp_manager.resolver.list_presets()
                         if p.name == bsp_name),
                        None,
                    )
                if bsp and bsp.build and bsp.build.path:
                    return bsp.build.path
            except Exception:
                pass
            return ""

        def _run_bsp_command(self, *args: str, log_file: Optional[str] = None, show_progress: bool = False) -> None:
            """
            Run a `bsp` CLI sub-command in a background thread and stream
            its stdout/stderr to the output log panel.

            Args:
                *args: Sub-command and arguments forwarded to the bsp CLI.
                log_file: Optional path to a file where the output is also
                    written (created / appended to in the build folder).
                show_progress: When True, parse output for progress markers and
                    display a ProgressBar in the log panel.
            """
            if self._running_process is not None:
                self._log("[yellow]A command is already running — please wait[/yellow]")
                return

            # Build the command, forwarding registry path if set.
            # Use -u (unbuffered) so BitBake log lines flow to the GUI
            # line-by-line rather than waiting for an 8 KB buffer to fill.
            cmd = [sys.executable, "-u", "-m", "bsp.cli_runner"]
            if self._registry_path:
                cmd += ["--registry", self._registry_path]
            if self._remote:
                cmd += ["--remote", self._remote]
            if self._branch:
                cmd += ["--branch", self._branch]
            if self._no_update:
                cmd.append("--no-update")
            cmd += list(args)

            self._log(f"[bold]$ bsp {' '.join(args)}[/bold]")
            if log_file:
                self._log(f"[dim]Log file: {log_file}[/dim]")
            self._set_status(f"Running: bsp {' '.join(args)}")
            self._set_cancel_button(disabled=False)

            thread = threading.Thread(
                target=self._stream_command, args=(cmd, log_file, show_progress), daemon=True
            )
            thread.start()

        def _stream_command(self, cmd: list, log_file: Optional[str] = None, show_progress: bool = False) -> None:
            """Execute *cmd*, streaming its combined output to the log (background thread).

            If *log_file* is set, all output lines are also written there.
            The file is opened inside a context manager so it is always closed,
            even if an exception occurs during streaming.
            """
            import contextlib

            def _open_log():
                """Open the log file or return a null context manager on failure."""
                if not log_file:
                    return contextlib.nullcontext(None)
                try:
                    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
                    fp = open(log_file, "w", encoding="utf-8")  # noqa: WPS515
                    fp.write(
                        f"# BSP build log — {datetime.datetime.now().isoformat()}\n"
                        f"# Command: {' '.join(cmd)}\n\n"
                    )
                    fp.flush()
                    return fp
                except OSError as exc:
                    self.call_from_thread(
                        self._log,
                        f"[yellow]Warning: cannot open log file {log_file}: {exc}[/yellow]",
                    )
                    return contextlib.nullcontext(None)

            with _open_log() as log_fp:
                try:
                    # PYTHONUNBUFFERED=1 propagates through the process tree
                    # (bsp.cli_runner → kas → bitbake) so that each NOTE:/WARNING:/
                    # ERROR: line from BitBake is flushed immediately rather than
                    # waiting for a full 8 KB stdout block buffer to fill.
                    _env = os.environ.copy()
                    _env["PYTHONUNBUFFERED"] = "1"
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        start_new_session=True,
                        env=_env,
                    )
                    self._running_process = proc
                    _build_start = datetime.datetime.now()

                    if show_progress:
                        self.call_from_thread(self._show_progress_row)

                    for line in proc.stdout:
                        stripped = line.rstrip()
                        self.call_from_thread(self._log, stripped)
                        if show_progress:
                            m = _RE_BUILD_TASK.search(stripped)
                            if m:
                                current, total = int(m.group(1)), int(m.group(2))
                                self.call_from_thread(
                                    self._update_build_progress,
                                    current, total, f"Task {current}/{total}",
                                )
                            else:
                                m = _RE_BMAP_PERCENT.search(stripped)
                                if m:
                                    pct = int(m.group(1))
                                    self.call_from_thread(
                                        self._update_build_progress,
                                        pct, 100, f"Flashing {pct}%",
                                    )
                        if log_fp is not None:
                            log_fp.write(line)
                            log_fp.flush()

                    proc.wait()
                    rc = proc.returncode

                    if rc == 0:
                        _elapsed = datetime.datetime.now() - _build_start
                        _h, _rem = divmod(int(_elapsed.total_seconds()), 3600)
                        _m, _s = divmod(_rem, 60)
                        _elapsed_str = (
                            f"{_h}h {_m:02d}m {_s:02d}s" if _h
                            else f"{_m}m {_s:02d}s" if _m
                            else f"{_s}s"
                        )
                        self.call_from_thread(
                            self._log, f"[green]Command finished successfully[/green] [dim](elapsed: {_elapsed_str})[/dim]"
                        )
                        self.call_from_thread(self._set_status, "Done")
                        # Refresh artifacts pane in case the build produced new images
                        if self._selected_bsp_name:
                            self.call_from_thread(
                                self._update_artifacts_pane, self._selected_bsp_name
                            )
                    elif rc == -15:  # SIGTERM — user cancelled
                        self.call_from_thread(
                            self._log, "[yellow]Command cancelled by user[/yellow]"
                        )
                        self.call_from_thread(self._set_status, "Cancelled")
                    else:
                        self.call_from_thread(
                            self._log,
                            f"[red]Command exited with code {rc}[/red]",
                        )
                        self.call_from_thread(self._set_status, f"Failed (exit {rc})")

                    if log_fp is not None:
                        log_fp.write(f"\n# Exit code: {rc}\n")

                except Exception as exc:
                    self.call_from_thread(
                        self._log, f"[red]Error running command: {exc}[/red]"
                    )
                    self.call_from_thread(self._set_status, f"Error: {exc}")
                finally:
                    if log_fp is not None and log_file:
                        self.call_from_thread(
                            self._log,
                            f"[dim]Build log saved → {log_file}[/dim]",
                        )
                    self._running_process = None
                    self.call_from_thread(
                        self._set_cancel_button, disabled=True
                    )
                    if show_progress:
                        self.call_from_thread(self._hide_progress_row)

        def _set_cancel_button(self, *, disabled: bool) -> None:
            """Enable or disable the Cancel button (must run on UI thread)."""
            self.query_one("#btn-cancel", Button).disabled = disabled

        def _show_progress_row(self) -> None:
            """Show the progress row and reset the ProgressBar (UI thread)."""
            row = self.query_one("#progress-row")
            row.add_class("visible")
            self.query_one("#build-progress", ProgressBar).update(progress=0, total=100)
            self.query_one("#progress-status", Static).update("")

        def _hide_progress_row(self) -> None:
            """Hide the progress row (UI thread)."""
            self.query_one("#progress-row").remove_class("visible")

        def _update_build_progress(self, current: int, total: int, label: str) -> None:
            """Update the ProgressBar and its status label (UI thread)."""
            self.query_one("#build-progress", ProgressBar).update(progress=current, total=total)
            self.query_one("#progress-status", Static).update(label)

        # ── Helpers ──────────────────────────────────────────────

        def _log(self, message: str) -> None:
            """Append *message* to the output log widget."""
            log_widget = self.query_one("#output-log", CopyableTextArea)
            # Strip Rich markup tags so plain text is appended
            plain = re.sub(r"\[/?[^\[\]]*\]", "", message)
            log_widget.insert(plain + "\n", location=log_widget.document.end)
            log_widget.scroll_end(animate=False)

        def _set_status(self, message: str) -> None:
            """Update the status bar with *message*."""
            status = self.query_one("#status-bar", Static)
            status.update(message)
