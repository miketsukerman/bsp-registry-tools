"""
Registry writer: CRUD operations, validation, diff, and git helpers for BSP registry files.

All mutations operate on the raw YAML dict so that round-trip serialisation
preserves comments and field order as much as possible.  The caller is
responsible for calling :meth:`RegistryWriter.save` after any mutations.
"""

import copy
import difflib
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

# =============================================================================
# Public types
# =============================================================================

SUPPORTED_REGISTRY_VERSION = "2.0"

# Mapping from entity-type name to the YAML path inside the document.
# Container entries live under the top-level ``containers`` dict; everything
# else lives under ``registry.<list_key>``.
_ENTITY_LIST_KEYS: Dict[str, str] = {
    "device": "devices",
    "release": "releases",
    "feature": "features",
    "preset": "bsp",
    "vendor": "vendors",
    "distro": "distro",
    "framework": "frameworks",
}

# The YAML key used to identify an entity within its list (slug vs name).
_ENTITY_ID_KEY: Dict[str, str] = {
    "device": "slug",
    "release": "slug",
    "feature": "slug",
    "preset": "name",
    "vendor": "slug",
    "distro": "slug",
    "framework": "slug",
}


@dataclass
class ValidationIssue:
    """A single registry validation problem."""
    severity: str  # "error" or "warning"
    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.path}: {self.message}"


# =============================================================================
# RegistryWriter
# =============================================================================

