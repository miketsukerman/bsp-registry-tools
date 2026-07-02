"""
Artifact deployer: discovers and uploads Yocto build artifacts to cloud storage.
"""

import datetime
import hashlib
import json
import logging
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import DeployConfig
from .storage.base import CloudStorageBackend


# =============================================================================
# Deploy result
# =============================================================================


@dataclass
class UploadedArtifact:
    """Metadata for a single uploaded artifact."""
    local_path: Path
    remote_url: str
    size_bytes: int
    sha256: str


@dataclass
class UploadedCache:
    """Metadata for a single uploaded Yocto cache archive."""
    cache_type: str       # "downloads" or "sstate"
    local_archive: Path
    remote_url: str
    size_bytes: int
    sha256: str


@dataclass
class DeployResult:
    """Result of a full deployment run."""
    artifacts: List[UploadedArtifact] = field(default_factory=list)
    cache_uploads: List[UploadedCache] = field(default_factory=list)
    manifest_url: Optional[str] = None
    dry_run: bool = False

    @property
    def total_bytes(self) -> int:
        return sum(a.size_bytes for a in self.artifacts)

    @property
    def success_count(self) -> int:
        return len(self.artifacts)


# =============================================================================
# ArtifactDeployer
# =============================================================================


class ArtifactDeployer:
    """
    Discovers Yocto build artifacts and uploads them to a cloud storage backend.

    This class is provider-agnostic; all cloud interaction is delegated to the
    ``CloudStorageBackend`` instance passed to the constructor.

    Args:
        deploy_config: Deployment configuration (patterns, dirs, prefix, etc.)
        storage_backend: Concrete ``CloudStorageBackend`` to use for uploads.
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

    def collect_artifacts(self, build_path: str) -> List[Path]:
        """
        Find all artifact files under *build_path* that match the configured
        patterns and artifact directories.

        Args:
            build_path: Top-level build output directory (e.g.
                        ``"build/poky/my-device/scarthgap"``).

        Returns:
            Deduplicated, sorted list of matching ``Path`` objects.
        """
        build_root = Path(build_path)
        found: List[Path] = []
        seen = set()

        for artifact_dir in self.config.artifact_dirs:
            search_dir = build_root / artifact_dir
            if not search_dir.is_dir():
                self.logger.debug("Artifact dir not found, skipping: %s", search_dir)
                continue
            for pattern in self.config.patterns:
                for match in sorted(search_dir.glob(pattern)):
                    if match.is_file() and match not in seen:
                        found.append(match)
                        seen.add(match)

        self.logger.info("Collected %d artifact(s) from %s", len(found), build_path)
        return found

    def compose_remote_prefix(
        self,
        device: str = "",
        release: str = "",
        distro: str = "",
        vendor: str = "",
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
            date=now.strftime("%Y-%m-%d"),
            datetime=now.strftime("%Y%m%d-%H%M%S"),
        )
        return prefix.strip("/")

    def compose_archive_name(
        self,
        device: str = "",
        release: str = "",
        distro: str = "",
        vendor: str = "",
    ) -> str:
        """
        Build the archive filename (without extension) from the
        ``DeployConfig.archive.name`` template.

        Supports the same placeholders as :meth:`compose_remote_prefix`.

        Args:
            device: Device slug.
            release: Release slug.
            distro: Effective distro slug.
            vendor: Board vendor slug.

        Returns:
            Resolved archive base-name string.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        archive = self.config.archive
        template = archive.name if archive else "artifacts-{device}-{date}"
        name = template.format(
            device=device or "unknown",
            release=release or "unknown",
            distro=distro or "unknown",
            vendor=vendor or "unknown",
            date=now.strftime("%Y-%m-%d"),
            datetime=now.strftime("%Y%m%d-%H%M%S"),
        )
        return name.strip("/")

    def deploy(
        self,
        build_path: str,
        device: str = "",
        release: str = "",
        distro: str = "",
        vendor: str = "",
        downloads_path: Optional[str] = None,
        sstate_path: Optional[str] = None,
    ) -> DeployResult:
        """
        Collect and upload all matching artifacts.

        When ``DeployConfig.yocto_cache`` is enabled and *downloads_path* /
        *sstate_path* point to existing directories the corresponding cache
        directories are packed into ``tar.gz`` archives and uploaded under
        ``{prefix}/cache/``.

        Args:
            build_path: Top-level Yocto build output directory.
            device: Device slug (used for prefix expansion).
            release: Release slug.
            distro: Effective distro slug.
            vendor: Board vendor slug.
            downloads_path: Optional absolute path to the ``DL_DIR`` cache
                            directory.  Passed by :class:`~bsp.bsp_manager.BspManager`
                            when Yocto cache upload is enabled.
            sstate_path: Optional absolute path to the ``SSTATE_DIR`` cache
                         directory.

        Returns:
            ``DeployResult`` with metadata for every uploaded artifact and,
            when ``include_manifest`` is enabled, the manifest URL.
        """
        result = DeployResult(dry_run=self.backend.dry_run)
        artifacts = self.collect_artifacts(build_path)

        if not artifacts:
            print(f"No artifacts found in '{build_path}'. Nothing to deploy.")
            return result

        prefix = self.compose_remote_prefix(
            device=device, release=release, distro=distro, vendor=vendor
        )
        action_verb = "[dry-run] Would upload" if self.backend.dry_run else "Deploying"
        print(
            f"{action_verb} {len(artifacts)} artifact(s) to {self.config.provider} "
            f"under prefix '{prefix}'..."
        )

        failed: List[Tuple[Path, Exception]] = []

        if self.config.archive:
            # Bundle all artifacts into a single compressed archive before upload.
            archive_basename = self.compose_archive_name(
                device=device, release=release, distro=distro, vendor=vendor
            )
            print(f"  Creating archive {archive_basename}...")
            tmp_archive = self._create_archive(
                artifacts,
                archive_basename,
                self.config.archive.format,
            )
            try:
                archive_remote = f"{prefix}/{tmp_archive.name}"
                print(f"  Uploading {tmp_archive.name}...")
                url = self.backend.upload_file(tmp_archive, archive_remote)
                size = tmp_archive.stat().st_size if not self.backend.dry_run else 0
                sha = self._sha256(tmp_archive) if not self.backend.dry_run else ""
                result.artifacts.append(
                    UploadedArtifact(
                        local_path=tmp_archive,
                        remote_url=url,
                        size_bytes=size,
                        sha256=sha,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.error("Failed to upload archive %s: %s", tmp_archive, exc)
                failed.append((tmp_archive, exc))
            finally:
                tmp_archive.unlink(missing_ok=True)
                shutil.rmtree(tmp_archive.parent, ignore_errors=True)
        else:
            for local_path in artifacts:
                rel = local_path.name
                remote_path = f"{prefix}/{rel}"
                print(f"  Uploading {rel}...")
                try:
                    url = self.backend.upload_file(local_path, remote_path)
                    size = local_path.stat().st_size if not self.backend.dry_run else 0
                    sha = self._sha256(local_path) if not self.backend.dry_run else ""
                    result.artifacts.append(
                        UploadedArtifact(
                            local_path=local_path,
                            remote_url=url,
                            size_bytes=size,
                            sha256=sha,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    self.logger.error("Failed to upload %s: %s", local_path, exc)
                    failed.append((local_path, exc))

        if failed:
            self.logger.warning(
                "%d upload(s) failed out of %d total.",
                len(failed),
                len(artifacts),
            )

        # --- Yocto cache upload ---
        cache_cfg = self.config.yocto_cache
        if cache_cfg and cache_cfg.enabled:
            result.cache_uploads = self._upload_caches(
                prefix=prefix,
                downloads_path=downloads_path,
                sstate_path=sstate_path,
                include_downloads=cache_cfg.downloads,
                include_sstate=cache_cfg.sstate,
            )

        if self.config.include_manifest and result.artifacts:
            manifest_url = self._upload_manifest(result, prefix, device, release, distro, vendor)
            result.manifest_url = manifest_url

        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def generate_manifest(
        self,
        result: DeployResult,
        device: str = "",
        release: str = "",
        distro: str = "",
        vendor: str = "",
    ) -> str:
        """
        Build a JSON manifest describing all uploaded artifacts and, when
        present, any uploaded Yocto cache archives.

        Args:
            result: Completed ``DeployResult``.
            device: Device slug.
            release: Release slug.
            distro: Effective distro slug.
            vendor: Board vendor slug.

        Returns:
            JSON string.
        """
        manifest: Dict = {
            "schema_version": "1",
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "build": {
                "device": device,
                "release": release,
                "distro": distro,
                "vendor": vendor,
            },
            "provider": self.config.provider,
            "dry_run": result.dry_run,
            "artifacts": [
                {
                    "name": a.local_path.name,
                    "remote_url": a.remote_url,
                    "size_bytes": a.size_bytes,
                    "sha256": a.sha256,
                }
                for a in result.artifacts
            ],
            "total_size_bytes": result.total_bytes,
        }

        if result.cache_uploads:
            manifest["yocto_cache"] = {
                uc.cache_type: {
                    "name": uc.local_archive.name,
                    "remote_url": uc.remote_url,
                    "size_bytes": uc.size_bytes,
                    "sha256": uc.sha256,
                }
                for uc in result.cache_uploads
            }

        return json.dumps(manifest, indent=2)

    def _upload_caches(
        self,
        prefix: str,
        downloads_path: Optional[str],
        sstate_path: Optional[str],
        include_downloads: bool = True,
        include_sstate: bool = True,
    ) -> List["UploadedCache"]:
        """
        Pack and upload Yocto cache directories.

        Each enabled cache directory that exists on disk is packed into a
        ``tar.gz`` archive and uploaded under ``{prefix}/cache/``.  Missing or
        disabled cache directories are silently skipped.

        Args:
            prefix: Resolved remote path prefix (e.g. ``"acme/myboard/scarthgap/2025-01-15"``).
            downloads_path: Local path to ``DL_DIR``.
            sstate_path: Local path to ``SSTATE_DIR``.
            include_downloads: Whether to upload the downloads cache.
            include_sstate: Whether to upload the sstate cache.

        Returns:
            List of :class:`UploadedCache` entries for each successfully
            uploaded cache archive.
        """
        cache_prefix = f"{prefix}/cache"
        uploaded: List[UploadedCache] = []

        candidates = []
        if include_downloads and downloads_path:
            candidates.append(("downloads", downloads_path))
        if include_sstate and sstate_path:
            candidates.append(("sstate", sstate_path))

        for cache_type, local_dir in candidates:
            dir_path = Path(local_dir)
            if not dir_path.is_dir():
                self.logger.info(
                    "Yocto cache dir not found, skipping upload: %s (%s)",
                    local_dir, cache_type,
                )
                continue

            archive_name = f"{cache_type}.tar.gz"
            remote_path = f"{cache_prefix}/{archive_name}"

            if self.backend.dry_run:
                print(
                    f"[dry-run] Would pack and upload Yocto {cache_type} cache: "
                    f"{local_dir} → {remote_path}"
                )
                uploaded.append(
                    UploadedCache(
                        cache_type=cache_type,
                        local_archive=dir_path / archive_name,
                        remote_url=f"dry-run:{remote_path}",
                        size_bytes=0,
                        sha256="",
                    )
                )
                continue

            tmp_dir = Path(tempfile.mkdtemp(prefix="bsp_cache_"))
            archive_path = tmp_dir / archive_name
            try:
                print(f"  Packing Yocto {cache_type} cache: {local_dir} → {archive_path}")
                with tarfile.open(archive_path, "w:gz") as tar:
                    tar.add(str(dir_path), arcname=cache_type)

                print(
                    f"  Uploading Yocto {cache_type} cache archive "
                    f"({archive_path.stat().st_size} bytes) → {remote_path}"
                )
                url = self.backend.upload_file(archive_path, remote_path)
                size = archive_path.stat().st_size
                sha = self._sha256(archive_path)
                uploaded.append(
                    UploadedCache(
                        cache_type=cache_type,
                        local_archive=archive_path,
                        remote_url=url,
                        size_bytes=size,
                        sha256=sha,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.error(
                    "Failed to upload Yocto %s cache: %s", cache_type, exc
                )
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        return uploaded

    def _upload_manifest(
        self,
        result: DeployResult,
        prefix: str,
        device: str,
        release: str,
        distro: str,
        vendor: str,
    ) -> Optional[str]:
        """Generate and upload the JSON manifest; return its remote URL."""
        manifest_json = self.generate_manifest(
            result, device=device, release=release, distro=distro, vendor=vendor
        )
        remote_manifest = f"{prefix}/manifest.json"

        if self.backend.dry_run:
            self.logger.info("[dry-run] Would upload manifest → %s", remote_manifest)
            return f"dry-run:{remote_manifest}"

        try:
            # Write to a temp file then upload
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, prefix="bsp_manifest_"
            ) as fh:
                fh.write(manifest_json)
                tmp_path = Path(fh.name)
            url = self.backend.upload_file(tmp_path, remote_manifest)
            tmp_path.unlink(missing_ok=True)
            return url
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Failed to upload manifest: %s", exc)
            return None

    @staticmethod
    def _sha256(path: Path) -> str:
        """Return the hex SHA-256 digest of a file."""
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _create_archive(
        files: List[Path],
        basename: str,
        fmt: str,
    ) -> Path:
        """
        Pack *files* into a temporary compressed archive and return its path.

        The caller is responsible for deleting the returned file **and its
        parent temporary directory** when done.

        Args:
            files: Ordered list of files to include.
            basename: Archive file name without extension
                      (e.g. ``"firmware-my-device-2024-01-15"``).
            fmt: Archive format.  Supported: ``"tar.gz"``,
                 ``"tar.bz2"``, ``"tar.xz"``, ``"zip"``.

        Returns:
            ``Path`` to the created archive file inside a temporary directory.

        Raises:
            ValueError: If *fmt* is not a recognised format.
        """
        _TAR_MODES = {
            "tar.gz": ("gz", ".tar.gz"),
            "tar.bz2": ("bz2", ".tar.bz2"),
            "tar.xz": ("xz", ".tar.xz"),
        }

        fmt_lower = fmt.lower()
        tmp_dir = Path(tempfile.mkdtemp(prefix="bsp_archive_"))
        try:
            if fmt_lower in _TAR_MODES:
                mode, ext = _TAR_MODES[fmt_lower]
                archive_path = tmp_dir / f"{basename}{ext}"
                with tarfile.open(archive_path, f"w:{mode}") as tar:
                    for file_path in files:
                        tar.add(file_path, arcname=file_path.name)
            elif fmt_lower == "zip":
                archive_path = tmp_dir / f"{basename}.zip"
                with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for file_path in files:
                        zf.write(file_path, arcname=file_path.name)
            else:
                raise ValueError(
                    f"Unsupported archive format '{fmt}'. "
                    "Choose one of: tar.gz, tar.bz2, tar.xz, zip."
                )
        except Exception:
            # Clean up the temp dir if archive creation fails.
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        return archive_path
