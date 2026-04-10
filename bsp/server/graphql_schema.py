"""
Strawberry GraphQL schema for the BSP registry.

Queries
-------
- bsp             — list all BSP presets
- devices         — list all devices
- releases        — list releases (optional ``device`` filter)
- features        — list features
- distros         — list distros
- frameworks      — list frameworks
- containers      — list containers

Mutations
---------
- exportBsp       — resolve & export config as YAML
- buildBsp        — trigger a blocking BSP build
- shellCommand    — run a command inside the build container
"""

from __future__ import annotations

import io
import sys
from typing import List, Optional

import strawberry
from strawberry.fastapi import GraphQLRouter


# ---------------------------------------------------------------------------
# GraphQL types (mirrors Pydantic models in types.py)
# ---------------------------------------------------------------------------


@strawberry.type
class EnvVar:
    name: str
    value: str


@strawberry.type
class DockerArg:
    name: str
    value: str


@strawberry.type
class VendorReleaseGql:
    slug: str
    description: str
    includes: List[str]


@strawberry.type
class SocVendorOverrideGql:
    vendor: str
    includes: List[str]
    releases: List[VendorReleaseGql]
    distro: Optional[str]


@strawberry.type
class VendorOverrideGql:
    vendor: str
    includes: List[str]
    releases: List[VendorReleaseGql]
    soc_vendors: List[SocVendorOverrideGql]
    slug: Optional[str]
    distro: Optional[str]


@strawberry.type
class DeviceGql:
    slug: str
    description: str
    vendor: str
    soc_vendor: str
    soc_family: Optional[str]
    includes: List[str]
    local_conf: List[str]


@strawberry.type
class ReleaseGql:
    slug: str
    description: str
    includes: List[str]
    yocto_version: Optional[str]
    isar_version: Optional[str]
    environment: Optional[str]
    distro: Optional[str]
    vendor_overrides: List[VendorOverrideGql]


@strawberry.type
class FeatureCompatibilityGql:
    vendor: List[str]
    soc_vendor: List[str]
    soc_family: List[str]


@strawberry.type
class FeatureGql:
    slug: str
    description: str
    compatibility: Optional[FeatureCompatibilityGql]
    compatible_with: List[str]
    includes: List[str]
    local_conf: List[str]


@strawberry.type
class DistroGql:
    slug: str
    description: str
    vendor: str
    includes: List[str]
    framework: Optional[str]


@strawberry.type
class FrameworkGql:
    slug: str
    description: str
    vendor: str
    includes: List[str]


@strawberry.type
class ContainerGql:
    name: str
    image: Optional[str]
    file: Optional[str]
    args: List[DockerArg]
    runtime_args: Optional[str]
    privileged: bool


@strawberry.type
class BspPresetGql:
    name: str
    description: str
    device: str
    release: Optional[str]
    releases: List[str]
    vendor_release: Optional[str]
    override: Optional[str]
    features: List[str]
    targets: List[str]


# ---------------------------------------------------------------------------
# Mutation result types
# ---------------------------------------------------------------------------


@strawberry.type
class ExportResult:
    yaml_content: str


@strawberry.type
class BuildResult:
    status: str
    message: str


@strawberry.type
class ShellCommandResult:
    return_code: int
    output: str


# ---------------------------------------------------------------------------
# Dataclass → GraphQL type converters
# ---------------------------------------------------------------------------


def _vr(vr) -> VendorReleaseGql:
    return VendorReleaseGql(slug=vr.slug, description=vr.description, includes=list(vr.includes))


def _svo(svo) -> SocVendorOverrideGql:
    return SocVendorOverrideGql(
        vendor=svo.vendor,
        includes=list(svo.includes),
        releases=[_vr(r) for r in svo.releases],
        distro=svo.distro,
    )


def _vo(vo) -> VendorOverrideGql:
    return VendorOverrideGql(
        vendor=vo.vendor,
        includes=list(vo.includes),
        releases=[_vr(r) for r in vo.releases],
        soc_vendors=[_svo(s) for s in vo.soc_vendors],
        slug=vo.slug,
        distro=vo.distro,
    )


