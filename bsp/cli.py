"""
CLI entry point for the BSP registry manager.
"""

import argparse
import logging
import sys
from importlib.metadata import version as _pkg_version, PackageNotFoundError
from pathlib import Path

from .bsp_manager import BspManager
from .exceptions import COLORAMA_AVAILABLE, ColoramaFormatter
from .registry_fetcher import DEFAULT_REMOTE_URL, DEFAULT_BRANCH, RegistryFetcher
from .utils import SUPPORTED_REGISTRY_VERSION

# =============================================================================
# Registry sub-command argument helpers
# =============================================================================


def _add_device_args(p: argparse.ArgumentParser, required: bool = True) -> None:
    p.add_argument("--slug", required=required, help="Unique device slug")
    p.add_argument("--description", default=None, help="Human-readable description")
    p.add_argument("--vendor", default=None, help="Board vendor slug")
    p.add_argument("--soc-vendor", dest="soc_vendor", default=None,
                   help="SoC vendor slug")
    p.add_argument("--soc-family", dest="soc_family", default=None,
                   help="SoC family identifier (optional)")
    p.add_argument("--includes", nargs="*", default=None, metavar="FILE",
                   help="KAS include files")


def _add_release_args(p: argparse.ArgumentParser, required: bool = True) -> None:
    p.add_argument("--slug", required=required, help="Unique release slug")
    p.add_argument("--description", default=None, help="Human-readable description")
    p.add_argument("--yocto-version", dest="yocto_version", default=None,
                   help="Yocto Project version string")
    p.add_argument("--includes", nargs="*", default=None, metavar="FILE",
                   help="KAS include files")
    p.add_argument("--environment", default=None,
                   help="Named environment to use for this release")
    p.add_argument("--distro", default=None, help="Distro slug for this release")


def _add_feature_args(p: argparse.ArgumentParser, required: bool = True) -> None:
    p.add_argument("--slug", required=required, help="Unique feature slug")
    p.add_argument("--description", default=None, help="Human-readable description")
    p.add_argument("--includes", nargs="*", default=None, metavar="FILE",
                   help="KAS include files")


def _add_preset_args(p: argparse.ArgumentParser, required: bool = True) -> None:
    p.add_argument("--name", required=required, help="Unique preset name")
    p.add_argument("--description", default=None, help="Human-readable description")
    p.add_argument("--device", dest="preset_device", default=None,
                   help="Device slug")
    p.add_argument("--release", dest="preset_release", default=None,
                   help="Release slug")
    p.add_argument("--features", nargs="*", default=None, metavar="FEATURE",
                   help="Feature slugs to enable")
    p.add_argument("--container", dest="preset_container", default=None,
                   help="Docker container name for the build")
    p.add_argument("--build-path", dest="build_path", default=None,
                   help="Build output directory path")


def _add_vendor_args(p: argparse.ArgumentParser, required: bool = True) -> None:
    p.add_argument("--slug", required=required, help="Unique vendor slug")
    p.add_argument("--name", dest="vendor_name", default=None,
                   help="Human-readable vendor name")
    p.add_argument("--description", default=None, help="Vendor description")
    p.add_argument("--website", default=None, help="Vendor website URL")
    p.add_argument("--includes", nargs="*", default=None, metavar="FILE",
                   help="KAS include files common to all boards")


def _add_distro_args(p: argparse.ArgumentParser, required: bool = True) -> None:
    p.add_argument("--slug", required=required, help="Unique distro slug")
    p.add_argument("--description", default=None, help="Human-readable description")
    p.add_argument("--vendor", default=None, help="Distro vendor/maintainer name")
    p.add_argument("--framework", default=None,
                   help="Build-system framework slug this distro is based on")
    p.add_argument("--includes", nargs="*", default=None, metavar="FILE",
                   help="KAS include files")


def _add_container_args(p: argparse.ArgumentParser, required: bool = True) -> None:
    p.add_argument("--name", dest="container_name", required=required,
                   help="Unique container name")
    p.add_argument("--image", default=None, help="Docker image name/tag")
    p.add_argument("--file", dest="dockerfile", default=None,
                   help="Path to Dockerfile")
    p.add_argument("--privileged", action="store_true", default=None,
                   help="Run container in privileged mode")


# =============================================================================
# Main Entry Point with Enhanced Commands (v2.0)
# =============================================================================


