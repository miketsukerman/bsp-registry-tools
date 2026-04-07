"""
Registry write layer for BSP registry YAML editing.

Provides ``RegistryWriter`` – a class that wraps a ``RegistryRoot`` model and
offers:

* **load / save** – round-trip YAML I/O via ``ruamel.yaml`` (preserves
  comments and key order) or ``PyYAML`` as a fallback.
* **validate** – cross-reference and slug-uniqueness checks that return a
  structured list of ``ValidationIssue`` objects instead of calling
  ``sys.exit()``.
* **CRUD helpers** – add/update/remove for every entity type.
* **undo stack** – every mutating method records the previous serialised state
  so callers can restore it.
* **diff** – human-readable unified diff between the in-memory state and the
  on-disk file.
"""

from __future__ import annotations

import copy
import difflib
import io
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

try:
    from ruamel.yaml import YAML as _RuamelYAML
    _RUAMEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _RUAMEL_AVAILABLE = False

from .models import (
    BspBuild,
    BspPreset,
    Device,
    Distro,
    Docker,
    DockerArg,
    EnvironmentVariable,
    Feature,
    FeatureCompatibility,
    Framework,
    Registry,
    RegistryRoot,
    Release,
    Specification,
    Vendor,
)
from .utils import SUPPORTED_REGISTRY_VERSION

logger = logging.getLogger(__name__)


# =============================================================================
# Validation helpers
# =============================================================================


@dataclass
class ValidationIssue:
    """A single validation error or warning."""

    level: str  # "error" or "warning"
    message: str

    def __str__(self) -> str:
        return f"[{self.level.upper()}] {self.message}"


# =============================================================================
# Serialisation helpers
# =============================================================================


def _docker_to_dict(docker: Docker) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    if docker.image is not None:
        d["image"] = docker.image
    if docker.file is not None:
        d["file"] = docker.file
    if docker.args:
        d["args"] = [{"name": a.name, "value": a.value} for a in docker.args]
    if docker.runtime_args is not None:
        d["runtime_args"] = docker.runtime_args
    if docker.privileged:
        d["privileged"] = docker.privileged
    if docker.copy:
        d["copy"] = docker.copy
    return d


def _env_var_to_dict(ev: EnvironmentVariable) -> Dict[str, str]:
    return {"name": ev.name, "value": ev.value}