def _device(d) -> DeviceGql:
    return DeviceGql(
        slug=d.slug,
        description=d.description,
        vendor=d.vendor,
        soc_vendor=d.soc_vendor,
        soc_family=d.soc_family,
        includes=list(d.includes),
        local_conf=list(d.local_conf),
    )


def _release(r) -> ReleaseGql:
    return ReleaseGql(
        slug=r.slug,
        description=r.description,
        includes=list(r.includes),
        yocto_version=r.yocto_version,
        isar_version=r.isar_version,
        environment=r.environment,
        distro=r.distro,
        vendor_overrides=[_vo(vo) for vo in r.vendor_overrides],
    )


def _feature(f) -> FeatureGql:
    compat = None
    if f.compatibility:
        compat = FeatureCompatibilityGql(
            vendor=list(f.compatibility.vendor),
            soc_vendor=list(f.compatibility.soc_vendor),
            soc_family=list(f.compatibility.soc_family),
        )
    return FeatureGql(
        slug=f.slug,
        description=f.description,
        compatibility=compat,
        compatible_with=list(f.compatible_with),
        includes=list(f.includes),
        local_conf=list(f.local_conf),
    )


def _distro(d) -> DistroGql:
    return DistroGql(
        slug=d.slug,
        description=d.description,
        vendor=d.vendor,
        includes=list(d.includes),
        framework=d.framework,
    )


def _framework(f) -> FrameworkGql:
    return FrameworkGql(
        slug=f.slug,
        description=f.description,
        vendor=f.vendor,
        includes=list(f.includes),
    )


def _container(name: str, docker) -> ContainerGql:
    return ContainerGql(
        name=name,
        image=docker.image,
        file=docker.file,
        args=[DockerArg(name=a.name, value=a.value) for a in (docker.args or [])],
        runtime_args=docker.runtime_args,
        privileged=docker.privileged,
    )


