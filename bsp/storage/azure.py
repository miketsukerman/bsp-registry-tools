"""
Azure Blob Storage backend for cloud artifact deployment.
"""

import logging
import os
from pathlib import Path
from typing import List, Optional

from .base import CloudStorageBackend

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
        with open(local_path, "rb") as data:
            container_client.upload_blob(name=remote_path, data=data, overwrite=True)

        return self.get_upload_url(remote_path)

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

    def get_upload_url(self, remote_path: str) -> str:
        """Return the blob URL for *remote_path*."""
        if self._client is None:
            return f"dry-run:{remote_path}"
        account_url = self._client.url.rstrip("/")
        return f"{account_url}/{self.container_name}/{remote_path}"
