"""
Shared pytest fixtures and YAML constants for bsp-registry-tools tests.
"""

import tempfile
import pytest
from pathlib import Path


# =============================================================================
# Shared YAML test data (v2.0 schema)
# =============================================================================

MINIMAL_REGISTRY_YAML = """
specification:
  version: "2.0"
containers:
  - ubuntu-22.04:
      image: "test/ubuntu-22.04:latest"
      file: Dockerfile.ubuntu
      args:
        - name: "DISTRO"
          value: "ubuntu:22.04"
registry:
  devices:
    - slug: test-device
      description: "Test Device"
      vendor: test-vendor
      soc_vendor: test-soc-vendor
      build:
        path: build/test
        container: "ubuntu-22.04"
        includes:
          - test.yml
  releases:
    - slug: test-release
      description: "Test Release"
  bsp:
    - name: test-bsp
      description: "Test BSP"
      device: test-device
      release: test-release
"""

REGISTRY_WITH_ENV_YAML = """
specification:
  version: "2.0"
environment:
  - name: "DL_DIR"
    value: "/tmp/downloads"
  - name: "SSTATE_DIR"
    value: "/tmp/sstate"
  - name: "GITCONFIG_FILE"
    value: "$ENV{HOME}/.gitconfig"
containers:
  - ubuntu-22.04:
      image: "test/ubuntu-22.04:latest"
      file: Dockerfile.ubuntu
      args: []
registry:
  devices:
    - slug: qemu-arm64
      description: "QEMU ARM64"
      vendor: qemu
      soc_vendor: arm
      build:
        path: build/qemu-arm64
        container: "ubuntu-22.04"
        includes:
          - kas/qemu/qemuarm64.yml
    - slug: qemu-x86-64
      description: "QEMU x86-64"
      vendor: qemu
      soc_vendor: intel
      build:
        path: build/qemu-x86-64
        container: "ubuntu-22.04"
        includes:
          - kas/qemu/qemux86-64.yml
  releases:
    - slug: scarthgap
      description: "Scarthgap (Yocto 5.0 LTS)"
      yocto_version: "5.0"
  bsp:
    - name: qemu-arm64
      description: "QEMU ARM64 BSP"
      device: qemu-arm64
      release: scarthgap
    - name: qemu-x86-64
      description: "QEMU x86-64 BSP"
      device: qemu-x86-64
      release: scarthgap
"""

INVALID_YAML = """
specification:
  version: [invalid
"""

EMPTY_REGISTRY_YAML = """
specification:
  version: "2.0"
registry:
  devices: []
  releases: []
  bsp: []
"""

REGISTRY_WITH_FEATURES_YAML = """
specification:
  version: "2.0"
containers:
  - ubuntu-22.04:
      image: "test/ubuntu-22.04:latest"
      file: Dockerfile.ubuntu
      args: []
registry:
  devices:
    - slug: test-device
      description: "Test Device"
      vendor: test-vendor
      soc_vendor: nxp
      soc_family: imx8
      build:
        path: build/test
        container: "ubuntu-22.04"
        includes:
          - device.yml
  releases:
    - slug: test-release
      description: "Test Release"
      includes:
        - release.yml
  features:
    - slug: ota
      description: "OTA Update"
      includes:
        - ota.yml
      local_conf:
        - 'IMAGE_INSTALL += "swupdate"'
    - slug: secure-boot
      description: "Secure Boot"
      compatibility:
        soc_vendor:
          - nxp
      includes:
        - secure-boot.yml
    - slug: vendor-only
      description: "Vendor Only Feature"
      compatibility:
        vendor:
          - advantech
  bsp:
    - name: test-bsp
      description: "Test BSP"
      device: test-device
      release: test-release
"""

REGISTRY_WITH_NAMED_ENVIRONMENTS_YAML = """
specification:
  version: "2.0"
containers:
  - poky-container:
      image: "poky:latest"
      file: Dockerfile.poky
      args: []
  - isar-container:
      image: "isar:latest"
      file: Dockerfile.isar
      args: []
      privileged: true
environments:
  default:
    container: poky-container
    variables:
      - name: "YOCTO_ENV_VAR"
        value: "default-value"
  isar-env:
    container: isar-container
    variables:
      - name: "ISAR_VAR"
        value: "isar-value"
registry:
  devices:
    - slug: test-device
      description: "Test Device"
      vendor: test-vendor
      soc_vendor: arm
      build:
        path: build/test
        includes:
          - device.yml
  releases:
    - slug: poky-release
      description: "Poky Release"
      includes:
        - poky.yml
    - slug: isar-release
      description: "Isar Release"
      environment: isar-env
      includes:
        - isar.yml
"""

