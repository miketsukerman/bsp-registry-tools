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
# Helpers
# =============================================================================


def _collect_deploy_overrides(args) -> dict:
    """
    Extract deploy-related CLI arguments into a flat override dict.

    Keys with ``None`` values are omitted so they do not clobber registry
    config defaults in the merge step inside ``BspManager``.
    """
    overrides = {}
    provider = getattr(args, "deploy_provider", None)
    if provider is not None:
        overrides["provider"] = provider
    container = getattr(args, "deploy_container", None)
    if container is not None:
        overrides["container"] = container
    prefix = getattr(args, "deploy_prefix", None)
    if prefix is not None:
        overrides["prefix"] = prefix
    patterns = getattr(args, "deploy_patterns", None)
    if patterns:
        overrides["patterns"] = patterns
    archive_name = getattr(args, "deploy_archive_name", None)
    if archive_name is not None:
        overrides["archive_name"] = archive_name
    archive_format = getattr(args, "deploy_archive_format", None)
    if archive_format is not None:
        overrides["archive_format"] = archive_format
    return overrides


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

        # Create subparsers for different commands
        subparsers = parser.add_subparsers(dest="command", help="Command to execute", required=True)

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
            "--deploy",
            action="store_true",
            dest="deploy_after_build",
            help="Deploy artifacts to cloud storage after a successful build"
        )
        build_parser.add_argument(
            "--deploy-provider",
            type=str,
            dest="deploy_provider",
            metavar="PROVIDER",
            help="Cloud storage provider for deployment (azure, aws)"
        )
        build_parser.add_argument(
            "--deploy-container",
            "--deploy-bucket",
            type=str,
            dest="deploy_container",
            metavar="CONTAINER",
            help="Azure container or AWS bucket name for deployment"
        )
        build_parser.add_argument(
            "--deploy-prefix",
            type=str,
            dest="deploy_prefix",
            metavar="PREFIX",
            help="Remote path prefix template for deployment"
        )
        build_parser.add_argument(
            "--deploy-archive-name",
            type=str,
            dest="deploy_archive_name",
            default=None,
            metavar="NAME",
            help=(
                "Bundle all artifacts into a single archive with this name before uploading "
                "(supports {device}, {release}, {distro}, {vendor}, {date}, {datetime})"
            )
        )
        build_parser.add_argument(
            "--deploy-archive-format",
            type=str,
            dest="deploy_archive_format",
            default=None,
            metavar="FORMAT",
            choices=["tar.gz", "tar.bz2", "tar.xz", "zip"],
            help="Compression format for the archive bundle (default: tar.gz)"
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
        # Deploy command
        # ----------------------------------------------------------------
        deploy_parser = subparsers.add_parser(
            "deploy", help="Deploy build artifacts to cloud storage"
        )
        deploy_parser.add_argument(
            "bsp_name",
            nargs="?",
            type=str,
            help="Name of the BSP preset whose artifacts to deploy"
        )
        deploy_parser.add_argument(
            "--device", "-d",
            type=str,
            dest="device",
            help="Device slug (use with --release for component-based deployment)"
        )
        deploy_parser.add_argument(
            "--release",
            type=str,
            dest="release",
            help="Release slug (use with --device for component-based deployment)"
        )
        deploy_parser.add_argument(
            "--feature", "-f",
            action="append",
            dest="features",
            metavar="FEATURE",
            help="Feature slug (can be specified multiple times)"
        )
        deploy_parser.add_argument(
            "--provider",
            type=str,
            dest="deploy_provider",
            default=None,
            metavar="PROVIDER",
            help="Cloud storage provider: azure (default) or aws"
        )
        deploy_parser.add_argument(
            "--container",
            "--bucket",
            type=str,
            dest="deploy_container",
            default=None,
            metavar="CONTAINER",
            help="Azure Blob container name or AWS S3 bucket name"
        )
        deploy_parser.add_argument(
            "--prefix",
            type=str,
            dest="deploy_prefix",
            default=None,
            metavar="PREFIX",
            help=(
                "Remote path prefix template "
                "(supports {device}, {release}, {distro}, {vendor}, {date})"
            )
        )
        deploy_parser.add_argument(
            "--pattern",
            action="append",
            dest="deploy_patterns",
            metavar="PATTERN",
            help="Glob pattern for artifacts to upload (can be specified multiple times)"
        )
        deploy_parser.add_argument(
            "--archive-name",
            type=str,
            dest="deploy_archive_name",
            default=None,
            metavar="NAME",
            help=(
                "Bundle all artifacts into a single archive with this name before uploading "
                "(supports {device}, {release}, {distro}, {vendor}, {date}, {datetime})"
            )
        )
        deploy_parser.add_argument(
            "--archive-format",
            type=str,
            dest="deploy_archive_format",
            default=None,
            metavar="FORMAT",
            choices=["tar.gz", "tar.bz2", "tar.xz", "zip"],
            help="Compression format for the archive bundle (default: tar.gz)"
        )
        deploy_parser.add_argument(
            "--dry-run",
            action="store_true",
            dest="dry_run",
            help="List what would be uploaded without actually uploading"
        )

        args = parser.parse_args()

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
            deploy_after_build = getattr(args, "deploy_after_build", False)
            deploy_overrides = _collect_deploy_overrides(args)

            if _check_exclusive(bsp_name, device, release, build_parser):
                return 1
            if bsp_name:
                bsp_mgr.build_bsp(
                    bsp_name,
                    checkout_only=checkout_only,
                    deploy_after_build=deploy_after_build,
                    deploy_overrides=deploy_overrides,
                )
            elif device and release:
                bsp_mgr.build_by_components(
                    device, release, features,
                    checkout_only=checkout_only,
                    deploy_after_build=deploy_after_build,
                    deploy_overrides=deploy_overrides,
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

        elif args.command == "deploy":
            device = getattr(args, "device", None)
            release = getattr(args, "release", None)
            features = getattr(args, "features", None) or []
            bsp_name = getattr(args, "bsp_name", None)
            dry_run = getattr(args, "dry_run", False)
            deploy_overrides = _collect_deploy_overrides(args)

            if _check_exclusive(bsp_name, device, release, deploy_parser):
                return 1
            if bsp_name:
                bsp_mgr.deploy_bsp(
                    bsp_name,
                    deploy_overrides=deploy_overrides,
                    dry_run=dry_run,
                )
            elif device and release:
                bsp_mgr.deploy_by_components(
                    device, release, features,
                    deploy_overrides=deploy_overrides,
                    dry_run=dry_run,
                )
            else:
                logging.error(
                    "Specify either a BSP preset name or both --device and --release."
                )
                deploy_parser.print_help()
                return 1

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
