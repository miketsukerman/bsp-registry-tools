"""
Registry parser for BSP Assistant: loads registry files, extracts components,
and constructs compatibility matrices.
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional, Set

from bsp.models import Device, Feature, RegistryRoot, Release
from bsp.utils import get_registry_from_yaml_file

from .cache import RegistryCache
from .models import (
    CompatibilityMatrix,
    DeviceInfo,
    FeatureInfo,
    PresetInfo,
    RegistryData,
)

logger = logging.getLogger(__name__)

# Known Yocto lifecycle classifications
KNOWN_EOL_RELEASES: Set[str] = {
    "kirkstone",
    "dunfell",
    "honister",
    "hardknott",
    "gatesgarth",
    "zeus",
    "warrior",
    "thud",
    "sumo",
    "rocko",
    "pyro",
    "morty",
    "krogoth",
    "fido",
    "dizzy",
    "daisy",
    "dora",
    "danny",
    "edison",
    "denzil",
}

KNOWN_DEV_RELEASES: Set[str] = {
    "master",
    "bleeding-edge",
    "development",
    "dev",
}

KNOWN_LTS_RELEASES: Set[str] = {
    "scarthgap",
    "kirkstone",
    "dunfell",
}


class RegistryParser:
    """
    Parser for BSP registry YAML files.

    Extracts presets, devices, features, and releases, and builds an in-memory
    compatibility matrix. Caches parsed results for high performance.
    """

    def __init__(
        self,
        registry_path: Optional[str] = None,
        cache: Optional[RegistryCache] = None,
    ) -> None:
        self.cache = cache if cache is not None else RegistryCache()
        self.registry_path = registry_path
        self._raw_root: Optional[RegistryRoot] = None
        self._registry_data: Optional[RegistryData] = None

        if registry_path:
            self.load_registry(registry_path)

    def load_registry(self, registry_path: str) -> RegistryData:
        """
        Load and parse bsp-registry.yaml file and all recursive includes.

        Args:
            registry_path: Path to the root registry YAML file.

        Returns:
            RegistryData object containing extracted components and matrix.
        """
        cached = self.cache.get(registry_path)
        if cached is not None:
            self._registry_data = cached
            self._raw_root = cached.raw_root
            self.registry_path = registry_path
            return cached

        path_obj = Path(registry_path).resolve()
        if not path_obj.exists():
            raise FileNotFoundError(f"Registry file not found: {registry_path}")

        # Use bsp.utils to parse YAML and resolve all includes recursively
        root: RegistryRoot = get_registry_from_yaml_file(path_obj)
        data = self.load_registry_root(root, cache_key=str(path_obj))
        return data

    def load_registry_root(
        self, root: RegistryRoot, cache_key: Optional[str] = None
    ) -> RegistryData:
        """
        Build RegistryData directly from a loaded RegistryRoot instance.

        Args:
            root: Already parsed and validated RegistryRoot object.
            cache_key: Optional key to cache the resulting RegistryData.

        Returns:
            RegistryData object containing extracted components and matrix.
        """
        self._raw_root = root

        presets = self.extract_presets()
        devices = self.extract_devices()
        features = self.extract_features()
        releases = [r.slug for r in root.registry.releases]
        matrix = self.get_compatibility_matrix()

        data = RegistryData(
            presets=presets,
            devices=devices,
            features=features,
            releases=releases,
            compatibility_matrix=matrix,
            raw_root=root,
        )

        self._registry_data = data
        if cache_key:
            self.cache.set(cache_key, data)
        return data

    @property
    def registry_data(self) -> RegistryData:
        """Return the loaded RegistryData or raise ValueError if not loaded."""
        if self._registry_data is None:
            if self.registry_path:
                return self.load_registry(self.registry_path)
            raise ValueError("Registry has not been loaded yet. Call load_registry() first.")
        return self._registry_data

    @property
    def raw_root(self) -> RegistryRoot:
        """Return the raw RegistryRoot or raise ValueError if not loaded."""
        if self._raw_root is None:
            if self.registry_path:
                self.load_registry(self.registry_path)
                assert self._raw_root is not None
                return self._raw_root
            raise ValueError("Registry has not been loaded yet. Call load_registry() first.")
        return self._raw_root

    def _determine_stability(
        self,
        releases: List[str],
        description: str,
    ) -> str:
        """Determine stability status (stable, development, eol)."""
        desc_lower = description.lower()
        if "eol" in desc_lower:
            return "eol"
        if "development" in desc_lower or "bleeding edge" in desc_lower:
            return "development"

        for r in releases:
            r_lower = r.lower()
            if r_lower in KNOWN_EOL_RELEASES:
                return "eol"
            if r_lower in KNOWN_DEV_RELEASES:
                return "development"

        return "stable"

    def extract_presets(self) -> List[PresetInfo]:
        """
        Extract complete preset information from registry.

        Returns:
            List of PresetInfo dataclasses.
        """
        root = self.raw_root
        raw_presets = root.registry.bsp or []
        results: List[PresetInfo] = []

        for p in raw_presets:
            if p.releases:
                releases = list(p.releases)
            elif p.release:
                releases = [p.release]
            else:
                releases = []

            # Check if tested combinations exist (e.g. testing block or lava/direct)
            tested = bool(p.testing is not None)

            stability = self._determine_stability(releases, p.description)

            preset_info = PresetInfo(
                name=p.name,
                description=p.description or "",
                device=p.device,
                releases=releases,
                features=list(p.features or []),
                stability_status=stability,  # type: ignore[arg-type]
                tested_combinations=tested,
            )
            results.append(preset_info)

        return results

    def _is_feature_device_compatible(self, feature: Feature, device: Device) -> bool:
        """Check if a feature is compatible with a device hardware."""
        compat = feature.compatibility
        if compat is None:
            return True

        if compat.vendor and device.vendor not in compat.vendor:
            return False

        if compat.soc_vendor and device.soc_vendor not in compat.soc_vendor:
            return False

        if compat.soc_family:
            dev_soc_family = device.soc_family or ""
            if dev_soc_family not in compat.soc_family:
                return False

        return True

    def _is_feature_release_compatible(self, feature: Feature, release: Release) -> bool:
        """Check if a feature is compatible with a release framework/distro."""
        if not feature.compatible_with:
            return True

        distro_slug = release.distro
        framework_slug = None
        if distro_slug:
            for d in self.raw_root.registry.distro:
                if d.slug == distro_slug:
                    framework_slug = d.framework
                    break

        for item in feature.compatible_with:
            if distro_slug and item == distro_slug:
                return True
            if framework_slug and item == framework_slug:
                return True

        return False

    def extract_devices(self) -> List[DeviceInfo]:
        """
        Extract hardware device information and compute supported features.

        Returns:
            List of DeviceInfo dataclasses.
        """
        root = self.raw_root
        devices = root.registry.devices
        features = root.registry.features
        results: List[DeviceInfo] = []

        for d in devices:
            supported: List[str] = []
            for f in features:
                if self._is_feature_device_compatible(f, d):
                    supported.append(f.slug)

            name = d.description if d.description else d.slug
            dev_info = DeviceInfo(
                slug=d.slug,
                name=name,
                vendor=d.vendor,
                soc_family=d.soc_family or "",
                supported_features=supported,
            )
            results.append(dev_info)

        return results

    def extract_features(self) -> List[FeatureInfo]:
        """
        Extract feature information, compatible vendors, and compatible releases.

        Returns:
            List of FeatureInfo dataclasses.
        """
        root = self.raw_root
        features = root.registry.features
        releases = root.registry.releases
        vendors = root.registry.vendors
        results: List[FeatureInfo] = []

        for f in features:
            compat_vendors: List[str] = []
            if f.compatibility and f.compatibility.vendor:
                compat_vendors = list(f.compatibility.vendor)
            else:
                compat_vendors = [v.slug for v in vendors]

            compat_releases: List[str] = []
            for r in releases:
                if self._is_feature_release_compatible(f, r):
                    compat_releases.append(r.slug)

            feat_info = FeatureInfo(
                slug=f.slug,
                name=f.slug.replace("-", " ").title(),
                description=f.description or "",
                compatible_vendors=compat_vendors,
                compatible_releases=compat_releases,
            )
            results.append(feat_info)

        return results

    def get_compatibility_matrix(self) -> CompatibilityMatrix:
        """
        Build and return in-memory compatibility matrix.

        Returns:
            CompatibilityMatrix instance.
        """
        root = self.raw_root
        devices = root.registry.devices
        features = root.registry.features
        releases = root.registry.releases
        presets = root.registry.bsp or []

        device_features: Dict[str, List[str]] = {}
        for d in devices:
            device_features[d.slug] = [
                f.slug for f in features if self._is_feature_device_compatible(f, d)
            ]

        # Device to releases: based on presets that use the device, or vendor overrides
        device_releases: Dict[str, List[str]] = {}
        for d in devices:
            dev_rels: Set[str] = set()
            for p in presets:
                if p.device == d.slug:
                    if p.releases:
                        dev_rels.update(p.releases)
                    elif p.release:
                        dev_rels.add(p.release)
            # If no presets, all releases matching vendor overrides or default
            if not dev_rels:
                for r in releases:
                    # If release has vendor overrides, check if matching device.vendor
                    if r.vendor_overrides:
                        for vo in r.vendor_overrides:
                            if vo.vendor == d.vendor:
                                dev_rels.add(r.slug)
                    else:
                        dev_rels.add(r.slug)
            device_releases[d.slug] = sorted(list(dev_rels))

        feature_releases: Dict[str, List[str]] = {}
        for f in features:
            feature_releases[f.slug] = [
                r.slug for r in releases if self._is_feature_release_compatible(f, r)
            ]

        feature_vendors: Dict[str, List[str]] = {}
        for f in features:
            if f.compatibility and f.compatibility.vendor:
                feature_vendors[f.slug] = list(f.compatibility.vendor)
            else:
                feature_vendors[f.slug] = [v.slug for v in root.registry.vendors]

        return CompatibilityMatrix(
            device_features=device_features,
            device_releases=device_releases,
            feature_releases=feature_releases,
            feature_vendors=feature_vendors,
        )

    def find_presets_by_device(self, device: str) -> List[PresetInfo]:
        """
        Find all presets targeting a specific device.

        Args:
            device: Device slug or name (case-insensitive, normalized).

        Returns:
            List of matching PresetInfo objects.
        """
        norm_device = device.lower().replace("-", "").replace("_", "")
        matches: List[PresetInfo] = []
        for p in self.registry_data.presets:
            p_dev_norm = p.device.lower().replace("-", "").replace("_", "")
            if norm_device == p_dev_norm or norm_device in p_dev_norm or p_dev_norm in norm_device:
                matches.append(p)
        return matches

    def find_presets_by_feature(self, feature: str) -> List[PresetInfo]:
        """
        Find all presets enabling a specific feature.

        Args:
            feature: Feature slug (case-insensitive).

        Returns:
            List of matching PresetInfo objects.
        """
        norm_feature = feature.lower()
        matches: List[PresetInfo] = []
        for p in self.registry_data.presets:
            p_feats = [f.lower() for f in p.features]
            if norm_feature in p_feats:
                matches.append(p)
        return matches
