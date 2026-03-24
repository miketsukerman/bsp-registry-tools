"""
GUI launcher for BSP Registry Manager using textual TUI framework.

This module provides an interactive terminal-based GUI for managing
Advantech Board Support Packages (BSPs). It offers a visual alternative
to the CLI with real-time log output, BSP selection, and action buttons.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

try:
    from textual import on
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
    from textual.screen import ModalScreen
    from textual.widgets import (
        Button,
        DataTable,
        Footer,
        Header,
        Input,
        Label,
        RichLog,
        Static,
        TabbedContent,
        TabPane,
    )
    TEXTUAL_AVAILABLE = True
except ImportError:
    TEXTUAL_AVAILABLE = False

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
    app.run()
    return 0


def main() -> int:
    """
    Entry point for the ``bsp-launcher`` console script.

    Parses command-line arguments and launches the BSP Registry GUI.

    Returns:
        Exit code (0 for success, non-zero for errors).
    """
    from .registry_fetcher import DEFAULT_REMOTE_URL, DEFAULT_BRANCH

    parser = argparse.ArgumentParser(
        prog="bsp-launcher",
        description="BSP Registry Launcher — interactive TUI for Advantech BSP management",
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

    class BspLauncherApp(App):
        """
        BSP Registry Launcher — interactive TUI for Advantech BSP management.

        Features:
        - Browse and select BSPs from the registry
        - View BSP details (description, OS info, build configuration)
        - Launch builds, shell sessions, and config exports
        - Real-time output log with automatic scrolling
        - Registry refresh (remote pull or local reload)
        """

        TITLE = "BSP Registry Launcher"
        SUB_TITLE = "Advantech Board Support Package Manager"

        CSS = """
        /* ── Layout ─────────────────────────────────────────── */
        #main-layout {
            height: 1fr;
        }

        #left-panel {
            width: 35%;
            border: round $primary;
            padding: 0 1;
        }

        #right-panel {
            width: 65%;
            border: round $primary;
            padding: 0 1;
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

        /* ── BSP table ───────────────────────────────────────── */
        #bsp-table {
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
            padding: 0 1;
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
            Binding("c", "containers", "Containers"),
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
                    yield Label("Available BSPs", classes="detail-key")
                    yield DataTable(id="bsp-table", cursor_type="row")

                with Vertical(id="right-panel"):
                    yield Label("BSP Details", classes="detail-key")
                    yield ScrollableContainer(
                        Static("Select a BSP from the list.", id="detail-view"),
                        id="detail-scroll",
                    )

            # Action buttons
            with Horizontal(id="action-bar"):
                yield Button("▶ Build", id="btn-build", variant="success", disabled=True)
                yield Button("$ Shell", id="btn-shell", variant="default", disabled=True)
                yield Button("↑ Export", id="btn-export", variant="default", disabled=True)
                yield Button("☰ Containers", id="btn-containers", variant="default")
                yield Button("✕ Cancel", id="btn-cancel", variant="error", disabled=True)

            # Output log panel
            with Vertical(id="log-panel"):
                yield Label("Output Log", classes="detail-key")
                yield RichLog(id="output-log", highlight=True, markup=True, wrap=True)

            yield Static("", id="status-bar")
            yield Footer()

        # ── Lifecycle ────────────────────────────────────────────

        def on_mount(self) -> None:
            """Initialize BSP table columns and load the registry."""
            table = self.query_one("#bsp-table", DataTable)
            table.add_columns("Name", "Description")
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
                self.call_from_thread(self._populate_bsp_table, registry_path)

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

        def _populate_bsp_table(self, registry_path: str) -> None:
            """Update the BSP table with loaded data (runs on UI thread)."""
            table = self.query_one("#bsp-table", DataTable)
            # Clear rows AND columns to fully reset table state, preventing
            # "row key already exists" errors if populate is called more than once.
            table.clear(columns=True)
            table.add_columns("Name", "Description")

            label = self.query_one("#registry-label", Label)
            label.update(f"Registry: {registry_path}")

            if self._bsp_manager and self._bsp_manager.model.registry.bsp:
                for bsp in self._bsp_manager.model.registry.bsp:
                    table.add_row(bsp.name, bsp.description, key=bsp.name)
                self._set_status(
                    f"Loaded {len(self._bsp_manager.model.registry.bsp)} BSP(s)"
                )
                self._log(
                    f"[green]Registry loaded:[/green] {len(self._bsp_manager.model.registry.bsp)} BSP(s) available"
                )
            else:
                self._set_status("No BSPs found in registry")
                self._log("[yellow]No BSPs found in registry[/yellow]")

        # ── BSP selection ────────────────────────────────────────

        @on(DataTable.RowSelected, "#bsp-table")
        def on_bsp_selected(self, event: DataTable.RowSelected) -> None:
            """Show details for the selected BSP and enable action buttons."""
            self._selected_bsp_name = str(event.row_key.value)
            self._show_bsp_details(self._selected_bsp_name)

            # Enable action buttons
            for btn_id in ("#btn-build", "#btn-shell", "#btn-export"):
                self.query_one(btn_id, Button).disabled = False

        def _show_bsp_details(self, bsp_name: str) -> None:
            """Render BSP details in the right panel."""
            if not self._bsp_manager:
                return

            bsp = None
            for b in self._bsp_manager.model.registry.bsp:
                if b.name == bsp_name:
                    bsp = b
                    break

            if bsp is None:
                return

            lines = [
                f"[bold cyan]Name:[/bold cyan]        {bsp.name}",
                f"[bold cyan]Description:[/bold cyan] {bsp.description}",
            ]

            if bsp.os:
                lines.append(
                    f"[bold cyan]OS:[/bold cyan]          {bsp.os.name} / {bsp.os.build_system} {bsp.os.version}"
                )

            lines.append(f"[bold cyan]Build Path:[/bold cyan]  {bsp.build.path}")

            if bsp.build.configuration:
                lines.append("[bold cyan]Config Files:[/bold cyan]")
                for cfg in bsp.build.configuration:
                    lines.append(f"  • {cfg}")

            if bsp.build.environment:
                env = bsp.build.environment
                if env.container:
                    lines.append(f"[bold cyan]Container:[/bold cyan]   {env.container}")
                elif env.docker and env.docker.image:
                    lines.append(f"[bold cyan]Image:[/bold cyan]       {env.docker.image}")

            detail_widget = self.query_one("#detail-view", Static)
            detail_widget.update("\n".join(lines))

        # ── Button actions ───────────────────────────────────────

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

        @on(Button.Pressed, "#btn-containers")
        def on_containers(self) -> None:
            self.action_containers()

        @on(Button.Pressed, "#btn-cancel")
        def on_cancel(self) -> None:
            self.action_cancel()

        # ── Keybinding actions ───────────────────────────────────

        def action_refresh(self) -> None:
            """Reload the BSP registry."""
            self._bsp_manager = None
            self._selected_bsp_name = None
            for btn_id in ("#btn-build", "#btn-shell", "#btn-export", "#btn-cancel"):
                self.query_one(btn_id, Button).disabled = True
            self.query_one("#detail-view", Static).update("Select a BSP from the list.")
            self._load_registry_async()

        def action_build(self) -> None:
            """Build the selected BSP."""
            if not self._selected_bsp_name:
                self._log("[yellow]No BSP selected[/yellow]")
                return

            def _confirmed(confirmed: bool) -> None:
                if confirmed:
                    self._run_bsp_command("build", self._selected_bsp_name)

            self.push_screen(
                ConfirmScreen(f"Build BSP '{self._selected_bsp_name}'?"),
                _confirmed,
            )

        def action_shell(self) -> None:
            """Open a shell for the selected BSP."""
            if not self._selected_bsp_name:
                self._log("[yellow]No BSP selected[/yellow]")
                return
            self._log(
                f"[yellow]Shell mode opens a terminal — run:[/yellow] "
                f"bsp shell {self._selected_bsp_name}"
            )
            self._set_status(
                f"Tip: run 'bsp shell {self._selected_bsp_name}' in your terminal"
            )

        def action_export_config(self) -> None:
            """Export configuration for the selected BSP."""
            if not self._selected_bsp_name:
                self._log("[yellow]No BSP selected[/yellow]")
                return
            self._run_bsp_command("export", self._selected_bsp_name)

        def action_containers(self) -> None:
            """List all containers in the registry."""
            self._run_bsp_command("containers")

        def action_cancel(self) -> None:
            """Terminate the currently running build/export command."""
            proc = self._running_process
            if proc is None:
                self._log("[yellow]No command is currently running[/yellow]")
                return
            try:
                proc.terminate()
                self._log("[yellow]Cancelling running command…[/yellow]")
                self._set_status("Cancelling…")
            except OSError:
                pass

        # ── CLI subprocess helper ────────────────────────────────

        def _run_bsp_command(self, *args: str) -> None:
            """
            Run a `bsp` CLI sub-command in a background thread and stream
            its stdout/stderr to the output log panel.
            """
            if self._running_process is not None:
                self._log("[yellow]A command is already running — please wait[/yellow]")
                return

            # Build the command, forwarding registry path if set
            cmd = [sys.executable, "-m", "bsp.cli_runner"]
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
            self._set_status(f"Running: bsp {' '.join(args)}")
            self._set_cancel_button(disabled=False)

            thread = threading.Thread(
                target=self._stream_command, args=(cmd,), daemon=True
            )
            thread.start()

        def _stream_command(self, cmd: list) -> None:
            """Execute *cmd*, streaming its combined output to the log (background thread)."""
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                self._running_process = proc

                for line in proc.stdout:
                    self.call_from_thread(self._log, line.rstrip())

                proc.wait()
                rc = proc.returncode

                if rc == 0:
                    self.call_from_thread(
                        self._log, "[green]Command finished successfully[/green]"
                    )
                    self.call_from_thread(self._set_status, "Done")
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

            except Exception as exc:
                self.call_from_thread(
                    self._log, f"[red]Error running command: {exc}[/red]"
                )
                self.call_from_thread(self._set_status, f"Error: {exc}")
            finally:
                self._running_process = None
                self.call_from_thread(
                    self._set_cancel_button, disabled=True
                )

        def _set_cancel_button(self, *, disabled: bool) -> None:
            """Enable or disable the Cancel button (must run on UI thread)."""
            self.query_one("#btn-cancel", Button).disabled = disabled

        # ── Helpers ──────────────────────────────────────────────

        def _log(self, message: str) -> None:
            """Append *message* to the output log widget."""
            log_widget = self.query_one("#output-log", RichLog)
            log_widget.write(message)

        def _set_status(self, message: str) -> None:
            """Update the status bar with *message*."""
            status = self.query_one("#status-bar", Static)
            status.update(message)
