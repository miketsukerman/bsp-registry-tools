"""
Interactive CLI interface for BSP Assistant using Rich terminal formatting.
"""

import sys
from typing import List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from .models import UserRequirements
from .recommendation_engine import RecommendationEngine
from .registry_parser import RegistryParser


class AssistantCLI:
    """Command-line interface handler for BSP Assistant commands."""

    def __init__(
        self,
        engine: RecommendationEngine,
        console: Optional[Console] = None,
    ) -> None:
        self.engine = engine
        self.console = console or Console()

    def discover_interactive(
        self,
        board: Optional[str] = None,
        features: Optional[List[str]] = None,
        release_pref: Optional[str] = None,
        release_ver: Optional[str] = None,
        non_interactive: bool = False,
    ) -> int:
        """
        Interactive multi-step Q&A discovery workflow.
        """
        self.console.print()
        self.console.print("🤖 [bold cyan]Welcome to BSP Assistant![/bold cyan]")
        self.console.print("   [dim]Let me help you find the perfect Board Support Package.[/dim]")
        self.console.print()

        devices = self.engine.registry_data.devices
        all_features = self.engine.registry_data.features

        # 1. Device selection
        if not board and not non_interactive:
            device_names = [f"{d.slug} ({d.name})" for d in devices[:6]]
            self.console.print("[bold yellow]? What board do you have?[/bold yellow]")
            if device_names:
                self.console.print(f"  [dim]Available: {', '.join(d.slug for d in devices[:5])}...[/dim]")
            board_input = Prompt.ask("  → Enter board name or slug", console=self.console)
            board = board_input.strip() if board_input else None

        # 2. Features selection
        if features is None and not non_interactive:
            self.console.print()
            self.console.print("[bold yellow]? What features do you need?[/bold yellow]")
            if all_features:
                self.console.print(f"  [dim]Available features: {', '.join(f.slug for f in all_features)}[/dim]")
            feat_input = Prompt.ask("  → Enter features (comma-separated, or press Enter for none)", console=self.console)
            if feat_input and feat_input.strip():
                features = [f.strip() for f in feat_input.split(",") if f.strip()]
            else:
                features = []
        elif features is None:
            features = []

        # 3. Release preference
        if not release_pref and not non_interactive:
            self.console.print()
            self.console.print("[bold yellow]? Preferred Yocto release?[/bold yellow]")
            self.console.print("  [cyan]1[/cyan]) Latest")
            self.console.print("  [cyan]2[/cyan]) LTS")
            self.console.print("  [cyan]3[/cyan]) Specific release...")
            choice = Prompt.ask("  → Selection", choices=["1", "2", "3", "latest", "lts", "specific"], default="1", console=self.console)
            if choice in ("1", "latest"):
                release_pref = "latest"
            elif choice in ("2", "lts"):
                release_pref = "lts"
            else:
                release_pref = "specific"
                release_ver = Prompt.ask("  → Enter release slug (e.g. scarthgap)", console=self.console).strip()
        elif not release_pref:
            release_pref = "latest"

        # Run recommendation
        reqs = UserRequirements(
            board_name=board,
            features=features or [],
            release_preference=release_pref,  # type: ignore[arg-type]
            release_version=release_ver,
        )

        rec = self.engine.recommend(reqs)

        # Render rich output
        self._render_recommendation(rec)
        return 0

    def _render_recommendation(self, rec) -> None:
        """Display recommended preset with formatted details."""
        self.console.print()
        lines: List[str] = [
            f"[bold green]🎯 Recommended Preset:[/bold green]\n",
            f"  [bold cyan]{rec.preset_name}[/bold cyan]\n",
            "[bold yellow]📊 Why this choice?[/bold yellow]",
        ]
        for r in rec.reasoning:
            lines.append(f"  {r}")

        lines.append(f"\n[bold magenta]⏱️  Estimated build time:[/bold magenta] {rec.build_time_estimate}")

        if rec.alternatives:
            lines.append("\n[bold blue]💡 Alternative options:[/bold blue]")
            for alt in rec.alternatives:
                lines.append(f"  • {alt}")

        if rec.known_issues:
            lines.append("\n[bold red]⚠️  Known issues / Warnings:[/bold red]")
            for issue in rec.known_issues:
                lines.append(f"  • {issue}")

        lines.append(f"\n[bold white]🚀 Ready to build?[/bold white]\n   [green]bsp build {rec.preset_name}[/green]")

        panel_content = "\n".join(lines)
        self.console.print(Panel(panel_content, border_style="cyan", expand=False))

    def explain(self, preset_name: str) -> int:
        """Explain a preset configuration in detail."""
        found = self.engine._find_preset(preset_name)
        if not found:
            self.console.print(f"[bold red]Error:[/bold red] Preset '{preset_name}' not found.")
            return 1

        device_info = self.engine._devices_by_slug.get(found.device.lower())
        risk = self.engine.get_risk_assessment(found.name)

        title = f"Preset Explanation: {found.name}"
        lines: List[str] = [
            f"[bold]Description:[/bold] {found.description or '(no description)'}",
            f"[bold]Target Device:[/bold] {found.device} ({device_info.name if device_info else 'Unknown'})",
            f"[bold]Vendor:[/bold] {device_info.vendor if device_info else 'N/A'}",
            f"[bold]SoC Family:[/bold] {device_info.soc_family if device_info else 'N/A'}",
            f"[bold]Releases:[/bold] {', '.join(found.releases) if found.releases else 'default'}",
            f"[bold]Stability Status:[/bold] [{('green' if found.stability_status == 'stable' else 'yellow')}]{found.stability_status}[/]",
            f"[bold]Tested Combinations:[/bold] {'[green]Yes[/green]' if found.tested_combinations else '[yellow]No[/yellow]'}",
            f"[bold]Included Features:[/bold] {', '.join(found.features) if found.features else '(none)'}",
            f"[bold]Risk Level:[/bold] [{('green' if risk.risk_level == 'low' else 'red')}]{risk.risk_level.upper()}[/]",
        ]

        if risk.warnings:
            lines.append("\n[bold red]Warnings / Considerations:[/bold red]")
            for w in risk.warnings:
                lines.append(f"  • {w}")

        lines.append(f"\n[bold]Build Command:[/bold] [green]bsp build {found.name}[/green]")

        self.console.print(Panel("\n".join(lines), title=title, border_style="green", expand=False))
        return 0

    def check(
        self,
        board: str,
        features: List[str],
        release: Optional[str] = None,
    ) -> int:
        """Check compatibility for a board, release, and feature set."""
        report = self.engine.check_compatibility(board, features)

        title = f"Compatibility Check: {board}"
        table = Table(title=title, show_header=True, header_style="bold cyan")
        table.add_column("Feature", style="dim")
        table.add_column("Status")
        table.add_column("Note")

        for f in report.supported_features:
            table.add_row(f, "[green]✓ Compatible[/green]", "Supported by device hardware")

        for f in report.unsupported_features:
            table.add_row(f, "[red]✗ Incompatible[/red]", "Vendor/SoC constraints not met")

        self.console.print()
        self.console.print(table)

        if report.compatible:
            self.console.print("\n[bold green]✓ Overall Status: Fully Compatible[/bold green]")
        else:
            self.console.print("\n[bold red]✗ Overall Status: Compatibility Issues Detected[/bold red]")

        for r in report.reasons:
            self.console.print(f"  • {r}")

        return 0 if report.compatible else 1

    def find(self, feature: str, release: Optional[str] = None) -> int:
        """Find presets and devices compatible with a specific feature."""
        feat_norm = feature.lower()
        presets = self.engine.registry_data.presets
        matching_presets = [
            p for p in presets if feat_norm in [f.lower() for f in p.features]
        ]

        devices = self.engine.registry_data.devices
        compatible_devices = [
            d for d in devices if feat_norm in [f.lower() for f in d.supported_features]
        ]

        self.console.print()
        self.console.print(f"[bold cyan]Presets providing feature '{feature}':[/bold cyan]")
        if matching_presets:
            p_table = Table(show_header=True, header_style="bold magenta")
            p_table.add_column("Preset Name")
            p_table.add_column("Device")
            p_table.add_column("Releases")
            p_table.add_column("Features")
            for p in matching_presets:
                p_table.add_row(p.name, p.device, ", ".join(p.releases), ", ".join(p.features))
            self.console.print(p_table)
        else:
            self.console.print(f"  [dim]No existing presets include '{feature}' directly.[/dim]")

        self.console.print()
        self.console.print(f"[bold cyan]Hardware devices compatible with '{feature}':[/bold cyan]")
        if compatible_devices:
            d_table = Table(show_header=True, header_style="bold blue")
            d_table.add_column("Device Slug")
            d_table.add_column("Name")
            d_table.add_column("Vendor")
            d_table.add_column("SoC Family")
            for d in compatible_devices:
                d_table.add_row(d.slug, d.name, d.vendor, d.soc_family or "N/A")
            self.console.print(d_table)
        else:
            self.console.print(f"  [dim]No devices currently compatible with '{feature}'.[/dim]")

        return 0

    def list_devices(self) -> int:
        """List all devices with vendor, SoC, and supported features count."""
        devices = self.engine.registry_data.devices
        table = Table(title="Available Hardware Devices", show_header=True, header_style="bold cyan")
        table.add_column("Slug", style="bold")
        table.add_column("Name")
        table.add_column("Vendor")
        table.add_column("SoC Family")
        table.add_column("Supported Features")

        for d in devices:
            feat_str = ", ".join(d.supported_features[:4])
            if len(d.supported_features) > 4:
                feat_str += f" (+{len(d.supported_features) - 4} more)"
            table.add_row(d.slug, d.name, d.vendor, d.soc_family or "N/A", feat_str or "(none)")

        self.console.print()
        self.console.print(table)
        return 0

    def list_features(self) -> int:
        """List all available features and their descriptions."""
        features = self.engine.registry_data.features
        table = Table(title="Available Features", show_header=True, header_style="bold cyan")
        table.add_column("Slug", style="bold")
        table.add_column("Name")
        table.add_column("Description")
        table.add_column("Compatible Vendors")

        for f in features:
            vendors_str = ", ".join(f.compatible_vendors) if f.compatible_vendors else "all"
            table.add_row(f.slug, f.name, f.description, vendors_str)

        self.console.print()
        self.console.print(table)
        return 0

    def suggest(self, board: str) -> int:
        """Suggest presets for a specific board."""
        reqs = UserRequirements(board_name=board, features=[], release_preference="latest")
        rec = self.engine.recommend(reqs)
        self._render_recommendation(rec)
        return 0


