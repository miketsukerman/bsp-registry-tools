"""
Abstract base class for cloud storage backends.
"""

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List


class CloudStorageBackend(ABC):
    """
    Abstract base class for cloud storage backends.

    Concrete implementations (Azure, AWS) must implement
    ``upload_file`` and ``list_artifacts``.  The ``upload_directory``
    method is provided here and delegates to ``upload_file``.
    """

    def __init__(self, dry_run: bool = False):
        """
        Args:
            dry_run: When ``True`` no files are actually uploaded; the
                     method calls are logged but not executed.
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