def _preset(p) -> BspPresetGql:
    return BspPresetGql(
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


# ---------------------------------------------------------------------------
# Context helper
# ---------------------------------------------------------------------------


def _mgr(info):
    """Return the BspManager from Strawberry request context."""
    return info.context["request"].app.state.bsp_manager


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


@strawberry.type
class Query:

    @strawberry.field(description="List all named BSP presets.")
    def bsp(self, info: strawberry.types.Info) -> List[BspPresetGql]:
        mgr = _mgr(info)
        presets = mgr.resolver.list_presets() if mgr.resolver else []
        return [_preset(p) for p in presets]

    @strawberry.field(description="List all hardware device definitions.")
    def devices(self, info: strawberry.types.Info) -> List[DeviceGql]:
        mgr = _mgr(info)
        return [_device(d) for d in (mgr.model.registry.devices if mgr.model else [])]

    @strawberry.field(description="List releases, optionally filtered by device slug.")
    def releases(
        self,
        info: strawberry.types.Info,
        device: Optional[str] = strawberry.UNSET,
    ) -> List[ReleaseGql]:
        mgr = _mgr(info)
        releases = mgr.model.registry.releases if mgr.model else []
        if device is not strawberry.UNSET and device:
            try:
                dev = mgr.resolver.get_device(device)
            except SystemExit:
                return []
            releases = [
                r for r in releases
                if not r.vendor_overrides
                or any(vo.vendor == dev.vendor for vo in r.vendor_overrides)
            ]
        return [_release(r) for r in releases]

    @strawberry.field(description="List all optional BSP feature definitions.")
    def features(self, info: strawberry.types.Info) -> List[FeatureGql]:
        mgr = _mgr(info)
        return [_feature(f) for f in (mgr.model.registry.features if mgr.model else [])]

    @strawberry.field(description="List all Linux distribution / build-system definitions.")
    def distros(self, info: strawberry.types.Info) -> List[DistroGql]:
        mgr = _mgr(info)
        return [_distro(d) for d in (mgr.model.registry.distro if mgr.model else [])]

    @strawberry.field(description="List all build-system framework definitions.")
    def frameworks(self, info: strawberry.types.Info) -> List[FrameworkGql]:
        mgr = _mgr(info)
        return [_framework(f) for f in (mgr.model.registry.frameworks if mgr.model else [])]

    @strawberry.field(description="List all Docker container definitions.")
    def containers(self, info: strawberry.types.Info) -> List[ContainerGql]:
        mgr = _mgr(info)
        return [_container(n, d) for n, d in (mgr.containers or {}).items()]


# ---------------------------------------------------------------------------
# Mutation helpers
# ---------------------------------------------------------------------------


def _resolve_features(features) -> List[str]:
    """Return a plain list of feature slugs, handling UNSET / None gracefully."""
    if features is strawberry.UNSET or not features:
        return []
    return list(features)


def _is_set(value) -> bool:
    """Return True when *value* was explicitly provided (not UNSET and not None)."""
    return value is not strawberry.UNSET and value is not None


# ---------------------------------------------------------------------------
# Mutation
# ---------------------------------------------------------------------------


@strawberry.type
class Mutation:

    @strawberry.mutation(description="Resolve and export a BSP configuration as YAML.")
    def export_bsp(
        self,
        info: strawberry.types.Info,
        bsp_name: Optional[str] = strawberry.UNSET,
        device: Optional[str] = strawberry.UNSET,
        release: Optional[str] = strawberry.UNSET,
        features: Optional[List[str]] = strawberry.UNSET,
    ) -> ExportResult:
        mgr = _mgr(info)
        _feats = _resolve_features(features)
        buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            if _is_set(bsp_name):
                mgr.export_bsp_config(bsp_name=bsp_name, output_file=None)
            elif _is_set(device) and _is_set(release):
                mgr.export_by_components(device, release, _feats, output_file=None)
            else:
                raise ValueError("Provide bspName or both device and release.")
        except SystemExit as exc:
            raise ValueError(f"Export failed: {exc}") from exc
        finally:
            sys.stdout = old_stdout
        return ExportResult(yaml_content=buf.getvalue())

    @strawberry.mutation(description="Trigger a BSP build (blocking).")
    def build_bsp(
        self,
        info: strawberry.types.Info,
        bsp_name: Optional[str] = strawberry.UNSET,
        device: Optional[str] = strawberry.UNSET,
        release: Optional[str] = strawberry.UNSET,
        features: Optional[List[str]] = strawberry.UNSET,
        checkout_only: bool = False,
    ) -> BuildResult:
        mgr = _mgr(info)
        _feats = _resolve_features(features)
        try:
            if _is_set(bsp_name):
                mgr.build_bsp(bsp_name, checkout_only=checkout_only)
            elif _is_set(device) and _is_set(release):
                mgr.build_by_components(device, release, _feats, checkout_only=checkout_only)
            else:
                raise ValueError("Provide bspName or both device and release.")
        except SystemExit as exc:
            raise ValueError(f"Build failed: {exc}") from exc
        action = "Checkout" if checkout_only else "Build"
        return BuildResult(status="ok", message=f"{action} completed successfully")

    @strawberry.mutation(
        description="Run a non-interactive command inside the BSP build container."
    )
    def shell_command(
        self,
        info: strawberry.types.Info,
        command: str,
        bsp_name: Optional[str] = strawberry.UNSET,
        device: Optional[str] = strawberry.UNSET,
        release: Optional[str] = strawberry.UNSET,
        features: Optional[List[str]] = strawberry.UNSET,
    ) -> ShellCommandResult:
        mgr = _mgr(info)
        _feats = _resolve_features(features)
        buf = io.StringIO()
        rc = 0
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = buf
        sys.stderr = buf
        try:
            if _is_set(bsp_name):
                mgr.shell_into_bsp(bsp_name=bsp_name, command=command)
            elif _is_set(device) and _is_set(release):
                mgr.shell_by_components(device, release, _feats, command=command)
            else:
                raise ValueError("Provide bspName or both device and release.")
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
        return ShellCommandResult(return_code=rc, output=buf.getvalue())


# ---------------------------------------------------------------------------
# Schema + router factory
# ---------------------------------------------------------------------------


schema = strawberry.Schema(query=Query, mutation=Mutation)


def create_graphql_router() -> GraphQLRouter:
    """Return a Strawberry :class:`~strawberry.fastapi.GraphQLRouter`."""
    return GraphQLRouter(schema)