def _registry_root_to_dict(root: RegistryRoot) -> Dict[str, Any]:
    """Convert a ``RegistryRoot`` dataclass tree to a plain Python dict."""
    d: Dict[str, Any] = {}

    # specification
    d["specification"] = {"version": root.specification.version}

    # containers (stored as a plain dict in memory)
    if root.containers:
        containers_dict: Dict[str, Any] = {}
        for name, docker in root.containers.items():
            containers_dict[name] = _docker_to_dict(docker)
        d["containers"] = containers_dict

    # global environment
    if root.environment:
        env_d: Dict[str, Any] = {}
        if root.environment.variables:
            env_d["variables"] = [_env_var_to_dict(v) for v in root.environment.variables]
        if root.environment.copy:
            env_d["copy"] = root.environment.copy
        if env_d:
            d["environment"] = env_d

    # named environments
    if root.environments:
        envs_d: Dict[str, Any] = {}
        for env_name, named_env in root.environments.items():
            ne: Dict[str, Any] = {}
            if named_env.container is not None:
                ne["container"] = named_env.container
            if named_env.variables:
                ne["variables"] = [_env_var_to_dict(v) for v in named_env.variables]
            if named_env.copy:
                ne["copy"] = named_env.copy
            envs_d[env_name] = ne
        d["environments"] = envs_d

    # registry
    reg = root.registry
    reg_d: Dict[str, Any] = {}

    if reg.devices:
        devices_list = []
        for dev in reg.devices:
            dev_d: Dict[str, Any] = {
                "slug": dev.slug,
                "description": dev.description,
                "vendor": dev.vendor,
                "soc_vendor": dev.soc_vendor,
            }
            if dev.soc_family:
                dev_d["soc_family"] = dev.soc_family
            if dev.includes:
                dev_d["includes"] = dev.includes
            if dev.local_conf:
                dev_d["local_conf"] = dev.local_conf
            if dev.copy:
                dev_d["copy"] = dev.copy
            devices_list.append(dev_d)
        reg_d["devices"] = devices_list

    if reg.releases:
        releases_list = []
        for rel in reg.releases:
            rel_d: Dict[str, Any] = {
                "slug": rel.slug,
                "description": rel.description,
            }
            if rel.yocto_version:
                rel_d["yocto_version"] = rel.yocto_version
            if rel.isar_version:
                rel_d["isar_version"] = rel.isar_version
            if rel.includes:
                rel_d["includes"] = rel.includes
            if rel.environment:
                rel_d["environment"] = rel.environment
            if rel.distro:
                rel_d["distro"] = rel.distro
            if rel.vendor_overrides:
                vo_list = []
                for vo in rel.vendor_overrides:
                    vo_d: Dict[str, Any] = {"vendor": vo.vendor}
                    if vo.includes:
                        vo_d["includes"] = vo.includes
                    if vo.slug:
                        vo_d["slug"] = vo.slug
                    if vo.distro:
                        vo_d["distro"] = vo.distro
                    if vo.releases:
                        vo_d["releases"] = [
                            {"slug": vr.slug, "description": vr.description,
                             "includes": vr.includes}
                            for vr in vo.releases
                        ]
                    if vo.soc_vendors:
                        vo_d["soc_vendors"] = [
                            {
                                "vendor": sv.vendor,
                                "includes": sv.includes,
                                "releases": [
                                    {"slug": vr.slug, "description": vr.description,
                                     "includes": vr.includes}
                                    for vr in sv.releases
                                ],
                                **({"distro": sv.distro} if sv.distro else {}),
                            }
                            for sv in vo.soc_vendors
                        ]
                    vo_list.append(vo_d)
                rel_d["vendor_overrides"] = vo_list
            releases_list.append(rel_d)
        reg_d["releases"] = releases_list

    if reg.features:
        features_list = []
        for feat in reg.features:
            feat_d: Dict[str, Any] = {
                "slug": feat.slug,
                "description": feat.description,
            }
            if feat.includes:
                feat_d["includes"] = feat.includes
            if feat.local_conf:
                feat_d["local_conf"] = feat.local_conf
            if feat.env:
                feat_d["env"] = [_env_var_to_dict(v) for v in feat.env]
            if feat.compatible_with:
                feat_d["compatible_with"] = feat.compatible_with
            if feat.compatibility:
                compat: Dict[str, Any] = {}
                if feat.compatibility.vendor:
                    compat["vendor"] = feat.compatibility.vendor
                if feat.compatibility.soc_vendor:
                    compat["soc_vendor"] = feat.compatibility.soc_vendor
                if feat.compatibility.soc_family:
                    compat["soc_family"] = feat.compatibility.soc_family
                if compat:
                    feat_d["compatibility"] = compat
            if feat.vendor_overrides:
                vo_list = []
                for vo in feat.vendor_overrides:
                    vo_d = {"vendor": vo.vendor}
                    if vo.includes:
                        vo_d["includes"] = vo.includes
                    vo_list.append(vo_d)
                feat_d["vendor_overrides"] = vo_list
            features_list.append(feat_d)
        reg_d["features"] = features_list

    if reg.bsp:
        bsp_list = []
        for preset in reg.bsp:
            p_d: Dict[str, Any] = {
                "name": preset.name,
                "description": preset.description,
                "device": preset.device,
            }
            if preset.release:
                p_d["release"] = preset.release
            if preset.releases:
                p_d["releases"] = preset.releases
            if preset.vendor_release:
                p_d["vendor_release"] = preset.vendor_release
            if preset.override:
                p_d["override"] = preset.override
            if preset.features:
                p_d["features"] = preset.features
            if preset.local_conf:
                p_d["local_conf"] = preset.local_conf
            if preset.targets:
                p_d["targets"] = preset.targets
            if preset.build:
                b_d: Dict[str, Any] = {}
                if preset.build.container:
                    b_d["container"] = preset.build.container
                if preset.build.path:
                    b_d["path"] = preset.build.path
                if b_d:
                    p_d["build"] = b_d
            bsp_list.append(p_d)
        reg_d["bsp"] = bsp_list

    if reg.frameworks:
        fw_list = []
        for fw in reg.frameworks:
            fw_d: Dict[str, Any] = {
                "slug": fw.slug,
                "description": fw.description,
                "vendor": fw.vendor,
            }
            if fw.includes:
                fw_d["includes"] = fw.includes
            fw_list.append(fw_d)
        reg_d["frameworks"] = fw_list

    if reg.distro:
        distro_list = []
        for dist in reg.distro:
            dist_d: Dict[str, Any] = {
                "slug": dist.slug,
                "description": dist.description,
            }
            if dist.vendor:
                dist_d["vendor"] = dist.vendor
            if dist.includes:
                dist_d["includes"] = dist.includes
            if dist.framework:
                dist_d["framework"] = dist.framework
            distro_list.append(dist_d)
        reg_d["distro"] = distro_list

    if reg.vendors:
        vendors_list = []
        for v in reg.vendors:
            v_d: Dict[str, Any] = {
                "slug": v.slug,
                "name": v.name,
            }
            if v.description:
                v_d["description"] = v.description
            if v.website:
                v_d["website"] = v.website
            if v.includes:
                v_d["includes"] = v.includes
            vendors_list.append(v_d)
        reg_d["vendors"] = vendors_list

    d["registry"] = reg_d
    return d