def main() -> int:
    """
    Main entry point for the BSP registry manager.

    Parses command line arguments, initializes the BSP manager,
    and executes the requested command.

    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    try:
        # Parse command line arguments
        try:
            _version = _pkg_version("bsp-registry-tools")
        except PackageNotFoundError:
            _version = "unknown"
        _version_str = (
            f"bsp-registry-tools {_version}\n"
            f"Supported model description version: {SUPPORTED_REGISTRY_VERSION}"
        )

        parser = argparse.ArgumentParser(description="Advantech Board Support Package Registry")
        parser.add_argument("--version", action="version", version=_version_str,
                            help="Show program version and supported model description version")
        parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
        parser.add_argument("--registry", "-r", default=None, help="BSP Registry file (local path)")
        parser.add_argument("--no-color", action="store_true", help="Disable colored output")
        parser.add_argument("--remote", default=DEFAULT_REMOTE_URL,
                            help="Remote registry git URL (default: %(default)s)")
        parser.add_argument("--branch", default=DEFAULT_BRANCH,
                            help="Remote registry branch (default: %(default)s)")
        parser.add_argument("--update", dest="update", action="store_true", default=True,
                            help="Update the cached registry clone before use (default)")
        parser.add_argument("--no-update", dest="update", action="store_false",
                            help="Skip updating the cached registry clone")
        parser.add_argument("--local", action="store_true",
                            help="Force local registry lookup only (do not use remote)")

        # GUI shortcut: `bsp gui` launches the TUI launcher
        parser.add_argument(
            '--gui',
            action='store_true',
            help='Launch the interactive GUI (requires the [gui] extra)'
        )

        # Create subparsers for different commands
        subparsers = parser.add_subparsers(dest='command', help='Command to execute', required=False)

        # GUI subcommand (alias for --gui flag)
        subparsers.add_parser('gui', help='Launch the interactive GUI launcher')

        # ----------------------------------------------------------------
        # Build command
        # ----------------------------------------------------------------
        build_parser = subparsers.add_parser("build", help="Build an image for BSP")
        build_parser.add_argument(
            "bsp_name",
            nargs="?",
            type=str,
            help="Name of the BSP preset to build (mutually exclusive with --device/--release)"
        )
        build_parser.add_argument(
            "--device", "-d",
            type=str,
            dest="device",
            help="Device slug (use with --release for component-based build)"
        )
        build_parser.add_argument(
            "--release",
            type=str,
            dest="release",
            help="Release slug (use with --device for component-based build)"
        )
        build_parser.add_argument(
            "--feature", "-f",
            action="append",
            dest="features",
            metavar="FEATURE",
            help="Feature slug to enable (can be specified multiple times)"
        )
        build_parser.add_argument(
            "--clean",
            action="store_true",
            help="Clean before building"
        )
        build_parser.add_argument(
            "--checkout",
            action="store_true",
            help="Checkout and validate build configuration without building (fast)"
        )
        build_parser.add_argument(
            "--target",
            type=str,
            dest="kas_target",
            metavar="TARGET",
            help="KAS build target to pass to BitBake (e.g. core-image-minimal)"
        )

        # ----------------------------------------------------------------
        # List command (with optional subtype)
        # ----------------------------------------------------------------
        list_parser = subparsers.add_parser("list", help="List available BSPs and components")
        list_parser.add_argument(
            "list_type",
            nargs="?",
            choices=["devices", "releases", "features", "distros"],
            default=None,
            help="Component type to list (omit to list BSP presets)"
        )
        list_parser.add_argument(
            "--device", "-d",
            type=str,
            dest="device",
            help='Filter releases by device slug (only used with "releases")'
        )

        # List containers command
        subparsers.add_parser("containers", help="List available containers")

        # ----------------------------------------------------------------
        # Tree command
        # ----------------------------------------------------------------
        tree_parser = subparsers.add_parser("tree", help="Display a tree view of the BSP registry")
        tree_mode_group = tree_parser.add_mutually_exclusive_group()
        tree_mode_group.add_argument(
            "--full",
            action="store_true",
            help="Show full details including includes and descriptions for all items"
        )
        tree_mode_group.add_argument(
            "--compact",
            action="store_true",
            help="Show compact output with names/slugs only"
        )

        # ----------------------------------------------------------------
        # Export command
        # ----------------------------------------------------------------
        export_parser = subparsers.add_parser("export", help="Export BSP configuration")
        export_parser.add_argument(
            "bsp_name",
            nargs="?",
            type=str,
            help="Name of the BSP preset to export (mutually exclusive with --device/--release)"
        )
        export_parser.add_argument(
            "--device", "-d",
            type=str,
            dest="device",
            help="Device slug"
        )
        export_parser.add_argument(
            "--release",
            type=str,
            dest="release",
            help="Release slug"
        )
        export_parser.add_argument(
            "--feature", "-f",
            action="append",
            dest="features",
            metavar="FEATURE",
            help="Feature slug to enable (can be specified multiple times)"
        )
        export_parser.add_argument(
            "--output", "-o",
            type=str,
            help="Output file path (default: stdout)"
        )

        # ----------------------------------------------------------------
        # Shell command
        # ----------------------------------------------------------------
        shell_parser = subparsers.add_parser("shell", help="Enter interactive shell for BSP")
        shell_parser.add_argument(
            "bsp_name",
            nargs="?",
            type=str,
            help="Name of the BSP preset (mutually exclusive with --device/--release)"
        )
        shell_parser.add_argument(
            "--device", "-d",
            type=str,
            dest="device",
            help="Device slug"
        )
        shell_parser.add_argument(
            "--release",
            type=str,
            dest="release",
            help="Release slug"
        )
        shell_parser.add_argument(
            "--feature", "-f",
            action="append",
            dest="features",
            metavar="FEATURE",
            help="Feature slug to enable (can be specified multiple times)"
        )
        shell_parser.add_argument(
            "--command", "-c",
            type=str,
            dest="shell_command",
            help="Command to execute in shell (optional, if not provided starts interactive shell)"
        )

        # ----------------------------------------------------------------
        # Flash command
        # ----------------------------------------------------------------
        flash_parser = subparsers.add_parser(
            "flash",
            help="Flash BSP image to a target device (SD card or eMMC)"
        )
        flash_parser.add_argument(
            "bsp_name",
            nargs="?",
            type=str,
            help="BSP preset name to flash"
        )
        flash_parser.add_argument(
            "--target", "-t",
            type=str,
            required=True,
            metavar="DEVICE",
            help="Target block device (e.g. /dev/sda, /dev/mmcblk0)"
        )
        flash_parser.add_argument(
            "--image", "-i",
            type=str,
            default=None,
            metavar="IMAGE",
            help="Path to the image file to flash (auto-selected if omitted)"
        )

        # ----------------------------------------------------------------
        # Registry editing command group
        # ----------------------------------------------------------------
        registry_parser = subparsers.add_parser(
            "registry",
            help="Edit the BSP registry (add/remove/update entities)"
        )
        registry_subparsers = registry_parser.add_subparsers(
            dest="registry_action",
            help="Registry editing action",
            required=True,
        )

        # registry init
        reg_init_parser = registry_subparsers.add_parser(
            "init",
            help="Create a minimal bsp-registry.yaml in the current directory"
        )
        reg_init_parser.add_argument(
            "--output", "-o",
            type=str,
            default="bsp-registry.yaml",
            metavar="PATH",
            help="Output file path (default: bsp-registry.yaml)"
        )
        reg_init_parser.add_argument(
            "--force", action="store_true",
            help="Overwrite an existing file"
        )

        # registry validate
        registry_subparsers.add_parser(
            "validate",
            help="Validate the registry and report errors/warnings"
        )

        # registry diff
        registry_subparsers.add_parser(
            "diff",
            help="Show diff between in-memory (or saved) registry and remote cached copy"
        )

        # registry add <entity>
        reg_add_parser = registry_subparsers.add_parser(
            "add",
            help="Add an entity to the registry"
        )
        reg_add_entity_sub = reg_add_parser.add_subparsers(
            dest="entity", help="Entity type to add", required=True
        )

        # add device
        _add_device_args(reg_add_entity_sub.add_parser(
            "device", help="Add a hardware device"
        ))
        # add release
        _add_release_args(reg_add_entity_sub.add_parser(
            "release", help="Add a Yocto/Isar release"
        ))
        # add feature
        _add_feature_args(reg_add_entity_sub.add_parser(
            "feature", help="Add an optional BSP feature"
        ))
        # add preset
        _add_preset_args(reg_add_entity_sub.add_parser(
            "preset", help="Add a BSP preset"
        ))
        # add vendor
        _add_vendor_args(reg_add_entity_sub.add_parser(
            "vendor", help="Add a board vendor"
        ))
        # add distro
        _add_distro_args(reg_add_entity_sub.add_parser(
            "distro", help="Add a distro/build-system definition"
        ))
        # add container
        _add_container_args(reg_add_entity_sub.add_parser(
            "container", help="Add a Docker container definition"
        ))

        # registry edit <entity> <slug>
        reg_edit_parser = registry_subparsers.add_parser(
            "edit",
            help="Edit an existing entity in the registry"
        )
        reg_edit_entity_sub = reg_edit_parser.add_subparsers(
            dest="entity", help="Entity type to edit", required=True
        )
        for entity_name, adder in [
            ("device", _add_device_args),
            ("release", _add_release_args),
            ("feature", _add_feature_args),
            ("preset", _add_preset_args),
            ("vendor", _add_vendor_args),
            ("distro", _add_distro_args),
            ("container", _add_container_args),
        ]:
            ep = reg_edit_entity_sub.add_parser(entity_name, help=f"Edit a {entity_name}")
            ep.add_argument("slug_or_name", metavar="SLUG_OR_NAME",
                            help="Slug or name of the entity to edit")
            adder(ep, required=False)
            ep.add_argument(
                "--editor", action="store_true",
                help="Open entity YAML block in $EDITOR for free-form editing"
            )

        # registry remove <entity> <slug>
        reg_remove_parser = registry_subparsers.add_parser(
            "remove",
            help="Remove an entity from the registry"
        )
        reg_remove_entity_sub = reg_remove_parser.add_subparsers(
            dest="entity", help="Entity type to remove", required=True
        )
        for entity_name in ("device", "release", "feature", "preset",
                            "vendor", "distro", "container"):
            rp = reg_remove_entity_sub.add_parser(entity_name, help=f"Remove a {entity_name}")
            rp.add_argument("slug_or_name", metavar="SLUG_OR_NAME",
                            help="Slug or name of the entity to remove")
            rp.add_argument("--force", action="store_true",
                            help="Skip confirmation prompt")
            rp.add_argument("--commit", action="store_true",
                            help="Auto-commit the change with git")

        # registry show <entity> <slug>
        reg_show_parser = registry_subparsers.add_parser(
            "show",
            help="Pretty-print a single entity from the registry"
        )
        reg_show_entity_sub = reg_show_parser.add_subparsers(
            dest="entity", help="Entity type to show", required=True
        )
        for entity_name in ("device", "release", "feature", "preset",
                            "vendor", "distro", "container"):
            sp = reg_show_entity_sub.add_parser(entity_name, help=f"Show a {entity_name}")
            sp.add_argument("slug_or_name", metavar="SLUG_OR_NAME",
                            help="Slug or name of the entity to show")

        args = parser.parse_args()

        # --gui flag or 'bsp gui' subcommand → launch TUI
        if getattr(args, 'gui', False) or args.command == 'gui':
            from .gui import launch_gui
            return launch_gui(
                registry_path=args.registry,
                remote=args.remote if args.remote != DEFAULT_REMOTE_URL else None,
                branch=args.branch if args.branch != DEFAULT_BRANCH else None,
                no_update=not args.update,
            )

        if not args.command:
            parser.print_help()
            return 1

        # Setup logging based on verbosity
        log_level = logging.DEBUG if args.verbose else logging.WARNING

        # Setup logging colors
        if args.no_color or not COLORAMA_AVAILABLE:
            logging.basicConfig(
                level=log_level,
                format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
        else:
            logging.basicConfig(level=log_level)
            logger = logging.getLogger()
            handler = logger.handlers[0]
            handler.setFormatter(ColoramaFormatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            ))

        # Resolve registry file path
        LOCAL_DEFAULTS = ["bsp-registry.yaml", "bsp-registry.yml"]
        local_registry = next((name for name in LOCAL_DEFAULTS if Path(name).is_file()), None)
        if args.registry is not None:
            registry_path = args.registry
            logging.info("Using explicitly provided registry: %s", registry_path)
        elif args.local:
            registry_path = local_registry or LOCAL_DEFAULTS[0]
            logging.info("Using local registry (--local): %s", registry_path)
        elif local_registry is not None:
            registry_path = local_registry
            logging.info("Using local registry: %s", registry_path)
        else:
            fetcher = RegistryFetcher()
            registry_path = str(fetcher.fetch_registry(
                repo_url=args.remote,
                branch=args.branch,
                update=args.update,
            ))
            logging.info("Using remote registry cached at: %s", registry_path)

        # Initialize and run BSP manager
        # The `registry init` sub-command creates a new file from scratch, so
        # it must bypass BspManager initialisation (the file may not exist yet).
        if args.command == "registry" and getattr(args, "registry_action", None) == "init":
            return _dispatch_registry(args, registry_path)

        bsp_mgr = BspManager(registry_path, verbose=args.verbose)
        bsp_mgr.initialize()

        # ----------------------------------------------------------------
        # Dispatch commands
        # ----------------------------------------------------------------
        def _check_exclusive(bsp_name, device, release, parser):
            """Return True and log error if bsp_name and device/release are both set."""
            if bsp_name and (device or release):
                logging.error(
                    "Cannot mix a positional preset name with --device/--release. "
                    "Use either '<command> <preset>' or "
                    "'<command> --device <d> --release <r>'."
                )
                return True
            return False

        if args.command == "build":
            checkout_only = getattr(args, "checkout", False)
            device = getattr(args, "device", None)
            release = getattr(args, "release", None)
            features = getattr(args, "features", None) or []
            bsp_name = getattr(args, "bsp_name", None)
            kas_target = getattr(args, "kas_target", None)

            if _check_exclusive(bsp_name, device, release, build_parser):
                return 1
            if bsp_name:
                bsp_mgr.build_bsp(bsp_name, checkout_only=checkout_only, target=kas_target)
            elif device and release:
                bsp_mgr.build_by_components(
                    device, release, features, checkout_only=checkout_only, target=kas_target
                )
            else:
                logging.error(
                    "Specify either a BSP preset name or both --device and --release."
                )
                build_parser.print_help()
                return 1

        elif args.command == "list":
            list_type = getattr(args, "list_type", None)
            device = getattr(args, "device", None)
            use_color = not args.no_color
            if list_type == "devices":
                bsp_mgr.list_devices(use_color=use_color)
            elif list_type == "releases":
                bsp_mgr.list_releases(device_slug=device, use_color=use_color)
            elif list_type == "features":
                bsp_mgr.list_features(use_color=use_color)
            elif list_type == "distros":
                bsp_mgr.list_distros(use_color=use_color)
            else:
                bsp_mgr.list_bsp(use_color=use_color)

        elif args.command == "containers":
            bsp_mgr.list_containers(use_color=not args.no_color)

        elif args.command == "tree":
            full = getattr(args, "full", False)
            compact = getattr(args, "compact", False)
            mode = "full" if full else ("compact" if compact else "default")
            bsp_mgr.tree_bsp(use_color=not args.no_color, mode=mode)

        elif args.command == "export":
            device = getattr(args, "device", None)
            release = getattr(args, "release", None)
            features = getattr(args, "features", None) or []
            bsp_name = getattr(args, "bsp_name", None)
            output = getattr(args, "output", None)

            if _check_exclusive(bsp_name, device, release, export_parser):
                return 1
            if bsp_name:
                bsp_mgr.export_bsp_config(bsp_name=bsp_name, output_file=output)
            elif device and release:
                bsp_mgr.export_by_components(
                    device, release, features, output_file=output
                )
            else:
                logging.error(
                    "Specify either a BSP preset name or both --device and --release."
                )
                export_parser.print_help()
                return 1

        elif args.command == "shell":
            shell_command = getattr(args, "shell_command", None)
            device = getattr(args, "device", None)
            release = getattr(args, "release", None)
            features = getattr(args, "features", None) or []
            bsp_name = getattr(args, "bsp_name", None)

            if _check_exclusive(bsp_name, device, release, shell_parser):
                return 1
            if bsp_name:
                bsp_mgr.shell_into_bsp(bsp_name=bsp_name, command=shell_command)
            elif device and release:
                bsp_mgr.shell_by_components(
                    device, release, features, command=shell_command
                )
            else:
                logging.error(
                    "Specify either a BSP preset name or both --device and --release."
                )
                shell_parser.print_help()
                return 1

        elif args.command == "flash":
            bsp_name = getattr(args, "bsp_name", None)
            target = getattr(args, "target", None)
            image = getattr(args, "image", None)
            if not bsp_name:
                logging.error("Specify a BSP preset name.")
                flash_parser.print_help()
                return 1
            bsp_mgr.flash_bsp(bsp_name=bsp_name, target=target, image_path=image)

        elif args.command == "registry":
            # Registry editing commands do not use BspManager; they use
            # RegistryWriter directly so they can mutate and save the YAML.
            bsp_mgr.cleanup()
            return _dispatch_registry(args, registry_path)

        else:
            logging.error(f"Unknown command: {args.command}")
            parser.print_help()
            return 1

        bsp_mgr.cleanup()
        logging.info("Command completed successfully")
        return 0

    except KeyboardInterrupt:
        logging.info("BSP manager interrupted by user")
        return 130  # Standard exit code for SIGINT
    except SystemExit as e:
        # Re-raise system exit with proper code
        return e.code if isinstance(e.code, int) else 1
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        return 1


# =============================================================================
# Registry command dispatcher
# =============================================================================


def _dispatch_registry(args: argparse.Namespace, registry_path: str) -> int:
    """Handle all ``bsp registry <action>`` sub-commands.

    Args:
        args: Parsed namespace from argparse.
        registry_path: Path to the registry file (already resolved by main).

    Returns:
        Exit code (0 on success, 1 on error).
    """
    from .registry_writer import RegistryWriter, ValidationIssue
    from .models import BspBuild, BspPreset, Device, Distro, Docker, Feature, Release, Vendor

    action = args.registry_action

    # ---------------------------------------------------------------
    # init – create a new registry skeleton
    # ---------------------------------------------------------------
    if action == "init":
        output_path = Path(getattr(args, "output", "bsp-registry.yaml"))
        force = getattr(args, "force", False)
        if output_path.exists() and not force:
            print(f"Error: '{output_path}' already exists. Use --force to overwrite.")
            return 1
        _write_minimal_registry(output_path)
        print(f"Created minimal registry: {output_path}")
        return 0

    # ---------------------------------------------------------------
    # validate
    # ---------------------------------------------------------------
    if action == "validate":
        writer = RegistryWriter()
        try:
            writer.load(Path(registry_path))
        except SystemExit:
            return 1
        issues = writer.validate()
        if not issues:
            _color_print("✓ Registry is valid", "green")
            return 0
        has_errors = False
        for issue in issues:
            if issue.level == "error":
                _color_print(f"ERROR: {issue.message}", "red")
                has_errors = True
            else:
                _color_print(f"WARNING: {issue.message}", "yellow")
        return 1 if has_errors else 0

    # ---------------------------------------------------------------
    # diff
    # ---------------------------------------------------------------
    if action == "diff":
        writer = RegistryWriter()
        try:
            writer.load(Path(registry_path))
        except SystemExit:
            return 1
        diff_output = writer.diff()
        if diff_output:
            print(diff_output, end="")
        else:
            print("No differences found.")
        return 0

    # ---------------------------------------------------------------
    # add
    # ---------------------------------------------------------------
    if action == "add":
        entity = args.entity
        writer = RegistryWriter()
        try:
            writer.load(Path(registry_path))
        except SystemExit:
            return 1

        try:
            if entity == "device":
                obj = Device(
                    slug=args.slug,
                    description=args.description or "",
                    vendor=args.vendor or "",
                    soc_vendor=args.soc_vendor or "",
                    soc_family=args.soc_family,
                    includes=args.includes or [],
                )
                writer.add_device(obj)

            elif entity == "release":
                obj = Release(
                    slug=args.slug,
                    description=args.description or "",
                    yocto_version=args.yocto_version,
                    includes=args.includes or [],
                    environment=args.environment,
                    distro=args.distro,
                )
                writer.add_release(obj)

            elif entity == "feature":
                obj = Feature(
                    slug=args.slug,
                    description=args.description or "",
                    includes=args.includes or [],
                )
                writer.add_feature(obj)

            elif entity == "preset":
                build = None
                if args.preset_container or args.build_path:
                    build = BspBuild(
                        container=args.preset_container,
                        path=args.build_path,
                    )
                obj = BspPreset(
                    name=args.name,
                    description=args.description or "",
                    device=args.preset_device or "",
                    release=args.preset_release,
                    features=args.features or [],
                    build=build,
                )
                writer.add_preset(obj)

            elif entity == "vendor":
                obj = Vendor(
                    slug=args.slug,
                    name=args.vendor_name or args.slug,
                    description=args.description or "",
                    website=args.website or "",
                    includes=args.includes or [],
                )
                writer.add_vendor(obj)

            elif entity == "distro":
                obj = Distro(
                    slug=args.slug,
                    description=args.description or "",
                    vendor=args.vendor or "",
                    framework=args.framework,
                    includes=args.includes or [],
                )
                writer.add_distro(obj)

            elif entity == "container":
                obj = Docker(
                    image=args.image,
                    file=args.dockerfile,
                    privileged=bool(args.privileged),
                )
                writer.add_container(args.container_name, obj)

        except ValueError as exc:
            print(f"Error: {exc}")
            return 1

        issues = writer.validate()
        _print_issues(issues)
        writer.save()
        _git_stage_if_available(writer)
        print(f"Added {entity} to {registry_path}")
        return 0

    # ---------------------------------------------------------------
    # edit
    # ---------------------------------------------------------------
    if action == "edit":
        entity = args.entity
        slug_or_name = args.slug_or_name
        writer = RegistryWriter()
        try:
            writer.load(Path(registry_path))
        except SystemExit:
            return 1

        use_editor = getattr(args, "editor", False)
        if use_editor:
            return _open_in_editor(writer, entity, slug_or_name, registry_path)

        # Collect only the fields the user actually supplied (not None)
        fields: dict = {}
        try:
            if entity == "device":
                if args.description is not None:
                    fields["description"] = args.description
                if args.vendor is not None:
                    fields["vendor"] = args.vendor
                if args.soc_vendor is not None:
                    fields["soc_vendor"] = args.soc_vendor
                if args.soc_family is not None:
                    fields["soc_family"] = args.soc_family
                if args.includes is not None:
                    fields["includes"] = args.includes
                writer.update_device(slug_or_name, **fields)

            elif entity == "release":
                if args.description is not None:
                    fields["description"] = args.description
                if args.yocto_version is not None:
                    fields["yocto_version"] = args.yocto_version
                if args.includes is not None:
                    fields["includes"] = args.includes
                if args.environment is not None:
                    fields["environment"] = args.environment
                if args.distro is not None:
                    fields["distro"] = args.distro
                writer.update_release(slug_or_name, **fields)

            elif entity == "feature":
                if args.description is not None:
                    fields["description"] = args.description
                if args.includes is not None:
                    fields["includes"] = args.includes
                writer.update_feature(slug_or_name, **fields)

            elif entity == "preset":
                if args.description is not None:
                    fields["description"] = args.description
                if args.preset_device is not None:
                    fields["device"] = args.preset_device
                if args.preset_release is not None:
                    fields["release"] = args.preset_release
                if args.features is not None:
                    fields["features"] = args.features
                writer.update_preset(slug_or_name, **fields)

            elif entity == "vendor":
                if args.vendor_name is not None:
                    fields["name"] = args.vendor_name
                if args.description is not None:
                    fields["description"] = args.description
                if args.website is not None:
                    fields["website"] = args.website
                if args.includes is not None:
                    fields["includes"] = args.includes
                writer.update_vendor(slug_or_name, **fields)

            elif entity == "distro":
                if args.description is not None:
                    fields["description"] = args.description
                if args.vendor is not None:
                    fields["vendor"] = args.vendor
                if args.framework is not None:
                    fields["framework"] = args.framework
                if args.includes is not None:
                    fields["includes"] = args.includes
                writer.update_distro(slug_or_name, **fields)

            elif entity == "container":
                if args.image is not None:
                    fields["image"] = args.image
                if args.dockerfile is not None:
                    fields["file"] = args.dockerfile
                if args.privileged is not None:
                    fields["privileged"] = args.privileged
                writer.update_container(slug_or_name, **fields)

        except KeyError as exc:
            print(f"Error: {exc}")
            return 1

        issues = writer.validate()
        _print_issues(issues)
        writer.save()
        _git_stage_if_available(writer)
        commit = getattr(args, "commit", False)
        if commit:
            writer.git_commit(f"bsp registry: edit {entity} {slug_or_name}")
        print(f"Updated {entity} '{slug_or_name}' in {registry_path}")
        return 0

    # ---------------------------------------------------------------
    # remove
    # ---------------------------------------------------------------
    if action == "remove":
        entity = args.entity
        slug_or_name = args.slug_or_name
        force = getattr(args, "force", False)
        commit = getattr(args, "commit", False)

        writer = RegistryWriter()
        try:
            writer.load(Path(registry_path))
        except SystemExit:
            return 1

        refs = writer.find_references(entity, slug_or_name)
        if refs:
            print(f"Warning: the following entities reference {entity} '{slug_or_name}':")
            for ref in refs:
                print(f"  - {ref}")

        if not force:
            try:
                answer = input(
                    f"Remove {entity} '{slug_or_name}'? [y/N] "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return 1
            if answer not in ("y", "yes"):
                print("Aborted.")
                return 0

        try:
            remover = {
                "device": writer.remove_device,
                "release": writer.remove_release,
                "feature": writer.remove_feature,
                "preset": writer.remove_preset,
                "vendor": writer.remove_vendor,
                "distro": writer.remove_distro,
                "container": writer.remove_container,
            }.get(entity)
            if remover is None:
                print(f"Unknown entity type: {entity}")
                return 1
            remover(slug_or_name)
        except KeyError as exc:
            print(f"Error: {exc}")
            return 1

        writer.save()
        _git_stage_if_available(writer)
        if commit:
            writer.git_commit(f"bsp registry: remove {entity} {slug_or_name}")
        print(f"Removed {entity} '{slug_or_name}' from {registry_path}")
        return 0

    # ---------------------------------------------------------------
    # show
    # ---------------------------------------------------------------
    if action == "show":
        entity = args.entity
        slug_or_name = args.slug_or_name

        writer = RegistryWriter()
        try:
            writer.load(Path(registry_path))
        except SystemExit:
            return 1

        yaml_block = _entity_to_yaml(writer, entity, slug_or_name)
        if yaml_block is None:
            print(f"Error: {entity} '{slug_or_name}' not found")
            return 1
        print(yaml_block, end="")
        return 0

    print(f"Unknown registry action: {action}")
    return 1


# =============================================================================
# Registry command helpers
# =============================================================================


_MINIMAL_REGISTRY_YAML = """\
specification:
  version: "2.0"

