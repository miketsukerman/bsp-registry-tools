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
from .models import ArchiveConfig
from .registry_fetcher import DEFAULT_REMOTE_URL, DEFAULT_BRANCH, RegistryFetcher
from .remotes_manager import RemotesManager
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
    archive_format = getattr(args, "deploy_archive_format", None)
    if archive_name is not None or archive_format is not None:
        defaults = ArchiveConfig()
        overrides["archive"] = ArchiveConfig(
            name=archive_name if archive_name is not None else defaults.name,
            format=archive_format if archive_format is not None else defaults.format,
        )
    return overrides


def _collect_gather_overrides(args) -> dict:
    """
    Extract gather-related CLI arguments into a flat deploy-override dict.

    Only the storage location fields are extracted (provider, container,
    prefix).  Archive and pattern fields are upload-only concepts and are
    therefore intentionally omitted.

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
    return overrides


# =============================================================================
# Remotes sub-command dispatcher (no registry loading required)
# =============================================================================


def _dispatch_remotes(args) -> int:
    """Handle all ``bsp remotes`` sub-commands.

    Returns an integer exit code (0 = success).
    """
    mgr = RemotesManager()
    subcmd = getattr(args, "remotes_command", None)

    if subcmd is None:
        # Plain ``bsp remotes`` — list all remotes
        remotes = mgr.load()
        if not remotes:
            print("(no remotes configured)")
            print(f"Use 'bsp remotes add <name> <url>' to register a remote.")
            return 0
        verbose = getattr(args, "remotes_verbose", False)
        for r in remotes:
            if verbose:
                print(f"{r.name}\t{r.url} (branch: {r.branch})")
            else:
                print(r.name)
        return 0

    if subcmd == "add":
        entry = mgr.add(name=args.name, url=args.url, branch=args.branch)
        print(f"Added remote '{entry.name}' -> {entry.url} (branch: {entry.branch})")
        return 0

    if subcmd in ("remove", "rm"):
        mgr.remove(args.name)
        print(f"Removed remote '{args.name}'")
        return 0

    if subcmd == "rename":
        updated = mgr.rename(args.old_name, args.new_name)
        print(f"Renamed remote '{args.old_name}' -> '{updated.name}'")
        return 0

    if subcmd == "set-url":
        updated = mgr.set_url(args.name, args.url)
        if args.branch:
            updated = mgr.set_branch(args.name, args.branch)
        print(f"Updated remote '{updated.name}': {updated.url} (branch: {updated.branch})")
        return 0

    if subcmd == "show":
        r = mgr.get(args.name)
        print(f"name:   {r.name}")
        print(f"url:    {r.url}")
        print(f"branch: {r.branch}")
        return 0

    logging.error("Unknown remotes sub-command: %s", subcmd)
    return 1


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
        parser.add_argument(
            "--remote",
            action="append",
            dest="remote",
            metavar="URL[@BRANCH][@name=NAME]",
            default=None,
            help=(
                "Remote registry git URL.  May be specified multiple times for "
                "multi-registry mode.  Each value may embed a branch and an "
                "optional display name using the format "
                "``URL@BRANCH@name=NAME``.  "
                "(default: %(const)s)"
            ),
        )
        parser.add_argument("--branch", default=DEFAULT_BRANCH,
                            help="Remote registry branch for a single --remote (default: %(default)s)")
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
            help="BSP preset to build, optionally prefixed with registry name (registry:preset). Mutually exclusive with --device/--release."
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
        build_parser.add_argument(
            "--test",
            action="store_true",
            dest="run_test",
            help="Submit a LAVA HIL test job after a successful build"
        )
        build_parser.add_argument(
            "--wait",
            action="store_true",
            help="Wait for the LAVA job to complete (requires --test)"
        )
        build_parser.add_argument(
            "--lava-server",
            type=str,
            dest="lava_server",
            metavar="URL",
            help="LAVA server base URL (overrides registry 'lava.server')"
        )
        build_parser.add_argument(
            "--lava-token",
            type=str,
            dest="lava_token",
            metavar="TOKEN",
            help="LAVA authentication token (overrides registry 'lava.token')"
        )
        build_parser.add_argument(
            "--artifact-url",
            type=str,
            dest="artifact_url",
            metavar="URL",
            help="Base URL where build artifacts are served to the LAVA lab"
        )
        build_parser.add_argument(
            "--target",
            type=str,
            dest="target",
            metavar="TARGET",
            help="Bitbake build target (image or recipe) to pass to KAS (overrides registry targets)"
        )
        build_parser.add_argument(
            "--task",
            type=str,
            dest="task",
            metavar="TASK",
            help="Bitbake task to run (e.g. compile, configure) to pass to KAS"
        )
        build_parser.add_argument(
            '--path',
            type=str,
            dest='build_path',
            metavar='PATH',
            help='Override output build directory path'
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
        list_parser.add_argument(
            "--remote",
            type=str,
            dest="filter_remote",
            metavar="NAME",
            default=None,
            help="Show only entries from the named remote registry"
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
        tree_parser.add_argument(
            "--remote",
            type=str,
            dest="filter_remote",
            metavar="NAME",
            default=None,
            help="Show only entries from the named remote registry"
        )

        # ----------------------------------------------------------------
        # Export command
        # ----------------------------------------------------------------
        export_parser = subparsers.add_parser("export", help="Export BSP configuration")
        export_parser.add_argument(
            "bsp_name",
            nargs="?",
            type=str,
            help="BSP preset to export, optionally prefixed with registry name (registry:preset). Mutually exclusive with --device/--release."
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
        # Server command
        # ----------------------------------------------------------------
        server_parser = subparsers.add_parser(
            "server", help="Start a GraphQL / REST HTTP server"
        )
        server_parser.add_argument(
            "--host",
            default="127.0.0.1",
            help="Host address to bind to (default: %(default)s)",
        )
        server_parser.add_argument(
            "--port",
            type=int,
            default=8080,
            help="Port to listen on (default: %(default)s)",
        )
        server_parser.add_argument(
            "--reload",
            action="store_true",
            help="Enable auto-reload on code changes (development only)",
        )

        # ----------------------------------------------------------------
        # Shell command
        # ----------------------------------------------------------------
        shell_parser = subparsers.add_parser("shell", help="Enter interactive shell for BSP")
        shell_parser.add_argument(
            "bsp_name",
            nargs="?",
            type=str,
            help="BSP preset, optionally prefixed with registry name (registry:preset). Mutually exclusive with --device/--release."
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
            help="BSP preset whose artifacts to deploy, optionally prefixed with registry name (registry:preset)."
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

        # ----------------------------------------------------------------
        # Gather command
        # ----------------------------------------------------------------
        gather_parser = subparsers.add_parser(
            "gather",
            help="Download BSP build artifacts from cloud storage"
        )
        gather_parser.add_argument(
            "bsp_name",
            nargs="?",
            type=str,
            help="BSP preset whose artifacts to download, optionally prefixed with registry name (registry:preset). Mutually exclusive with --device/--release."
        )
        gather_parser.add_argument(
            "--device", "-d",
            type=str,
            dest="device",
            help="Device slug (use with --release for component-based gather)"
        )
        gather_parser.add_argument(
            "--release",
            type=str,
            dest="release",
            help="Release slug (use with --device for component-based gather)"
        )
        gather_parser.add_argument(
            "--feature", "-f",
            action="append",
            dest="features",
            metavar="FEATURE",
            help="Feature slug (can be specified multiple times)"
        )
        gather_parser.add_argument(
            "--dest-dir",
            type=str,
            dest="dest_dir",
            default=None,
            metavar="PATH",
            help=(
                "Local directory to write downloaded artifacts into. "
                "Defaults to the build path configured in the registry."
            )
        )
        gather_parser.add_argument(
            "--provider",
            type=str,
            dest="deploy_provider",
            default=None,
            metavar="PROVIDER",
            help="Cloud storage provider: azure (default) or aws"
        )
        gather_parser.add_argument(
            "--container",
            "--bucket",
            type=str,
            dest="deploy_container",
            default=None,
            metavar="CONTAINER",
            help="Azure Blob container name or AWS S3 bucket name"
        )
        gather_parser.add_argument(
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
        gather_parser.add_argument(
            "--date",
            type=str,
            dest="gather_date",
            default=None,
            metavar="DATE",
            help=(
                "Date override for the {date} placeholder in the prefix template "
                "(YYYY-MM-DD). Defaults to today's date."
            )
        )
        gather_parser.add_argument(
            "--dry-run",
            action="store_true",
            dest="dry_run",
            help="List what would be downloaded without actually downloading"
        )

        # ----------------------------------------------------------------
        # Test command
        # ----------------------------------------------------------------
        test_parser = subparsers.add_parser(
            "test",
            help="Submit a LAVA HIL test job for a BSP preset or component combination"
        )
        test_parser.add_argument(
            "bsp_name",
            nargs="?",
            type=str,
            help="BSP preset to test, optionally prefixed with registry name (registry:preset). Mutually exclusive with --device/--release."
        )
        test_parser.add_argument(
            "--device", "-d",
            type=str,
            dest="device",
            help="Device slug (use with --release for component-based test)"
        )
        test_parser.add_argument(
            "--release",
            type=str,
            dest="release",
            help="Release slug (use with --device for component-based test)"
        )
        test_parser.add_argument(
            "--feature", "-f",
            action="append",
            dest="features",
            metavar="FEATURE",
            help="Feature slug to enable (can be specified multiple times)"
        )
        test_parser.add_argument(
            "--wait",
            action="store_true",
            help="Block until the LAVA job completes and print test results"
        )
        test_parser.add_argument(
            "--lava-server",
            type=str,
            dest="lava_server",
            metavar="URL",
            help="LAVA server base URL (overrides registry 'lava.server')"
        )
        test_parser.add_argument(
            "--lava-token",
            type=str,
            dest="lava_token",
            metavar="TOKEN",
            help="LAVA authentication token (overrides registry 'lava.token')"
        )
        test_parser.add_argument(
            "--artifact-url",
            type=str,
            dest="artifact_url",
            metavar="URL",
            help="Base URL where build artifacts are served to the LAVA lab"
        )

        # ----------------------------------------------------------------
        # Remotes command  (git-remote-style management of named remote
        # registries persisted in ~/.config/bsp/remotes.yaml)
        # ----------------------------------------------------------------
        remotes_parser = subparsers.add_parser(
            "remotes",
            help="Manage named remote BSP registry sources (like git remote)",
        )
        remotes_parser.add_argument(
            "-v", "--verbose-list",
            dest="remotes_verbose",
            action="store_true",
            help="Show URL and branch alongside each remote name when listing",
        )
        remotes_subparsers = remotes_parser.add_subparsers(
            dest="remotes_command",
            help="Remotes sub-command",
        )

        # bsp remotes add <name> <url> [--branch BRANCH]
        remotes_add = remotes_subparsers.add_parser(
            "add",
            help="Register a new named remote registry",
        )
        remotes_add.add_argument("name", help="Unique name for the remote (e.g. 'advantech')")
        remotes_add.add_argument("url", help="Git repository URL of the remote registry")
        remotes_add.add_argument(
            "--branch", "-b",
            default=DEFAULT_BRANCH,
            help="Branch to fetch from (default: %(default)s)",
        )

        # bsp remotes remove <name>
        remotes_remove = remotes_subparsers.add_parser(
            "remove",
            aliases=["rm"],
            help="Remove a named remote",
        )
        remotes_remove.add_argument("name", help="Name of the remote to remove")

        # bsp remotes rename <old> <new>
        remotes_rename = remotes_subparsers.add_parser(
            "rename",
            help="Rename a remote",
        )
        remotes_rename.add_argument("old_name", metavar="old-name", help="Current name of the remote")
        remotes_rename.add_argument("new_name", metavar="new-name", help="New name for the remote")

        # bsp remotes set-url <name> <url>
        remotes_set_url = remotes_subparsers.add_parser(
            "set-url",
            help="Change the URL of an existing remote",
        )
        remotes_set_url.add_argument("name", help="Name of the remote to update")
        remotes_set_url.add_argument("url", help="New Git repository URL")
        remotes_set_url.add_argument(
            "--branch", "-b",
            default=None,
            help="Also update the branch",
        )

        # bsp remotes show <name>
        remotes_show = remotes_subparsers.add_parser(
            "show",
            help="Show details about a named remote",
        )
        remotes_show.add_argument("name", help="Name of the remote to show")

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

        # ----------------------------------------------------------------
        # Dispatch remotes commands — these do NOT need a loaded registry.
        # ----------------------------------------------------------------
        if args.command == "remotes":
            return _dispatch_remotes(args)

        # Resolve registry file path
        LOCAL_DEFAULTS = ["bsp-registry.yaml", "bsp-registry.yml"]
        local_registry = next((name for name in LOCAL_DEFAULTS if Path(name).is_file()), None)
        if args.registry is not None:
            registry_path = args.registry
            logging.info("Using explicitly provided registry: %s", registry_path)
            bsp_mgr = BspManager(registry_path, verbose=args.verbose)
        elif args.local:
            registry_path = local_registry or LOCAL_DEFAULTS[0]
            logging.info("Using local registry (--local): %s", registry_path)
            bsp_mgr = BspManager(registry_path, verbose=args.verbose)
        elif local_registry is not None:
            registry_path = local_registry
            logging.info("Using local registry: %s", registry_path)
            bsp_mgr = BspManager(registry_path, verbose=args.verbose)
        else:
            from .registry_fetcher import RemoteRegistrySpec
            fetcher = RegistryFetcher()

            # If no --remote flags given on the CLI, fall back to configured remotes
            if args.remote:
                remotes_raw = args.remote
            else:
                stored = RemotesManager().load()
                if stored:
                    # Encode stored remotes as URL@BRANCH@name=NAME strings so the
                    # existing parse / fetch_multiple path handles them uniformly.
                    remotes_raw = [
                        f"{r.url}@{r.branch}@name={r.name}" for r in stored
                    ]
                    logging.info(
                        "Using %d configured remote(s): %s",
                        len(stored),
                        [r.name for r in stored],
                    )
                else:
                    remotes_raw = [DEFAULT_REMOTE_URL]

            if len(remotes_raw) == 1 and remotes_raw[0] == DEFAULT_REMOTE_URL and not args.remote:
                # Single default remote — use the legacy single-registry path for backward compat
                registry_path = str(fetcher.fetch_registry(
                    repo_url=DEFAULT_REMOTE_URL,
                    branch=args.branch,
                    update=args.update,
                ))
                logging.info("Using remote registry cached at: %s", registry_path)
                bsp_mgr = BspManager(registry_path, verbose=args.verbose)
            elif len(remotes_raw) == 1:
                # Single explicit remote — backward-compat single-registry path
                spec = RemoteRegistrySpec.parse(remotes_raw[0], default_branch=args.branch)
                registry_path = str(fetcher.fetch_registry(
                    repo_url=spec.url,
                    branch=spec.branch,
                    update=args.update,
                ))
                logging.info("Using remote registry cached at: %s", registry_path)
                bsp_mgr = BspManager(registry_path, verbose=args.verbose)
            else:
                # Multiple remotes — multi-registry mode
                specs = [RemoteRegistrySpec.parse(r, default_branch=args.branch) for r in remotes_raw]
                registry_pairs = fetcher.fetch_multiple(specs, update=args.update)
                logging.info(
                    "Loaded %d remote registries: %s",
                    len(registry_pairs),
                    [name for name, _ in registry_pairs],
                )
                config_paths = [(name, str(path)) for name, path in registry_pairs]
                bsp_mgr = BspManager(config_paths=config_paths, verbose=args.verbose)

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
            run_test = getattr(args, "run_test", False)
            wait = getattr(args, "wait", False)
            lava_server = getattr(args, "lava_server", None)
            lava_token = getattr(args, "lava_token", None)
            artifact_url = getattr(args, "artifact_url", None)
            target = getattr(args, "target", None)
            task = getattr(args, "task", None)
            build_path = getattr(args, "build_path", None)

            if _check_exclusive(bsp_name, device, release, build_parser):
                return 1
            if bsp_name:
                bsp_mgr.build_bsp(
                    bsp_name,
                    checkout_only=checkout_only,
                    deploy_after_build=deploy_after_build,
                    deploy_overrides=deploy_overrides,
                    target=target,
                    task=task,
                    build_path_override=build_path,
                    feature_slugs=features,
                )
                if run_test:
                    passed = bsp_mgr.test_bsp(
                        bsp_name,
                        lava_server=lava_server,
                        lava_token=lava_token,
                        artifact_url=artifact_url,
                        wait=wait,
                    )
                    if not passed:
                        return 1
            elif device and release:
                bsp_mgr.build_by_components(
                    device, release, features,
                    checkout_only=checkout_only,
                    deploy_after_build=deploy_after_build,
                    deploy_overrides=deploy_overrides,
                    target=target,
                    task=task,
                    build_path_override=build_path,
                )
                if run_test:
                    passed = bsp_mgr.test_by_components(
                        device,
                        release,
                        features,
                        lava_server=lava_server,
                        lava_token=lava_token,
                        artifact_url=artifact_url,
                        wait=wait,
                    )
                    if not passed:
                        return 1
            else:
                logging.error(
                    "Specify either a BSP preset name or both --device and --release."
                )
                build_parser.print_help()
                return 1

        elif args.command == "list":
            list_type = getattr(args, "list_type", None)
            device = getattr(args, "device", None)
            registry_filter = getattr(args, "filter_remote", None)
            use_color = not args.no_color
            if list_type == "devices":
                bsp_mgr.list_devices(use_color=use_color, registry_filter=registry_filter)
            elif list_type == "releases":
                bsp_mgr.list_releases(device_slug=device, use_color=use_color, registry_filter=registry_filter)
            elif list_type == "features":
                bsp_mgr.list_features(use_color=use_color, registry_filter=registry_filter)
            elif list_type == "distros":
                bsp_mgr.list_distros(use_color=use_color, registry_filter=registry_filter)
            else:
                bsp_mgr.list_bsp(use_color=use_color, registry_filter=registry_filter)

        elif args.command == "containers":
            bsp_mgr.list_containers(use_color=not args.no_color)

        elif args.command == "tree":
            full = getattr(args, "full", False)
            compact = getattr(args, "compact", False)
            mode = "full" if full else ("compact" if compact else "default")
            registry_filter = getattr(args, "filter_remote", None)
            bsp_mgr.tree_bsp(use_color=not args.no_color, mode=mode, registry_filter=registry_filter)

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

        elif args.command == "server":
            try:
                import uvicorn
                from .server import create_app
            except ImportError:
                logging.error(
                    "Server dependencies are not installed. "
                    "Install them with: pip install bsp-registry-tools[server]"
                )
                return 1

            app = create_app(manager=bsp_mgr)
            uvicorn.run(
                app,
                host=args.host,
                port=args.port,
                reload=args.reload,
            )
            return 0

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

        elif args.command == "gather":
            device = getattr(args, "device", None)
            release = getattr(args, "release", None)
            features = getattr(args, "features", None) or []
            bsp_name = getattr(args, "bsp_name", None)
            dest_dir = getattr(args, "dest_dir", None)
            dry_run = getattr(args, "dry_run", False)
            date_override = getattr(args, "gather_date", None)
            gather_overrides = _collect_gather_overrides(args)

            if _check_exclusive(bsp_name, device, release, gather_parser):
                return 1
            if bsp_name:
                bsp_mgr.gather_bsp(
                    bsp_name,
                    dest_dir=dest_dir,
                    deploy_overrides=gather_overrides,
                    dry_run=dry_run,
                    date_override=date_override,
                )
            elif device and release:
                bsp_mgr.gather_by_components(
                    device, release, features,
                    dest_dir=dest_dir,
                    deploy_overrides=gather_overrides,
                    dry_run=dry_run,
                    date_override=date_override,
                )
            else:
                logging.error(
                    "Specify either a BSP preset name or both --device and --release."
                )
                gather_parser.print_help()
                return 1

        elif args.command == "test":
            device = getattr(args, "device", None)
            release = getattr(args, "release", None)
            features = getattr(args, "features", None) or []
            bsp_name = getattr(args, "bsp_name", None)
            wait = getattr(args, "wait", False)
            lava_server = getattr(args, "lava_server", None)
            lava_token = getattr(args, "lava_token", None)
            artifact_url = getattr(args, "artifact_url", None)

            if _check_exclusive(bsp_name, device, release, test_parser):
                return 1
            if bsp_name:
                passed = bsp_mgr.test_bsp(
                    bsp_name,
                    lava_server=lava_server,
                    lava_token=lava_token,
                    artifact_url=artifact_url,
                    wait=wait,
                )
            elif device and release:
                passed = bsp_mgr.test_by_components(
                    device,
                    release,
                    features,
                    lava_server=lava_server,
                    lava_token=lava_token,
                    artifact_url=artifact_url,
                    wait=wait,
                )
            else:
                logging.error(
                    "Specify either a BSP preset name or both --device and --release."
                )
                test_parser.print_help()
                return 1
            if not passed:
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
