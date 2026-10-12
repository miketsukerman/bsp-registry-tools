"""
Thread-safe in-memory caching layer for parsed BSP registry data.
"""

import os
import threading
from typing import Dict, Optional, Tuple

from .models import RegistryData


class RegistryCache:
    """Thread-safe cache for parsed registry data keyed by path and file mtime."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: Dict[str, Tuple[float, RegistryData]] = {}

    def __bool__(self) -> bool:
        return True

    def has(self, registry_path: str) -> bool:
        """
        Check if registry_path is present and valid in cache.

        Args:
            registry_path: Path to the registry YAML file.

        Returns:
            True if present and valid, False otherwise.
        """
        return self.get(registry_path) is not None

    def get(self, registry_path: str) -> Optional[RegistryData]:
        """
        Retrieve cached registry data if still valid based on file modification time.

        Args:
            registry_path: Path to the registry YAML file.

        Returns:
            Cached RegistryData or None if not found or stale.
        """
        canonical_path = os.path.abspath(registry_path)
        with self._lock:
            if canonical_path not in self._cache:
                return None
            cached_mtime, data = self._cache[canonical_path]

        try:
            current_mtime = os.path.getmtime(canonical_path)
            if current_mtime != cached_mtime:
                self.invalidate(canonical_path)
                return None
        except OSError:
            # If file cannot be accessed, return None
            return None

        return data

    def set(
        self,
        registry_path: str,
        data: RegistryData,
        mtime: Optional[float] = None,
    ) -> None:
        """
        Store registry data in the cache.

        Args:
            registry_path: Path to the registry YAML file.
            data: Parsed RegistryData instance.
            mtime: Optional mtime override. If omitted, fetched from filesystem.
        """
        canonical_path = os.path.abspath(registry_path)
        if mtime is None:
            try:
                mtime = os.path.getmtime(canonical_path)
            except OSError:
                mtime = 0.0

        with self._lock:
            self._cache[canonical_path] = (mtime, data)

    def invalidate(self, registry_path: str) -> None:
        """Remove a specific registry from the cache."""
        canonical_path = os.path.abspath(registry_path)
        with self._lock:
            self._cache.pop(canonical_path, None)

    def clear(self) -> None:
        """Clear all entries from the cache."""
        with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)
