"""
Tests for configuration data classes and factory functions (v2.0 schema).
"""

from bsp import (
    EnvironmentVariable,
    DockerArg,
    Docker,
    Specification,
    NamedEnvironment,
    DeviceBuild,
    Device,
    VendorIncludes,
    Distro,
    Release,
    FeatureCompatibility,
    Feature,
    BspPreset,
    Registry,
    RegistryRoot,
    empty_list,
    empty_dict,
)


# =============================================================================
# Tests for Factory Functions
# =============================================================================

class TestFactoryFunctions:
    def test_empty_list_returns_list(self):
        result = empty_list()
        assert isinstance(result, list)
        assert result == []

    def test_empty_list_returns_new_instance(self):
        a = empty_list()
        b = empty_list()
        a.append(1)
        assert b == []

    def test_empty_dict_returns_dict(self):
        result = empty_dict()
        assert isinstance(result, dict)
        assert result == {}

    def test_empty_dict_returns_new_instance(self):
        a = empty_dict()
        b = empty_dict()
        a["key"] = "value"
        assert b == {}


# =============================================================================
# Tests for Shared Data Classes
# =============================================================================

class TestSharedDataClasses:
    def test_environment_variable(self):
        ev = EnvironmentVariable(name="MY_VAR", value="my_value")
        assert ev.name == "MY_VAR"
        assert ev.value == "my_value"

    def test_docker_arg(self):
        arg = DockerArg(name="DISTRO", value="ubuntu:22.04")
        assert arg.name == "DISTRO"
        assert arg.value == "ubuntu:22.04"

    def test_docker_with_optional_fields(self):
        docker = Docker(image="my-image:latest", file=None)
        assert docker.image == "my-image:latest"
        assert docker.file is None
        assert docker.args == []
        assert docker.privileged is False
        assert docker.runtime_args is None

    def test_docker_with_args(self):
        args = [DockerArg(name="VERSION", value="22.04")]
        docker = Docker(image="my-image", file="Dockerfile", args=args)
        assert len(docker.args) == 1
        assert docker.args[0].name == "VERSION"

    def test_docker_privileged_default_false(self):
        docker = Docker(image="my-image:latest", file=None)
        assert docker.privileged is False

    def test_docker_privileged_can_be_set_true(self):
        docker = Docker(image="my-image:latest", file=None, privileged=True)
        assert docker.privileged is True

    def test_docker_runtime_args_default_none(self):
        docker = Docker(image="my-image:latest", file=None)
        assert docker.runtime_args is None

    def test_docker_runtime_args_can_be_set(self):
        docker = Docker(
            image="my-image:latest",
            file=None,
            runtime_args="-p 2222:2222 --cap-add=NET_ADMIN"
        )
        assert docker.runtime_args == "-p 2222:2222 --cap-add=NET_ADMIN"

    def test_specification(self):
        spec = Specification(version="2.0")
        assert spec.version == "2.0"


# =============================================================================
# Tests for v2.0 Data Classes
# =============================================================================

class TestV2DataClasses:
    def test_named_environment_defaults(self):
        env = NamedEnvironment()
        assert env.container is None
        assert env.variables == []

    def test_named_environment_with_container(self):
        env = NamedEnvironment(container="my-container")
        assert env.container == "my-container"

    def test_named_environment_with_variables(self):
        var = EnvironmentVariable(name="K", value="V")
        env = NamedEnvironment(variables=[var])
        assert len(env.variables) == 1

    def test_device_build_defaults(self):
        build = DeviceBuild(path="build/test")
        assert build.path == "build/test"
        assert build.container is None
        assert build.includes == []
        assert build.local_conf == []
        assert build.copy == []

    def test_device_build_with_container(self):
        build = DeviceBuild(path="build/test", container="my-container")
        assert build.container == "my-container"

    def test_device(self):
        build = DeviceBuild(path="build/test")
        dev = Device(
            slug="qemu-arm64",
            description="QEMU ARM64",
            vendor="qemu",
            soc_vendor="arm",
            build=build,
        )
        assert dev.slug == "qemu-arm64"
        assert dev.soc_family is None

    def test_device_with_soc_family(self):
        build = DeviceBuild(path="build/test")
        dev = Device(
            slug="imx8-board",
            description="iMX8 Board",
            vendor="advantech",
            soc_vendor="nxp",
            build=build,
            soc_family="imx8",
        )
        assert dev.soc_family == "imx8"

    def test_vendor_includes(self):
        vi = VendorIncludes(vendor="advantech", includes=["adv.yml"])
        assert vi.vendor == "advantech"
        assert vi.includes == ["adv.yml"]

    def test_release_defaults(self):
        release = Release(slug="scarthgap", description="Scarthgap")
        assert release.slug == "scarthgap"
        assert release.includes == []
        assert release.yocto_version is None
        assert release.isar_version is None
        assert release.vendor_includes == []
        assert release.environment is None

    def test_release_with_versions(self):
        release = Release(
            slug="scarthgap",
            description="Scarthgap",
            yocto_version="5.0",
        )
        assert release.yocto_version == "5.0"

    def test_feature_compatibility_defaults(self):
        compat = FeatureCompatibility()
        assert compat.vendor == []
        assert compat.soc_vendor == []
        assert compat.soc_family == []

    def test_feature_defaults(self):
        feat = Feature(slug="ota", description="OTA Update")
        assert feat.slug == "ota"
        assert feat.compatibility is None
        assert feat.includes == []
        assert feat.local_conf == []
        assert feat.env == []

    def test_bsp_preset_defaults(self):
        preset = BspPreset(
            name="my-preset",
            description="My Preset",
            device="qemu-arm64",
            release="scarthgap",
        )
        assert preset.name == "my-preset"
        assert preset.features == []

    def test_bsp_preset_with_features(self):
        preset = BspPreset(
            name="my-preset",
            description="My Preset",
            device="qemu-arm64",
            release="scarthgap",
            features=["ota", "secure-boot"],
        )
        assert preset.features == ["ota", "secure-boot"]

    def test_registry_defaults(self):
        reg = Registry()
        assert reg.devices == []
        assert reg.releases == []
        assert reg.features == []
        assert reg.bsp == []

    def test_registry_root_defaults(self):
        spec = Specification(version="2.0")
        reg = Registry()
        root = RegistryRoot(specification=spec, registry=reg)
        assert root.containers == {}
        assert root.environment == []
        assert root.environments == {}

    def test_distro_defaults(self):
        distro = Distro(slug="poky", description="Poky reference distro", vendor="yocto")
        assert distro.slug == "poky"
        assert distro.description == "Poky reference distro"
        assert distro.vendor == "yocto"
        assert distro.includes == []

    def test_distro_with_includes(self):
        distro = Distro(
            slug="isar",
            description="Isar build system",
            vendor="siemens",
            includes=["kas/isar/isar.yaml"],
        )
        assert distro.includes == ["kas/isar/isar.yaml"]

    def test_release_distro_field_defaults_to_none(self):
        release = Release(slug="scarthgap", description="Scarthgap")
        assert release.distro is None

    def test_release_with_distro(self):
        release = Release(slug="scarthgap", description="Scarthgap", distro="poky")
        assert release.distro == "poky"

    def test_registry_distro_defaults_to_empty(self):
        reg = Registry()
        assert reg.distro == []

    def test_registry_with_distros(self):
        distro = Distro(slug="poky", description="Poky", vendor="yocto")
        reg = Registry(distro=[distro])
        assert len(reg.distro) == 1
        assert reg.distro[0].slug == "poky"
