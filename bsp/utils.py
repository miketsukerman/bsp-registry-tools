"""
YAML parsing utilities and Docker build helper for BSP registry tools.
"""

import logging
import os
import re
import subprocess
import sys

import dacite
import yaml
from pathlib import Path
from typing import List, Optional, Dict, Any, Set

from .models import Docker, DockerArg, RegistryRoot

# =============================================================================
# YAML Configuration Parser with Container Support
# =============================================================================

SUPPORTED_REGISTRY_VERSION = "2.0"


def read_yaml_file(filename: Path) -> str:
    """
    Read a YAML file into a string with comprehensive error handling.

    Args:
        filename: Path to the YAML file to read

    Returns:
        String containing the complete file contents

    Raises:
        SystemExit: If file cannot be read due to I/O errors or permissions
    """
    try:
        with open(filename, 'r', encoding='utf-8') as yaml_file:
            return yaml_file.read()
    except (IOError, OSError) as e:
        logging.error(f"Failed to read YAML file {filename}: {e}")
        sys.exit(1)


def parse_yaml_file(yaml_string: str) -> Dict[Any, Any]:
    """
    Parse YAML string into a Python dictionary with validation.

    Args:
        yaml_string: YAML formatted string to parse

    Returns:
        Dictionary representation of YAML content

    Raises:
        SystemExit: If YAML parsing fails due to malformed content
    """
    try:
        return yaml.safe_load(yaml_string)
    except yaml.YAMLError as e:
        logging.error(f"Failed to parse YAML: {e}")
        sys.exit(1)


def convert_containers_list_to_dict(containers_list: List[Dict[str, Any]]) -> Dict[str, Docker]:
    """
    Convert containers list format to dictionary format for dacite.

    The YAML format uses a list of dictionaries where each dictionary has one key
    (the container name). This function converts it to a dictionary keyed by container name.

    Example input:
        [
            {"ubuntu-20.04": {"file": "...", "image": "...", "args": [...]}},
            {"ubuntu-22.04": {"file": "...", "image": "...", "args": [...]}}
        ]

    Example output:
        {
            "ubuntu-20.04": {"file": "...", "image": "...", "args": [...]},
            "ubuntu-22.04": {"file": "...", "image": "...", "args": [...]}
        }

    Args:
        containers_list: List of container definitions in YAML format

    Returns:
        Dictionary mapping container names to Docker configurations
    """
    containers_dict = {}

    for container_item in containers_list:
        for container_name, container_config in container_item.items():
            if isinstance(container_config, dict):
                # Convert to Docker dataclass
                containers_dict[container_name] = Docker(
                    image=container_config.get('image'),
                    file=container_config.get('file'),
                    args=[DockerArg(name=arg['name'], value=arg['value'])
                          for arg in container_config.get('args', [])],
                    runtime_args=container_config.get('runtime_args'),
                    privileged=container_config.get('privileged', False),
                    copy=container_config.get('copy', []),
                )
            else:
                logging.warning(f"Invalid container configuration for {container_name}, skipping")

    return containers_dict


def _deep_merge_yaml_dicts(base: Dict[Any, Any], override: Dict[Any, Any]) -> Dict[Any, Any]:
    """
    Deep-merge two YAML dictionaries.

    Merging rules:
    - Lists are concatenated: base list first, then override list.
    - Nested dicts are merged recursively with the same rules.
    - Scalar values: the *override* value wins.

    Args:
        base: Base dictionary (lower priority for scalars)
        override: Override dictionary (higher priority for scalars)

    Returns:
        New merged dictionary
    """
    result = dict(base)
    for key, value in override.items():
        if key in result:
            if isinstance(result[key], list) and isinstance(value, list):
                result[key] = [*result[key], *value]
            elif isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = _deep_merge_yaml_dicts(result[key], value)
            else:
                result[key] = value
        else:
            result[key] = value
    return result


