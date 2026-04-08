"""
Artifact deployer: discovers and uploads Yocto build artifacts to cloud storage.
"""

import datetime
import hashlib
import json
import logging
import tempfile
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
class DeployResult:
    """Result of a full deployment run."""
    artifacts: List[UploadedArtifact] = field(default_factory=list)
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

    def deploy(
        self,
        build_path: str,
        device: str = "",
        release: str = "",
        distro: str = "",
        vendor: str = "",
    ) -> DeployResult:
        """
        Collect and upload all matching artifacts.

        Args:
            build_path: Top-level Yocto build output directory.
            device: Device slug (used for prefix expansion).
            release: Release slug.
            distro: Effective distro slug.
            vendor: Board vendor slug.

        Returns:
            ``DeployResult`` with metadata for every uploaded artifact and,
            when ``include_manifest`` is enabled, the manifest URL.
        """
        result = DeployResult(dry_run=self.backend.dry_run)
        artifacts = self.collect_artifacts(build_path)

        if not artifacts:
            self.logger.warning("No artifacts found in '%s'. Nothing to deploy.", build_path)
            return result

        prefix = self.compose_remote_prefix(
            device=device, release=release, distro=distro, vendor=vendor
        )
        self.logger.info(
            "Deploying %d artifact(s) to %s provider under prefix '%s'",
            len(artifacts),
            self.config.provider,
            prefix,
        )

        failed: List[Tuple[Path, Exception]] = []

        for local_path in artifacts:
            rel = local_path.name
            remote_path = f"{prefix}/{rel}"
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
        Build a JSON manifest describing all uploaded artifacts.

        Args:
            result: Completed ``DeployResult``.
            device: Device slug.
            release: Release slug.
            distro: Effective distro slug.
            vendor: Board vendor slug.

        Returns:
            JSON string.
        """
        manifest = {
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
        return json.dumps(manifest, indent=2)

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