registry:
  devices: []
  releases: []
  features: []
  bsp: []
"""


def _write_minimal_registry(path: Path) -> None:
    """Write a minimal registry skeleton to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_MINIMAL_REGISTRY_YAML, encoding="utf-8")


def _color_print(message: str, color: str) -> None:
    """Print *message* with optional ANSI colour when colorama is available."""
    if COLORAMA_AVAILABLE:
        from colorama import Fore, Style
        mapping = {
            "red": Fore.RED,
            "green": Fore.GREEN,
            "yellow": Fore.YELLOW,
            "cyan": Fore.CYAN,
        }
        code = mapping.get(color, "")
        print(f"{code}{message}{Style.RESET_ALL}")
    else:
        print(message)


def _print_issues(issues) -> None:
    """Print validation issues to stdout (warnings only; errors would have blocked save)."""
    for issue in issues:
        if issue.level == "warning":
            _color_print(f"WARNING: {issue.message}", "yellow")
        else:
            _color_print(f"ERROR: {issue.message}", "red")


def _git_stage_if_available(writer) -> None:
    """Silently try to git-stage the registry file."""
    writer.git_stage()


def _entity_to_yaml(writer, entity: str, slug_or_name: str):
    """Return a YAML string for a single entity, or ``None`` if not found."""
    import yaml

    root = writer.root
    if root is None:
        return None

    reg = root.registry

    entity_map = {
        "device": reg.devices or [],
        "release": reg.releases or [],
        "feature": reg.features or [],
        "vendor": reg.vendors or [],
        "distro": reg.distro or [],
        "preset": reg.bsp or [],
    }

    if entity == "container":
        containers = root.containers or {}
        docker = containers.get(slug_or_name)
        if docker is None:
            return None
        from .registry_writer import _docker_to_dict
        data = {slug_or_name: _docker_to_dict(docker)}
        return yaml.dump(data, default_flow_style=False, allow_unicode=True)

    items = entity_map.get(entity)
    if items is None:
        return None

    attr = "name" if entity == "preset" else "slug"
    obj = next((item for item in items if getattr(item, attr, None) == slug_or_name), None)
    if obj is None:
        return None

    import dataclasses
    data = dataclasses.asdict(obj)
    return yaml.dump({slug_or_name: data}, default_flow_style=False, allow_unicode=True)