def _load_and_merge_includes(filename: Path, _visited: Optional[Set[Path]] = None) -> Dict[Any, Any]:
    """
    Load a YAML file and recursively process ``include`` directives.

    When the parsed YAML contains a top-level ``include`` key its value must be
    a list of paths (relative to the file that contains the directive).  Each
    referenced file is loaded recursively and deep-merged into the result
    **before** the content of the current file is applied, so entries defined
    in the including file always take precedence over entries from the included
    files.

    The ``specification`` block is silently stripped from included files
    because version validation is performed only once on the root registry.

    Circular includes are detected and cause an immediate error exit.

    Args:
        filename: Absolute (or resolvable) path to the YAML file to load
        _visited: Set of already-visited canonical file paths used internally
                  for cycle detection; callers should leave this as ``None``.

    Returns:
        Merged YAML dictionary with all ``include`` directives resolved

    Raises:
        SystemExit: On I/O errors, YAML parse errors, circular includes, or if
                    the ``include`` value is not a list
    """
    if _visited is None:
        _visited = set()

    canonical = filename.resolve()
    if canonical in _visited:
        logging.error(f"Circular include detected: {filename}")
        sys.exit(1)
    _visited = _visited | {canonical}

    yaml_string = read_yaml_file(filename)
    yaml_dict = parse_yaml_file(yaml_string)

    if yaml_dict is None:
        return {}

    base_dir = filename.parent

    # Pop the include directive before any further processing
    includes = yaml_dict.pop('include', None)
    if includes is None:
        includes = []

    if not isinstance(includes, list):
        logging.error(f"'include' in {filename} must be a list of file paths")
        sys.exit(1)

    # Accumulate included content first (lower priority)
    accumulated: Dict[Any, Any] = {}
    for include_path in includes:
        include_file = (base_dir / include_path).resolve()
        logging.debug(f"Processing include: {include_file}")
        included_dict = _load_and_merge_includes(Path(include_file), _visited)
        # Strip specification from included files; only root file is validated
        included_dict.pop('specification', None)
        accumulated = _deep_merge_yaml_dicts(accumulated, included_dict)

    # Merge current file on top (higher priority)
    return _deep_merge_yaml_dicts(accumulated, yaml_dict)


def get_registry_from_yaml_file(filename: Path) -> RegistryRoot:
    """
    Parse YAML file into structured RegistryRoot object using dacite.

    This function converts the raw YAML dictionary into strongly-typed
    dataclasses with comprehensive type checking and validation.

    Top-level ``include`` directives are resolved first so that a registry can
    be split across multiple files::

        include:
          - devices/boards.yaml
          - releases/scarthgap.yaml

    Included paths are relative to the file that contains the directive and
    can themselves contain further ``include`` directives.

    Args:
        filename: Path to registry YAML file

    Returns:
        Structured registry configuration as RegistryRoot object

    Raises:
        SystemExit: If configuration is invalid, malformed, or missing required fields
    """
    yaml_dict = _load_and_merge_includes(filename)

    # Fail fast if the registry version is not supported
    spec = yaml_dict.get('specification') or {}
    version = spec.get('version') if isinstance(spec, dict) else None
    if version != SUPPORTED_REGISTRY_VERSION:
        logging.error(
            f"Unsupported registry version '{version}' in {filename}. "
            f"This tool requires version '{SUPPORTED_REGISTRY_VERSION}'. "
            f"See docs/migration-v1-to-v2.md in the bsp-registry-tools repository "
            f"for upgrade instructions."
        )
        sys.exit(1)

    # Pre-process containers list to dictionary format if needed
    if 'containers' in yaml_dict and isinstance(yaml_dict['containers'], list):
        logging.debug("Converting containers list to dictionary format")
        yaml_dict['containers'] = convert_containers_list_to_dict(yaml_dict['containers'])

    try:
        # Use dacite to convert dictionary to strongly-typed dataclass
        # strict=False allows forward compatibility with new fields
        cfg = dacite.Config(strict=False)
        ast = dacite.from_dict(data_class=RegistryRoot, data=yaml_dict, config=cfg)
        return ast
    except dacite.UnexpectedDataError as e:
        logging.error(f"Configuration error in {filename}: Unknown fields found - {e}")
        sys.exit(1)
    except dacite.WrongTypeError as e:
        logging.error(f"Type error in configuration {filename}: Field type mismatch - {e}")
        sys.exit(1)
    except dacite.MissingValueError as e:
        logging.error(f"Missing value in configuration {filename}: Required field missing - {e}")
        sys.exit(1)


# =============================================================================
# Advantech Manifest Filename Parser
# =============================================================================

