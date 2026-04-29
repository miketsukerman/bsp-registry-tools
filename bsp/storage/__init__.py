"""
Cloud storage backends for BSP artifact deployment.

Usage
-----
Create a backend via the factory function::

    from bsp.storage import create_backend

    backend = create_backend("azure", container_name="bsp-artifacts")
    backend = create_backend("aws", bucket_name="bsp-artifacts")

Or import a concrete class directly::

    from bsp.storage.azure import AzureStorageBackend
    from bsp.storage.aws import AwsStorageBackend
"""

from .base import CloudStorageBackend
from .factory import create_backend

__all__ = [
    "CloudStorageBackend",
    "create_backend",
]
