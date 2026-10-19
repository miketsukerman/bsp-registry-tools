"""
BSP Assistant - AI-guided interactive discovery and recommendation system for the BSP Registry.
"""

from .models import (
    CompatibilityMatrix,
    CompatibilityReport,
    DeviceInfo,
    FeatureInfo,
    PresetInfo,
    Recommendation,
    RegistryData,
    RiskAssessment,
    UserRequirements,
)
from .registry_parser import RegistryParser
from .recommendation_engine import RecommendationEngine

__all__ = [
    "PresetInfo",
    "DeviceInfo",
    "FeatureInfo",
    "UserRequirements",
    "Recommendation",
    "RiskAssessment",
    "CompatibilityReport",
    "CompatibilityMatrix",
    "RegistryData",
    "RegistryParser",
    "RecommendationEngine",
]
