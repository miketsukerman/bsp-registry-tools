"""
Factory function for creating cloud storage backends.
"""

from typing import Any

from .base import CloudStorageBackend


def create_backend(provider: str, **kwargs: Any) -> CloudStorageBackend:
    """
    Create and return a ``CloudStorageBackend`` for the given *provider*.

    Args:
        provider: Storage provider name.  Supported values:
                  ``"azure"`` (default) and ``"aws"``.
        **kwargs: Provider-specific keyword arguments forwarded to the
                  backend constructor.

                  Azure (``AzureStorageBackend``):
                    - ``container_name`` (str, required)
                    - ``account_url`` (str, optional)
                    - ``connection_string`` (str, optional)
                    - ``credential`` (optional)
                    - ``dry_run`` (bool, default ``False``)

                  AWS (``AwsStorageBackend``):
                    - ``bucket_name`` (str, required)
                    - ``region`` (str, optional)
                    - ``profile`` (str, optional)
                    - ``dry_run`` (bool, default ``False``)

    Returns:
        Configured ``CloudStorageBackend`` instance.

    Raises:
        ValueError: If *provider* is not a supported value.
    """
    provider = provider.lower().strip()

    if provider == "azure":
        from .azure import AzureStorageBackend
        return AzureStorageBackend(**kwargs)

    if provider == "aws":
        from .aws import AwsStorageBackend
        return AwsStorageBackend(**kwargs)

    raise ValueError(
        f"Unknown storage provider '{provider}'. "
        "Supported providers: 'azure', 'aws'."
    )