class RegistryWriter:
    """
    Stateful read/write interface for a BSP registry YAML file.

    Usage::

        writer = RegistryWriter()
        writer.load(Path("bsp-registry.yaml"))
        writer.add_device(slug="my-board", description="My Board",
                          vendor="acme", soc_vendor="nxp")
        writer.save()

    All mutating methods call :meth:`_push_undo` before modifying ``_data``
    so that :meth:`undo` can roll back the last change.
    """

    def __init__(self) -> None:
        self._path: Optional[Path] = None
        self._data: Dict[str, Any] = {}
        self._original: Dict[str, Any] = {}
        self._history: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def load(self, path: Path) -> None:
        """Load a registry YAML file into memory.

        Args:
            path: Path to the YAML registry file.

        Raises:
            FileNotFoundError: If the path does not exist.
            yaml.YAMLError: If the file is not valid YAML.
        """
        path = Path(path)
        with open(path, "r", encoding="utf-8") as fh:
            self._data = yaml.safe_load(fh) or {}
        self._path = path
        self._original = copy.deepcopy(self._data)
        self._history = []

    def save(self, path: Optional[Path] = None) -> None:
        """Atomically save the current state to disk.

        Before writing, the existing file is copied to ``<path>.bak``.
        The new content is first written to ``<path>.tmp`` and then renamed
        so that the operation is as atomic as the OS allows.

        Args:
            path: Destination path.  Defaults to the path passed to
                  :meth:`load`.

        Raises:
            RuntimeError: If no path has been set.
        """
        dest = Path(path) if path is not None else self._path
        if dest is None:
            raise RuntimeError("No path set; call load() or pass a path to save().")

        dest = dest.resolve()
        tmp_path = dest.with_suffix(dest.suffix + ".tmp")
        bak_path = dest.with_suffix(dest.suffix + ".bak")

        # Backup existing file
        if dest.exists():
            try:
                import shutil
                shutil.copy2(dest, bak_path)
            except OSError as exc:
                logging.warning("Could not create backup %s: %s", bak_path, exc)

        # Write to .tmp then atomically rename
        with open(tmp_path, "w", encoding="utf-8") as fh:
            yaml.dump(self._data, fh, default_flow_style=False, allow_unicode=True,
                      sort_keys=False)
        os.replace(tmp_path, dest)

        if path is not None:
            self._path = dest
        logging.debug("Registry saved to %s", dest)

    # ------------------------------------------------------------------
    # Undo support
    # ------------------------------------------------------------------

    def _push_undo(self) -> None:
        """Push a deep copy of the current state onto the undo stack."""
        self._history.append(copy.deepcopy(self._data))

    def undo(self) -> None:
        """Roll back the last mutation and save to disk.

        Raises:
            RuntimeError: If there is nothing to undo or no path is set.
        """
        if not self._history:
            raise RuntimeError("Nothing to undo.")
        self._data = self._history.pop()
        self.save()

    # ------------------------------------------------------------------
    # Scaffolding
    # ------------------------------------------------------------------

    @staticmethod
    def init_registry(path: Path, spec_version: str = SUPPORTED_REGISTRY_VERSION,
                      force: bool = False) -> None:
        """Create a minimal empty registry file at *path*.

        Args:
            path: Destination file path.
            spec_version: Registry specification version string.
            force: If True, overwrite an existing file.

        Raises:
            FileExistsError: If the file already exists and *force* is False.
        """
        path = Path(path)
        if path.exists() and not force:
            raise FileExistsError(
                f"Registry file already exists: {path}. "
                "Use force=True / --force to overwrite."
            )
        skeleton: Dict[str, Any] = {
            "specification": {"version": spec_version},
            "containers": {},
            "registry": {
                "devices": [],
                "releases": [],
                "features": [],
                "bsp": [],
                "vendors": [],
                "distro": [],
                "frameworks": [],
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            yaml.dump(skeleton, fh, default_flow_style=False, allow_unicode=True,
                      sort_keys=False)
        logging.info("Initialised empty registry at %s", path)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> List[ValidationIssue]:
        """Validate the loaded registry and return a list of issues.

        Checks performed:
        - Specification version equals ``"2.0"``
        - Required fields present for each entity type
        - Slug uniqueness within each entity list
        - Cross-reference integrity (device.vendor → vendors,
          preset.device → devices, preset.release → releases,
          preset.features → features)

        Returns:
            List of :class:`ValidationIssue` objects (may be empty).
        """
        issues: List[ValidationIssue] = []

        # Version check
        spec = self._data.get("specification") or {}
        version = spec.get("version", "")
        if str(version) != SUPPORTED_REGISTRY_VERSION:
            issues.append(ValidationIssue(
                severity="error",
                path="specification.version",
                message=(
                    f"Unsupported version '{version}'. "
                    f"Expected '{SUPPORTED_REGISTRY_VERSION}'."
                ),
            ))

        registry = self._data.get("registry") or {}

        # Collect slugs / names for cross-reference checks
        device_slugs = {d.get("slug") for d in (registry.get("devices") or []) if d.get("slug")}
        release_slugs = {r.get("slug") for r in (registry.get("releases") or []) if r.get("slug")}
        feature_slugs = {f.get("slug") for f in (registry.get("features") or []) if f.get("slug")}
        vendor_slugs = {v.get("slug") for v in (registry.get("vendors") or []) if v.get("slug")}

        # --- devices ---
        self._validate_entity_list(
            issues,
            entities=registry.get("devices") or [],
            entity_type="device",
            required_fields=["slug", "description", "vendor", "soc_vendor"],
            path_prefix="registry.devices",
        )

        # --- releases ---
        self._validate_entity_list(
            issues,
            entities=registry.get("releases") or [],
            entity_type="release",
            required_fields=["slug", "description"],
            path_prefix="registry.releases",
        )

        # --- features ---
        self._validate_entity_list(
            issues,
            entities=registry.get("features") or [],
            entity_type="feature",
            required_fields=["slug", "description"],
            path_prefix="registry.features",
        )

        # --- vendors ---
        self._validate_entity_list(
            issues,
            entities=registry.get("vendors") or [],
            entity_type="vendor",
            required_fields=["slug", "name"],
            path_prefix="registry.vendors",
        )

        # --- distros ---
        self._validate_entity_list(
            issues,
            entities=registry.get("distro") or [],
            entity_type="distro",
            required_fields=["slug", "description"],
            path_prefix="registry.distro",
        )

        # --- frameworks ---
        self._validate_entity_list(
            issues,
            entities=registry.get("frameworks") or [],
            entity_type="framework",
            required_fields=["slug", "description"],
            path_prefix="registry.frameworks",
        )

        # --- presets ---
        for i, preset in enumerate(registry.get("bsp") or []):
            path_prefix = f"registry.bsp[{i}]"
            for field in ["name", "description", "device"]:
                if not preset.get(field):
                    issues.append(ValidationIssue(
                        severity="error",
                        path=f"{path_prefix}.{field}",
                        message=f"Required field '{field}' is missing or empty.",
                    ))
            # Must have release or releases
            if not preset.get("release") and not preset.get("releases"):
                issues.append(ValidationIssue(
                    severity="error",
                    path=f"{path_prefix}.release",
                    message="Preset must specify 'release' or 'releases'.",
                ))
            # Cross-reference: device
            dev = preset.get("device")
            if dev and dev not in device_slugs:
                issues.append(ValidationIssue(
                    severity="error",
                    path=f"{path_prefix}.device",
                    message=f"Device slug '{dev}' not found in registry.devices.",
                ))
            # Cross-reference: release
            rel = preset.get("release")
            if rel and rel not in release_slugs:
                issues.append(ValidationIssue(
                    severity="error",
                    path=f"{path_prefix}.release",
                    message=f"Release slug '{rel}' not found in registry.releases.",
                ))
            # Cross-reference: releases list
            for rel_slug in (preset.get("releases") or []):
                if rel_slug not in release_slugs:
                    issues.append(ValidationIssue(
                        severity="error",
                        path=f"{path_prefix}.releases",
                        message=f"Release slug '{rel_slug}' not found in registry.releases.",
                    ))
            # Cross-reference: features
            for feat_slug in (preset.get("features") or []):
                if feat_slug not in feature_slugs:
                    issues.append(ValidationIssue(
                        severity="error",
                        path=f"{path_prefix}.features",
                        message=f"Feature slug '{feat_slug}' not found in registry.features.",
                    ))

        # Cross-reference: device.vendor → vendors (warning only — vendor may be implicit)
        for i, device in enumerate(registry.get("devices") or []):
            dev_vendor = device.get("vendor")
            if dev_vendor and vendor_slugs and dev_vendor not in vendor_slugs:
                issues.append(ValidationIssue(
                    severity="warning",
                    path=f"registry.devices[{i}].vendor",
                    message=(
                        f"Device vendor '{dev_vendor}' has no matching entry in "
                        "registry.vendors (this is OK if vendors list is intentionally empty)."
                    ),
                ))

        return issues

    def _validate_entity_list(
        self,
        issues: List[ValidationIssue],
        entities: List[Dict],
        entity_type: str,
        required_fields: List[str],
        path_prefix: str,
    ) -> None:
        """Check required fields and slug uniqueness for a flat entity list."""
        seen_ids: Dict[str, int] = {}
        id_key = _ENTITY_ID_KEY.get(entity_type, "slug")
        for i, entity in enumerate(entities):
            for field in required_fields:
                if not entity.get(field):
                    issues.append(ValidationIssue(
                        severity="error",
                        path=f"{path_prefix}[{i}].{field}",
                        message=f"Required field '{field}' is missing or empty.",
                    ))
            entity_id = entity.get(id_key)
            if entity_id:
                if entity_id in seen_ids:
                    issues.append(ValidationIssue(
                        severity="error",
                        path=f"{path_prefix}[{i}].{id_key}",
                        message=(
                            f"Duplicate {id_key} '{entity_id}' "
                            f"(first occurrence at index {seen_ids[entity_id]})."
                        ),
                    ))
                else:
                    seen_ids[entity_id] = i

    # ------------------------------------------------------------------
    # Diff
    # ------------------------------------------------------------------

    def diff(self, other_path: Path) -> str:
        """Return a unified diff between the loaded registry and another file.

        Args:
            other_path: Path to the registry file to compare against.

        Returns:
            Unified diff string (may be empty if files are identical).
        """
        current_yaml = yaml.dump(self._data, default_flow_style=False,
                                 allow_unicode=True, sort_keys=False)
        with open(other_path, "r", encoding="utf-8") as fh:
            other_data = yaml.safe_load(fh) or {}
        other_yaml = yaml.dump(other_data, default_flow_style=False,
                               allow_unicode=True, sort_keys=False)

        from_name = str(self._path) if self._path else "<current>"
        to_name = str(other_path)

        lines = list(difflib.unified_diff(
            current_yaml.splitlines(keepends=True),
            other_yaml.splitlines(keepends=True),
            fromfile=from_name,
            tofile=to_name,
        ))
        return "".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_registry(self) -> Dict[str, Any]:
        """Return the ``registry`` sub-dict, creating it if absent."""
        if "registry" not in self._data:
            self._data["registry"] = {}
        return self._data["registry"]  # type: ignore[return-value]

    def _get_entity_list(self, entity_type: str) -> List[Dict[str, Any]]:
        """Return the mutable list for the given entity type."""
        registry = self._get_registry()
        list_key = _ENTITY_LIST_KEYS[entity_type]
        if list_key not in registry:
            registry[list_key] = []
        lst = registry[list_key]
        if lst is None:
            registry[list_key] = []
            lst = registry[list_key]
        return lst  # type: ignore[return-value]

    def _find_entity(self, entity_type: str, id_value: str) -> Optional[Dict[str, Any]]:
        """Find and return an entity dict by its id value, or None."""
        id_key = _ENTITY_ID_KEY[entity_type]
        for entity in self._get_entity_list(entity_type):
            if entity.get(id_key) == id_value:
                return entity
        return None

    def _require_entity(self, entity_type: str, id_value: str) -> Dict[str, Any]:
        """Like :meth:`_find_entity` but raises ``KeyError`` if not found."""
        entity = self._find_entity(entity_type, id_value)
        if entity is None:
            id_key = _ENTITY_ID_KEY[entity_type]
            raise KeyError(
                f"{entity_type} with {id_key}='{id_value}' not found."
            )
        return entity

    def _check_unique(self, entity_type: str, id_value: str) -> None:
        """Raise ``ValueError`` if the id is already taken."""
        if self._find_entity(entity_type, id_value) is not None:
            id_key = _ENTITY_ID_KEY[entity_type]
            raise ValueError(
                f"A {entity_type} with {id_key}='{id_value}' already exists."
            )

    # ------------------------------------------------------------------
    # References
    # ------------------------------------------------------------------

    def find_references(self, entity_type: str, id_value: str) -> List[str]:
        """Return a list of human-readable paths that reference the given entity.

        Currently checks:
        - ``preset.device`` for devices
        - ``preset.release`` / ``preset.releases`` for releases
        - ``preset.features`` for features

        Args:
            entity_type: One of ``"device"``, ``"release"``, ``"feature"``.
            id_value: The slug / name of the entity.

        Returns:
            List of path strings, e.g. ``["registry.bsp[0].device"]``.
        """
        refs: List[str] = []
        registry = self._get_registry()
        presets = registry.get("bsp") or []

        for i, preset in enumerate(presets):
            prefix = f"registry.bsp[{i}] ({preset.get('name', '?')})"
            if entity_type == "device" and preset.get("device") == id_value:
                refs.append(f"{prefix}.device")
            elif entity_type == "release":
                if preset.get("release") == id_value:
                    refs.append(f"{prefix}.release")
                if id_value in (preset.get("releases") or []):
                    refs.append(f"{prefix}.releases")
            elif entity_type == "feature":
                if id_value in (preset.get("features") or []):
                    refs.append(f"{prefix}.features")

        return refs

    # ------------------------------------------------------------------
    # CRUD — Device
    # ------------------------------------------------------------------

    def add_device(
        self,
        slug: str,
        description: str,
        vendor: str,
        soc_vendor: str,
        soc_family: Optional[str] = None,
        architecture: Optional[str] = None,
        includes: Optional[List[str]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Add a new device entry.

        Returns:
            The newly created device dict.

        Raises:
            ValueError: If a device with the same slug already exists.
        """
        self._check_unique("device", slug)
        entry: Dict[str, Any] = {"slug": slug, "description": description,
                                  "vendor": vendor, "soc_vendor": soc_vendor}
        if soc_family is not None:
            entry["soc_family"] = soc_family
        if architecture is not None:
            entry["architecture"] = architecture
        if includes is not None:
            entry["includes"] = includes
        entry.update(extra)
        self._push_undo()
        self._get_entity_list("device").append(entry)
        return entry

    def edit_device(self, slug: str, **fields: Any) -> Dict[str, Any]:
        """Update fields of an existing device.

        Only keys with non-None values in *fields* are applied.

        Returns:
            The updated device dict.

        Raises:
            KeyError: If no device with *slug* exists.
        """
        entity = self._require_entity("device", slug)
        self._push_undo()
        for key, value in fields.items():
            if value is not None:
                entity[key] = value
        return entity

    def remove_device(self, slug: str) -> None:
        """Remove a device entry.

        Logs a warning if any preset references this device.

        Raises:
            KeyError: If no device with *slug* exists.
        """
        self._require_entity("device", slug)
        refs = self.find_references("device", slug)
        if refs:
            logging.warning(
                "Removing device '%s' which is referenced by: %s",
                slug, ", ".join(refs),
            )
        self._push_undo()
        lst = self._get_entity_list("device")
        self._data["registry"]["devices"] = [
            e for e in lst if e.get("slug") != slug
        ]

    def show_device(self, slug: Optional[str] = None) -> Union[Dict, List]:
        """Return one or all device dicts (read-only)."""
        if slug is not None:
            return self._require_entity("device", slug)
        return list(self._get_entity_list("device"))

    # ------------------------------------------------------------------
    # CRUD — Release
    # ------------------------------------------------------------------

    def add_release(
        self,
        slug: str,
        description: str,
        includes: Optional[List[str]] = None,
        yocto_version: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Add a new release entry."""
        self._check_unique("release", slug)
        entry: Dict[str, Any] = {"slug": slug, "description": description}
        if includes is not None:
            entry["includes"] = includes
        if yocto_version is not None:
            entry["yocto_version"] = yocto_version
        entry.update(extra)
        self._push_undo()
        self._get_entity_list("release").append(entry)
        return entry

    def edit_release(self, slug: str, **fields: Any) -> Dict[str, Any]:
        """Update fields of an existing release."""
        entity = self._require_entity("release", slug)
        self._push_undo()
        for key, value in fields.items():
            if value is not None:
                entity[key] = value
        return entity

    def remove_release(self, slug: str) -> None:
        """Remove a release entry, warning about preset references."""
        self._require_entity("release", slug)
        refs = self.find_references("release", slug)
        if refs:
            logging.warning(
                "Removing release '%s' which is referenced by: %s",
                slug, ", ".join(refs),
            )
        self._push_undo()
        lst = self._get_entity_list("release")
        self._data["registry"]["releases"] = [
            e for e in lst if e.get("slug") != slug
        ]

    def show_release(self, slug: Optional[str] = None) -> Union[Dict, List]:
        """Return one or all release dicts (read-only)."""
        if slug is not None:
            return self._require_entity("release", slug)
        return list(self._get_entity_list("release"))

    # ------------------------------------------------------------------
    # CRUD — Feature
    # ------------------------------------------------------------------

    def add_feature(
        self,
        slug: str,
        description: str,
        includes: Optional[List[str]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Add a new feature entry."""
        self._check_unique("feature", slug)
        entry: Dict[str, Any] = {"slug": slug, "description": description}
        if includes is not None:
            entry["includes"] = includes
        entry.update(extra)
        self._push_undo()
        self._get_entity_list("feature").append(entry)
        return entry

    def edit_feature(self, slug: str, **fields: Any) -> Dict[str, Any]:
        """Update fields of an existing feature."""
        entity = self._require_entity("feature", slug)
        self._push_undo()
        for key, value in fields.items():
            if value is not None:
                entity[key] = value
        return entity

    def remove_feature(self, slug: str) -> None:
        """Remove a feature entry, warning about preset references."""
        self._require_entity("feature", slug)
        refs = self.find_references("feature", slug)
        if refs:
            logging.warning(
                "Removing feature '%s' which is referenced by: %s",
                slug, ", ".join(refs),
            )
        self._push_undo()
        lst = self._get_entity_list("feature")
        self._data["registry"]["features"] = [
            e for e in lst if e.get("slug") != slug
        ]

    def show_feature(self, slug: Optional[str] = None) -> Union[Dict, List]:
        """Return one or all feature dicts (read-only)."""
        if slug is not None:
            return self._require_entity("feature", slug)
        return list(self._get_entity_list("feature"))

    # ------------------------------------------------------------------
    # CRUD — Preset (bsp)
    # ------------------------------------------------------------------

    def add_preset(
        self,
        name: str,
        description: str,
        device: str,
        release: Optional[str] = None,
        releases: Optional[List[str]] = None,
        features: Optional[List[str]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Add a new BSP preset entry."""
        self._check_unique("preset", name)
        entry: Dict[str, Any] = {"name": name, "description": description,
                                  "device": device}
        if release is not None:
            entry["release"] = release
        if releases is not None:
            entry["releases"] = releases
        if features is not None:
            entry["features"] = features
        entry.update(extra)
        self._push_undo()
        self._get_entity_list("preset").append(entry)
        return entry

    def edit_preset(self, name: str, **fields: Any) -> Dict[str, Any]:
        """Update fields of an existing preset."""
        entity = self._require_entity("preset", name)
        self._push_undo()
        for key, value in fields.items():
            if value is not None:
                entity[key] = value
        return entity

    def remove_preset(self, name: str) -> None:
        """Remove a preset entry."""
        self._require_entity("preset", name)
        self._push_undo()
        lst = self._get_entity_list("preset")
        self._data["registry"]["bsp"] = [
            e for e in lst if e.get("name") != name
        ]

    def show_preset(self, name: Optional[str] = None) -> Union[Dict, List]:
        """Return one or all preset dicts (read-only)."""
        if name is not None:
            return self._require_entity("preset", name)
        return list(self._get_entity_list("preset"))

    # ------------------------------------------------------------------
    # CRUD — Vendor
    # ------------------------------------------------------------------

    def add_vendor(
        self,
        slug: str,
        name: str,
        description: str = "",
        includes: Optional[List[str]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Add a new vendor entry."""
        self._check_unique("vendor", slug)
        entry: Dict[str, Any] = {"slug": slug, "name": name}
        if description:
            entry["description"] = description
        if includes is not None:
            entry["includes"] = includes
        entry.update(extra)
        self._push_undo()
        self._get_entity_list("vendor").append(entry)
        return entry

    def edit_vendor(self, slug: str, **fields: Any) -> Dict[str, Any]:
        """Update fields of an existing vendor."""
        entity = self._require_entity("vendor", slug)
        self._push_undo()
        for key, value in fields.items():
            if value is not None:
                entity[key] = value
        return entity

    def remove_vendor(self, slug: str) -> None:
        """Remove a vendor entry."""
        self._require_entity("vendor", slug)
        self._push_undo()
        lst = self._get_entity_list("vendor")
        self._data["registry"]["vendors"] = [
            e for e in lst if e.get("slug") != slug
        ]

    def show_vendor(self, slug: Optional[str] = None) -> Union[Dict, List]:
        """Return one or all vendor dicts (read-only)."""
        if slug is not None:
            return self._require_entity("vendor", slug)
        return list(self._get_entity_list("vendor"))

    # ------------------------------------------------------------------
    # CRUD — Distro
    # ------------------------------------------------------------------

    def add_distro(
        self,
        slug: str,
        description: str,
        vendor: str = "",
        includes: Optional[List[str]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Add a new distro entry."""
        self._check_unique("distro", slug)
        entry: Dict[str, Any] = {"slug": slug, "description": description}
        if vendor:
            entry["vendor"] = vendor
        if includes is not None:
            entry["includes"] = includes
        entry.update(extra)
        self._push_undo()
        self._get_entity_list("distro").append(entry)
        return entry

    def edit_distro(self, slug: str, **fields: Any) -> Dict[str, Any]:
        """Update fields of an existing distro."""
        entity = self._require_entity("distro", slug)
        self._push_undo()
        for key, value in fields.items():
            if value is not None:
                entity[key] = value
        return entity

    def remove_distro(self, slug: str) -> None:
        """Remove a distro entry."""
        self._require_entity("distro", slug)
        self._push_undo()
        lst = self._get_entity_list("distro")
        self._data["registry"]["distro"] = [
            e for e in lst if e.get("slug") != slug
        ]

    def show_distro(self, slug: Optional[str] = None) -> Union[Dict, List]:
        """Return one or all distro dicts (read-only)."""
        if slug is not None:
            return self._require_entity("distro", slug)
        return list(self._get_entity_list("distro"))

    # ------------------------------------------------------------------
    # CRUD — Framework
    # ------------------------------------------------------------------

    def add_framework(
        self,
        slug: str,
        description: str,
        vendor: str = "",
        includes: Optional[List[str]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Add a new framework entry."""
        self._check_unique("framework", slug)
        entry: Dict[str, Any] = {"slug": slug, "description": description}
        if vendor:
            entry["vendor"] = vendor
        if includes is not None:
            entry["includes"] = includes
        entry.update(extra)
        self._push_undo()
        self._get_entity_list("framework").append(entry)
        return entry

    def edit_framework(self, slug: str, **fields: Any) -> Dict[str, Any]:
        """Update fields of an existing framework."""
        entity = self._require_entity("framework", slug)
        self._push_undo()
        for key, value in fields.items():
            if value is not None:
                entity[key] = value
        return entity

    def remove_framework(self, slug: str) -> None:
        """Remove a framework entry."""
        self._require_entity("framework", slug)
        self._push_undo()
        lst = self._get_entity_list("framework")
        self._data["registry"]["frameworks"] = [
            e for e in lst if e.get("slug") != slug
        ]

    def show_framework(self, slug: Optional[str] = None) -> Union[Dict, List]:
        """Return one or all framework dicts (read-only)."""
        if slug is not None:
            return self._require_entity("framework", slug)
        return list(self._get_entity_list("framework"))

    # ------------------------------------------------------------------
    # CRUD — Container
    # ------------------------------------------------------------------

    def _get_containers(self) -> Dict[str, Any]:
        """Return the top-level containers dict, creating it if absent."""
        if "containers" not in self._data:
            self._data["containers"] = {}
        containers = self._data["containers"]
        if containers is None:
            self._data["containers"] = {}
            containers = self._data["containers"]
        return containers  # type: ignore[return-value]

    def add_container(
        self,
        name: str,
        image: str,
        file: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Add a new container entry under the top-level ``containers`` key.

        Raises:
            ValueError: If a container with *name* already exists.
        """
        containers = self._get_containers()
        if name in containers:
            raise ValueError(f"A container named '{name}' already exists.")
        entry: Dict[str, Any] = {"image": image}
        if file is not None:
            entry["file"] = file
        entry.update(extra)
        self._push_undo()
        containers[name] = entry
        return entry

    def edit_container(self, name: str, **fields: Any) -> Dict[str, Any]:
        """Update fields of an existing container.

        Raises:
            KeyError: If no container with *name* exists.
        """
        containers = self._get_containers()
        if name not in containers:
            raise KeyError(f"Container '{name}' not found.")
        self._push_undo()
        for key, value in fields.items():
            if value is not None:
                containers[name][key] = value
        return containers[name]

    def remove_container(self, name: str) -> None:
        """Remove a container entry.

        Raises:
            KeyError: If no container with *name* exists.
        """
        containers = self._get_containers()
        if name not in containers:
            raise KeyError(f"Container '{name}' not found.")
        self._push_undo()
        del containers[name]

    def show_container(self, name: Optional[str] = None) -> Union[Dict, Any]:
        """Return one container dict, or the full containers mapping."""
        containers = self._get_containers()
        if name is not None:
            if name not in containers:
                raise KeyError(f"Container '{name}' not found.")
            return {name: containers[name]}
        return dict(containers)

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    def git_stage(self, path: Optional[Path] = None) -> None:
        """Run ``git add <path>`` for the registry file.

        No-ops gracefully if not inside a git repository.

        Args:
            path: Path to stage.  Defaults to the loaded registry path.
        """
        target = Path(path) if path is not None else self._path
        if target is None:
            logging.warning("git_stage: no path set; skipping.")
            return
        try:
            subprocess.run(
                ["git", "add", str(target)],
                check=True,
                capture_output=True,
            )
            logging.debug("git add %s", target)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            logging.debug("git_stage: git add failed (not a git repo?): %s", exc)

    def git_commit(self, message: str, path: Optional[Path] = None) -> None:
        """Run ``git commit -m <message> <path>``.

        No-ops gracefully if not inside a git repository or if there is
        nothing to commit.

        Args:
            message: Commit message.
            path: Path to commit.  Defaults to the loaded registry path.
        """
        target = Path(path) if path is not None else self._path
        if target is None:
            logging.warning("git_commit: no path set; skipping.")
            return
        try:
            subprocess.run(
                ["git", "commit", "-m", message, str(target)],
                check=True,
                capture_output=True,
            )
            logging.debug("git commit -m %r %s", message, target)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            logging.debug("git_commit: git commit failed: %s", exc)
