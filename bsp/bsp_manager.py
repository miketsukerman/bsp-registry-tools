"""
Main BSP management class coordinating registry, builds, and exports.
"""

import logging
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import replace, fields as dataclass_fields
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from .deployer import ArtifactDeployer, DeployResult
from .environment import EnvironmentManager
from .exceptions import COLORAMA_AVAILABLE
from .gatherer import ArtifactGatherer, GatherResult
from .kas_manager import KasManager
from .models import BspPreset, DeployConfig, Docker, EnvironmentVariable
from .path_resolver import resolver
from .resolver import ResolvedConfig, V2Resolver
from .storage import create_backend
from .utils import get_registry_from_yaml_file, build_docker

if COLORAMA_AVAILABLE:
    from colorama import Fore, Style

# =============================================================================
# Helpers
# =============================================================================


def _expand_env(value: str) -> str:
    """Expand ``$ENV{VAR}`` placeholders in *value* with OS environment values."""
    import re
    def _replace(m):
        var = m.group(1)
        return os.environ.get(var, m.group(0))
    return re.sub(r'\$ENV\{([^}]+)\}', _replace, value)

# =============================================================================
# Main BSP Management Class with v2.0 Support
# =============================================================================


class BspManager:
    """
    Main BSP management class for BSP registry management (v2.0 schema).

    This class coordinates the overall BSP management flow including
    configuration loading, device/release/feature discovery, build execution,
    shell access, and configuration export operations with container support.

    Multiple registries are supported: pass *config_paths* as an ordered list
    of ``(name, path)`` pairs to load several independent registries at once.
    All listing commands annotate output with ``[registry-name]`` prefixes and
    all preset-selection arguments accept the ``registry:preset`` syntax for
    unambiguous targeting.
    """

    def __init__(
        self,
        config_path: str = "bsp-registry.yaml",
        verbose: bool = False,
        config_paths: Optional[List[Tuple[str, str]]] = None,
    ):
        """
        Initialize BSP manager.

        Args:
            config_path: Path to BSP registry configuration file (single-registry
                         mode, backward compatible).
            verbose: If True, stream docker build output live during builds.
            config_paths: Ordered list of ``(name, path)`` pairs for
                          multi-registry mode.  When provided *config_path* is
                          ignored.
        """
        if config_paths:
            self._config_pairs: List[Tuple[str, Path]] = [
                (name, Path(path)) for name, path in config_paths
            ]
        else:
            self._config_pairs = [("default", Path(config_path))]

        self.verbose = verbose
        self.logger = logging.getLogger(self.__class__.__name__)

        # Multi-registry state — populated by load_configuration / initialize
        self.registries: List[Tuple[str, object]] = []   # [(name, RegistryRoot)]
        self.resolvers: List[Tuple[str, V2Resolver]] = [] # [(name, V2Resolver)]

        # Active single-registry state (backward compatible public API)
        self.model = None          # RegistryRoot | None
        self.config_path = self._config_pairs[0][1]
        self.env_manager = None    # EnvironmentManager | None
        self.containers = {}       # Dict[str, Docker]
        self.resolver = None       # V2Resolver | None

    def load_configuration(self) -> None:
        """
        Load and parse BSP configuration from all registry YAML files.

        In multi-registry mode each registry is loaded independently without
        merging.  The first registry provides the backward-compatible
        ``self.model`` and ``self.config_path`` attributes.

        Raises:
            SystemExit: If any configuration file is missing, invalid, or not v2.0.
        """
        self.registries = []
        for reg_name, reg_path in self._config_pairs:
            try:
                if not reg_path.exists():
                    logging.error(f"Config file not found: {reg_path}")
                    sys.exit(1)
                model = get_registry_from_yaml_file(reg_path)
                self.registries.append((reg_name, model))
                logging.info(
                    f"Registry '{reg_name}' loaded successfully from {reg_path}"
                )
            except SystemExit:
                raise
            except Exception as e:
                logging.error(f"Failed to load registry '{reg_name}': {e}")
                sys.exit(1)

        # Backward-compat: point self.model / config_path at the first registry
        if self.registries:
            first_name, first_model = self.registries[0]
            self.model = first_model
            self.config_path = self._config_pairs[0][1]

            if first_model.containers:
                self.containers = first_model.containers
                logging.info(f"Loaded {len(self.containers)} container definitions")

            if first_model.environment and first_model.environment.variables:
                self.env_manager = EnvironmentManager(first_model.environment.variables)
                logging.info(
                    f"Environment configuration initialized with "
                    f"{len(first_model.environment.variables)} variables"
                )

    def initialize(self) -> None:
        """Initialize BSP manager components and validate configuration."""
        logging.info("Initializing BSP manager...")
        self.load_configuration()

        # Create one V2Resolver per registry
        self.resolvers = []
        for reg_name, reg_model in self.registries:
            reg_containers = reg_model.containers or {}
            self.resolvers.append((reg_name, V2Resolver(reg_model, reg_containers)))

        # Backward compat: point self.resolver at the first registry's resolver
        if self.resolvers:
            self.resolver = self.resolvers[0][1]

        # Validate environment configuration if present
        if self.env_manager:
            if not self.env_manager.validate_environment():
                logging.error("Environment configuration validation failed")
                sys.exit(1)

        logging.info("BSP manager initialized successfully")

    # ------------------------------------------------------------------
    # Listing commands
    # ------------------------------------------------------------------

    def _color_helpers(self, use_color: bool):
        """
        Return ``(header, name, dim)`` color-formatting helpers.

        Each helper accepts a string and returns it wrapped in the
        appropriate ANSI escape sequences when *use_color* is ``True``
        and colorama is available; otherwise the string is returned
        unchanged.
        """
        colored = use_color and COLORAMA_AVAILABLE

        def _c(text: str, *styles) -> str:
            if not colored:
                return text
            return "".join(styles) + text + Style.RESET_ALL

        def _header(text: str) -> str:
            return _c(text, Fore.CYAN, Style.BRIGHT)

        def _name(text: str) -> str:
            return _c(text, Fore.YELLOW)

        def _dim(text: str) -> str:
            return _c(text, Style.DIM)

        return _header, _name, _dim

    # ------------------------------------------------------------------
    # Multi-registry helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_registry_preset(value: str) -> Tuple[Optional[str], str]:
        """Split a ``registry:preset`` string into ``(registry_name, preset_name)``.

        If *value* contains no colon the returned registry_name is ``None``.
        """
        if ":" in value:
            registry_name, preset_name = value.split(":", 1)
            return registry_name.strip(), preset_name.strip()
        return None, value

    def _iter_registries(self) -> Iterator[Tuple[str, object, V2Resolver, Path]]:
        """Iterate over (name, model, resolver, config_path) tuples for all registries."""
        if self.resolvers:
            for (reg_name, reg_model), (_, reg_resolver), (_, reg_path) in zip(
                self.registries, self.resolvers, self._config_pairs
            ):
                yield reg_name, reg_model, reg_resolver, reg_path
        elif self.resolver is not None and self.registries:
            # Backward-compat: resolver was set directly without populating self.resolvers
            reg_name, reg_model = self.registries[0]
            _, reg_path = self._config_pairs[0]
            yield reg_name, reg_model, self.resolver, reg_path

    @contextmanager
    def _use_registry_context(
        self,
        model: object,
        reg_resolver: V2Resolver,
        config_path: Path,
    ) -> Iterator[None]:
        """Context manager that temporarily switches the active registry.

        This allows preset-based action methods (build, shell, export, deploy,
        gather, test) to call downstream helpers (``_get_kas_manager_for_resolved``,
        ``_resolve_deploy_config``, etc.) which rely on ``self.model``,
        ``self.resolver``, and ``self.config_path`` for the *correct* registry.
        """
        old_model = self.model
        old_resolver = self.resolver
        old_config_path = self.config_path
        self.model = model
        self.resolver = reg_resolver
        self.config_path = config_path
        try:
            yield
        finally:
            self.model = old_model
            self.resolver = old_resolver
            self.config_path = old_config_path

    def _resolve_preset_multi(
        self, bsp_name: str, extra_feature_slugs: Optional[List[str]] = None
    ) -> Tuple[ResolvedConfig, BspPreset, str, object, V2Resolver, Path]:
        """Resolve a BSP preset across all loaded registries.

        Handles the ``registry:preset`` syntax for unambiguous targeting.

        Args:
            bsp_name: Preset name, optionally prefixed with a registry name
                      using the ``registry:preset`` format.
            extra_feature_slugs: Additional features to enable on top of the
                                 preset's own feature list.

        Returns:
            Tuple of ``(resolved, preset, registry_name, model, resolver, config_path)``.

        Raises:
            SystemExit: If the registry or preset is not found.
        """
        registry_hint, preset_name = self._parse_registry_preset(bsp_name)

        if registry_hint is not None:
            # Look only in the named registry
            for reg_name, reg_model, reg_resolver, reg_path in self._iter_registries():
                if reg_name == registry_hint:
                    resolved, preset = reg_resolver.resolve_preset(
                        preset_name, extra_feature_slugs=extra_feature_slugs
                    )
                    return resolved, preset, reg_name, reg_model, reg_resolver, reg_path
            logging.error(
                f"Registry '{registry_hint}' not found. "
                f"Available: {', '.join(n for n, _, _, _ in self._iter_registries()) or '(none)'}"
            )
            sys.exit(1)

        # Search all registries in order; warn on ambiguity
        found_in: List[Tuple[str, object, V2Resolver, Path]] = []
        for reg_name, reg_model, reg_resolver, reg_path in self._iter_registries():
            for p in reg_resolver.list_presets():
                if p.name == preset_name:
                    found_in.append((reg_name, reg_model, reg_resolver, reg_path))
                    break

        if not found_in:
            logging.error(f"BSP preset not found: '{preset_name}'")
            all_presets = [
                f"{n}:{p.name}"
                for n, _, r, _ in self._iter_registries()
                for p in r.list_presets()
            ]
            print("Available presets: " + (", ".join(all_presets) or "(none)"))
            sys.exit(1)

        if len(found_in) > 1:
            names = [n for n, _, _, _ in found_in]
            logging.warning(
                "Preset '%s' found in multiple registries: %s. "
                "Using '%s'. Use '%s:%s' to be explicit.",
                preset_name,
                names,
                names[0],
                names[0],
                preset_name,
            )

        reg_name, reg_model, reg_resolver, reg_path = found_in[0]
        resolved, preset = reg_resolver.resolve_preset(
            preset_name, extra_feature_slugs=extra_feature_slugs
        )
        return resolved, preset, reg_name, reg_model, reg_resolver, reg_path

    # ------------------------------------------------------------------
    # Listing commands
    # ------------------------------------------------------------------

    def list_bsp(self, use_color: bool = True) -> None:
        """
        List all BSP presets defined in the registry (or registries).

        In v2, presets are optional shortcuts. If no presets are defined,
        a helpful message is shown instead of exiting with an error.

        When multiple registries are loaded each preset is annotated with
        ``[registry-name]`` so the source is always visible.

        Args:
            use_color: Enable colored output (requires colorama).
        """
        _header, _name, _dim = self._color_helpers(use_color)

        multi = len(self.registries) > 1

        # Collect all presets across registries
        all_preset_rows: List[Tuple[str, object]] = []  # (registry_name, preset)
        for reg_name, reg_model, reg_resolver, _ in self._iter_registries():
            raw = reg_model.registry.bsp if reg_model else []
            if raw:
                for preset in reg_resolver.list_presets():
                    all_preset_rows.append((reg_name, preset))

        if not all_preset_rows:
            print("No BSP presets defined in registry")
            print(
                "Use 'bsp list devices', 'bsp list releases', or "
                "'bsp list features' to see available components."
            )
            return

        print(_header("Available BSP presets:"))
        for reg_name, preset in all_preset_rows:
            extra_parts = []
            if preset.vendor_release:
                extra_parts.append(f"vendor_release: {preset.vendor_release}")
            if getattr(preset, "override", None):
                extra_parts.append(f"override: {preset.override}")
            if preset.features:
                extra_parts.append(f"features: {', '.join(preset.features)}")
            extra_str = (", " + ", ".join(extra_parts)) if extra_parts else ""
            reg_prefix = _dim(f"[{reg_name}] ") if multi else ""
            print(
                f"- {reg_prefix}{_name(preset.name)}: {preset.description} "
                + _dim(f"(device: {preset.device}, release: {preset.release}{extra_str})")
            )

    def list_devices(self, use_color: bool = True) -> None:
        """
        List all hardware devices defined across all registries.

        When multiple registries are loaded each entry is annotated with
        ``[registry-name]`` so the source is always visible.

        Args:
            use_color: Enable colored output (requires colorama).
        """
        _header, _name, _dim = self._color_helpers(use_color)
        multi = len(self.registries) > 1

        all_devices: List[Tuple[str, object]] = []
        for reg_name, reg_model, _, _ in self._iter_registries():
            devices = reg_model.registry.devices if reg_model else []
            for d in (devices or []):
                all_devices.append((reg_name, d))

        if not all_devices:
            print("No devices found in registry")
            return

        print(_header("Available devices:"))
        for reg_name, device in all_devices:
            soc_family = (
                f", soc_family: {device.soc_family}" if device.soc_family else ""
            )
            reg_prefix = _dim(f"[{reg_name}] ") if multi else ""
            print(
                f"- {reg_prefix}{_name(device.slug)}: {device.description} "
                + _dim(f"(vendor: {device.vendor}, soc_vendor: {device.soc_vendor}{soc_family})")
            )

    def list_releases(self, device_slug: Optional[str] = None, use_color: bool = True) -> None:
        """
        List all release definitions across all registries.

        For each release, vendor overrides are shown together with their
        optional sub-releases (vendor releases).

        Args:
            device_slug: If provided, filter releases to those compatible with
                         the device's vendor (via vendor_overrides). A release is
                         shown when it has no vendor_overrides (generic), or when
                         it has at least one vendor_overrides entry whose vendor
                         matches the device's board vendor.  When omitted, all
                         releases are shown.
            use_color: Enable colored output (requires colorama).
        """
        _header, _name, _dim = self._color_helpers(use_color)
        multi = len(self.registries) > 1

        # Collect (registry_name, device|None, releases_list) per registry
        rows: List[Tuple[str, List]] = []
        for reg_name, reg_model, reg_resolver, _ in self._iter_registries():
            releases = reg_model.registry.releases if reg_model else []
            if not releases:
                continue
            if device_slug:
                # Find device in this registry (may not be present)
                try:
                    device = reg_resolver.get_device(device_slug)
                    releases = [
                        r for r in releases
                        if not r.vendor_overrides
                        or any(vo.vendor == device.vendor for vo in r.vendor_overrides)
                    ]
                except SystemExit:
                    # Device not found in this registry — skip releases from it
                    continue
            rows.append((reg_name, releases))

        if not rows:
            print("No releases found in registry")
            return

        if device_slug:
            print(_header(f"Releases compatible with device '{device_slug}':"))
        else:
            print(_header("Available releases:"))

        for reg_name, releases in rows:
            if multi:
                print(_dim(f"[{reg_name}]"))
                indent = "  "
            else:
                indent = ""
            for release in releases:
                yocto = f" [Yocto {release.yocto_version}]" if release.yocto_version else ""
                isar = f" [Isar {release.isar_version}]" if release.isar_version else ""
                distro_str = f", distro: {release.distro}" if release.distro else ""
                env_str = f", environment: {release.environment}" if release.environment else ""
                meta = f"{yocto}{isar}{distro_str}{env_str}"
                print(
                    f"{indent}- {_name(release.slug)}: {release.description}"
                    + (_dim(meta) if meta else "")
                )
                # Show vendor overrides and their sub-releases
                for vo in release.vendor_overrides:
                    vo_parts = [f"vendor: {vo.vendor}"]
                    if vo.slug:
                        vo_parts.append(f"slug: {vo.slug}")
                    if vo.distro:
                        vo_parts.append(f"distro: {vo.distro}")
                    vo_line = f"{indent}  " + _dim(f"  override [{', '.join(vo_parts)}]")
                    print(vo_line)
                    for vr in vo.releases:
                        print(f"{indent}  " + _dim(f"    release: {vr.slug} — {vr.description}"))
                    for svo in vo.soc_vendors:
                        svo_parts = [f"soc_vendor: {svo.vendor}"]
                        if svo.distro:
                            svo_parts.append(f"distro: {svo.distro}")
                        print(f"{indent}  " + _dim(f"    [{', '.join(svo_parts)}]"))
                        for vr in svo.releases:
                            print(f"{indent}  " + _dim(f"      release: {vr.slug} — {vr.description}"))

    def list_features(self, use_color: bool = True) -> None:
        """
        List all feature definitions across all registries.

        Args:
            use_color: Enable colored output (requires colorama).
        """
        _header, _name, _dim = self._color_helpers(use_color)
        multi = len(self.registries) > 1

        all_features: List[Tuple[str, object]] = []
        for reg_name, reg_model, _, _ in self._iter_registries():
            features = reg_model.registry.features if reg_model else []
            for f in (features or []):
                all_features.append((reg_name, f))

        if not all_features:
            print("No features found in registry")
            return

        print(_header("Available features:"))
        for reg_name, feature in all_features:
            compat_parts = []
            if feature.compatibility:
                if feature.compatibility.vendor:
                    compat_parts.append(f"vendor: {feature.compatibility.vendor}")
                if feature.compatibility.soc_vendor:
                    compat_parts.append(f"soc_vendor: {feature.compatibility.soc_vendor}")
                if feature.compatibility.soc_family:
                    compat_parts.append(f"soc_family: {feature.compatibility.soc_family}")
            if feature.compatible_with:
                compat_parts.append(f"compatible_with: {', '.join(feature.compatible_with)}")
            compat_str = _dim(f" [requires {', '.join(compat_parts)}]") if compat_parts else ""
            reg_prefix = _dim(f"[{reg_name}] ") if multi else ""
            print(f"- {reg_prefix}{_name(feature.slug)}: {feature.description}{compat_str}")

    def list_distros(self, use_color: bool = True) -> None:
        """
        List all distribution/build-system definitions across all registries.

        Args:
            use_color: Enable colored output (requires colorama).
        """
        _header, _name, _dim = self._color_helpers(use_color)
        multi = len(self.registries) > 1

        all_distros: List[Tuple[str, object]] = []
        for reg_name, reg_model, _, _ in self._iter_registries():
            distros = reg_model.registry.distro if reg_model else []
            for d in (distros or []):
                all_distros.append((reg_name, d))

        if not all_distros:
            print("No distros found in registry")
            return

        print(_header("Available distros:"))
        for reg_name, distro in all_distros:
            fw_str = f", framework: {distro.framework}" if distro.framework else ""
            reg_prefix = _dim(f"[{reg_name}] ") if multi else ""
            print(
                f"- {reg_prefix}{_name(distro.slug)}: {distro.description} "
                + _dim(f"(vendor: {distro.vendor}{fw_str})")
            )

    def list_frameworks(self, use_color: bool = True) -> None:
        """
        List all build-system framework definitions across all registries.

        Args:
            use_color: Enable colored output (requires colorama).
        """
        _header, _name, _dim = self._color_helpers(use_color)
        multi = len(self.registries) > 1

        all_frameworks: List[Tuple[str, object]] = []
        for reg_name, reg_model, _, _ in self._iter_registries():
            frameworks = reg_model.registry.frameworks if reg_model else []
            for fw in (frameworks or []):
                all_frameworks.append((reg_name, fw))

        if not all_frameworks:
            print("No frameworks found in registry")
            return

        print(_header("Available frameworks:"))
        for reg_name, framework in all_frameworks:
            reg_prefix = _dim(f"[{reg_name}] ") if multi else ""
            print(
                f"- {reg_prefix}{_name(framework.slug)}: {framework.description} "
                + _dim(f"(vendor: {framework.vendor})")
            )

    def list_containers(self, use_color: bool = True) -> None:
        """
        List all available containers across all registries.

        Args:
            use_color: Enable colored output (requires colorama).
        """
        _header, _name, _dim = self._color_helpers(use_color)
        multi = len(self.registries) > 1

        # Collect all containers from all registries
        all_containers: List[Tuple[str, str, object]] = []  # (reg_name, container_name, config)
        for reg_name, reg_model, _, _ in self._iter_registries():
            reg_containers = reg_model.containers or {} if reg_model else {}
            for container_name, container_config in reg_containers.items():
                all_containers.append((reg_name, container_name, container_config))

        if not all_containers:
            print("No container definitions found in registry")
            return

        print(_header("Available Containers:"))
        for reg_name, container_name, container_config in all_containers:
            reg_prefix = _dim(f"[{reg_name}] ") if multi else ""
            print(f"- {reg_prefix}{_name(container_name)}:")
            print(f"    Image: {_dim(container_config.image)}")
            print(f"    File: {_dim(container_config.file)}")
            if container_config.args:
                args_str = ', '.join([f'{arg.name}={arg.value}' for arg in container_config.args])
                print(f"    Args: {_dim(args_str)}")

    def tree_bsp(self, use_color: bool = True, mode: str = "default") -> None:
        """
        Print a colored ASCII tree of the full BSP registry hierarchy.

        The tree is organized into sections (Frameworks, Distros, Releases,
        Devices, Features, BSP Presets) and uses Unicode box-drawing characters
        for the connectors.  Colorama colors are applied when *use_color* is
        ``True`` and colorama is installed; otherwise plain text is rendered.

        Args:
            use_color: Enable colored output (requires colorama).  Ignored when
                       colorama is not installed.
            mode: Display mode — ``"default"`` (standard detail level including
                  vendor overrides/releases), ``"compact"`` (names/slugs only),
                  or ``"full"`` (all details including includes lists).
        """

        colored = use_color and COLORAMA_AVAILABLE

        # -----------------------------------------------------------------
        # Color helpers (no-op when color is disabled)
        # -----------------------------------------------------------------
        def _c(text: str, *styles) -> str:
            if not colored:
                return text
            return "".join(styles) + text + Style.RESET_ALL

        # Convenience aliases
        def _header(text: str) -> str:
            return _c(text, Fore.CYAN, Style.BRIGHT)

        def _name(text: str) -> str:
            return _c(text, Fore.YELLOW)

        def _dim(text: str) -> str:
            return _c(text, Style.DIM)

        def _slug(text: str) -> str:
            return _c(text, Fore.GREEN) if colored else text

        # -----------------------------------------------------------------
        # Tree connector characters
        # -----------------------------------------------------------------
        BRANCH = "├── "
        LAST   = "└── "
        PIPE   = "│   "
        BLANK  = "    "

        compact = mode == "compact"
        full    = mode == "full"

        def _print_sub_lines(sub_lines: list, prefix: str) -> None:
            """Print a list of already-formatted sub-lines with tree connectors."""
            for idx, line in enumerate(sub_lines):
                conn = LAST if idx == len(sub_lines) - 1 else BRANCH
                print(f"{prefix}{conn}{line}")

        def _print_includes(includes: list, prefix: str, label: str = "includes") -> None:
            """Print an includes list as a sub-tree node."""
            if not includes:
                return
            print(f"{prefix}{BRANCH}{_dim(label + ':')}")
            inc_prefix = prefix + PIPE
            for inc_idx, inc in enumerate(includes):
                conn = LAST if inc_idx == len(includes) - 1 else BRANCH
                print(f"{inc_prefix}{conn}{_dim(inc)}")

        def _print_tree_item(sec_name: str, item: object, item_connector: str, parent_prefix: str, item_prefix: str) -> None:
            """Print a single tree item under its section, handling all section types."""
            if sec_name == "Frameworks":
                detail = _dim(f" (vendor: {item.vendor})") if not compact else ""
                print(f"{parent_prefix}{item_connector}{_name(item.slug)}: {item.description}{detail}")
                if full:
                    _print_includes(item.includes, item_prefix)

            elif sec_name == "Distros":
                if not compact:
                    parts = [f"vendor: {item.vendor}"] if item.vendor else []
                    if item.framework:
                        parts.append(f"framework: {item.framework}")
                    detail = _dim(f" ({', '.join(parts)})") if parts else ""
                else:
                    detail = ""
                print(f"{parent_prefix}{item_connector}{_name(item.slug)}: {item.description}{detail}")
                if full:
                    _print_includes(item.includes, item_prefix)

            elif sec_name == "Releases":
                if not compact:
                    tags = []
                    if item.yocto_version:
                        tags.append(f"Yocto {item.yocto_version}")
                    if item.isar_version:
                        tags.append(f"Isar {item.isar_version}")
                    tag_str = _dim(f" [{', '.join(tags)}]") if tags else ""
                else:
                    tag_str = ""
                print(f"{parent_prefix}{item_connector}{_name(item.slug)}: {item.description}{tag_str}")
                if compact:
                    return
                if full:
                    sub_lines = []
                    if item.distro:
                        sub_lines.append(_dim(f"distro: {item.distro}"))
                    if item.includes:
                        sub_lines.append(_dim(f"includes: {', '.join(item.includes)}"))
                    _print_sub_lines(sub_lines, item_prefix)
                    for vo_idx, vo in enumerate(item.vendor_overrides):
                        is_last_vo = vo_idx == len(item.vendor_overrides) - 1
                        vo_conn   = LAST if is_last_vo else BRANCH
                        vo_prefix = item_prefix + (BLANK if is_last_vo else PIPE)
                        vo_tags = []
                        if vo.slug:
                            vo_tags.append(f"slug: {vo.slug}")
                        if vo.distro:
                            vo_tags.append(f"distro: {vo.distro}")
                        vo_tag_str = _dim(f" ({', '.join(vo_tags)})") if vo_tags else ""
                        print(f"{item_prefix}{vo_conn}{_dim('vendor override: ')}{_slug(vo.vendor)}{vo_tag_str}")
                        vo_sub = []
                        if vo.includes:
                            vo_sub.append(_dim(f"includes: {', '.join(vo.includes)}"))
                        _print_sub_lines(vo_sub, vo_prefix)
                        if vo.soc_vendors:
                            for svo_idx, svo in enumerate(vo.soc_vendors):
                                is_last_svo = svo_idx == len(vo.soc_vendors) - 1
                                svo_conn   = LAST if is_last_svo else BRANCH
                                svo_prefix = vo_prefix + (BLANK if is_last_svo else PIPE)
                                svo_tag_str = _dim(f" (distro: {svo.distro})") if svo.distro else ""
                                print(f"{vo_prefix}{svo_conn}{_dim('soc vendor: ')}{_slug(svo.vendor)}{svo_tag_str}")
                                svo_sub = []
                                if svo.includes:
                                    svo_sub.append(_dim(f"includes: {', '.join(svo.includes)}"))
                                _print_sub_lines(svo_sub, svo_prefix)
                                for vr_idx, vr in enumerate(svo.releases):
                                    is_last_vr = vr_idx == len(svo.releases) - 1
                                    vr_conn   = LAST if is_last_vr else BRANCH
                                    vr_prefix = svo_prefix + (BLANK if is_last_vr else PIPE)
                                    print(f"{svo_prefix}{vr_conn}{_dim('vendor release: ')}{_slug(vr.slug)}: {vr.description}")
                                    _print_includes(vr.includes, vr_prefix)
                        else:
                            for vr_idx, vr in enumerate(vo.releases):
                                is_last_vr = vr_idx == len(vo.releases) - 1
                                vr_conn   = LAST if is_last_vr else BRANCH
                                vr_prefix = vo_prefix + (BLANK if is_last_vr else PIPE)
                                print(f"{vo_prefix}{vr_conn}{_dim('vendor release: ')}{_slug(vr.slug)}: {vr.description}")
                                _print_includes(vr.includes, vr_prefix)
                else:
                    sub_items = []
                    if item.distro:
                        sub_items.append(_dim(f"distro: {item.distro}"))
                    for vo in item.vendor_overrides:
                        vo_parts = [f"vendor override: {vo.vendor}"]
                        if vo.slug:
                            vo_parts.append(f"slug: {vo.slug}")
                        if vo.distro:
                            vo_parts.append(f"distro: {vo.distro}")
                        if vo.soc_vendors:
                            svo_strs = []
                            for svo in vo.soc_vendors:
                                svo_p = [svo.vendor]
                                if svo.distro:
                                    svo_p.append(f"distro: {svo.distro}")
                                svo_vr_names = [vr.slug for vr in svo.releases]
                                if svo_vr_names:
                                    svo_p.append(f"releases: {', '.join(svo_vr_names)}")
                                svo_strs.append(f"[{'; '.join(svo_p)}]")
                            vo_parts.append(f"soc vendors: {', '.join(svo_strs)}")
                        else:
                            vr_names = [vr.slug for vr in vo.releases]
                            if vr_names:
                                vo_parts.append(f"releases: {', '.join(vr_names)}")
                        sub_items.append(_dim(", ".join(vo_parts)))
                    _print_sub_lines(sub_items, item_prefix)

            elif sec_name == "Devices":
                if not compact:
                    parts = [f"vendor: {item.vendor}", f"soc_vendor: {item.soc_vendor}"]
                    if item.soc_family:
                        parts.append(f"soc_family: {item.soc_family}")
                    detail = _dim(f" ({', '.join(parts)})")
                else:
                    detail = ""
                print(f"{parent_prefix}{item_connector}{_name(item.slug)}: {item.description}{detail}")
                if full:
                    _print_includes(item.includes, item_prefix)

            elif sec_name == "Features":
                if not compact:
                    compat_parts = []
                    if item.compatibility:
                        if item.compatibility.vendor:
                            compat_parts.append(f"vendor: {item.compatibility.vendor}")
                        if item.compatibility.soc_vendor:
                            compat_parts.append(f"soc_vendor: {item.compatibility.soc_vendor}")
                        if item.compatibility.soc_family:
                            compat_parts.append(f"soc_family: {item.compatibility.soc_family}")
                    if item.compatible_with:
                        compat_parts.append(f"compatible_with: {', '.join(item.compatible_with)}")
                    compat_str = _dim(f" [requires {', '.join(compat_parts)}]") if compat_parts else ""
                else:
                    compat_str = ""
                print(f"{parent_prefix}{item_connector}{_name(item.slug)}: {item.description}{compat_str}")
                if full:
                    _print_includes(item.includes, item_prefix)
                    has_vendor_overrides = bool(item.vendor_overrides)
                    for ro_idx, ro in enumerate(item.release_overrides):
                        is_last_ro = ro_idx == len(item.release_overrides) - 1 and not has_vendor_overrides
                        ro_conn   = LAST if is_last_ro else BRANCH
                        ro_prefix = item_prefix + (BLANK if is_last_ro else PIPE)
                        print(f"{item_prefix}{ro_conn}{_dim('release override: ')}{_slug(ro.release)}")
                        _print_includes(ro.includes, ro_prefix)
                    for vo_idx, vo in enumerate(item.vendor_overrides):
                        is_last_vo = vo_idx == len(item.vendor_overrides) - 1
                        vo_conn   = LAST if is_last_vo else BRANCH
                        vo_prefix = item_prefix + (BLANK if is_last_vo else PIPE)
                        vo_tags = []
                        if vo.slug:
                            vo_tags.append(f"slug: {vo.slug}")
                        if vo.distro:
                            vo_tags.append(f"distro: {vo.distro}")
                        vo_tag_str = _dim(f" ({', '.join(vo_tags)})") if vo_tags else ""
                        print(f"{item_prefix}{vo_conn}{_dim('vendor override: ')}{_slug(vo.vendor)}{vo_tag_str}")
                        vo_sub = []
                        if vo.includes:
                            vo_sub.append(_dim(f"includes: {', '.join(vo.includes)}"))
                        _print_sub_lines(vo_sub, vo_prefix)
                        if vo.soc_vendors:
                            for svo_idx, svo in enumerate(vo.soc_vendors):
                                is_last_svo = svo_idx == len(vo.soc_vendors) - 1
                                svo_conn   = LAST if is_last_svo else BRANCH
                                svo_prefix = vo_prefix + (BLANK if is_last_svo else PIPE)
                                svo_tag_str = _dim(f" (distro: {svo.distro})") if svo.distro else ""
                                print(f"{vo_prefix}{svo_conn}{_dim('soc vendor: ')}{_slug(svo.vendor)}{svo_tag_str}")
                                svo_sub = []
                                if svo.includes:
                                    svo_sub.append(_dim(f"includes: {', '.join(svo.includes)}"))
                                _print_sub_lines(svo_sub, svo_prefix)
                                for vr_idx, vr in enumerate(svo.releases):
                                    is_last_vr = vr_idx == len(svo.releases) - 1
                                    vr_conn   = LAST if is_last_vr else BRANCH
                                    vr_prefix = svo_prefix + (BLANK if is_last_vr else PIPE)
                                    print(f"{svo_prefix}{vr_conn}{_dim('vendor release: ')}{_slug(vr.slug)}: {vr.description}")
                                    _print_includes(vr.includes, vr_prefix)
                        else:
                            for vr_idx, vr in enumerate(vo.releases):
                                is_last_vr = vr_idx == len(vo.releases) - 1
                                vr_conn   = LAST if is_last_vr else BRANCH
                                vr_prefix = vo_prefix + (BLANK if is_last_vr else PIPE)
                                print(f"{vo_prefix}{vr_conn}{_dim('vendor release: ')}{_slug(vr.slug)}: {vr.description}")
                                _print_includes(vr.includes, vr_prefix)

            elif sec_name == "BSP Presets":
                print(f"{parent_prefix}{item_connector}{_name(item.name)}: {item.description}")
                if compact:
                    return
                sub_lines = [_dim(f"device: {item.device}  release: {item.release}")]
                if item.vendor_release:
                    sub_lines.append(_dim(f"vendor release: {item.vendor_release}"))
                if full and getattr(item, "override", None):
                    sub_lines.append(_dim(f"override: {item.override}"))
                if item.features:
                    sub_lines.append(_dim(f"features: {', '.join(item.features)}"))
                _print_sub_lines(sub_lines, item_prefix)



        # -----------------------------------------------------------------
        # Registry root
        # -----------------------------------------------------------------
        multi = len(self.registries) > 1
        print(_header("BSP Registry"))

        def _collect_section(section_key: str) -> List[Tuple[str, object]]:
            """Collect (registry_name, item) pairs for a given section key."""
            result = []
            for reg_name, reg_model, reg_resolver, _ in self._iter_registries():
                if not reg_model:
                    continue
                registry = reg_model.registry
                if section_key == "frameworks":
                    items = registry.frameworks or []
                elif section_key == "distros":
                    items = registry.distro or []
                elif section_key == "releases":
                    items = registry.releases or []
                elif section_key == "devices":
                    items = registry.devices or []
                elif section_key == "features":
                    items = registry.features or []
                elif section_key == "presets":
                    items = reg_resolver.list_presets()
                else:
                    items = []
                for it in items:
                    result.append((reg_name, it))
            return result

        # Determine which top-level sections are present and non-empty
        frameworks_rows = _collect_section("frameworks")
        distros_rows    = _collect_section("distros")
        releases_rows   = _collect_section("releases")
        devices_rows    = _collect_section("devices")
        features_rows   = _collect_section("features")
        presets_rows    = _collect_section("presets")

        sections = [
            ("Frameworks", frameworks_rows),
            ("Distros",    distros_rows),
            ("Releases",   releases_rows),
            ("Devices",    devices_rows),
            ("Features",   features_rows),
            ("BSP Presets", presets_rows),
        ]
        # Filter empty sections
        sections = [(name, rows) for name, rows in sections if rows]

        for sec_idx, (sec_name, rows) in enumerate(sections):
            is_last_section = sec_idx == len(sections) - 1
            sec_connector  = LAST if is_last_section else BRANCH
            sec_prefix     = BLANK if is_last_section else PIPE

            print(f"{sec_connector}{_header(sec_name)} ({len(rows)})")

            # When multiple registries are loaded, group items under registry sub-nodes
            if multi:
                # Gather unique registry names in order
                reg_names_seen: List[str] = []
                for rn, _ in rows:
                    if rn not in reg_names_seen:
                        reg_names_seen.append(rn)

                for rn_idx, rn in enumerate(reg_names_seen):
                    is_last_rn = rn_idx == len(reg_names_seen) - 1
                    rn_connector = LAST if is_last_rn else BRANCH
                    rn_prefix    = sec_prefix + (BLANK if is_last_rn else PIPE)
                    rn_items = [it for r, it in rows if r == rn]
                    print(f"{sec_prefix}{rn_connector}{_dim('[' + rn + ']')} ({len(rn_items)})")

                    for item_idx, item in enumerate(rn_items):
                        is_last_item = item_idx == len(rn_items) - 1
                        item_connector = LAST if is_last_item else BRANCH
                        item_prefix    = rn_prefix + (BLANK if is_last_item else PIPE)
                        _print_tree_item(sec_name, item, item_connector, rn_prefix, item_prefix)
            else:
                items = [it for _, it in rows]
                for item_idx, item in enumerate(items):
                    is_last_item = item_idx == len(items) - 1
                    item_connector = LAST if is_last_item else BRANCH
                    item_prefix    = sec_prefix + (BLANK if is_last_item else PIPE)
                    _print_tree_item(sec_name, item, item_connector, sec_prefix, item_prefix)

        if not sections:
            print(f"{LAST}{_dim('(empty registry)')}")

    # ------------------------------------------------------------------
    # Preset lookup
    # ------------------------------------------------------------------

    def get_bsp_by_name(self, bsp_name: str) -> BspPreset:
        """
        Retrieve a BSP preset configuration by name.

        Accepts ``registry:preset`` syntax for unambiguous lookup when
        multiple registries are loaded.  When a plain preset name is given,
        all registries are searched in order; if the name appears in more
        than one registry a warning is emitted and the first match is
        returned.

        Args:
            bsp_name: Name of the preset to retrieve, optionally prefixed
                      with the registry name (``registry:preset``).

        Returns:
            BspPreset configuration object

        Raises:
            SystemExit: If preset with given name is not found
        """
        # Use _resolve_preset_multi for the lookup; we only need the preset object
        _, preset, _, _, _, _ = self._resolve_preset_multi(bsp_name)
        return preset

    # ------------------------------------------------------------------
    # Build directory helpers
    # ------------------------------------------------------------------

    def prepare_build_directory(self, build_path: str) -> None:
        """
        Prepare build directory, creating it if necessary.

        Args:
            build_path: Path to build directory

        Raises:
            SystemExit: If directory cannot be created
        """
        logging.info(f"Preparing build directory: {build_path}")
        resolver.ensure_directory(build_path)

    def _copy_files(
        self, resolved: ResolvedConfig, build_path_override: Optional[str] = None
    ) -> None:
        """
        Copy files into the build environment before the build starts.

        Each entry in ``resolved.copy`` is a single-key dict mapping a source
        path to a destination path.  The source path is resolved relative to
        the registry file's parent directory.  The destination path is resolved
        relative to the BSP's build directory (``resolved.build_path``), so
        that copied files land directly inside the build workspace for the
        current BSP.  If the destination ends with ``/`` or is an existing
        directory the source filename is preserved inside it.

        The copied files are therefore accessible inside the build container
        because the build directory is mounted into the container during the
        build.

        Args:
            resolved: Resolved build configuration containing copy entries.
            build_path_override: Optional build path to use instead of
                                ``resolved.build_path``.

        Raises:
            SystemExit: If a source file does not exist.
        """
        if not resolved.copy:
            return

        base = self.config_path.parent
        # Destination paths are relative to the BSP's build directory so that
        # copied files land inside the build workspace for the current BSP.
        # When build_path is empty (no preset, direct resolve() call) fall back
        # to the registry directory to preserve backward-compatible behaviour.
        if build_path_override is not None:
            raw_build_path = build_path_override
        else:
            raw_build_path = resolved.build_path or ""
        if raw_build_path:
            build_abs = Path(raw_build_path)
            if not build_abs.is_absolute():
                build_abs = (base / build_abs).resolve()
            else:
                build_abs = build_abs.resolve()
        else:
            build_abs = base.resolve()

        for copy_entry in resolved.copy:
            for src, dst in copy_entry.items():
                src_path = Path(src)
                if not src_path.is_absolute():
                    src_path = (base / src_path).resolve()

                if not src_path.exists():
                    self.logger.error(
                        f"Copy source file not found: {src_path}"
                    )
                    sys.exit(1)

                dst_path = Path(dst)
                if not dst_path.is_absolute():
                    dst_path = (build_abs / dst_path).resolve()

                # If destination looks like a directory (trailing slash or
                # already is one), place the file inside it.
                if str(dst).endswith("/") or dst_path.is_dir():
                    dst_path = dst_path / src_path.name

                dst_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self.logger.info(f"Copying file: {src_path} -> {dst_path}")
                    shutil.copy2(str(src_path), str(dst_path))
                except OSError as e:
                    self.logger.error(
                        f"Failed to copy '{src_path}' to '{dst_path}': {e}"
                    )
                    sys.exit(1)
                self.logger.info(f"Copied {src_path} -> {dst_path}")

    # ------------------------------------------------------------------
    # Internal: KasManager factory for a resolved config
    # ------------------------------------------------------------------

    def _get_kas_manager_for_resolved(
        self,
        resolved: ResolvedConfig,
        use_container: bool = True,
        build_path_override: Optional[str] = None,
    ) -> KasManager:
        """
        Create a KasManager for the given ResolvedConfig.

        If the resolved config includes local_conf additions, a temporary
        KAS YAML file is generated to carry those into the build.  The
        caller is responsible for deleting the temp file when done.

        Environment variables are merged in this order (later entries win):
        1. Root-level ``environment`` list (global defaults)
        2. Named environment variables from ``resolved.env``

        Args:
            resolved: Resolved device+release+features build config
            use_container: Whether to use containerized KAS
            build_path_override: Optional build path to use instead of
                                ``resolved.build_path``.

        Returns:
            Configured KasManager instance
        """
        # Build per-build EnvironmentManager: root vars merged with
        # named-env / feature vars from the resolved config.
        root_vars: List[EnvironmentVariable] = (
            list(self.model.environment.variables)
            if self.model.environment and self.model.environment.variables
            else []
        )
        # resolved.env contains named-env vars first, then feature vars.
        # Merge by appending; later keys win in EnvironmentManager.
        merged_vars = root_vars + list(resolved.env)
        # Always create a fresh EnvironmentManager from merged vars so that
        # named-env and feature variables are applied for this specific build.
        # Fall back to the global env_manager only when no vars exist at all.
        env_mgr = EnvironmentManager(merged_vars) if merged_vars else self.env_manager

        downloads = env_mgr.get_value("DL_DIR") if env_mgr else None
        sstate = env_mgr.get_value("SSTATE_DIR") if env_mgr else None

        if downloads:
            resolver.ensure_directory(downloads)
        if sstate:
            resolver.ensure_directory(sstate)

        # Determine KAS file list: generate a composed YAML when we have
        # local_conf additions so that everything is in a single entry-point.
        if resolved.local_conf:
            temp_fd, temp_path = tempfile.mkstemp(
                prefix="bsp_composed_", suffix=".yml"
            )
            os.close(temp_fd)
            self.resolver.generate_kas_yaml(
                resolved,
                temp_path,
                base_dir=str(self.config_path.parent),
            )
            kas_files = [temp_path]
            self._temp_kas_file = temp_path
        else:
            # Resolve relative paths against the registry directory
            base = self.config_path.parent
            kas_files = []
            for f in resolved.kas_files:
                p = Path(f)
                if p.is_absolute():
                    kas_files.append(str(p))
                else:
                    kas_files.append(str((base / p).resolve()))
            self._temp_kas_file = None

        container_image = (
            resolved.container.image
            if resolved.container and use_container
            else None
        )
        container_runtime_args = (
            resolved.container.runtime_args
            if resolved.container and use_container
            else None
        )
        container_volumes = (
            resolved.container.volumes
            if resolved.container and use_container
            else []
        )
        effective_build_path = (
            build_path_override if build_path_override is not None else resolved.build_path
        )

        kas_mgr = KasManager(
            kas_files,
            effective_build_path,
            download_dir=downloads,
            sstate_dir=sstate,
            use_container=use_container,
            container_image=container_image,
            container_runtime_args=container_runtime_args,
            container_volumes=container_volumes,
            container_privileged=(
                resolved.container.privileged if resolved.container and use_container else False
            ),
            search_paths=[str(self.config_path.parent)],
            env_manager=env_mgr,
            verbose=self.verbose,
        )
        return kas_mgr

    def _cleanup_temp_kas_file(self) -> None:
        """Remove the temporary KAS YAML file if one was created."""
        temp_file = getattr(self, "_temp_kas_file", None)
        if temp_file and os.path.exists(temp_file):
            try:
                os.unlink(temp_file)
                logging.debug(f"Removed temporary KAS file: {temp_file}")
            except OSError as e:
                logging.warning(f"Could not remove temporary KAS file: {e}")
        self._temp_kas_file = None

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build_resolved(
        self,
        resolved: ResolvedConfig,
        checkout_only: bool = False,
        label: str = "",
        deploy_after_build: bool = False,
        preset: Optional[BspPreset] = None,
        deploy_overrides: Optional[Dict] = None,
        target: Optional[str] = None,
        task: Optional[str] = None,
        build_path_override: Optional[str] = None,
    ) -> None:
        """
        Execute a build (or checkout) for the given ResolvedConfig.

        Args:
            resolved: Resolved build configuration
            checkout_only: If True, only checkout and validate without building
            label: Descriptive label for log messages
            deploy_after_build: If True, deploy artifacts after a successful build
            preset: Optional BSP preset whose ``deploy`` block is applied on
                    top of the global deploy config before CLI overrides.
            deploy_overrides: CLI-level overrides for the deploy configuration
            target: Optional Bitbake build target to override registry targets
            task: Optional Bitbake task to run (e.g. compile, configure)
            build_path_override: If provided, overrides the build output path from the registry
        """
        action = "Checking out" if checkout_only else "Building"
        logging.info(f"{action} {label or resolved.device.slug}")

        # Build Docker image if needed (skip in checkout mode)
        if not checkout_only and resolved.container:
            container = resolved.container
            if container.file and container.image:
                build_docker(
                    str(self.config_path.parent),
                    container.file,
                    container.image,
                    container.args,
                    verbose=self.verbose,
                )
        else:
            if checkout_only:
                logging.info("Skipping Docker build in checkout mode")

        if build_path_override is not None:
            logging.info(f"Overriding build path: {build_path_override}")
        build_path = build_path_override or resolved.build_path
        self.prepare_build_directory(build_path)
        self._copy_files(resolved, build_path_override=build_path_override)

        kas_mgr = self._get_kas_manager_for_resolved(
            resolved,
            use_container=not checkout_only,
            build_path_override=build_path_override,
        )

        try:
            config_output = kas_mgr.dump_config(show_output=False)
            if config_output:
                logging.debug("Configuration dump:\n" + config_output)

            if checkout_only:
                logging.info("Performing checkout and validation (no build)...")
                kas_mgr.checkout_project()
                logging.info(f"Checkout and validation completed successfully!")
            else:
                kas_mgr.build_project(target=target, task=task)
                logging.info(f"Build completed successfully!")
                if deploy_after_build:
                    self._deploy_resolved(
                        resolved,
                        preset=preset,
                        deploy_overrides=deploy_overrides or {},
                        build_path_override=build_path_override,
                    )
        finally:
            self._cleanup_temp_kas_file()

    def build_bsp(
        self,
        bsp_name: str,
        checkout_only: bool = False,
        deploy_after_build: bool = False,
        deploy_overrides: Optional[Dict] = None,
        target: Optional[str] = None,
        task: Optional[str] = None,
        build_path_override: Optional[str] = None,
        feature_slugs: Optional[List[str]] = None,
    ) -> None:
        """
        Build a BSP by preset name.

        Args:
            bsp_name: Name of the BSP preset to build
            checkout_only: If True, only checkout and validate without building
            deploy_after_build: If True, deploy artifacts after a successful build
            deploy_overrides: CLI-level overrides for the deploy configuration
            target: Optional Bitbake build target to override registry targets
            task: Optional Bitbake task to run (e.g. compile, configure)
            build_path_override: If provided, overrides the build output path from the registry
            feature_slugs: Additional feature slugs to enable on top of those in the preset

        Raises:
            SystemExit: If preset not found or build fails
        """
        logging.info(f"{'Checking out' if checkout_only else 'Building'} BSP preset: {bsp_name}")
        resolved, preset, _, reg_model, reg_resolver, reg_path = self._resolve_preset_multi(
            bsp_name, extra_feature_slugs=feature_slugs
        )
        with self._use_registry_context(reg_model, reg_resolver, reg_path):
            self._build_resolved(
                resolved,
                checkout_only=checkout_only,
                label=f"{preset.name} - {preset.description}",
                deploy_after_build=deploy_after_build,
                preset=preset,
                deploy_overrides=deploy_overrides,
                target=target,
                task=task,
                build_path_override=build_path_override,
            )

    def build_by_components(
        self,
        device_slug: str,
        release_slug: str,
        feature_slugs: Optional[List[str]] = None,
        checkout_only: bool = False,
        deploy_after_build: bool = False,
        deploy_overrides: Optional[Dict] = None,
        target: Optional[str] = None,
        task: Optional[str] = None,
        build_path_override: Optional[str] = None,
    ) -> None:
        """
        Build by specifying device, release, and optional features directly.

        Args:
            device_slug: Device slug
            release_slug: Release slug
            feature_slugs: Optional list of feature slugs to enable
            checkout_only: If True, only checkout and validate without building
            deploy_after_build: If True, deploy artifacts after a successful build
            deploy_overrides: CLI-level overrides for the deploy configuration
            target: Optional Bitbake build target to override registry targets
            task: Optional Bitbake task to run (e.g. compile, configure)
            build_path_override: If provided, overrides the build output path from the registry

        Raises:
            SystemExit: If any component is not found, incompatible, or build fails
        """
        logging.info(
            f"{'Checking out' if checkout_only else 'Building'} "
            f"device={device_slug} release={release_slug} "
            f"features={feature_slugs or []}"
        )
        resolved = self.resolver.resolve(device_slug, release_slug, feature_slugs)
        self._build_resolved(
            resolved,
            checkout_only=checkout_only,
            label=f"{device_slug}/{release_slug}",
            deploy_after_build=deploy_after_build,
            deploy_overrides=deploy_overrides,
            target=target,
            task=task,
            build_path_override=build_path_override,
        )

    # ------------------------------------------------------------------
    # Shell
    # ------------------------------------------------------------------

    def _shell_resolved(
        self,
        resolved: ResolvedConfig,
        command: Optional[str] = None,
        label: str = "",
    ) -> None:
        """
        Start a KAS shell session for the given ResolvedConfig.

        Args:
            resolved: Resolved build configuration
            command: Optional command to run in the shell
            label: Descriptive label for log messages
        """
        logging.info(f"Starting shell for {label or resolved.device.slug}")

        if resolved.container:
            container = resolved.container
            if container.file and container.image:
                logging.info("Building Docker image for shell environment...")
                build_docker(str(self.config_path.parent), container.file, container.image, container.args)

        self.prepare_build_directory(resolved.build_path)
        self._copy_files(resolved)

        kas_mgr = self._get_kas_manager_for_resolved(resolved, use_container=True)

        try:
            if command:
                logging.info(f"Executing command: {command}")
            else:
                logging.info("Starting interactive KAS shell session...")
                logging.info("Use 'Ctrl+D' or type 'exit' to leave the shell.")
            kas_mgr.shell_session(command=command)
        finally:
            self._cleanup_temp_kas_file()

    def shell_into_bsp(self, bsp_name: str, command: Optional[str] = None) -> None:
        """
        Enter interactive shell for a BSP preset.

        Args:
            bsp_name: Name of the BSP preset
            command: Optional command to execute in the shell

        Raises:
            SystemExit: If preset not found or shell fails
        """
        logging.info(f"Entering shell for BSP preset: {bsp_name}")
        resolved, preset, _, reg_model, reg_resolver, reg_path = self._resolve_preset_multi(bsp_name)
        with self._use_registry_context(reg_model, reg_resolver, reg_path):
            self._shell_resolved(
                resolved, command=command, label=f"{preset.name} - {preset.description}"
            )

    def shell_by_components(
        self,
        device_slug: str,
        release_slug: str,
        feature_slugs: Optional[List[str]] = None,
        command: Optional[str] = None,
    ) -> None:
        """
        Enter interactive shell by specifying device, release, and features directly.

        Args:
            device_slug: Device slug
            release_slug: Release slug
            feature_slugs: Optional list of feature slugs
            command: Optional command to execute in the shell

        Raises:
            SystemExit: If any component is not found or shell fails
        """
        logging.info(
            f"Entering shell for device={device_slug} release={release_slug} "
            f"features={feature_slugs or []}"
        )
        resolved = self.resolver.resolve(device_slug, release_slug, feature_slugs)
        self._shell_resolved(
            resolved, command=command, label=f"{device_slug}/{release_slug}"
        )

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _export_resolved(
        self,
        resolved: ResolvedConfig,
        output_file: Optional[str] = None,
        label: str = "",
    ) -> None:
        """
        Export KAS configuration for the given ResolvedConfig.

        Args:
            resolved: Resolved build configuration
            output_file: Optional file path to save the configuration
            label: Descriptive label for log messages
        """
        logging.info(f"Exporting KAS configuration for {label or resolved.device.slug}")

        downloads = None
        sstate = None
        if self.env_manager:
            downloads = self.env_manager.get_value("DL_DIR")
            sstate = self.env_manager.get_value("SSTATE_DIR")

        # Use a temporary build directory for export
        with tempfile.TemporaryDirectory(prefix="bsp_export_") as temp_dir:
            if resolved.local_conf:
                temp_fd, temp_path = tempfile.mkstemp(
                    prefix="bsp_composed_", suffix=".yml"
                )
                os.close(temp_fd)
                self.resolver.generate_kas_yaml(
                    resolved,
                    temp_path,
                    base_dir=str(self.config_path.parent),
                )
                kas_files = [temp_path]
            else:
                base = self.config_path.parent
                kas_files = []
                for f in resolved.kas_files:
                    p = Path(f)
                    if p.is_absolute():
                        kas_files.append(str(p))
                    else:
                        kas_files.append(str((base / p).resolve()))
                temp_path = None

            try:
                kas_mgr = KasManager(
                    kas_files,
                    temp_dir,
                    download_dir=downloads,
                    sstate_dir=sstate,
                    use_container=False,
                    search_paths=[str(self.config_path.parent)],
                    env_manager=self.env_manager,
                )
                config_yaml = kas_mgr.export_kas_config(output_file)
            finally:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)

        if not output_file:
            print("\n" + "=" * 60)
            print(f"KAS Configuration for {label or resolved.device.slug}")
            print("=" * 60)
            print(config_yaml)
            print("=" * 60)

        logging.info("Configuration exported successfully!")

    def export_bsp_config(
        self, bsp_name: str, output_file: Optional[str] = None
    ) -> None:
        """
        Export KAS configuration for a BSP preset.

        Args:
            bsp_name: Name of the BSP preset to export
            output_file: Optional file path to save the configuration

        Raises:
            SystemExit: If preset not found or export fails
        """
        logging.info(f"Exporting KAS configuration for BSP preset: {bsp_name}")
        resolved, preset, _, reg_model, reg_resolver, reg_path = self._resolve_preset_multi(bsp_name)
        with self._use_registry_context(reg_model, reg_resolver, reg_path):
            self._export_resolved(
                resolved,
                output_file=output_file,
                label=f"{preset.name} - {preset.description}",
            )

    def export_by_components(
        self,
        device_slug: str,
        release_slug: str,
        feature_slugs: Optional[List[str]] = None,
        output_file: Optional[str] = None,
    ) -> None:
        """
        Export KAS configuration by specifying device, release, and features directly.

        Args:
            device_slug: Device slug
            release_slug: Release slug
            feature_slugs: Optional list of feature slugs
            output_file: Optional file path to save the configuration

        Raises:
            SystemExit: If any component is not found or export fails
        """
        logging.info(
            f"Exporting configuration for device={device_slug} release={release_slug} "
            f"features={feature_slugs or []}"
        )
        resolved = self.resolver.resolve(device_slug, release_slug, feature_slugs)
        self._export_resolved(
            resolved,
            output_file=output_file,
            label=f"{device_slug}/{release_slug}",
        )

    # ------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------

    def _resolve_deploy_config(
        self,
        resolved: ResolvedConfig,
        preset: Optional[BspPreset] = None,
        deploy_overrides: Optional[Dict] = None,
    ) -> DeployConfig:
        """
        Resolve the effective ``DeployConfig`` for a build.

        Merge order (later entries override earlier ones):
        1. Root-level ``deploy`` from the registry (global defaults)
        2. Preset-level ``deploy`` (if the preset defines one) — only fields
           that differ from the ``DeployConfig`` defaults are applied, so a
           minimal preset block only needs to specify the fields it wants to
           override.
        3. CLI-supplied *deploy_overrides* dict

        If no deploy config is defined anywhere a default ``DeployConfig``
        is returned.

        Args:
            resolved: Resolved build configuration.
            preset: Optional BSP preset whose ``deploy`` block (if any) is
                    merged on top of the root-level config.
            deploy_overrides: Dict of field overrides from the CLI.

        Returns:
            Effective ``DeployConfig`` instance.
        """
        # Start with global registry deploy config or defaults
        base = self.model.deploy if self.model and self.model.deploy else DeployConfig()

        # Expand $ENV{VAR} in account_url if present
        if base.account_url:
            base = replace(base, account_url=_expand_env(base.account_url))

        # Apply preset-level deploy overrides (only fields that differ from defaults)
        if preset is not None and preset.deploy is not None:
            preset_deploy = preset.deploy
            defaults = DeployConfig()
            preset_overrides = {
                f.name: getattr(preset_deploy, f.name)
                for f in dataclass_fields(preset_deploy)
                if getattr(preset_deploy, f.name) != getattr(defaults, f.name)
            }
            if preset_overrides:
                base = replace(base, **preset_overrides)

        # Apply CLI overrides
        if deploy_overrides:
            base = replace(base, **{k: v for k, v in deploy_overrides.items() if v is not None})

        return base

    def _deploy_resolved(
        self,
        resolved: ResolvedConfig,
        preset: Optional[BspPreset] = None,
        deploy_overrides: Optional[Dict] = None,
        dry_run: bool = False,
        build_path_override: Optional[str] = None,
    ) -> DeployResult:
        """
        Deploy build artifacts for the given ResolvedConfig.

        Args:
            resolved: Resolved build configuration containing build path and
                      device/release/distro metadata.
            preset: Optional BSP preset whose ``deploy`` block is applied on
                    top of the global deploy config before CLI overrides.
            deploy_overrides: CLI-level overrides for the deploy configuration.
            dry_run: When True log what would be uploaded without uploading.
            build_path_override: Optional build path override for artifact lookup.

        Returns:
            ``DeployResult`` with metadata for every uploaded artifact.
        """
        deploy_cfg = self._resolve_deploy_config(resolved, preset=preset, deploy_overrides=deploy_overrides)

        if dry_run:
            deploy_cfg = DeployConfig(**{**deploy_cfg.__dict__, "provider": deploy_cfg.provider})

        # Determine container/bucket name
        container_or_bucket = deploy_cfg.container or deploy_cfg.bucket
        if not container_or_bucket and not dry_run:
            logging.error(
                "No storage container/bucket configured for deployment. "
                "Set 'deploy.container' in the registry or pass --container/--bucket."
            )
            sys.exit(1)

        # Build provider-specific kwargs
        provider = deploy_cfg.provider
        if provider == "azure":
            backend_kwargs: Dict = {
                "container_name": container_or_bucket or "bsp-artifacts",
                "account_url": deploy_cfg.account_url,
                "dry_run": dry_run,
            }
        elif provider == "aws":
            backend_kwargs = {
                "bucket_name": container_or_bucket or "bsp-artifacts",
                "region": deploy_cfg.region,
                "profile": deploy_cfg.profile,
                "dry_run": dry_run,
            }
        else:
            logging.error("Unsupported deploy provider: %s", provider)
            sys.exit(1)

        try:
            backend = create_backend(provider, **backend_kwargs)
        except (ImportError, ValueError) as exc:
            logging.error("Failed to initialize storage backend: %s", exc)
            sys.exit(1)

        deployer = ArtifactDeployer(deploy_cfg, backend)
        effective_build_path = (
            build_path_override if build_path_override is not None else resolved.build_path
        )
        result = deployer.deploy(
            build_path=effective_build_path,
            device=resolved.device.slug,
            release=resolved.release.slug,
            distro=resolved.effective_distro or "",
            vendor=resolved.device.vendor,
        )

        # Print summary
        action = "[dry-run] Would upload" if dry_run else "Uploaded"
        if result.artifacts:
            print(f"\n{action} {result.success_count} artifact(s):")
            for art in result.artifacts:
                print(f"  {art.local_path.name} → {art.remote_url}")
            if result.manifest_url:
                print(f"  manifest.json → {result.manifest_url}")
        else:
            print("No artifacts found to deploy.")

        return result

    def deploy_bsp(
        self,
        bsp_name: str,
        deploy_overrides: Optional[Dict] = None,
        dry_run: bool = False,
    ) -> DeployResult:
        """
        Deploy artifacts for a BSP preset.

        Args:
            bsp_name: Name of the BSP preset.
            deploy_overrides: CLI-level overrides for the deploy configuration.
            dry_run: When True list what would be uploaded without uploading.

        Returns:
            ``DeployResult`` with upload metadata.

        Raises:
            SystemExit: If preset not found or deployment fails.
        """
        logging.info("Deploying artifacts for BSP preset: %s", bsp_name)
        resolved, preset, _, reg_model, reg_resolver, reg_path = self._resolve_preset_multi(bsp_name)
        with self._use_registry_context(reg_model, reg_resolver, reg_path):
            return self._deploy_resolved(resolved, preset=preset, deploy_overrides=deploy_overrides, dry_run=dry_run)

    def deploy_by_components(
        self,
        device_slug: str,
        release_slug: str,
        feature_slugs: Optional[List[str]] = None,
        deploy_overrides: Optional[Dict] = None,
        dry_run: bool = False,
    ) -> DeployResult:
        """
        Deploy artifacts by specifying device, release, and features directly.

        Args:
            device_slug: Device slug.
            release_slug: Release slug.
            feature_slugs: Optional list of feature slugs.
            deploy_overrides: CLI-level overrides for the deploy configuration.
            dry_run: When True list what would be uploaded without uploading.

        Returns:
            ``DeployResult`` with upload metadata.

        Raises:
            SystemExit: If any component is not found or deployment fails.
        """
        logging.info(
            "Deploying artifacts for device=%s release=%s features=%s",
            device_slug, release_slug, feature_slugs or [],
        )
        resolved = self.resolver.resolve(device_slug, release_slug, feature_slugs)
        return self._deploy_resolved(resolved, deploy_overrides=deploy_overrides, dry_run=dry_run)

    # ------------------------------------------------------------------
    # Gather (download artifacts from cloud storage)
    # ------------------------------------------------------------------

    def _gather_resolved(
        self,
        resolved: ResolvedConfig,
        preset: Optional[BspPreset] = None,
        dest_dir: Optional[str] = None,
        deploy_overrides: Optional[Dict] = None,
        dry_run: bool = False,
        date_override: Optional[str] = None,
    ) -> GatherResult:
        """
        Download build artifacts for the given :class:`~bsp.resolver.ResolvedConfig`.

        The effective :class:`~bsp.models.DeployConfig` is resolved with the
        same merge order as :meth:`_deploy_resolved` so that ``gather`` and
        ``deploy`` always refer to the same storage location.

        Args:
            resolved: Resolved build configuration containing device/release
                      metadata and the default local build path.
            preset: Optional BSP preset whose ``deploy`` block is applied on
                    top of the global deploy config before CLI overrides.
            dest_dir: Local directory to write downloaded artifacts into.
                      When ``None`` the build path from *resolved* is used.
            deploy_overrides: CLI-level overrides for the deploy configuration.
            dry_run: When ``True`` log what would be downloaded without
                     actually downloading anything.
            date_override: Override for the ``{date}`` placeholder in the
                           prefix template (``YYYY-MM-DD``).

        Returns:
            :class:`~bsp.gatherer.GatherResult` with the local paths of every
            downloaded artifact.
        """
        deploy_cfg = self._resolve_deploy_config(resolved, preset=preset, deploy_overrides=deploy_overrides)

        # Determine container/bucket name
        container_or_bucket = deploy_cfg.container or deploy_cfg.bucket
        if not container_or_bucket and not dry_run:
            logging.error(
                "No storage container/bucket configured for gather. "
                "Set 'deploy.container' in the registry or pass --container/--bucket."
            )
            sys.exit(1)

        # Build provider-specific kwargs
        provider = deploy_cfg.provider
        if provider == "azure":
            backend_kwargs: Dict = {
                "container_name": container_or_bucket or "bsp-artifacts",
                "account_url": deploy_cfg.account_url,
                "dry_run": dry_run,
            }
        elif provider == "aws":
            backend_kwargs = {
                "bucket_name": container_or_bucket or "bsp-artifacts",
                "region": deploy_cfg.region,
                "profile": deploy_cfg.profile,
                "dry_run": dry_run,
            }
        else:
            logging.error("Unsupported gather provider: %s", provider)
            sys.exit(1)

        try:
            backend = create_backend(provider, **backend_kwargs)
        except (ImportError, ValueError) as exc:
            logging.error("Failed to initialize storage backend: %s", exc)
            sys.exit(1)

        effective_dest = dest_dir if dest_dir is not None else resolved.build_path

        gatherer = ArtifactGatherer(deploy_cfg, backend)
        result = gatherer.gather(
            dest_dir=effective_dest,
            device=resolved.device.slug,
            release=resolved.release.slug,
            distro=resolved.effective_distro or "",
            vendor=resolved.device.vendor,
            date_override=date_override,
        )

        # Print summary
        action = "[dry-run] Would download" if dry_run else "Downloaded"
        if result.artifacts:
            print(f"\n{action} {result.total_count} artifact(s) → {effective_dest}:")
            for local_path in result.artifacts:
                print(f"  {local_path.name}")
        else:
            print(f"No artifacts found to gather from '{provider}' storage.")

        return result

    def gather_bsp(
        self,
        bsp_name: str,
        dest_dir: Optional[str] = None,
        deploy_overrides: Optional[Dict] = None,
        dry_run: bool = False,
        date_override: Optional[str] = None,
    ) -> GatherResult:
        """
        Download artifacts for a BSP preset from cloud storage.

        Args:
            bsp_name: Name of the BSP preset.
            dest_dir: Local destination directory.  Defaults to the preset's
                      configured build path when ``None``.
            deploy_overrides: CLI-level overrides for the deploy configuration.
            dry_run: When ``True`` log what would be downloaded without
                     actually downloading anything.
            date_override: Override for the ``{date}`` prefix placeholder.

        Returns:
            :class:`~bsp.gatherer.GatherResult` with download metadata.
        """
        logging.info("Gathering artifacts for BSP preset: %s", bsp_name)
        resolved, preset, _, reg_model, reg_resolver, reg_path = self._resolve_preset_multi(bsp_name)
        with self._use_registry_context(reg_model, reg_resolver, reg_path):
            return self._gather_resolved(
                resolved,
                preset=preset,
                dest_dir=dest_dir,
                deploy_overrides=deploy_overrides,
                dry_run=dry_run,
                date_override=date_override,
            )

    def gather_by_components(
        self,
        device_slug: str,
        release_slug: str,
        feature_slugs: Optional[List[str]] = None,
        dest_dir: Optional[str] = None,
        deploy_overrides: Optional[Dict] = None,
        dry_run: bool = False,
        date_override: Optional[str] = None,
    ) -> GatherResult:
        """
        Download artifacts by specifying device, release, and features directly.

        Args:
            device_slug: Device slug.
            release_slug: Release slug.
            feature_slugs: Optional list of feature slugs.
            dest_dir: Local destination directory.  Defaults to the resolved
                      build path when ``None``.
            deploy_overrides: CLI-level overrides for the deploy configuration.
            dry_run: When ``True`` log what would be downloaded without
                     actually downloading anything.
            date_override: Override for the ``{date}`` prefix placeholder.

        Returns:
            :class:`~bsp.gatherer.GatherResult` with download metadata.
        """
        logging.info(
            "Gathering artifacts for device=%s release=%s features=%s",
            device_slug, release_slug, feature_slugs or [],
        )
        resolved = self.resolver.resolve(device_slug, release_slug, feature_slugs)
        return self._gather_resolved(
            resolved,
            dest_dir=dest_dir,
            deploy_overrides=deploy_overrides,
            dry_run=dry_run,
            date_override=date_override,
        )

    # ------------------------------------------------------------------
    # Test (HIL via LAVA + Robot Framework)
    # ------------------------------------------------------------------

    def _test_resolved(
        self,
        resolved: ResolvedConfig,
        testing_config=None,
        lava_server: Optional[str] = None,
        lava_token: Optional[str] = None,
        artifact_url: Optional[str] = None,
        wait: bool = False,
        label: str = "",
    ) -> bool:
        """
        Submit a LAVA HIL test job for the given ResolvedConfig.

        Resolves LAVA settings from (in priority order):
        1. CLI overrides (*lava_server*, *lava_token*, *artifact_url*)
        2. Per-preset ``testing.lava`` block
        3. Registry-level ``lava:`` block

        Args:
            resolved: Resolved build configuration (provides device, release,
                      build path, and features).
            testing_config: Optional :class:`~bsp.models.TestingConfig` from
                            the BSP preset.  When ``None`` the caller must
                            supply *lava_server* and a device_type via
                            command-line flags.
            lava_server: LAVA server URL override (CLI ``--lava-server``).
            lava_token: LAVA authentication token override (CLI ``--lava-token``).
            artifact_url: Artifact base URL override (CLI ``--artifact-url``).
            wait: If ``True``, block until the job finishes and print results.
            label: Descriptive label for log messages.

        Returns:
            ``True`` when the test run passed (or when *wait* is ``False``),
            ``False`` on test failure.
        """
        from .lava_client import LavaClient
        from .lava_job_builder import build_lava_job

        # Gather server-level defaults from the registry
        registry_lava = getattr(self.model, "lava", None) if self.model else None

        def _registry_lava_str(attr: str) -> str:
            """Return the expanded string value of a registry LAVA field, or ''."""
            return _expand_env(getattr(registry_lava, attr) if registry_lava else "")

        # Resolve LAVA connection settings (CLI > preset > registry)
        lava_cfg = testing_config.lava if (testing_config and testing_config.lava) else None

        server = lava_server or _registry_lava_str("server")
        token = lava_token or _registry_lava_str("token")
        username = _registry_lava_str("username")
        wait_timeout = registry_lava.wait_timeout if registry_lava else 3600
        poll_interval = registry_lava.poll_interval if registry_lava else 30

        device_type = lava_cfg.device_type if lava_cfg else ""
        job_template_path = None
        if lava_cfg and lava_cfg.job_template:
            tpl = Path(lava_cfg.job_template)
            if not tpl.is_absolute():
                tpl = (self.config_path.parent / tpl).resolve()
            job_template_path = str(tpl)

        # Resolve artifact_server_url: registry default → preset override
        effective_artifact_server_url = _expand_env(
            (lava_cfg.artifact_server_url if lava_cfg else "")
            or _registry_lava_str("artifact_server_url")
        )
        # Resolve artifact_name from the preset (no registry-level equivalent)
        effective_artifact_name = _expand_env(lava_cfg.artifact_name if lava_cfg else "")

        # Resolve artifact_url: CLI flag > preset artifact_url
        # artifact_url is the "full URL" escape hatch and wins over the
        # artifact_server_url + artifact_name composition.
        effective_artifact_url = _expand_env(
            artifact_url
            or (lava_cfg.artifact_url if lava_cfg else "")
        )

        lava_tags = lava_cfg.tags if lava_cfg else []

        # Resolve LAVA job context (arch / machine).
        # Priority for arch:    preset testing.lava.context.arch > device.architecture > ""
        # Priority for machine: preset testing.lava.context.machine > device.slug
        # The context dict is None only when both arch and machine end up empty.
        device_arch_fallback = getattr(resolved.device, "architecture", None) or ""
        device_machine_fallback = resolved.device.slug

        if lava_cfg and lava_cfg.context:
            effective_arch = lava_cfg.context.arch or device_arch_fallback
            effective_machine = lava_cfg.context.machine or device_machine_fallback
        else:
            effective_arch = device_arch_fallback
            effective_machine = device_machine_fallback

        if effective_arch or effective_machine:
            effective_lava_context: Optional[dict] = {
                "device_arch": effective_arch,
                "device_machine": effective_machine,
            }
        else:
            effective_lava_context = None

        robot_suites: List[str] = []
        robot_variables: dict = {}
        if lava_cfg and lava_cfg.robot:
            robot_suites = list(lava_cfg.robot.suites)
            robot_variables = {k: _expand_env(v) for k, v in lava_cfg.robot.variables.items()}

        if not server:
            logging.error(
                "LAVA server URL is not configured. "
                "Set it via --lava-server, the registry 'lava.server' field, "
                "or $ENV{LAVA_SERVER}."
            )
            return False

        if not device_type:
            logging.error(
                "LAVA device_type is not configured for this preset. "
                "Add a 'testing.lava.device_type' block to the preset in the registry."
            )
            return False

        logging.info("Building LAVA job definition for %s...", label or resolved.device.slug)
        job_yaml = build_lava_job(
            resolved=resolved,
            device_type=device_type,
            artifact_url=effective_artifact_url,
            artifact_server_url=effective_artifact_server_url,
            artifact_name=effective_artifact_name,
            lava_context=effective_lava_context,
            job_template_path=job_template_path,
            lava_tags=lava_tags,
            robot_suites=robot_suites,
            robot_variables=robot_variables,
            wait_timeout=wait_timeout,
        )
        logging.debug("LAVA job YAML:\n%s", job_yaml)

        client = LavaClient(
            server=server,
            token=token,
            username=username,
        )

        job_id = client.submit_job(job_yaml)
        job_url = client.job_url(job_id)
        print(f"LAVA Job ID: {job_id}")
        print(f"Job URL:     {job_url}")

        if not wait:
            print(
                "Job submitted. Re-run with --wait to block until the job completes."
            )
            return True

        try:
            health = client.wait_for_job(
                job_id, timeout=wait_timeout, poll_interval=poll_interval
            )
        except TimeoutError as exc:
            logging.error(str(exc))
            return False

        suites = client.get_job_results(job_id)
        overall_pass = health == "Complete" and all(s.passed for s in suites)

        # Print results table
        print(f"\nLAVA Job {job_id} — Health: {health}")
        if suites:
            print("\nTest Results:")
            for suite in suites:
                status_icon = "✓" if suite.passed else "✗"
                print(
                    f"  {status_icon} Suite: {suite.name:<30} "
                    f"{'PASS' if suite.passed else 'FAIL'}  "
                    f"({suite.total - suite.failures}/{suite.total} passed)"
                )
        else:
            print("  (no test result data returned by LAVA)")

        if not overall_pass:
            logging.error("HIL test run FAILED (job: %d).", job_id)
        else:
            logging.info("HIL test run PASSED (job: %d).", job_id)

        return overall_pass

    def test_bsp(
        self,
        bsp_name: str,
        lava_server: Optional[str] = None,
        lava_token: Optional[str] = None,
        artifact_url: Optional[str] = None,
        wait: bool = False,
    ) -> bool:
        """
        Submit a LAVA HIL test job for a BSP preset.

        Args:
            bsp_name: Name of the BSP preset to test.
            lava_server: LAVA server URL override (CLI ``--lava-server``).
            lava_token: LAVA authentication token override (CLI
                        ``--lava-token``).
            artifact_url: Artifact base URL override (CLI ``--artifact-url``).
            wait: If ``True``, block until the LAVA job completes.

        Returns:
            ``True`` on success (or when *wait* is ``False``), ``False`` on
            test failure.

        Raises:
            SystemExit: If the preset is not found.
        """
        logging.info("Submitting HIL test for BSP preset: %s", bsp_name)
        resolved, preset, _, reg_model, reg_resolver, reg_path = self._resolve_preset_multi(bsp_name)
        testing_config = getattr(preset, "testing", None)
        with self._use_registry_context(reg_model, reg_resolver, reg_path):
            return self._test_resolved(
                resolved,
                testing_config=testing_config,
                lava_server=lava_server,
                lava_token=lava_token,
                artifact_url=artifact_url,
                wait=wait,
                label=f"{preset.name} - {preset.description}",
            )

    def test_by_components(
        self,
        device_slug: str,
        release_slug: str,
        feature_slugs: Optional[List[str]] = None,
        lava_server: Optional[str] = None,
        lava_token: Optional[str] = None,
        artifact_url: Optional[str] = None,
        wait: bool = False,
    ) -> bool:
        """
        Submit a LAVA HIL test job by specifying device, release, and features.

        When using component-based invocation (no preset), only the CLI flags
        and the registry-level ``lava:`` block are used for LAVA settings.

        Args:
            device_slug: Device slug.
            release_slug: Release slug.
            feature_slugs: Optional list of feature slugs.
            lava_server: LAVA server URL (required when no registry-level
                         ``lava.server`` is set).
            lava_token: LAVA authentication token.
            artifact_url: Base URL for image artifacts.
            wait: Block until job completes when ``True``.

        Returns:
            ``True`` on success (or when *wait* is ``False``), ``False`` on
            test failure.
        """
        logging.info(
            "Submitting HIL test for device=%s release=%s features=%s",
            device_slug,
            release_slug,
            feature_slugs or [],
        )
        resolved = self.resolver.resolve(device_slug, release_slug, feature_slugs)
        return self._test_resolved(
            resolved,
            testing_config=None,
            lava_server=lava_server,
            lava_token=lava_token,
            artifact_url=artifact_url,
            wait=wait,
            label=f"{device_slug}/{release_slug}",
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Cleanup resources and perform any necessary finalization."""
        logging.debug("Cleaning up resources...")
        self._cleanup_temp_kas_file()
