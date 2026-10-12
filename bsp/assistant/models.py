"""
Data models for BSP Assistant discovery and recommendation engine.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional


@dataclass
class PresetInfo:
    """Complete preset information."""

    name: str
    description: str
    device: str
    releases: List[str] = field(default_factory=list)
    features: List[str] = field(default_factory=list)
    stability_status: Literal["stable", "development", "eol"] = "stable"
    tested_combinations: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PresetInfo":
        """Construct PresetInfo from dictionary."""
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            device=data["device"],
            releases=list(data.get("releases", [])),
            features=list(data.get("features", [])),
            stability_status=data.get("stability_status", "stable"),
            tested_combinations=bool(data.get("tested_combinations", False)),
        )


@dataclass
class DeviceInfo:
    """Hardware device information."""

    slug: str
    name: str
    vendor: str
    soc_family: str
    supported_features: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeviceInfo":
        """Construct DeviceInfo from dictionary."""
        return cls(
            slug=data["slug"],
            name=data.get("name", data["slug"]),
            vendor=data.get("vendor", ""),
            soc_family=data.get("soc_family", ""),
            supported_features=list(data.get("supported_features", [])),
        )


@dataclass
class FeatureInfo:
    """Feature information."""

    slug: str
    name: str
    description: str
    compatible_vendors: List[str] = field(default_factory=list)
    compatible_releases: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FeatureInfo":
        """Construct FeatureInfo from dictionary."""
        return cls(
            slug=data["slug"],
            name=data.get("name", data["slug"]),
            description=data.get("description", ""),
            compatible_vendors=list(data.get("compatible_vendors", [])),
            compatible_releases=list(data.get("compatible_releases", [])),
        )


@dataclass
class UserRequirements:
    """User input for discovery."""

    board_name: Optional[str] = None
    features: List[str] = field(default_factory=list)
    release_preference: Literal["latest", "lts", "specific"] = "latest"
    release_version: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserRequirements":
        """Construct UserRequirements from dictionary."""
        return cls(
            board_name=data.get("board_name"),
            features=list(data.get("features", [])),
            release_preference=data.get("release_preference", "latest"),
            release_version=data.get("release_version"),
        )


@dataclass
class Recommendation:
    """Recommended preset with reasoning."""

    preset_name: str
    confidence_score: float  # 0-100
    reasoning: List[str] = field(default_factory=list)
    alternatives: List[str] = field(default_factory=list)
    known_issues: List[str] = field(default_factory=list)
    build_time_estimate: str = "2-3 hours"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Recommendation":
        """Construct Recommendation from dictionary."""
        return cls(
            preset_name=data["preset_name"],
            confidence_score=float(data.get("confidence_score", 0.0)),
            reasoning=list(data.get("reasoning", [])),
            alternatives=list(data.get("alternatives", [])),
            known_issues=list(data.get("known_issues", [])),
            build_time_estimate=data.get("build_time_estimate", "2-3 hours"),
        )


@dataclass
class RiskAssessment:
    """Risk assessment for a preset configuration."""

    preset_name: str = ""
    risk_level: Literal["low", "medium", "high"] = "low"
    untested_combinations: bool = False
    eol_releases: bool = False
    known_issues: List[str] = field(default_factory=list)
    missing_features: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    is_tested: bool = True
    is_eol: bool = False

    def __post_init__(self):
        if self.untested_combinations:
            self.is_tested = False
        elif not self.is_tested:
            self.untested_combinations = True

        if self.eol_releases:
            self.is_eol = True
        elif self.is_eol:
            self.eol_releases = True

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RiskAssessment":
        """Construct RiskAssessment from dictionary."""
        return cls(
            preset_name=data.get("preset_name", ""),
            risk_level=data.get("risk_level", "low"),
            untested_combinations=bool(data.get("untested_combinations", False)),
            eol_releases=bool(data.get("eol_releases", False)),
            known_issues=list(data.get("known_issues", [])),
            missing_features=list(data.get("missing_features", [])),
            warnings=list(data.get("warnings", [])),
            is_tested=bool(data.get("is_tested", not data.get("untested_combinations", False))),
            is_eol=bool(data.get("is_eol", data.get("eol_releases", False))),
        )


@dataclass
class CompatibilityReport:
    """Compatibility check report."""

    compatible: bool = True
    preset_name: str = ""
    preset_or_board: str = ""
    device: str = ""
    release: str = ""
    supported_features: List[str] = field(default_factory=list)
    unsupported_features: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.preset_or_board:
            self.preset_or_board = self.preset_name or self.device
        elif not self.preset_name:
            self.preset_name = self.preset_or_board

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CompatibilityReport":
        """Construct CompatibilityReport from dictionary."""
        return cls(
            compatible=bool(data.get("compatible", True)),
            preset_name=data.get("preset_name", data.get("preset_or_board", "")),
            preset_or_board=data.get("preset_or_board", data.get("preset_name", "")),
            device=data.get("device", ""),
            release=data.get("release", ""),
            supported_features=list(data.get("supported_features", [])),
            unsupported_features=list(data.get("unsupported_features", [])),
            reasons=list(data.get("reasons", [])),
        )


@dataclass
class CompatibilityMatrix:
    """In-memory compatibility matrix across devices, features, releases, and vendors."""

    device_features: Dict[str, List[str]] = field(default_factory=dict)
    device_releases: Dict[str, List[str]] = field(default_factory=dict)
    feature_releases: Dict[str, List[str]] = field(default_factory=dict)
    feature_vendors: Dict[str, List[str]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CompatibilityMatrix":
        """Construct CompatibilityMatrix from dictionary."""
        return cls(
            device_features=dict(data.get("device_features", {})),
            device_releases=dict(data.get("device_releases", {})),
            feature_releases=dict(data.get("feature_releases", {})),
            feature_vendors=dict(data.get("feature_vendors", {})),
        )


@dataclass
class RegistryData:
    """Parsed registry data containing all components and compatibility information."""

    presets: List[PresetInfo] = field(default_factory=list)
    devices: List[DeviceInfo] = field(default_factory=list)
    features: List[FeatureInfo] = field(default_factory=list)
    releases: List[str] = field(default_factory=list)
    compatibility_matrix: CompatibilityMatrix = field(default_factory=CompatibilityMatrix)
    raw_root: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "presets": [p.to_dict() for p in self.presets],
            "devices": [d.to_dict() for d in self.devices],
            "features": [f.to_dict() for f in self.features],
            "releases": list(self.releases),
            "compatibility_matrix": self.compatibility_matrix.to_dict(),
        }