def _open_in_editor(writer, entity: str, slug_or_name: str, registry_path: str) -> int:
    """Open the entity's YAML block in ``$EDITOR`` and apply changes on save."""
    import os
    import subprocess
    import tempfile
    import yaml
    import dataclasses

    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))

    yaml_block = _entity_to_yaml(writer, entity, slug_or_name)
    if yaml_block is None:
        print(f"Error: {entity} '{slug_or_name}' not found")
        return 1

    # Write to temp file, open editor, read back
    fd, tmp = tempfile.mkstemp(suffix=".yaml", prefix=f"bsp-{entity}-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(yaml_block)
        result = subprocess.run([editor, tmp])
        if result.returncode != 0:
            print("Editor exited with non-zero code; discarding changes.")
            return 1
        with open(tmp, encoding="utf-8") as fh:
            new_yaml = fh.read()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    # Parse and apply changes
    try:
        new_data = yaml.safe_load(new_yaml)
    except yaml.YAMLError as exc:
        print(f"YAML parse error: {exc}")
        return 1

    if new_data is None:
        print("Empty document; discarding changes.")
        return 1

    # new_data is {slug_or_name: {field: value, ...}}
    fields_dict = new_data.get(slug_or_name, new_data)
    if not isinstance(fields_dict, dict):
        print("Unexpected YAML structure; discarding changes.")
        return 1

    try:
        updater = {
            "device": writer.update_device,
            "release": writer.update_release,
            "feature": writer.update_feature,
            "preset": writer.update_preset,
            "vendor": writer.update_vendor,
            "distro": writer.update_distro,
            "container": writer.update_container,
        }.get(entity)
        if updater is None:
            print(f"Unknown entity type: {entity}")
            return 1
        updater(slug_or_name, **fields_dict)
    except (KeyError, TypeError) as exc:
        print(f"Error applying changes: {exc}")
        return 1

    from .registry_writer import RegistryWriter
    issues = writer.validate()
    _print_issues(issues)
    writer.save()
    _git_stage_if_available(writer)
    print(f"Updated {entity} '{slug_or_name}' in {registry_path}")
    return 0
