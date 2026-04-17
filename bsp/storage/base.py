"""
Abstract base class for cloud storage backends.
"""

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional


class CloudStorageBackend(ABC):
    """
    Abstract base class for cloud storage backends.

    Concrete implementations (Azure, AWS) must implement
    ``upload_file``, ``download_file``, and ``list_artifacts``.  The
    ``upload_directory`` and ``download_prefix`` methods are provided here
    and delegate to ``upload_file`` / ``download_file`` respectively.
    """

    def __init__(self, dry_run: bool = False):
        """
        Args:
            dry_run: When ``True`` no files are actually uploaded or downloaded;
                     the method calls are logged but not executed.
        """
        self.dry_run = dry_run
        self.logger = logging.getLogger(self.__class__.__name__)

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    def upload_file(self, local_path: Path, remote_path: str) -> str:
        """
        Upload a single file to cloud storage.

        Args:
            local_path: Absolute path to the local file.
            remote_path: Destination path/key inside the container or bucket.

        Returns:
            The public or SAS URL of the uploaded blob/object, or a
            ``"dry-run:<remote_path>"`` string when ``dry_run`` is ``True``.

        Raises:
            RuntimeError: On upload failure.
        """

    @abstractmethod
    def download_file(self, remote_path: str, local_path: Path) -> None:
        """
        Download a single blob/object from cloud storage.

        Args:
            remote_path: Source path/key inside the container or bucket.
            local_path: Destination path on the local filesystem.  The
                        parent directory is created automatically if it does
                        not exist.

        Raises:
            RuntimeError: On download failure.
        """

    @abstractmethod
    def list_artifacts(self, remote_prefix: str) -> List[str]:
        """
        List objects/blobs under a given remote prefix.

        Args:
            remote_prefix: Path prefix inside the container or bucket.

        Returns:
            List of remote object paths.
        """

    # ------------------------------------------------------------------
    # Concrete helpers
    # ------------------------------------------------------------------

    def get_upload_url(self, remote_path: str) -> str:
        """
        Return a human-readable URL for a remote path.

        Subclasses may override this to generate pre-signed or SAS URLs.
        The base implementation returns the remote path unchanged.

        Args:
            remote_path: Remote object path returned by ``upload_file``.

        Returns:
            URL string.
        """
        return remote_path

    def get_manifest(self, remote_prefix: str) -> Optional[Dict]:
        """
        Fetch and parse a ``manifest.json`` stored under *remote_prefix*.

        The manifest is expected at ``{remote_prefix}/manifest.json`` and must
        be valid JSON.  Returns ``None`` when the manifest is missing or cannot
        be parsed instead of raising.

        Args:
            remote_prefix: Remote prefix used during deployment (same value
                           passed to :meth:`upload_file`).

        Returns:
            Parsed manifest as a Python dict, or ``None`` if not available.
        """
        import tempfile

        if self.dry_run:
            self.logger.info("[dry-run] Would fetch manifest from %s/manifest.json", remote_prefix)
            return None

        manifest_remote = f"{remote_prefix}/manifest.json"
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".json", prefix="bsp_manifest_", delete=False
            ) as fh:
                tmp = Path(fh.name)
            self.download_file(manifest_remote, tmp)
            with open(tmp) as fh:
                return json.load(fh)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("Manifest not available at %s: %s", manifest_remote, exc)
            return None
        finally:
            tmp.unlink(missing_ok=True)

    def download_prefix(self, remote_prefix: str, dest_dir: Path) -> List[Path]:
        """
        Download all blobs under *remote_prefix* into *dest_dir*.

        Uses :meth:`list_artifacts` to enumerate the remote objects and then
        calls :meth:`download_file` for each one.  Skips ``manifest.json``
        because it is metadata, not a build artifact.

        Args:
            remote_prefix: Remote prefix to enumerate.
            dest_dir: Local directory to write files into.

        Returns:
            List of local ``Path`` objects for every downloaded file.
        """
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        downloaded: List[Path] = []

        if self.dry_run:
            self.logger.info(
                "[dry-run] Would download all blobs under %s → %s", remote_prefix, dest_dir
            )
            return downloaded

        remote_keys = self.list_artifacts(remote_prefix)
        for key in remote_keys:
            filename = Path(key).name
            if filename == "manifest.json":
                continue
            local_path = dest_dir / filename
            try:
                self.download_file(key, local_path)
                downloaded.append(local_path)
            except Exception as exc:  # noqa: BLE001
                self.logger.error("Failed to download %s: %s", key, exc)

        return downloaded

    def upload_directory(
        self,
        local_dir: Path,
        remote_prefix: str,
        glob_pattern: str = "**/*",
    ) -> List[str]:
        """
        Upload all files under *local_dir* that match *glob_pattern*.

        Args:
            local_dir: Local directory to walk.
            remote_prefix: Prefix prepended to each file's relative path
                           when composing the remote object key.
            glob_pattern: Glob pattern passed to ``Path.glob``
                          (e.g. ``"**/*.wic.gz"``).  Only matching files
                          are uploaded.

        Returns:
            List of remote URLs for every uploaded file.
        """
        uploaded: List[str] = []
        local_dir = Path(local_dir)
        if not local_dir.is_dir():
            self.logger.warning("Artifact directory does not exist: %s", local_dir)
            return uploaded

        for file_path in sorted(local_dir.glob(glob_pattern)):
            if not file_path.is_file():
                continue
            rel = file_path.relative_to(local_dir)
            remote_path = f"{remote_prefix.rstrip('/')}/{rel}"
            url = self.upload_file(file_path, remote_path)
            uploaded.append(url)

        return uploaded
