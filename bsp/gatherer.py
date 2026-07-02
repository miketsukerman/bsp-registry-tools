"""
Artifact gatherer: downloads BSP build artifacts from cloud storage.

This is the download counterpart to :mod:`bsp.deployer`.
"""

import datetime
import logging
import shutil
import tarfile
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
    cache_artifacts: List[Path] = field(default_factory=list)
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
        gather_cache: bool = False,
        downloads_dest: Optional[str] = None,
        sstate_dest: Optional[str] = None,
    ) -> GatherResult:
        """
        Download all artifacts for the given BSP metadata from cloud storage.

        The method first attempts to locate a ``manifest.json`` uploaded by
        :class:`~bsp.deployer.ArtifactDeployer`.  When found, the manifest's
        artifact list is used directly (avoiding a full blob listing).  When
        no manifest exists the method falls back to listing all blobs under the
        resolved prefix via :meth:`~bsp.storage.base.CloudStorageBackend.list_artifacts`.

        When *gather_cache* is ``True`` the method also attempts to download
        and extract any Yocto cache archives (``downloads.tar.gz`` /
        ``sstate.tar.gz``) that were previously uploaded by
        :class:`~bsp.deployer.ArtifactDeployer`.  A missing cache is treated
        as a soft warning — it does **not** cause the overall gather to fail.

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
            gather_cache: When ``True``, attempt to download and restore Yocto
                          cache archives alongside the regular artifacts.
            downloads_dest: Local path to extract the ``downloads`` cache into.
                            When ``None``, defaults to a ``downloads/``
                            subdirectory inside *dest_dir*.
            sstate_dest: Local path to extract the ``sstate`` cache into.
                         When ``None``, defaults to a ``sstate/`` subdirectory
                         inside *dest_dir*.

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

        print(
            f"Gathering artifacts for device={device or 'unknown'} "
            f"release={release or 'unknown'} from prefix '{prefix}'..."
        )

        if self.backend.dry_run:
            print(f"[dry-run] Would download artifacts from '{prefix}' → {dest_dir}")
            if gather_cache:
                print(
                    f"[dry-run] Would restore Yocto caches from '{prefix}/cache/' if available"
                )
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

        # --- Optional Yocto cache restore ---
        if gather_cache:
            result.cache_artifacts = self._restore_caches(
                manifest=result.manifest,
                prefix=prefix,
                dest_dir=dest_dir,
                downloads_dest=downloads_dest,
                sstate_dest=sstate_dest,
            )

        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _restore_caches(
        self,
        manifest: Optional[Dict],
        prefix: str,
        dest_dir: str,
        downloads_dest: Optional[str],
        sstate_dest: Optional[str],
    ) -> List[Path]:
        """
        Download and extract Yocto cache archives if available.

        Archives are located from the manifest ``yocto_cache`` section when
        present, or discovered heuristically under ``{prefix}/cache/``.  A
        missing cache is silently skipped (info-level log only).

        Args:
            manifest: Parsed manifest dict (may be ``None``).
            prefix: Resolved remote path prefix.
            dest_dir: Fallback base directory used when *downloads_dest* /
                      *sstate_dest* are ``None``.
            downloads_dest: Local directory to restore the downloads cache into.
            sstate_dest: Local directory to restore the sstate cache into.

        Returns:
            List of local paths for extracted files (may be empty).
        """
        extracted: List[Path] = []
        cache_prefix = f"{prefix}/cache"

        # Build (cache_type, remote_path, local_dest) triples
        cache_specs = [
            (
                "downloads",
                f"{cache_prefix}/downloads.tar.gz",
                downloads_dest or str(Path(dest_dir) / "downloads"),
            ),
            (
                "sstate",
                f"{cache_prefix}/sstate.tar.gz",
                sstate_dest or str(Path(dest_dir) / "sstate"),
            ),
        ]

        # If the manifest has yocto_cache metadata, use that for the remote URLs
        manifest_cache: Dict = (manifest or {}).get("yocto_cache", {})

        for cache_type, default_remote, local_dest in cache_specs:
            # Prefer manifest-provided URL, fall back to heuristic path
            remote_url = manifest_cache.get(cache_type, {}).get("remote_url")
            if remote_url and not remote_url.startswith("dry-run:"):
                # Strip bucket/container prefix from full URL to get the key/path
                # For manifest-guided downloads, fall back to the key embedded in name
                remote_path = manifest_cache[cache_type].get("name")
                if remote_path:
                    remote_path = f"{cache_prefix}/{remote_path}"
                else:
                    remote_path = default_remote
            else:
                remote_path = default_remote

            tmp_archive = Path(local_dest) / f"_bsp_{cache_type}.tar.gz"
            try:
                Path(local_dest).mkdir(parents=True, exist_ok=True)
                self.logger.info(
                    "Downloading Yocto %s cache: %s → %s",
                    cache_type, remote_path, tmp_archive,
                )
                self.backend.download_file(remote_path, tmp_archive)
            except Exception as exc:  # noqa: BLE001
                self.logger.info(
                    "Yocto %s cache not available (skipping): %s", cache_type, exc
                )
                tmp_archive.unlink(missing_ok=True)
                continue

            # Extract the archive in-place
            try:
                self.logger.info(
                    "Extracting Yocto %s cache → %s", cache_type, local_dest
                )
                with tarfile.open(tmp_archive, "r:gz") as tar:
                    # Guard against path traversal: only extract members
                    # whose resolved path stays inside the destination directory.
                    dest_resolved = Path(local_dest).resolve()
                    for member in tar.getmembers():
                        member_path = (dest_resolved / member.name).resolve()
                        if not str(member_path).startswith(str(dest_resolved)):
                            self.logger.warning(
                                "Skipping potentially unsafe tar member: %s",
                                member.name,
                            )
                            continue
                        tar.extract(member, local_dest)  # noqa: S202
                extracted.append(Path(local_dest))
                self.logger.info(
                    "Restored Yocto %s cache into %s", cache_type, local_dest
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning(
                    "Failed to extract Yocto %s cache: %s", cache_type, exc
                )
            finally:
                tmp_archive.unlink(missing_ok=True)

        return extracted

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
            print(f"  Downloading {name}...")
            try:
                self.backend.download_file(remote_path, local_path)
                downloaded.append(local_path)
            except Exception as exc:  # noqa: BLE001
                self.logger.error("Failed to download %s: %s", remote_path, exc)

        return downloaded
