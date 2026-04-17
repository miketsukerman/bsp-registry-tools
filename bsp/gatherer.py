"""
Artifact gatherer: downloads BSP build artifacts from cloud storage.

This is the download counterpart to :mod:`bsp.deployer`.
"""

import datetime
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .models import DeployConfig
from .storage.base import CloudStorageBackend


# =============================================================================
# Gather result
# =============================================================================


@dataclass
class GatherResult:
    """Result of a full gather (download) run."""
    artifacts: List[Path] = field(default_factory=list)
    manifest: Optional[Dict] = None
    dest_dir: Optional[Path] = None
    dry_run: bool = False

    @property
    def total_count(self) -> int:
        return len(self.artifacts)


# =============================================================================
# ArtifactGatherer
# =============================================================================


class ArtifactGatherer:
    """
    Downloads BSP build artifacts from a cloud storage backend.

    This class is provider-agnostic; all cloud interaction is delegated to the
    ``CloudStorageBackend`` instance passed to the constructor.

    The remote prefix is resolved using the same template logic as
    :class:`~bsp.deployer.ArtifactDeployer` so that ``gather`` and ``deploy``
    always refer to the same storage location.

    Args:
        deploy_config: Deployment configuration (prefix template, container, etc.)
        storage_backend: Concrete ``CloudStorageBackend`` to use for downloads.
    """

    def __init__(
        self,
        deploy_config: DeployConfig,
        storage_backend: CloudStorageBackend,
    ):
        self.config = deploy_config
        self.backend = storage_backend
        self.logger = logging.getLogger(self.__class__.__name__)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compose_remote_prefix(
        self,
        device: str = "",
        release: str = "",
        distro: str = "",
        vendor: str = "",
        date_override: Optional[str] = None,
    ) -> str:
        """
        Build the remote path prefix from the ``DeployConfig.prefix`` template.

        Supported placeholders: ``{device}``, ``{release}``, ``{distro}``,
        ``{vendor}``, ``{date}`` (``YYYY-MM-DD``), ``{datetime}``
        (``YYYYMMDD-HHMMSS``).

        Args:
            device: Device slug.
            release: Release slug.
            distro: Effective distro slug.
            vendor: Board vendor slug.
            date_override: When provided, used as the ``{date}`` value instead
                           of today's date.  Useful for fetching artifacts
                           produced on a specific day.

        Returns:
            Resolved prefix string (no leading or trailing slash).
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        template = self.config.prefix or "{vendor}/{device}/{release}/{date}"
        prefix = template.format(
            device=device or "unknown",
            release=release or "unknown",
            distro=distro or "unknown",
            vendor=vendor or "unknown",
            date=date_override if date_override is not None else now.strftime("%Y-%m-%d"),
            datetime=now.strftime("%Y%m%d-%H%M%S"),
        )
        return prefix.strip("/")

    def gather(
        self,
        dest_dir: str,
        device: str = "",
        release: str = "",
        distro: str = "",
        vendor: str = "",
        date_override: Optional[str] = None,
    ) -> GatherResult:
        """
        Download all artifacts for the given BSP metadata from cloud storage.

        The method first attempts to locate a ``manifest.json`` uploaded by
        :class:`~bsp.deployer.ArtifactDeployer`.  When found, the manifest's
        artifact list is used directly (avoiding a full blob listing).  When
        no manifest exists the method falls back to listing all blobs under the
        resolved prefix via :meth:`~bsp.storage.base.CloudStorageBackend.list_artifacts`.

        Args:
            dest_dir: Local directory to write downloaded artifacts into.
                      Created automatically if it does not exist.
            device: Device slug (used for prefix expansion).
            release: Release slug.
            distro: Effective distro slug.
            vendor: Board vendor slug.
            date_override: Override for the ``{date}`` placeholder in the
                           prefix template (``YYYY-MM-DD``).  Defaults to
                           today's date when ``None``.

        Returns:
            :class:`GatherResult` with the local paths of every downloaded
            file and the parsed manifest (if available).
        """
        result = GatherResult(dry_run=self.backend.dry_run, dest_dir=Path(dest_dir))
        prefix = self.compose_remote_prefix(
            device=device,
            release=release,
            distro=distro,
            vendor=vendor,
            date_override=date_override,
        )

        self.logger.info(
            "Gathering artifacts for device=%s release=%s from prefix '%s'",
            device or "unknown",
            release or "unknown",
            prefix,
        )

        if self.backend.dry_run:
            self.logger.info("[dry-run] Would download artifacts from '%s' → %s", prefix, dest_dir)
            return result

        # Try manifest-guided download first
        manifest = self.backend.get_manifest(prefix)
        if manifest is not None:
            result.manifest = manifest
            result.artifacts = self._download_from_manifest(manifest, dest_dir, prefix)
        else:
            # Fall back to listing all blobs under the prefix
            result.artifacts = self.backend.download_prefix(prefix, Path(dest_dir))

        self.logger.info(
            "Gathered %d artifact(s) into %s",
            len(result.artifacts),
            dest_dir,
        )
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _download_from_manifest(
        self,
        manifest: Dict,
        dest_dir: str,
        prefix: str,
    ) -> List[Path]:
        """Download the artifacts listed in *manifest* to *dest_dir*."""
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        downloaded: List[Path] = []

        for entry in manifest.get("artifacts", []):
            name = entry.get("name")
            if not name:
                continue
            remote_path = f"{prefix}/{name}"
            local_path = dest / name
            try:
                self.backend.download_file(remote_path, local_path)
                downloaded.append(local_path)
            except Exception as exc:  # noqa: BLE001
                self.logger.error("Failed to download %s: %s", remote_path, exc)

        return downloaded
