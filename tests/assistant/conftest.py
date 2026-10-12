"""
Pytest fixtures for BSP Assistant tests.
"""

from pathlib import Path

import pytest

from bsp.assistant.cache import RegistryCache
from bsp.assistant.recommendation_engine import RecommendationEngine
from bsp.assistant.registry_parser import RegistryParser


@pytest.fixture
def fixtures_dir() -> Path:
    """Return path to assistant test fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def minimal_registry_path(fixtures_dir: Path) -> Path:
    """Return path to minimal-registry.yaml."""
    return fixtures_dir / "minimal-registry.yaml"


@pytest.fixture
def complex_registry_path(fixtures_dir: Path) -> Path:
    """Return path to complex-registry.yaml."""
    return fixtures_dir / "complex-registry.yaml"


@pytest.fixture
def fresh_cache() -> RegistryCache:
    """Return an empty RegistryCache."""
    return RegistryCache()


@pytest.fixture
def minimal_parser(minimal_registry_path: Path, fresh_cache: RegistryCache) -> RegistryParser:
    """Return a parser loaded with the minimal registry."""
    return RegistryParser(str(minimal_registry_path), cache=fresh_cache)


@pytest.fixture
def complex_parser(complex_registry_path: Path, fresh_cache: RegistryCache) -> RegistryParser:
    """Return a parser loaded with the complex registry."""
    return RegistryParser(str(complex_registry_path), cache=fresh_cache)


@pytest.fixture
def complex_engine(complex_parser: RegistryParser) -> RecommendationEngine:
    """Return a RecommendationEngine initialized with complex registry data."""
    return RecommendationEngine(complex_parser.registry_data)
