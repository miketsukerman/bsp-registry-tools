"""
FastAPI REST router for the BSP registry.

Endpoints
---------
GET  /api/v1/bsp                  — list BSP presets
GET  /api/v1/devices              — list devices
GET  /api/v1/releases             — list releases (optional ?device=<slug>)
GET  /api/v1/features             — list features
GET  /api/v1/distros              — list distros
GET  /api/v1/frameworks           — list frameworks
GET  /api/v1/containers           — list containers
POST /api/v1/export               — export resolved BSP config as YAML
POST /api/v1/build                — trigger a BSP build (blocking)
POST /api/v1/shell                — run a command inside the build container
"""

from __future__ import annotations

import io
import subprocess
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from .types import (
    BspPresetResponse,
    BuildRequest,
    BuildResponse,
    ContainerResponse,
    DeviceResponse,
    DistroResponse,
    ExportRequest,
    ExportResponse,
    FeatureResponse,
    FrameworkResponse,
    ReleaseResponse,
    ShellCommandRequest,
    ShellCommandResponse,
    container_to_response,
    device_to_response,
    distro_to_response,
    feature_to_response,
    framework_to_response,
    preset_to_response,
    release_to_response,
)

router = APIRouter(prefix="/api/v1", tags=["bsp"])


# ---------------------------------------------------------------------------
# Helper: pull the BspManager from app state
# ---------------------------------------------------------------------------


def _get_manager(request: Request):
    """Retrieve the :class:`~bsp.bsp_manager.BspManager` stored in app state."""
    return request.app.state.bsp_manager


# ---------------------------------------------------------------------------
# Query endpoints
# ---------------------------------------------------------------------------


@router.get("/bsp", response_model=List[BspPresetResponse], summary="List BSP presets")
def list_bsp(request: Request):
    """Return all named BSP presets defined in the registry."""
    mgr = _get_manager(request)
    presets = mgr.resolver.list_presets() if mgr.resolver else []
    return [preset_to_response(p) for p in presets]


@router.get("/devices", response_model=List[DeviceResponse], summary="List devices")
def list_devices(request: Request):
    """Return all hardware device definitions."""
    mgr = _get_manager(request)
    devices = mgr.model.registry.devices if mgr.model else []
    return [device_to_response(d) for d in devices]


@router.get("/releases", response_model=List[ReleaseResponse], summary="List releases")
def list_releases(
    request: Request,
    device: Optional[str] = Query(default=None, description="Filter by device slug"),
):
    """Return all release definitions, optionally filtered by device slug."""
    mgr = _get_manager(request)
    releases = mgr.model.registry.releases if mgr.model else []

    if device:
        try:
            dev = mgr.resolver.get_device(device)
        except SystemExit:
            raise HTTPException(status_code=404, detail=f"Device '{device}' not found")
        releases = [
            r for r in releases
            if not r.vendor_overrides
            or any(vo.vendor == dev.vendor for vo in r.vendor_overrides)
        ]

    return [release_to_response(r) for r in releases]


@router.get("/features", response_model=List[FeatureResponse], summary="List features")
def list_features(request: Request):
    """Return all optional BSP feature definitions."""
    mgr = _get_manager(request)
    features = mgr.model.registry.features if mgr.model else []
    return [feature_to_response(f) for f in features]


@router.get("/distros", response_model=List[DistroResponse], summary="List distros")
def list_distros(request: Request):
    """Return all Linux distribution / build-system definitions."""
    mgr = _get_manager(request)
    distros = mgr.model.registry.distro if mgr.model else []
    return [distro_to_response(d) for d in distros]


@router.get(
    "/frameworks", response_model=List[FrameworkResponse], summary="List frameworks"
)
def list_frameworks(request: Request):
    """Return all build-system framework definitions."""
    mgr = _get_manager(request)
    frameworks = mgr.model.registry.frameworks if mgr.model else []
    return [framework_to_response(f) for f in frameworks]


