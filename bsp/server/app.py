"""
FastAPI application factory for the BSP registry server.

The application mounts:

* ``/api/v1/…``   — REST endpoints (see :mod:`bsp.server.rest`)
* ``/graphql``     — GraphQL endpoint powered by Strawberry (see
  :mod:`bsp.server.graphql_schema`)
* ``/``            — Redirects to ``/docs`` (Swagger UI)

Usage::

    from bsp.server import create_app
    app = create_app(registry_path="bsp-registry.yaml")

    # Run with uvicorn:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
"""

from __future__ import annotations

from typing import Optional, Union

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from ..bsp_manager import BspManager
from .graphql_schema import create_graphql_router
from .rest import router as rest_router


def create_app(
    registry_path: Optional[str] = None,
    verbose: bool = False,
    manager: Optional[BspManager] = None,
) -> FastAPI:
    """
    Create and return the FastAPI application.

    The :class:`~bsp.bsp_manager.BspManager` is initialised once and stored
    in ``app.state.bsp_manager`` so that every request handler can access it
    without recreating the (potentially expensive) registry parse.

    Args:
        registry_path: Path to the BSP registry YAML file.  Used when
            *manager* is ``None``.
        verbose: Enable verbose/debug logging inside the manager.  Only
            relevant when *registry_path* is provided and a new manager is
            created.
        manager: An already-initialised :class:`~bsp.bsp_manager.BspManager`
            instance.  When provided, *registry_path* and *verbose* are
            ignored and the manager is used as-is (no second
            ``initialize()`` call).

    Returns:
        A configured :class:`~fastapi.FastAPI` instance ready to be served.
    """
    app = FastAPI(
        title="BSP Registry Tools",
        description=(
            "HTTP API exposing BSP registry functionality "
            "(list devices, releases, features, trigger builds, export configs) "
            "via both REST and GraphQL."
        ),
        version="1.0.0",
    )

    # ------------------------------------------------------------------
    # Initialise BspManager and attach to app state
    # ------------------------------------------------------------------
    if manager is not None:
        mgr = manager
    else:
        if registry_path is None:
            raise ValueError("Either 'registry_path' or 'manager' must be provided.")
        mgr = BspManager(registry_path, verbose=verbose)
        mgr.initialize()
    app.state.bsp_manager = mgr

    # ------------------------------------------------------------------
    # Shutdown hook: clean up temp dirs etc.
    # ------------------------------------------------------------------
    @app.on_event("shutdown")
    def _shutdown():
        mgr.cleanup()

    # ------------------------------------------------------------------
    # Mount REST router
    # ------------------------------------------------------------------
    app.include_router(rest_router)

    # ------------------------------------------------------------------
    # Mount GraphQL router at /graphql
    # ------------------------------------------------------------------
    graphql_router = create_graphql_router()
    app.include_router(graphql_router, prefix="/graphql")

    # ------------------------------------------------------------------
    # Root redirect → Swagger UI
    # ------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def _root():
        return RedirectResponse(url="/docs")

    return app
