"""
Azure Blob Storage backend for cloud artifact deployment.
"""

import datetime
import logging
import mimetypes
import os
from pathlib import Path
from typing import List, Optional

from .base import CloudStorageBackend

#: Far-future SAS expiry (32-bit ``time_t`` limit) used by default for
#: account-key signed URLs.
DEFAULT_SAS_EXPIRY = "2038-01-19T03:14:06Z"

#: Azure caps user-delegation key lifetimes at 7 days.
MAX_USER_DELEGATION_DAYS = 7

_INSTALL_HINT = (
    "Install the Azure extras to use this backend:\n"
    "  pip install 'bsp-registry-tools[azure]'\n"
    "or individually:\n"
    "  pip install azure-storage-blob azure-identity\n\n"
    "Authentication options (in order of precedence):\n"
    "  1. Set AZURE_STORAGE_CONNECTION_STRING environment variable\n"
    "  2. Set AZURE_STORAGE_ACCOUNT_URL + any credential env var\n"
    "     (AZURE_CLIENT_ID/SECRET/TENANT for service principal)\n"
    "  3. Run 'az login' for interactive Azure CLI authentication\n"
    "  4. Pass account_url + credential to AzureStorageBackend()"
)


class AzureStorageBackend(CloudStorageBackend):
    """
    Cloud storage backend for Azure Blob Storage.

    Authentication is resolved in this order:
    1. ``connection_string`` constructor argument
    2. ``AZURE_STORAGE_CONNECTION_STRING`` environment variable
    3. ``account_url`` constructor argument or ``AZURE_STORAGE_ACCOUNT_URL``
       env var combined with ``DefaultAzureCredential`` (supports env vars,
       Managed Identity, Azure CLI, and more).

    Args:
        container_name: Azure Blob container name.
        account_url: Storage account URL
                     (e.g. ``https://<account>.blob.core.windows.net``).
                     Takes precedence over ``AZURE_STORAGE_ACCOUNT_URL``.
        connection_string: Full connection string.  When provided,
                           ``account_url`` and ``credential`` are ignored.
        credential: Pre-constructed credential object accepted by
                    ``BlobServiceClient``.  When ``None`` the
                    ``DefaultAzureCredential`` is used.
        dry_run: Log uploads instead of executing them.
    """

    def __init__(
        self,
        container_name: str,
        account_url: Optional[str] = None,
        connection_string: Optional[str] = None,
        credential=None,
        dry_run: bool = False,
    ):
        super().__init__(dry_run=dry_run)
        self.container_name = container_name
        self._client = None
        self._account_key: Optional[str] = None

        if dry_run:
            # Skip SDK imports / credential resolution in dry-run mode
            return

        try:
            from azure.storage.blob import BlobServiceClient  # type: ignore[import]
        except ImportError:
            raise ImportError(
                "azure-storage-blob is not installed.\n" + _INSTALL_HINT
            ) from None

        conn_str = connection_string or os.environ.get(
            "AZURE_STORAGE_CONNECTION_STRING"
        )
        if conn_str:
            self._client = BlobServiceClient.from_connection_string(conn_str)
            self._account_key = self._parse_account_key(conn_str)
        else:
            url = account_url or os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
            if not url:
                raise ValueError(
                    "No Azure credentials found.\n" + _INSTALL_HINT
                )
            if credential is None:
                try:
                    from azure.identity import DefaultAzureCredential  # type: ignore[import]
                except ImportError:
                    raise ImportError(
                        "azure-identity is not installed.\n" + _INSTALL_HINT
                    ) from None
                credential = DefaultAzureCredential()
            self._client = BlobServiceClient(account_url=url, credential=credential)

    # ------------------------------------------------------------------

    def upload_file(self, local_path: Path, remote_path: str) -> str:
        """Upload *local_path* as blob *remote_path* in the configured container."""
        local_path = Path(local_path)
        if self.dry_run:
            self.logger.info("[dry-run] Would upload %s → %s", local_path, remote_path)
            return f"dry-run:{remote_path}"

        self.logger.info("Uploading %s → azure://%s/%s", local_path, self.container_name, remote_path)
        container_client = self._client.get_container_client(self.container_name)
        kwargs = {}
        content_settings = self._content_settings_for(remote_path)
        if content_settings is not None:
            kwargs["content_settings"] = content_settings
        with open(local_path, "rb") as data:
            container_client.upload_blob(
                name=remote_path, data=data, overwrite=True, **kwargs
            )

        return self.get_upload_url(remote_path)

    def _content_settings_for(self, remote_path: str):
        """
        Build ``ContentSettings`` for *remote_path*.

        Text-ish assets (``index.html``, ``manifest.json``) get their guessed
        MIME type so browsers render them.  Everything else is forced to
        ``application/octet-stream`` with **no** ``content_encoding``: for
        ``*.wic.gz`` ``mimetypes`` reports ``content_encoding="gzip"``, which
        makes browsers transparently decompress the image on download and
        corrupt it.
        """
        try:
            from azure.storage.blob import ContentSettings  # type: ignore[import]
        except ImportError:  # pragma: no cover - SDK guaranteed present here
            return None

        content_type, _encoding = mimetypes.guess_type(remote_path)
        if content_type in ("text/html", "application/json", "text/plain"):
            return ContentSettings(content_type=content_type)
        return ContentSettings(content_type="application/octet-stream")

    def download_file(self, remote_path: str, local_path: Path) -> None:
        """Download blob *remote_path* from the configured container to *local_path*."""
        local_path = Path(local_path)
        if self.dry_run:
            self.logger.info("[dry-run] Would download azure://%s/%s → %s", self.container_name, remote_path, local_path)
            return

        self.logger.info("Downloading azure://%s/%s → %s", self.container_name, remote_path, local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        blob_client = self._client.get_blob_client(container=self.container_name, blob=remote_path)
        with open(local_path, "wb") as fh:
            blob_client.download_blob().readinto(fh)

    def list_artifacts(self, remote_prefix: str) -> List[str]:
        """List blob names under *remote_prefix* in the configured container."""
        if self.dry_run:
            self.logger.info("[dry-run] Would list azure://%s/%s", self.container_name, remote_prefix)
            return []
        container_client = self._client.get_container_client(self.container_name)
        return [
            blob.name
            for blob in container_client.list_blobs(name_starts_with=remote_prefix)
        ]

    # ------------------------------------------------------------------
    # Signed (SAS) URLs
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_account_key(connection_string: str) -> Optional[str]:
        """Extract ``AccountKey`` from a connection string (never logged)."""
        for part in connection_string.split(";"):
            key, _, value = part.partition("=")
            if key.strip() == "AccountKey":
                return value.strip()
        return None

    @staticmethod
    def _parse_expiry(expiry) -> datetime.datetime:
        """Normalize *expiry* (ISO-8601 string or datetime) to an aware UTC datetime."""
        if expiry is None:
            expiry = DEFAULT_SAS_EXPIRY
        if isinstance(expiry, datetime.datetime):
            dt = expiry
        else:
            text = str(expiry).strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)

    def get_signed_url(self, remote_path: str, expiry=None) -> str:
        """
        Return a read-only SAS URL for *remote_path*.

        A **user-delegation SAS** is preferred when the backend is
        authenticated with ``DefaultAzureCredential``; Azure caps the
        delegation key lifetime at 7 days, so longer expiries are clamped
        (with a warning) rather than rejected.  When an account key is
        available (connection string) an **account-key SAS** is generated
        instead, which supports arbitrary expiry.

        Args:
            remote_path: Blob name inside the configured container.
            expiry: Expiry as an ISO-8601 string or ``datetime``.  Defaults
                    to :data:`DEFAULT_SAS_EXPIRY`.

        Returns:
            Fully-qualified HTTPS URL including the SAS token, or a
            ``"dry-run:<remote_path>"`` placeholder in dry-run mode.
        """
        if self.dry_run or self._client is None:
            return f"dry-run:{remote_path}"

        from azure.storage.blob import BlobSasPermissions, generate_blob_sas  # type: ignore[import]

        expiry_dt = self._parse_expiry(expiry)
        base_url = self.get_upload_url(remote_path)
        account_name = self._client.account_name

        sas_kwargs = dict(
            account_name=account_name,
            container_name=self.container_name,
            blob_name=remote_path,
            permission=BlobSasPermissions(read=True),
            expiry=expiry_dt,
            https_only=True,
        )

        if self._account_key:
            token = generate_blob_sas(account_key=self._account_key, **sas_kwargs)
        else:
            now = datetime.datetime.now(datetime.timezone.utc)
            max_expiry = now + datetime.timedelta(days=MAX_USER_DELEGATION_DAYS)
            if expiry_dt > max_expiry:
                self.logger.warning(
                    "Requested SAS expiry %s exceeds the Azure user-delegation "
                    "limit of %d days; clamping to %s.",
                    expiry_dt.isoformat(), MAX_USER_DELEGATION_DAYS,
                    max_expiry.isoformat(),
                )
                expiry_dt = max_expiry
                sas_kwargs["expiry"] = expiry_dt
            start = now - datetime.timedelta(minutes=5)
            delegation_key = self._client.get_user_delegation_key(start, expiry_dt)
            token = generate_blob_sas(
                user_delegation_key=delegation_key, **sas_kwargs
            )

        return f"{base_url}?{token}"

    def get_upload_url(self, remote_path: str) -> str:
        """Return the blob URL for *remote_path*."""
        if self._client is None:
            return f"dry-run:{remote_path}"
        account_url = self._client.url.rstrip("/")
        return f"{account_url}/{self.container_name}/{remote_path}"
