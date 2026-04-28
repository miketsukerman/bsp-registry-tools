"""
Argcomplete completers for the BSP CLI.
"""

from pathlib import Path
from typing import List

from .registry_fetcher import DEFAULT_REMOTE_URL, DEFAULT_BRANCH, RegistryFetcher, REGISTRY_FILENAMES


class BspNameCompleter:
    """
    Argcomplete completer that returns BSP names from the active registry.

    Resolves the registry path using the same priority logic as the CLI main():
      1. ``--registry`` explicitly provided
      2. ``--local`` flag set (use local file, no remote)
      3. ``bsp-registry.yaml`` / ``bsp-registry.yml`` in the current directory
      4. Cached remote registry (``update=False`` to avoid network I/O during completion)

    If no registry is found or the file cannot be parsed, returns ``[]``
    silently so the shell session is never disrupted.
    """

    def __call__(self, prefix: str, parsed_args, **kwargs) -> List[str]:
        registry_path = self._resolve_registry_path(parsed_args)
        if registry_path is None:
            return []

        try:
            from .utils import get_registry_from_yaml_file
            model = get_registry_from_yaml_file(Path(registry_path))
            bsp_list = model.registry.bsp or []
            return [bsp.name for bsp in bsp_list if bsp.name.startswith(prefix)]
        except (Exception, SystemExit):  # noqa: BLE001 — never crash a shell completion
            return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_registry_path(parsed_args) -> str | None:
        """Return the registry file path using CLI priority rules."""
        LOCAL_DEFAULTS = ["bsp-registry.yaml", "bsp-registry.yml"]

        explicit_registry = getattr(parsed_args, 'registry', None)
        local_flag = getattr(parsed_args, 'local', False)

        local_file = next((name for name in LOCAL_DEFAULTS if Path(name).is_file()), None)

        if explicit_registry is not None:
            return explicit_registry

        if local_flag:
            return local_file or LOCAL_DEFAULTS[0]

        if local_file is not None:
            return local_file

        # Fall back to cached remote — pass update=False to avoid network I/O
        try:
            remote = getattr(parsed_args, 'remote', DEFAULT_REMOTE_URL)
            branch = getattr(parsed_args, 'branch', DEFAULT_BRANCH)
            fetcher = RegistryFetcher()
            path = fetcher.fetch_registry(repo_url=remote, branch=branch, update=False)
            return str(path)
        except SystemExit:
            return None
        except Exception:  # noqa: BLE001
            return None