@router.get(
    "/containers", response_model=List[ContainerResponse], summary="List containers"
)
def list_containers(request: Request):
    """Return all Docker container definitions."""
    mgr = _get_manager(request)
    containers = mgr.containers or {}
    return [container_to_response(name, docker) for name, docker in containers.items()]


# ---------------------------------------------------------------------------
# Action endpoints
# ---------------------------------------------------------------------------


@router.post("/export", response_model=ExportResponse, summary="Export BSP config")
def export_bsp(request: Request, body: ExportRequest):
    """
    Resolve and export a BSP configuration as a KAS-compatible YAML string.

    Supply either ``bsp_name`` **or** both ``device`` and ``release``.
    """
    mgr = _get_manager(request)
    _check_exclusive(body.bsp_name, body.device, body.release)

    buf = io.StringIO()
    try:
        import sys
        old_stdout = sys.stdout
        sys.stdout = buf
        if body.bsp_name:
            mgr.export_bsp_config(bsp_name=body.bsp_name, output_file=None)
        else:
            if not body.device or not body.release:
                raise HTTPException(
                    status_code=422,
                    detail="Provide 'bsp_name' or both 'device' and 'release'.",
                )
            mgr.export_by_components(body.device, body.release, body.features, output_file=None)
    except SystemExit as exc:
        raise HTTPException(status_code=400, detail=f"Export failed: {exc}") from exc
    finally:
        sys.stdout = old_stdout

    return ExportResponse(yaml_content=buf.getvalue())


@router.post("/build", response_model=BuildResponse, summary="Trigger BSP build")
def build_bsp(request: Request, body: BuildRequest):
    """
    Trigger a BSP build.  This call **blocks** until the build completes.

    Supply either ``bsp_name`` **or** both ``device`` and ``release``.
    Set ``checkout_only`` to ``true`` to validate the configuration without
    building.
    """
    mgr = _get_manager(request)
    _check_exclusive(body.bsp_name, body.device, body.release)

    try:
        if body.bsp_name:
            mgr.build_bsp(body.bsp_name, checkout_only=body.checkout_only)
        else:
            if not body.device or not body.release:
                raise HTTPException(
                    status_code=422,
                    detail="Provide 'bsp_name' or both 'device' and 'release'.",
                )
            mgr.build_by_components(
                body.device,
                body.release,
                body.features,
                checkout_only=body.checkout_only,
            )
    except SystemExit as exc:
        raise HTTPException(status_code=400, detail=f"Build failed: {exc}") from exc

    action = "Checkout" if body.checkout_only else "Build"
    return BuildResponse(status="ok", message=f"{action} completed successfully")


@router.post(
    "/shell", response_model=ShellCommandResponse, summary="Run command in build container"
)
def shell_command(request: Request, body: ShellCommandRequest):
    """
    Execute a non-interactive command inside the BSP build container and
    return its output and return code.

    Supply either ``bsp_name`` **or** both ``device`` and ``release``.
    """
    mgr = _get_manager(request)
    _check_exclusive(body.bsp_name, body.device, body.release)

    if not body.command:
        raise HTTPException(status_code=422, detail="'command' must not be empty.")

    buf = io.StringIO()
    rc = 0
    try:
        import sys
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = buf
        sys.stderr = buf
        if body.bsp_name:
            mgr.shell_into_bsp(bsp_name=body.bsp_name, command=body.command)
        else:
            if not body.device or not body.release:
                raise HTTPException(
                    status_code=422,
                    detail="Provide 'bsp_name' or both 'device' and 'release'.",
                )
            mgr.shell_by_components(
                body.device, body.release, body.features, command=body.command
            )
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    return ShellCommandResponse(return_code=rc, output=buf.getvalue())


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_exclusive(bsp_name, device, release):
    if bsp_name and (device or release):
        raise HTTPException(
            status_code=422,
            detail="Provide either 'bsp_name' or 'device'/'release', not both.",
        )
