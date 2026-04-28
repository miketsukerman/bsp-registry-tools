"""
Pydantic response models for the BSP registry server.

These mirror the dataclasses in :mod:`bsp.models` but are expressed as
Pydantic models so they can be serialised/validated by FastAPI and re-used
as the base types for the Strawberry GraphQL schema.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------


class EnvVarResponse(BaseModel):
    """An environment variable name/value pair."""

    name: str
    value: str


class DockerArgResponse(BaseModel):
    """A Docker build argument."""

    name: str
    value: str


class DockerResponse(BaseModel):
    """Docker container configuration."""

    image: Optional[str] = None
    file: Optional[str] = None
    args: List[DockerArgResponse] = []
    runtime_args: Optional[str] = None
    privileged: bool = False


# ---------------------------------------------------------------------------
# Registry entities
# ---------------------------------------------------------------------------


class DeviceResponse(BaseModel):
    """Hardware device / board definition."""

    slug: str
    description: str
    vendor: str
    soc_vendor: str
    soc_family: Optional[str] = None
    includes: List[str] = []
    local_conf: List[str] = []


class VendorReleaseResponse(BaseModel):
    """A vendor-specific sub-release."""

    slug: str
    description: str
    includes: List[str] = []


class SocVendorOverrideResponse(BaseModel):
    """SoC-vendor-specific overrides within a board-vendor override."""

    vendor: str
    includes: List[str] = []
    releases: List[VendorReleaseResponse] = []
    distro: Optional[str] = None


class VendorOverrideResponse(BaseModel):
    """Vendor-specific KAS configuration overrides for a release."""

    vendor: str
    includes: List[str] = []
    releases: List[VendorReleaseResponse] = []
    soc_vendors: List[SocVendorOverrideResponse] = []
    slug: Optional[str] = None
    distro: Optional[str] = None


class ReleaseResponse(BaseModel):
    """Yocto/Isar release definition."""

    slug: str
    description: str
    includes: List[str] = []
    yocto_version: Optional[str] = None
    isar_version: Optional[str] = None
    environment: Optional[str] = None
    distro: Optional[str] = None
    vendor_overrides: List[VendorOverrideResponse] = []


class FeatureCompatibilityResponse(BaseModel):
    """Compatibility constraints for a feature."""

    vendor: List[str] = []
    soc_vendor: List[str] = []
    soc_family: List[str] = []


class FeatureResponse(BaseModel):
    """Optional BSP feature definition."""

    slug: str
    description: str
    compatibility: Optional[FeatureCompatibilityResponse] = None
    compatible_with: List[str] = []
    includes: List[str] = []
    local_conf: List[str] = []


class DistroResponse(BaseModel):
    """Linux distribution / build-system definition."""

    slug: str
    description: str
    vendor: str = ""
    includes: List[str] = []
    framework: Optional[str] = None


class FrameworkResponse(BaseModel):
    """Build-system framework definition."""

    slug: str
    description: str
    vendor: str
    includes: List[str] = []


class ContainerResponse(BaseModel):
    """Named Docker container definition."""

    name: str
    image: Optional[str] = None
    file: Optional[str] = None
    args: List[DockerArgResponse] = []
    runtime_args: Optional[str] = None
    privileged: bool = False


class BspPresetResponse(BaseModel):
    """Named BSP preset (shortcut for device + release + optional features)."""

    name: str
    description: str
    device: str
    release: Optional[str] = None
    releases: List[str] = []
    vendor_release: Optional[str] = None
    override: Optional[str] = None
    features: List[str] = []
    targets: List[str] = []


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class BuildRequest(BaseModel):
    """Request body for the build endpoint."""

    bsp_name: Optional[str] = None
    device: Optional[str] = None
    release: Optional[str] = None
    features: List[str] = []
    checkout_only: bool = False


class ExportRequest(BaseModel):
    """Request body for the export endpoint."""

    bsp_name: Optional[str] = None
    device: Optional[str] = None
    release: Optional[str] = None
    features: List[str] = []


class ShellCommandRequest(BaseModel):
    """Request body for the shell-command endpoint."""

    bsp_name: Optional[str] = None
    device: Optional[str] = None
    release: Optional[str] = None
    features: List[str] = []
    command: str


# ---------------------------------------------------------------------------
# Response envelopes
# ---------------------------------------------------------------------------


class ExportResponse(BaseModel):
    """Response from the export endpoint."""

    yaml_content: str


class BuildResponse(BaseModel):
    """Response from the build endpoint."""

    status: str
    message: str


class ShellCommandResponse(BaseModel):
    """Response from the shell-command endpoint."""

    return_code: int
    output: str


# ---------------------------------------------------------------------------
# Conversion helpers (dataclass → Pydantic)
# ---------------------------------------------------------------------------


def _docker_arg(arg) -> DockerArgResponse:
    return DockerArgResponse(name=arg.name, value=arg.value)


def _env_var(ev) -> EnvVarResponse:
    return EnvVarResponse(name=ev.name, value=ev.value)


def _vendor_release(vr) -> VendorReleaseResponse:
    return VendorReleaseResponse(
        slug=vr.slug,
        description=vr.description,
        includes=list(vr.includes),
    )


def _soc_vendor_override(svo) -> SocVendorOverrideResponse:
    return SocVendorOverrideResponse(
        vendor=svo.vendor,
        includes=list(svo.includes),
        releases=[_vendor_release(r) for r in svo.releases],
        distro=svo.distro,
    )


def _vendor_override(vo) -> VendorOverrideResponse:
    return VendorOverrideResponse(
        vendor=vo.vendor,
        includes=list(vo.includes),
        releases=[_vendor_release(r) for r in vo.releases],
        soc_vendors=[_soc_vendor_override(s) for s in vo.soc_vendors],
        slug=vo.slug,
        distro=vo.distro,
    )


def device_to_response(d) -> DeviceResponse:
    return DeviceResponse(
        slug=d.slug,
        description=d.description,
        vendor=d.vendor,
        soc_vendor=d.soc_vendor,
        soc_family=d.soc_family,
        includes=list(d.includes),
        local_conf=list(d.local_conf),
    )


def release_to_response(r) -> ReleaseResponse:
    return ReleaseResponse(
        slug=r.slug,
        description=r.description,
        includes=list(r.includes),
        yocto_version=r.yocto_version,
        isar_version=r.isar_version,
        environment=r.environment,
        distro=r.distro,
        vendor_overrides=[_vendor_override(vo) for vo in r.vendor_overrides],
    )


def feature_to_response(f) -> FeatureResponse:
    compat = None
    if f.compatibility:
        compat = FeatureCompatibilityResponse(
            vendor=list(f.compatibility.vendor),
            soc_vendor=list(f.compatibility.soc_vendor),
            soc_family=list(f.compatibility.soc_family),
        )
    return FeatureResponse(
        slug=f.slug,
        description=f.description,
        compatibility=compat,
        compatible_with=list(f.compatible_with),
        includes=list(f.includes),
        local_conf=list(f.local_conf),
    )


def distro_to_response(d) -> DistroResponse:
    return DistroResponse(
        slug=d.slug,
        description=d.description,
        vendor=d.vendor,
        includes=list(d.includes),
        framework=d.framework,
    )


def framework_to_response(f) -> FrameworkResponse:
    return FrameworkResponse(
        slug=f.slug,
        description=f.description,
        vendor=f.vendor,
        includes=list(f.includes),
    )


def container_to_response(name: str, docker) -> ContainerResponse:
    return ContainerResponse(
        name=name,
        image=docker.image,
        file=docker.file,
        args=[_docker_arg(a) for a in (docker.args or [])],
        runtime_args=docker.runtime_args,
        privileged=docker.privileged,
    )


def preset_to_response(p) -> BspPresetResponse:
    return BspPresetResponse(
        name=p.name,
        description=p.description,
        device=p.device,
        release=p.release,
        releases=list(p.releases),
        vendor_release=p.vendor_release,
        override=getattr(p, "override", None),
        features=list(p.features),
        targets=list(p.targets),
    )
