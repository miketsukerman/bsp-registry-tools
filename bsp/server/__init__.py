"""
BSP Registry Tools — HTTP server package.

Exposes the BSP registry functionality via both a REST API (FastAPI) and a
GraphQL API (Strawberry), serving the same data and operations as the CLI.

Usage::

    from bsp.server import create_app
    app = create_app(registry_path="bsp-registry.yaml")

Or via the CLI::

    bsp server --host 0.0.0.0 --port 8080
"""

from .app import create_app

__all__ = ["create_app"]
