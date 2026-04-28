"""
Persistent management of named BSP remote registries.

Remotes are stored in a YAML file at ``~/.config/bsp/remotes.yaml``
(overridable via ``BSP_REMOTES_CONFIG`` environment variable) in the
following format::

    remotes:
      - name: advantech
        url: https://github.com/Advantech-EECC/bsp-registry.git
        branch: main
      - name: custom
        url: https://github.com/my-org/bsp-registry.git
        branch: develop

The design mirrors ``git remote``:
- ``bsp remotes``              — list remote names
- ``bsp remotes add``          — register a new named remote
- ``bsp remotes remove``       — remove a named remote
- ``bsp remotes rename``       — rename a remote
- ``bsp remotes set-url``      — change the URL of an existing remote
- ``bsp remotes show``         — show details of a remote
"""

import os
import sys
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

import yaml

from .registry_fetcher import DEFAULT_BRANCH


# ---------------------------------------------------------------------------
# Config file location
# ---------------------------------------------------------------------------

DEFAULT_REMOTES_CONFIG = Path.home() / ".config" / "bsp" / "remotes.yaml"


def _remotes_config_path() -> Path:
    """Return the active remotes config file path.

    Can be overridden via the ``BSP_REMOTES_CONFIG`` environment variable.
    """
    env = os.environ.get("BSP_REMOTES_CONFIG")
    return Path(env) if env else DEFAULT_REMOTES_CONFIG


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RemoteEntry:
    """A single named remote registry entry.

    Attributes:
        name:   Display name used to reference this remote.
        url:    Git repository URL.
        branch: Branch to use when fetching (default ``"main"``).
    """

    name: str
    url: str
    branch: str = DEFAULT_BRANCH

    def to_dict(self) -> dict:
        return {"name": self.name, "url": self.url, "branch": self.branch}


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class RemotesManager:
    """Read/write the persistent remotes configuration file.

    All mutating operations load the file, apply the change, and save
    immediately so that concurrent CLI invocations see a consistent view.

    Args:
        config_path: Path to the YAML config file.  Defaults to
                     ``~/.config/bsp/remotes.yaml`` (or ``BSP_REMOTES_CONFIG``).
    """

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self.config_path = config_path or _remotes_config_path()
        self.logger = logging.getLogger(self.__class__.__name__)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> List[RemoteEntry]:
        """Load and return all configured remotes.

        Returns an empty list if the config file does not exist yet.
        """
        if not self.config_path.is_file():
            return []
        try:
            with open(self.config_path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError) as exc:
            self.logger.warning("Could not read remotes config '%s': %s", self.config_path, exc)
            return []

        remotes_raw = data.get("remotes", []) or []
        result = []
        for item in remotes_raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            url = item.get("url")
            if not name or not url:
                continue
            result.append(RemoteEntry(
                name=str(name),
                url=str(url),
                branch=str(item.get("branch", DEFAULT_BRANCH)),
            ))
        return result

    def save(self, remotes: List[RemoteEntry]) -> None:
        """Persist the given list of remotes to disk.

        Creates parent directories if they do not exist.
        """
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"remotes": [r.to_dict() for r in remotes]}
        with open(self.config_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)
        self.logger.debug("Saved remotes config to '%s'", self.config_path)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add(self, name: str, url: str, branch: str = DEFAULT_BRANCH) -> RemoteEntry:
        """Add a new remote.

        Args:
            name:   Unique name for the remote.
            url:    Git repository URL.
            branch: Branch to use (default ``"main"``).

        Returns:
            The newly created :class:`RemoteEntry`.

        Raises:
            SystemExit: If a remote with *name* already exists.
        """
        remotes = self.load()
        if any(r.name == name for r in remotes):
            logging.error("Remote '%s' already exists.  Use 'set-url' or 'rename' to modify it.", name)
            sys.exit(1)
        entry = RemoteEntry(name=name, url=url, branch=branch)
        remotes.append(entry)
        self.save(remotes)
        return entry

    def remove(self, name: str) -> None:
        """Remove a remote by name.

        Raises:
            SystemExit: If no remote with *name* exists.
        """
        remotes = self.load()
        new_remotes = [r for r in remotes if r.name != name]
        if len(new_remotes) == len(remotes):
            logging.error("Remote '%s' not found.", name)
            self._print_available(remotes)
            sys.exit(1)
        self.save(new_remotes)

    def rename(self, old_name: str, new_name: str) -> RemoteEntry:
        """Rename a remote.

        Args:
            old_name: Current remote name.
            new_name: New remote name.

        Returns:
            The updated :class:`RemoteEntry`.

        Raises:
            SystemExit: If *old_name* does not exist or *new_name* already exists.
        """
        remotes = self.load()
        if not any(r.name == old_name for r in remotes):
            logging.error("Remote '%s' not found.", old_name)
            self._print_available(remotes)
            sys.exit(1)
        if any(r.name == new_name for r in remotes):
            logging.error("Remote '%s' already exists.", new_name)
            sys.exit(1)
        for r in remotes:
            if r.name == old_name:
                r.name = new_name
                updated = r
                break
        self.save(remotes)
        return updated

    def set_url(self, name: str, url: str) -> RemoteEntry:
        """Change the URL of an existing remote.

        Args:
            name: Remote name.
            url:  New git repository URL.

        Returns:
            The updated :class:`RemoteEntry`.

        Raises:
            SystemExit: If no remote with *name* exists.
        """
        remotes = self.load()
        for r in remotes:
            if r.name == name:
                r.url = url
                self.save(remotes)
                return r
        logging.error("Remote '%s' not found.", name)
        self._print_available(remotes)
        sys.exit(1)

    def set_branch(self, name: str, branch: str) -> RemoteEntry:
        """Change the branch of an existing remote.

        Args:
            name:   Remote name.
            branch: New branch name.

        Returns:
            The updated :class:`RemoteEntry`.

        Raises:
            SystemExit: If no remote with *name* exists.
        """
        remotes = self.load()
        for r in remotes:
            if r.name == name:
                r.branch = branch
                self.save(remotes)
                return r
        logging.error("Remote '%s' not found.", name)
        self._print_available(remotes)
        sys.exit(1)

    def get(self, name: str) -> RemoteEntry:
        """Return a remote by name.

        Raises:
            SystemExit: If no remote with *name* exists.
        """
        for r in self.load():
            if r.name == name:
                return r
        logging.error("Remote '%s' not found.", name)
        self._print_available(self.load())
        sys.exit(1)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _print_available(self, remotes: List[RemoteEntry]) -> None:
        available = ", ".join(r.name for r in remotes) or "(none)"
        print(f"Available remotes: {available}")