REGISTRY_WITH_COPY_YAML = """
specification:
  version: "2.0"
containers:
  - ubuntu-22.04:
      image: "test/ubuntu-22.04:latest"
      file: Dockerfile.ubuntu
      args: []
registry:
  devices:
    - slug: test-device
      description: "Test Device"
      vendor: test-vendor
      soc_vendor: arm
      build:
        path: build/test
        container: "ubuntu-22.04"
        includes:
          - test.yml
        copy:
          - src/file.txt: dst/
  releases:
    - slug: test-release
      description: "Test Release"
  bsp:
    - name: test-bsp
      description: "Test BSP"
      device: test-device
      release: test-release
"""

REGISTRY_WITH_RUNTIME_ARGS_YAML = """
specification:
  version: "2.0"
containers:
  - net-container:
      image: "net-build:latest"
      file: Dockerfile.net
      args: []
      runtime_args: "-p 2222:2222 --device=/dev/net/tun --cap-add=NET_ADMIN"
registry:
  devices:
    - slug: test-device
      description: "Test Device"
      vendor: test-vendor
      soc_vendor: arm
      build:
        path: build/test
        container: "net-container"
        includes:
          - test.yml
  releases:
    - slug: test-release
      description: "Test Release"
  bsp:
    - name: test-bsp
      description: "Test BSP"
      device: test-device
      release: test-release
"""


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def tmp_dir():
    """Provide a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def registry_file(tmp_dir):
    """Create a minimal registry YAML file in a temp directory."""
    registry_path = tmp_dir / "bsp-registry.yaml"
    registry_path.write_text(MINIMAL_REGISTRY_YAML)
    return registry_path


@pytest.fixture
def registry_with_env_file(tmp_dir):
    """Create a registry YAML file with environment variables."""
    registry_path = tmp_dir / "bsp-registry.yaml"
    registry_path.write_text(REGISTRY_WITH_ENV_YAML)
    return registry_path


@pytest.fixture
def registry_with_features_file(tmp_dir):
    """Create a registry YAML file with features."""
    registry_path = tmp_dir / "bsp-registry.yaml"
    registry_path.write_text(REGISTRY_WITH_FEATURES_YAML)
    return registry_path


@pytest.fixture
def registry_with_named_environments_file(tmp_dir):
    """Create a registry YAML file with named environments."""
    registry_path = tmp_dir / "bsp-registry.yaml"
    registry_path.write_text(REGISTRY_WITH_NAMED_ENVIRONMENTS_YAML)
    return registry_path


@pytest.fixture
def registry_with_copy_file(tmp_dir):
    """Create a registry YAML file with copy entries."""
    registry_path = tmp_dir / "bsp-registry.yaml"
    registry_path.write_text(REGISTRY_WITH_COPY_YAML)
    return registry_path


@pytest.fixture
def registry_with_runtime_args_file(tmp_dir):
    """Create a registry YAML file with container runtime_args."""
    registry_path = tmp_dir / "bsp-registry.yaml"
    registry_path.write_text(REGISTRY_WITH_RUNTIME_ARGS_YAML)
    return registry_path


@pytest.fixture
def kas_config_file(tmp_dir):
    """Create a simple KAS configuration YAML file."""
    kas_content = """
header:
  version: 14

distro: poky
machine: qemuarm64

target:
  - core-image-minimal
"""
    kas_path = tmp_dir / "test.yml"
    kas_path.write_text(kas_content)
    return kas_path


@pytest.fixture
def kas_config_with_includes(tmp_dir):
    """Create KAS configuration files with includes."""
    base_content = """
header:
  version: 14
  includes:
    - include.yml

machine: qemuarm64
"""
    include_content = """
header:
  version: 14

distro: poky
"""
    base_path = tmp_dir / "base.yml"
    include_path = tmp_dir / "include.yml"
    base_path.write_text(base_content)
    include_path.write_text(include_content)
    return base_path, include_path