# Regex for the 5-field manifest filename:
#   [product]_[os_distro]_[version]_[kernel version]_[chip name][.xml]
_RE_MANIFEST_5 = re.compile(
    r'^(?P<product>[^_]+)'
    r'_(?P<os_distro>[^_]+)'
    r'_(?P<version>v\d+\.\d+\.\d+)'
    r'_kernel-(?P<kernel_version>[\d.]+)'
    r'_(?P<chip>[^_.]+)'
    r'(?:\.xml)?$'
)

# Regex for the 8-field release-package filename:
#   [product]_[os_distro]_[version]_[kernel version]_[chip]_[ram]_[storage]_[date][.tgz|.tar.gz|…]
_RE_MANIFEST_8 = re.compile(
    r'^(?P<product>[^_]+)'
    r'_(?P<os_distro>[^_]+)'
    r'_(?P<version>v\d+\.\d+\.\d+)'
    r'_kernel-(?P<kernel_version>[\d.]+)'
    r'_(?P<chip>[^_]+)'
    r'_(?P<ram>[^_]+)'
    r'_(?P<storage>[^_]+)'
    r'_(?P<release_date>\d{4}-\d{2}-\d{2})'
    r'(?:\.[a-zA-Z0-9.]+)?$'
)


def parse_advantech_manifest_name(filename: str) -> Dict[str, Optional[str]]:
    """
    Parse an Advantech manifest or release-package filename into its components.

    Two filename formats are supported:

    **5-field manifest format** (e.g. ``aom2721a1_yocto4.0.18-le1.1_v1.0.0_kernel-6.6.28_qcs6490.xml``)::

        [product]_[os_distro]_[version]_[kernel-version]_[chip][.xml]

    **8-field release-package format** (e.g. ``aom2721a1_yocto4.0.18-le1.1_v1.0.0_kernel-6.6.28_qcs6490_8g_ufs_2025-02-08.tgz``)::

        [product]_[os_distro]_[version]_[kernel-version]_[chip]_[ram]_[storage]_[date][.ext]

    The returned dictionary always contains the following keys; fields not
    present in the filename are set to ``None``:

    * ``product`` — Device name with PCB version (e.g. ``'aom2721a1'``).
    * ``os_distro`` — OS distro / BSP string (e.g. ``'yocto4.0.18-le1.1'``).
    * ``yocto_version`` — Yocto/BSP version extracted from ``os_distro`` when
      the distro starts with ``'yocto'`` (e.g. ``'4.0.18'``), otherwise ``None``.
    * ``bsp_version`` — Vendor BSP sub-version extracted from ``os_distro``
      (e.g. ``'le1.1'``), otherwise ``None``.
    * ``version`` — Software release version (e.g. ``'v1.0.0'``).
    * ``kernel_version`` — Bare kernel version (e.g. ``'6.6.28'``; the
      ``kernel-`` prefix is stripped).
    * ``chip`` — Chip model (e.g. ``'qcs6490'``).
    * ``ram`` — RAM capacity (e.g. ``'8g'``); ``None`` for 5-field format.
    * ``storage`` — Storage type (e.g. ``'ufs'``); ``None`` for 5-field format.
    * ``release_date`` — ISO 8601 release date (e.g. ``'2025-02-08'``);
      ``None`` for 5-field format.

    Args:
        filename: Manifest or release-package filename (basename or full path).
                  The file extension and leading directory components are
                  ignored during parsing.

    Returns:
        Dictionary with all fields described above.

    Raises:
        ValueError: If the filename does not match either supported format.

    Examples::

        >>> parse_advantech_manifest_name(
        ...     "aom2721a1_yocto4.0.18-le1.1_v1.0.0_kernel-6.6.28_qcs6490.xml"
        ... )
        {
            'product': 'aom2721a1',
            'os_distro': 'yocto4.0.18-le1.1',
            'yocto_version': '4.0.18',
            'bsp_version': 'le1.1',
            'version': 'v1.0.0',
            'kernel_version': '6.6.28',
            'chip': 'qcs6490',
            'ram': None,
            'storage': None,
            'release_date': None,
        }
    """
    # Strip any leading directory path and work only with the basename
    basename = Path(filename).name

    # Try the longer 8-field format first (more specific)
    match = _RE_MANIFEST_8.match(basename)
    if match is None:
        match = _RE_MANIFEST_5.match(basename)
    if match is None:
        raise ValueError(
            f"Filename '{filename}' does not match the Advantech manifest naming "
            "convention.  Expected format: "
            "[product]_[os_distro]_[version]_kernel-[kernel_version]_[chip]"
            "[_[ram]_[storage]_[YYYY-MM-DD]][.ext]"
        )

    groups = match.groupdict()
    os_distro: str = groups['os_distro']

    # Decompose os_distro into yocto_version and bsp_version.
    # Pattern: 'yocto<major>.<minor>.<patch>-<bsp_sub>'
    # Examples: 'yocto4.0.18-le1.1' → yocto_version='4.0.18', bsp_version='le1.1'
    #           'yocto5.0'           → yocto_version='5.0',    bsp_version=None
    yocto_version: Optional[str] = None
    bsp_version: Optional[str] = None
    distro_match = re.match(r'^yocto([\d.]+)(?:-(.+))?$', os_distro)
    if distro_match:
        yocto_version = distro_match.group(1)
        bsp_version = distro_match.group(2)  # None when there is no '-' suffix

    return {
        'product': groups['product'],
        'os_distro': os_distro,
        'yocto_version': yocto_version,
        'bsp_version': bsp_version,
        'version': groups['version'],
        'kernel_version': groups['kernel_version'],
        'chip': groups['chip'],
        'ram': groups.get('ram'),
        'storage': groups.get('storage'),
        'release_date': groups.get('release_date'),
    }