def parse_feature_list(features_arg) -> List[str]:
    """Parse comma-separated or list-based feature arguments."""
    if not features_arg:
        return []
    if isinstance(features_arg, str):
        return [f.strip() for f in features_arg.split(",") if f.strip()]
    result = []
    for item in features_arg:
        for f in str(item).split(","):
            if f.strip():
                result.append(f.strip())
    return result


def dispatch_assistant(args, bsp_mgr) -> int:
    """
    Entry point dispatching assistant subcommands from the main bsp CLI.

    Args:
        args: Parsed argparse namespace.
        bsp_mgr: Initialized BspManager instance with registry loaded.

    Returns:
        Integer exit code (0 for success, non-zero for errors).
    """
    parser = RegistryParser()
    if getattr(bsp_mgr, "registry", None) is not None:
        registry_data = parser.load_registry_root(bsp_mgr.registry)
    elif getattr(bsp_mgr, "config_path", None) is not None:
        registry_data = parser.load_registry(str(bsp_mgr.config_path))
    else:
        raise ValueError("No registry found in BSP manager")

    engine = RecommendationEngine(registry_data)
    cli = AssistantCLI(engine=engine)

    cmd = getattr(args, "assistant_command", None)

    if cmd is None or cmd == "discover":
        feats = parse_feature_list(getattr(args, "features", None))
        return cli.discover_interactive(
            board=getattr(args, "board", None),
            features=feats,
            release_pref=getattr(args, "release_pref", None),
            release_ver=getattr(args, "release", None),
            non_interactive=getattr(args, "non_interactive", False),
        )
    elif cmd == "explain":
        return cli.explain(args.preset)
    elif cmd == "check":
        feats = parse_feature_list(getattr(args, "features", None))
        board = getattr(args, "board", None)
        if not board:
            cli.console.print("[bold red]Error:[/bold red] --board is required for 'check'.")
            return 1
        return cli.check(
            board=board,
            features=feats,
            release=getattr(args, "release", None),
        )
    elif cmd == "find":
        feature = getattr(args, "feature", None)
        if not feature:
            cli.console.print("[bold red]Error:[/bold red] --feature is required for 'find'.")
            return 1
        return cli.find(
            feature=feature,
            release=getattr(args, "release", None),
        )
    elif cmd == "list-devices":
        return cli.list_devices()
    elif cmd == "list-features":
        return cli.list_features()
    elif cmd == "suggest":
        board = getattr(args, "board", None)
        if not board:
            cli.console.print("[bold red]Error:[/bold red] --board is required for 'suggest'.")
            return 1
        return cli.suggest(board=board)
    else:
        cli.console.print(f"[bold red]Unknown assistant command:[/bold red] {cmd}")
        return 1
