"""
AWS S3 backend for cloud artifact deployment.
"""

import os
from pathlib import Path
from typing import Dict, List, Optional

from .base import CloudStorageBackend

_INSTALL_HINT = (
    "Install the AWS extras to use this backend:\n"
    "  pip install 'bsp-registry-tools[aws]'\n"
    "or individually:\n"
    "  pip install boto3\n\n"
    "Authentication options (standard AWS credential chain):\n"
    "  1. Environment variables: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY\n"
    "  2. Shared credentials file: ~/.aws/credentials\n"
    "  3. AWS IAM role / instance profile (EC2 / ECS / Lambda)\n"
    "  4. Run 'aws configure' to set up credentials interactively"
)


#: AWS caps presigned URL lifetimes at 7 days.
_MAX_PRESIGN_SECONDS = 7 * 24 * 3600


class AwsStorageBackend(CloudStorageBackend):
    """
    Cloud storage backend for AWS S3.

    Authentication follows the standard boto3 credential chain
    (environment variables, shared credentials file, IAM role, etc.).

    Args:
        bucket_name: S3 bucket name.
        region: AWS region name (e.g. ``"us-east-1"``).  When ``None``
                the region is taken from the environment or boto3 config.
        profile: AWS credential profile name.  When ``None`` the default
                 profile is used.
        dry_run: Log uploads instead of executing them.
    """

    def __init__(
        self,
        bucket_name: str,
        region: Optional[str] = None,
        profile: Optional[str] = None,
        dry_run: bool = False,
    ):
        super().__init__(dry_run=dry_run)
        self.bucket_name = bucket_name
        self.region = region
        self._s3 = None

        if dry_run:
            return

        try:
            import boto3  # type: ignore[import]
        except ImportError:
            raise ImportError(
                "boto3 is not installed.\n" + _INSTALL_HINT
            ) from None

        session_kwargs: dict = {}
        if profile:
            session_kwargs["profile_name"] = profile
        if region:
            session_kwargs["region_name"] = region

        session = boto3.Session(**session_kwargs)
        self._s3 = session.client("s3")

    # ------------------------------------------------------------------

    def upload_file(self, local_path: Path, remote_path: str) -> str:
        """Upload *local_path* as key *remote_path* in the configured bucket."""
        local_path = Path(local_path)
        if self.dry_run:
            self.logger.info("[dry-run] Would upload %s → s3://%s/%s", local_path, self.bucket_name, remote_path)
            return f"dry-run:{remote_path}"

        self.logger.info("Uploading %s → s3://%s/%s", local_path, self.bucket_name, remote_path)
        self._s3.upload_file(str(local_path), self.bucket_name, remote_path)
        return self.get_upload_url(remote_path)

    def download_file(self, remote_path: str, local_path: Path) -> None:
        """Download key *remote_path* from the configured bucket to *local_path*."""
        local_path = Path(local_path)
        if self.dry_run:
            self.logger.info("[dry-run] Would download s3://%s/%s → %s", self.bucket_name, remote_path, local_path)
            return

        self.logger.info("Downloading s3://%s/%s → %s", self.bucket_name, remote_path, local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self._s3.download_file(self.bucket_name, remote_path, str(local_path))

    def list_artifacts(self, remote_prefix: str) -> List[str]:
        """List object keys under *remote_prefix* in the configured bucket."""
        if self.dry_run:
            self.logger.info("[dry-run] Would list s3://%s/%s", self.bucket_name, remote_prefix)
            return []

        paginator = self._s3.get_paginator("list_objects_v2")
        keys: List[str] = []
        for page in paginator.paginate(Bucket=self.bucket_name, Prefix=remote_prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys

    def list_artifacts_detailed(self, remote_prefix: str) -> List[Dict]:
        """List objects under *remote_prefix* together with size and timestamps."""
        if self.dry_run:
            self.logger.info(
                "[dry-run] Would list s3://%s/%s", self.bucket_name, remote_prefix
            )
            return []

        paginator = self._s3.get_paginator("list_objects_v2")
        records: List[Dict] = []
        for page in paginator.paginate(Bucket=self.bucket_name, Prefix=remote_prefix):
            for obj in page.get("Contents", []):
                last_modified = obj.get("LastModified")
                records.append({
                    "path": obj["Key"],
                    "size": obj.get("Size"),
                    "last_modified": last_modified.isoformat() if last_modified else None,
                    "etag": (obj.get("ETag") or "").strip('"') or None,
                })
        return records

    def get_signed_url(self, remote_path: str, expiry=None) -> str:
        """
        Return a read-only presigned HTTPS URL for *remote_path*.

        Args:
            remote_path: Object key inside the configured bucket.
            expiry: Lifetime in seconds (S3 presigned URLs are capped at
                    7 days).  Defaults to 7 days.

        Returns:
            Presigned URL, or a ``"dry-run:<remote_path>"`` placeholder in
            dry-run mode.
        """
        if self.dry_run or self._s3 is None:
            return f"dry-run:{remote_path}"

        expires_in = int(expiry) if expiry is not None else _MAX_PRESIGN_SECONDS
        expires_in = min(expires_in, _MAX_PRESIGN_SECONDS)
        return self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket_name, "Key": remote_path},
            ExpiresIn=expires_in,
        )

    def get_upload_url(self, remote_path: str) -> str:
        """Return the S3 URI for *remote_path*."""
        return f"s3://{self.bucket_name}/{remote_path}"