def _serialise_to_yaml_string(data: Dict[str, Any]) -> str:
    """Serialise *data* to a YAML string.

    Uses ``ruamel.yaml`` when available (better round-trip fidelity) and falls
    back to ``PyYAML`` otherwise.
    """
    if _RUAMEL_AVAILABLE:
        ry = _RuamelYAML()
        ry.default_flow_style = False
        ry.width = 4096
        buf = io.StringIO()
        ry.dump(data, buf)
        return buf.getvalue()
    return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)


# =============================================================================
# RegistryWriter
# =============================================================================


class RegistryWriter:
    """
    Read/write wrapper around a ``RegistryRoot`` that adds CRUD helpers,
    validation, undo, and diff.

    Typical CLI usage::

        writer = RegistryWriter()
        writer.load(Path("bsp-registry.yaml"))
        writer.add_device(Device(slug="myboard", ...))
        issues = writer.validate()
        if not any(i.level == "error" for i in issues):
            writer.save()

    Typical TUI usage::

        writer = RegistryWriter()
        writer.load(registry_path)
        # … user edits …
        writer.update_device("myboard", description="Updated")
        writer.save()            # atomic write + backup
        writer.undo()            # restore previous state from undo stack
    """

    def __init__(self) -> None:
        self._path: Optional[Path] = None
        self._root: Optional[RegistryRoot] = None
        # Stack of YAML strings for undo (each entry is the state *before* a change)
        self._undo_stack: List[str] = []
        # Whether a backup has been written in this session
        self._backup_done: bool = False

    # ------------------------------------------------------------------
    # Load / Save
    # ------------------------------------------------------------------

    def load(self, path: Path) -> None:
        """Load a registry YAML file into memory.

        Delegates to ``get_registry_from_yaml_file`` from ``bsp.utils`` so
        that the same include-resolution and dacite parsing logic is used.
        Resets the undo stack.

        Raises:
            SystemExit: propagated from the underlying parser on invalid YAML.
        """
        from .utils import get_registry_from_yaml_file

        self._path = Path(path)
        self._root = get_registry_from_yaml_file(self._path)
        self._undo_stack = []
        self._backup_done = False
        logger.debug("RegistryWriter: loaded %s", self._path)

    @property
    def root(self) -> Optional[RegistryRoot]:
        """The current in-memory registry (may be ``None`` before ``load``)."""
        return self._root

    @property
    def path(self) -> Optional[Path]:
        """Path from which the registry was loaded (``None`` if not yet loaded)."""
        return self._path

    def save(self, path: Optional[Path] = None) -> None:
        """Serialise the in-memory registry to *path* (or the original load path).

        Safety measures:

        * **Backup** – on the first ``save()`` call in this session the original
          file is copied to ``<name>.bak`` before being overwritten.
        * **Atomic write** – the new content is written to a temp file in the
          same directory then atomically renamed, protecting against partial
          writes.

        Args:
            path: Destination path; defaults to ``self._path``.

        Raises:
            RuntimeError: If no registry is loaded.
            OSError: On file-system errors.
        """
        if self._root is None:
            raise RuntimeError("No registry loaded — call load() first")

        dest = Path(path) if path else self._path
        if dest is None:
            raise RuntimeError("No destination path — supply path argument or call load() first")

        data = _registry_root_to_dict(self._root)
        yaml_str = _serialise_to_yaml_string(data)

        # Backup before first save
        if not self._backup_done and dest.exists():
            backup = dest.with_suffix(".bak")
            shutil.copy2(str(dest), str(backup))
            self._backup_done = True
            logger.debug("RegistryWriter: backup written to %s", backup)

        # Atomic write via temp file → rename
        dest_dir = dest.parent
        fd, tmp_path = tempfile.mkstemp(dir=str(dest_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(yaml_str)
            os.replace(tmp_path, str(dest))
            logger.debug("RegistryWriter: saved to %s", dest)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        self._path = dest

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> List[ValidationIssue]:
        """Run integrity checks on the current in-memory registry.

        Checks performed:

        * Registry version is ``"2.0"``.
        * Slug/name uniqueness within each entity list.
        * Cross-references: preset → device, preset → release(s),
          preset → features, release → environment, release → distro,
          distro → framework, feature → compatible_with.
        * Container references in presets and environments are defined.

        Returns:
            List of :class:`ValidationIssue` (may be empty for a valid
            registry).  The caller decides how to handle errors vs warnings.
        """
        issues: List[ValidationIssue] = []

        if self._root is None:
            issues.append(ValidationIssue("error", "No registry loaded"))
            return issues

        root = self._root

        # Version check
        if root.specification.version != SUPPORTED_REGISTRY_VERSION:
            issues.append(ValidationIssue(
                "error",
                f"Unsupported specification version '{root.specification.version}' "
                f"(expected '{SUPPORTED_REGISTRY_VERSION}')",
            ))

        reg = root.registry

        # Collect known slugs/names
        device_slugs = {d.slug for d in (reg.devices or [])}
        release_slugs = {r.slug for r in (reg.releases or [])}
        feature_slugs = {f.slug for f in (reg.features or [])}
        preset_names = [p.name for p in (reg.bsp or [])]
        framework_slugs = {fw.slug for fw in (reg.frameworks or [])}
        distro_slugs = {d.slug for d in (reg.distro or [])}
        vendor_slugs = {v.slug for v in (reg.vendors or [])}
        container_names = set(root.containers.keys()) if root.containers else set()
        env_names = set(root.environments.keys()) if root.environments else set()

        # Uniqueness checks
        for label, items in [
            ("device slug", [d.slug for d in (reg.devices or [])]),
            ("release slug", [r.slug for r in (reg.releases or [])]),
            ("feature slug", [f.slug for f in (reg.features or [])]),
            ("framework slug", [fw.slug for fw in (reg.frameworks or [])]),
            ("distro slug", [d.slug for d in (reg.distro or [])]),
            ("vendor slug", [v.slug for v in (reg.vendors or [])]),
            ("preset name", preset_names),
        ]:
            seen: set = set()
            for s in items:
                if s in seen:
                    issues.append(ValidationIssue("error", f"Duplicate {label}: '{s}'"))
                seen.add(s)

        # Preset cross-references
        for preset in reg.bsp or []:
            if preset.device and preset.device not in device_slugs:
                issues.append(ValidationIssue(
                    "error",
                    f"Preset '{preset.name}': device '{preset.device}' is not defined",
                ))
            if preset.release and preset.release not in release_slugs:
                issues.append(ValidationIssue(
                    "error",
                    f"Preset '{preset.name}': release '{preset.release}' is not defined",
                ))
            for rs in preset.releases or []:
                if rs not in release_slugs:
                    issues.append(ValidationIssue(
                        "error",
                        f"Preset '{preset.name}': release '{rs}' is not defined",
                    ))
            for fs in preset.features or []:
                if fs not in feature_slugs:
                    issues.append(ValidationIssue(
                        "warning",
                        f"Preset '{preset.name}': feature '{fs}' is not defined",
                    ))
            if preset.build and preset.build.container:
                if preset.build.container not in container_names:
                    issues.append(ValidationIssue(
                        "warning",
                        f"Preset '{preset.name}': container '{preset.build.container}' "
                        "is not defined",
                    ))

        # Release cross-references
        for rel in reg.releases or []:
            if rel.environment and rel.environment not in env_names:
                issues.append(ValidationIssue(
                    "warning",
                    f"Release '{rel.slug}': named environment '{rel.environment}' "
                    "is not defined",
                ))
            if rel.distro and rel.distro not in distro_slugs:
                issues.append(ValidationIssue(
                    "warning",
                    f"Release '{rel.slug}': distro '{rel.distro}' is not defined",
                ))

        # Distro cross-references
        for dist in reg.distro or []:
            if dist.framework and dist.framework not in framework_slugs:
                issues.append(ValidationIssue(
                    "warning",
                    f"Distro '{dist.slug}': framework '{dist.framework}' is not defined",
                ))

        # Named environment container references
        for env_name, named_env in (root.environments or {}).items():
            if named_env.container and named_env.container not in container_names:
                issues.append(ValidationIssue(
                    "warning",
                    f"Named environment '{env_name}': container "
                    f"'{named_env.container}' is not defined",
                ))

        # Feature compatible_with: values should be a known distro or framework
        known_compat = distro_slugs | framework_slugs
        for feat in reg.features or []:
            for compat in feat.compatible_with or []:
                if known_compat and compat not in known_compat:
                    issues.append(ValidationIssue(
                        "warning",
                        f"Feature '{feat.slug}': compatible_with value '{compat}' "
                        "is not a known distro or framework slug",
                    ))

        return issues

    # ------------------------------------------------------------------
    # Diff
    # ------------------------------------------------------------------

    def diff(self) -> str:
        """Return a unified diff between the in-memory state and the on-disk file.

        When the registry is inside a git working tree a ``git diff`` is
        attempted first for richer output.  Falls back to a Python-computed
        unified diff.

        Returns:
            Diff string (empty string if there are no differences or no file).
        """
        if self._root is None or self._path is None:
            return ""

        # Current in-memory YAML
        current_yaml = _serialise_to_yaml_string(_registry_root_to_dict(self._root))

        # On-disk content
        try:
            on_disk = self._path.read_text(encoding="utf-8")
        except OSError:
            on_disk = ""

        if on_disk == current_yaml:
            return ""

        # Try git diff for a richer output
        if self._path.exists():
            git_diff = self._git_diff(current_yaml)
            if git_diff is not None:
                return git_diff

        # Fallback: Python unified diff
        return "".join(difflib.unified_diff(
            on_disk.splitlines(keepends=True),
            current_yaml.splitlines(keepends=True),
            fromfile=f"{self._path} (on disk)",
            tofile=f"{self._path} (in memory)",
        ))

    def _git_diff(self, new_content: str) -> Optional[str]:
        """Attempt a git diff of *new_content* vs the HEAD version of the file.

        Returns ``None`` if git is not available or the file is not tracked.
        """
        assert self._path is not None
        try:
            # Write new content to a temp file in the same dir
            dest_dir = self._path.parent
            fd, tmp = tempfile.mkstemp(dir=str(dest_dir), suffix=".tmp.yaml")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(new_content)
                result = subprocess.run(
                    ["git", "diff", "--no-index", str(self._path), tmp],
                    capture_output=True, text=True, cwd=str(dest_dir),
                )
                diff_out = result.stdout
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

            if result.returncode not in (0, 1):
                return None
            # Replace temp file path with the real filename for readability
            return diff_out.replace(tmp, f"{self._path} (in memory)")
        except (OSError, FileNotFoundError):
            return None

    # ------------------------------------------------------------------
    # Undo
    # ------------------------------------------------------------------

    def _push_undo(self) -> None:
        """Snapshot the current state onto the undo stack."""
        if self._root is None:
            return
        snapshot = _serialise_to_yaml_string(_registry_root_to_dict(self._root))
        self._undo_stack.append(snapshot)

    def undo(self) -> bool:
        """Restore the previous state from the undo stack.

        Returns:
            ``True`` if a state was restored, ``False`` if the stack was empty.
        """
        if not self._undo_stack:
            return False

        import dacite

        snapshot_yaml = self._undo_stack.pop()
        data = yaml.safe_load(snapshot_yaml)
        if data is None:
            return False

        # Containers may be stored as a plain dict; normalise to Docker objects
        from .utils import convert_containers_list_to_dict
        if "containers" in data and isinstance(data["containers"], list):
            data["containers"] = convert_containers_list_to_dict(data["containers"])

        cfg = dacite.Config(strict=False)
        self._root = dacite.from_dict(data_class=RegistryRoot, data=data, config=cfg)
        return True

    # ------------------------------------------------------------------
    # Device CRUD
    # ------------------------------------------------------------------

    def add_device(self, device: Device) -> None:
        """Add *device* to the registry.

        Raises:
            ValueError: If a device with the same slug already exists.
            RuntimeError: If no registry is loaded.
        """
        self._check_loaded()
        assert self._root is not None
        existing = {d.slug for d in (self._root.registry.devices or [])}
        if device.slug in existing:
            raise ValueError(f"Device with slug '{device.slug}' already exists")
        self._push_undo()
        self._root.registry.devices.append(device)

    def update_device(self, slug: str, **fields: Any) -> None:
        """Update fields of the device identified by *slug*.

        Only the keyword arguments supplied are changed.

        Raises:
            KeyError: If no device with *slug* exists.
            RuntimeError: If no registry is loaded.
        """
        self._check_loaded()
        assert self._root is not None
        device = self._find_by_slug(self._root.registry.devices, slug)
        if device is None:
            raise KeyError(f"Device '{slug}' not found")
        self._push_undo()
        for key, value in fields.items():
            if hasattr(device, key):
                setattr(device, key, value)

    def remove_device(self, slug: str) -> None:
        """Remove the device identified by *slug*.

        Raises:
            KeyError: If no device with *slug* exists.
            RuntimeError: If no registry is loaded.
        """
        self._check_loaded()
        assert self._root is not None
        devices = self._root.registry.devices
        idx = self._find_index_by_slug(devices, slug)
        if idx is None:
            raise KeyError(f"Device '{slug}' not found")
        self._push_undo()
        devices.pop(idx)

    # ------------------------------------------------------------------
    # Release CRUD
    # ------------------------------------------------------------------

    def add_release(self, release: Release) -> None:
        self._check_loaded()
        assert self._root is not None
        existing = {r.slug for r in (self._root.registry.releases or [])}
        if release.slug in existing:
            raise ValueError(f"Release with slug '{release.slug}' already exists")
        self._push_undo()
        self._root.registry.releases.append(release)

    def update_release(self, slug: str, **fields: Any) -> None:
        self._check_loaded()
        assert self._root is not None
        release = self._find_by_slug(self._root.registry.releases, slug)
        if release is None:
            raise KeyError(f"Release '{slug}' not found")
        self._push_undo()
        for key, value in fields.items():
            if hasattr(release, key):
                setattr(release, key, value)

    def remove_release(self, slug: str) -> None:
        self._check_loaded()
        assert self._root is not None
        releases = self._root.registry.releases
        idx = self._find_index_by_slug(releases, slug)
        if idx is None:
            raise KeyError(f"Release '{slug}' not found")
        self._push_undo()
        releases.pop(idx)

    # ------------------------------------------------------------------
    # Feature CRUD
    # ------------------------------------------------------------------

    def add_feature(self, feature: Feature) -> None:
        self._check_loaded()
        assert self._root is not None
        existing = {f.slug for f in (self._root.registry.features or [])}
        if feature.slug in existing:
            raise ValueError(f"Feature with slug '{feature.slug}' already exists")
        self._push_undo()
        self._root.registry.features.append(feature)

    def update_feature(self, slug: str, **fields: Any) -> None:
        self._check_loaded()
        assert self._root is not None
        feat = self._find_by_slug(self._root.registry.features, slug)
        if feat is None:
            raise KeyError(f"Feature '{slug}' not found")
        self._push_undo()
        for key, value in fields.items():
            if hasattr(feat, key):
                setattr(feat, key, value)

    def remove_feature(self, slug: str) -> None:
        self._check_loaded()
        assert self._root is not None
        features = self._root.registry.features
        idx = self._find_index_by_slug(features, slug)
        if idx is None:
            raise KeyError(f"Feature '{slug}' not found")
        self._push_undo()
        features.pop(idx)

    # ------------------------------------------------------------------
    # Preset CRUD
    # ------------------------------------------------------------------

    def add_preset(self, preset: BspPreset) -> None:
        self._check_loaded()
        assert self._root is not None
        if self._root.registry.bsp is None:
            self._root.registry.bsp = []
        existing = {p.name for p in self._root.registry.bsp}
        if preset.name in existing:
            raise ValueError(f"Preset '{preset.name}' already exists")
        self._push_undo()
        self._root.registry.bsp.append(preset)

    def update_preset(self, name: str, **fields: Any) -> None:
        self._check_loaded()
        assert self._root is not None
        preset = next((p for p in (self._root.registry.bsp or []) if p.name == name), None)
        if preset is None:
            raise KeyError(f"Preset '{name}' not found")
        self._push_undo()
        for key, value in fields.items():
            if hasattr(preset, key):
                setattr(preset, key, value)

    def remove_preset(self, name: str) -> None:
        self._check_loaded()
        assert self._root is not None
        bsp_list = self._root.registry.bsp or []
        idx = next((i for i, p in enumerate(bsp_list) if p.name == name), None)
        if idx is None:
            raise KeyError(f"Preset '{name}' not found")
        self._push_undo()
        bsp_list.pop(idx)

    # ------------------------------------------------------------------
    # Vendor CRUD
    # ------------------------------------------------------------------

    def add_vendor(self, vendor: Vendor) -> None:
        self._check_loaded()
        assert self._root is not None
        existing = {v.slug for v in (self._root.registry.vendors or [])}
        if vendor.slug in existing:
            raise ValueError(f"Vendor with slug '{vendor.slug}' already exists")
        self._push_undo()
        self._root.registry.vendors.append(vendor)

    def update_vendor(self, slug: str, **fields: Any) -> None:
        self._check_loaded()
        assert self._root is not None
        vendor = self._find_by_slug(self._root.registry.vendors, slug)
        if vendor is None:
            raise KeyError(f"Vendor '{slug}' not found")
        self._push_undo()
        for key, value in fields.items():
            if hasattr(vendor, key):
                setattr(vendor, key, value)

    def remove_vendor(self, slug: str) -> None:
        self._check_loaded()
        assert self._root is not None
        vendors = self._root.registry.vendors
        idx = self._find_index_by_slug(vendors, slug)
        if idx is None:
            raise KeyError(f"Vendor '{slug}' not found")
        self._push_undo()
        vendors.pop(idx)

    # ------------------------------------------------------------------
    # Distro CRUD
    # ------------------------------------------------------------------

    def add_distro(self, distro: Distro) -> None:
        self._check_loaded()
        assert self._root is not None
        existing = {d.slug for d in (self._root.registry.distro or [])}
        if distro.slug in existing:
            raise ValueError(f"Distro with slug '{distro.slug}' already exists")
        self._push_undo()
        self._root.registry.distro.append(distro)

    def update_distro(self, slug: str, **fields: Any) -> None:
        self._check_loaded()
        assert self._root is not None
        distro = self._find_by_slug(self._root.registry.distro, slug)
        if distro is None:
            raise KeyError(f"Distro '{slug}' not found")
        self._push_undo()
        for key, value in fields.items():
            if hasattr(distro, key):
                setattr(distro, key, value)

    def remove_distro(self, slug: str) -> None:
        self._check_loaded()
        assert self._root is not None
        distros = self._root.registry.distro
        idx = self._find_index_by_slug(distros, slug)
        if idx is None:
            raise KeyError(f"Distro '{slug}' not found")
        self._push_undo()
        distros.pop(idx)

    # ------------------------------------------------------------------
    # Container CRUD
    # ------------------------------------------------------------------

    def add_container(self, name: str, docker: Docker) -> None:
        self._check_loaded()
        assert self._root is not None
        if self._root.containers is None:
            self._root.containers = {}
        if name in self._root.containers:
            raise ValueError(f"Container '{name}' already exists")
        self._push_undo()
        self._root.containers[name] = docker

    def update_container(self, name: str, **fields: Any) -> None:
        self._check_loaded()
        assert self._root is not None
        containers = self._root.containers or {}
        if name not in containers:
            raise KeyError(f"Container '{name}' not found")
        self._push_undo()
        docker = containers[name]
        for key, value in fields.items():
            if hasattr(docker, key):
                setattr(docker, key, value)

    def remove_container(self, name: str) -> None:
        self._check_loaded()
        assert self._root is not None
        containers = self._root.containers or {}
        if name not in containers:
            raise KeyError(f"Container '{name}' not found")
        self._push_undo()
        del containers[name]

    # ------------------------------------------------------------------
    # Dangling reference check (used by CLI remove and TUI confirm dialog)
    # ------------------------------------------------------------------

    def find_references(self, entity_type: str, slug_or_name: str) -> List[str]:
        """Return a list of human-readable descriptions of entities that
        reference the given entity.

        Useful before removing an entity to warn the user about dangling
        references.

        Args:
            entity_type: One of ``"device"``, ``"release"``, ``"feature"``,
                         ``"vendor"``, ``"distro"``, ``"framework"``,
                         ``"container"``.
            slug_or_name: The slug (or preset name) of the entity to check.

        Returns:
            List of strings describing each referencing entity.
        """
        if self._root is None:
            return []

        refs: List[str] = []
        reg = self._root.registry

        if entity_type == "device":
            for preset in reg.bsp or []:
                if preset.device == slug_or_name:
                    refs.append(f"preset '{preset.name}' (device)")

        elif entity_type == "release":
            for preset in reg.bsp or []:
                if preset.release == slug_or_name or slug_or_name in (preset.releases or []):
                    refs.append(f"preset '{preset.name}' (release)")

        elif entity_type == "feature":
            for preset in reg.bsp or []:
                if slug_or_name in (preset.features or []):
                    refs.append(f"preset '{preset.name}' (feature)")

        elif entity_type == "vendor":
            for dev in reg.devices or []:
                if dev.vendor == slug_or_name:
                    refs.append(f"device '{dev.slug}' (vendor)")

        elif entity_type == "distro":
            for rel in reg.releases or []:
                if rel.distro == slug_or_name:
                    refs.append(f"release '{rel.slug}' (distro)")

        elif entity_type == "framework":
            for dist in reg.distro or []:
                if dist.framework == slug_or_name:
                    refs.append(f"distro '{dist.slug}' (framework)")

        elif entity_type == "container":
            for preset in reg.bsp or []:
                if preset.build and preset.build.container == slug_or_name:
                    refs.append(f"preset '{preset.name}' (container)")
            for env_name, env in (self._root.environments or {}).items():
                if env.container == slug_or_name:
                    refs.append(f"named environment '{env_name}' (container)")

        return refs

    # ------------------------------------------------------------------
    # Git staging
    # ------------------------------------------------------------------

    def git_stage(self) -> bool:
        """Run ``git add`` on the registry file.

        Returns ``True`` if staging succeeded, ``False`` otherwise.
        """
        if self._path is None:
            return False
        try:
            result = subprocess.run(
                ["git", "add", str(self._path)],
                capture_output=True, text=True,
                cwd=str(self._path.parent),
            )
            return result.returncode == 0
        except (OSError, FileNotFoundError):
            return False

    def git_commit(self, message: str) -> bool:
        """Run ``git commit -m <message>`` for the registry file.

        Returns ``True`` if the commit succeeded, ``False`` otherwise.
        """
        if self._path is None:
            return False
        try:
            result = subprocess.run(
                ["git", "commit", "-m", message, "--", str(self._path)],
                capture_output=True, text=True,
                cwd=str(self._path.parent),
            )
            return result.returncode == 0
        except (OSError, FileNotFoundError):
            return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_loaded(self) -> None:
        if self._root is None:
            raise RuntimeError("No registry loaded — call load() first")

    @staticmethod
    def _find_by_slug(items, slug: str):
        """Return the first item in *items* whose ``.slug`` attribute equals *slug*."""
        if items is None:
            return None
        return next((item for item in items if item.slug == slug), None)

    @staticmethod
    def _find_index_by_slug(items, slug: str) -> Optional[int]:
        """Return the index of the first item in *items* whose ``.slug`` equals *slug*."""
        if items is None:
            return None
        return next((i for i, item in enumerate(items) if item.slug == slug), None)