# =============================================================================
# Docker Operations
# =============================================================================

def build_docker(dockerfile_dir: str, dockerfile: str, tag: str,
                 build_args: Optional[List[DockerArg]] = None,
                 verbose: bool = False) -> None:
    """
    Build Docker image from Dockerfile with comprehensive validation.

    This function handles the complete Docker build process including:
    - Prerequisite validation (directory and file existence)
    - Build argument processing
    - Directory context management
    - Error handling and logging

    Args:
        dockerfile_dir: Directory containing Dockerfile and build context
        dockerfile: Dockerfile name (e.g., 'Dockerfile')
        tag: Image tag for the built image (e.g., 'my-bsp:latest')
        build_args: List of Docker build arguments for parameterized builds
        verbose: If True, stream docker build output live; otherwise show
                 only a status message and suppress build output

    Raises:
        SystemExit: If Docker build fails, prerequisites are missing, or Docker is unavailable
    """
    logging.info(f"Building docker container {tag} using {dockerfile}")

    # Validate prerequisites before attempting build
    if not os.path.isdir(dockerfile_dir):
        logging.error(f"Docker build directory does not exist: {dockerfile_dir}")
        sys.exit(1)

    dockerfile_path = os.path.join(dockerfile_dir, dockerfile)
    if not os.path.isfile(dockerfile_path):
        logging.error(f"Dockerfile not found: {dockerfile_path}")
        sys.exit(1)

    original_dir = os.getcwd()
    try:
        # Change to Dockerfile directory for proper build context
        os.chdir(dockerfile_dir)

        # Build docker command with all required parameters
        cmd = ["docker", "build", "-f", dockerfile, "-t", tag]

        # Add build arguments if provided (for parameterized Docker builds)
        if build_args:
            for argument in build_args:
                cmd.extend(["--build-arg", f"{argument.name}={argument.value}"])

        cmd.extend(["."])  # Build context is current directory

        logging.info(f"Running: {' '.join(cmd)}")

        if verbose:
            # Stream docker build output live so the user can follow progress
            subprocess.run(cmd, check=True)
        else:
            # Quiet mode: show a brief status line, then suppress build output
            print(f"Preparing docker environment: {tag} ...")
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            if result.stdout:
                logging.debug(result.stdout)
            if result.stderr:
                logging.debug(result.stderr)

        logging.info("Docker build completed successfully")

    except subprocess.CalledProcessError as e:
        logging.error(f"Docker build failed with return code {e.returncode}")
        if e.stderr:
            logging.error(f"Error output: {e.stderr}")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Unexpected error during Docker build: {e}")
        sys.exit(1)
    finally:
        # Always return to original directory regardless of build outcome
        os.chdir(original_dir)
