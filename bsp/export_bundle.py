"""
Helpers for building self-contained ``bsp export`` bundles.

A bundle is a directory that contains everything needed to reproduce a build
on another machine:

* the exported configuration (KAS YAML or Android repo manifest XML),
* every patch file referenced by the exported KAS configuration,
* the Dockerfile of the container used for the build (when the registry
  defines one),
* an ``environment.sh`` file with the environment variables coming from the
  BSP registry model used for the build,
* a ``setup.sh`` shell script performing the initial build setup,
* a ``README.md`` documenting the bundle contents and how to use them.
"""

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from .models import Docker, EnvironmentVariable

# Name of the generated initial-setup shell script.
SETUP_SCRIPT_NAME = "setup.sh"
# Default file names used when exporting into a bundle directory.
DEFAULT_KAS_CONFIG_NAME = "kas.yml"
DEFAULT_REPO_MANIFEST_NAME = "manifest.xml"
# Sub-directory used for patches that live outside the registry root.
EXTERNAL_PATCH_DIR = "patches"
# Sub-directory receiving the container definition of the exported build.
CONTAINER_DIR = "container"
# Name of the generated environment file sourced by the setup script.
ENVIRONMENT_FILE_NAME = "environment.sh"
# Name of the generated bundle documentation file.
README_FILE_NAME = "README.md"
# Image tag used when a container defines a Dockerfile but no image name.
DEFAULT_CONTAINER_IMAGE = "bsp-export-build:latest"

# Matches the ``$ENV{VAR}`` placeholders used in registry values.
_ENV_PLACEHOLDER = re.compile(r"\$ENV\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class ExportedContainer:
    """
    Container information recorded in an export bundle.

    Attributes:
        image: Container image name/tag used for the build
        dockerfile: Bundle-relative path of the copied Dockerfile, if any
        args: Docker build arguments as ``(name, value)`` pairs
        privileged: Whether the build container runs in privileged mode
        runtime_args: Extra container runtime arguments from the registry
    """
    image: Optional[str] = None
    dockerfile: Optional[Path] = None
    args: List[Tuple[str, str]] = field(default_factory=list)
    privileged: bool = False
    runtime_args: Optional[str] = None


@dataclass
class ExportedPatch:
    """
    A patch file copied into an export bundle.

    Attributes:
        path: Bundle-relative path of the copied patch file
        repo: Name of the repository the patch is applied to
        repo_path: Checkout path of that repository, relative to the workspace
    """
    path: Path
    repo: str = ""
    repo_path: str = ""


def _safe_relative_path(source: Path, base_dir: Optional[Path]) -> Path:
    """
    Return the bundle-relative destination path for *source*.

    Patches below *base_dir* keep their relative layout so that the paths
    recorded in the exported KAS configuration stay valid.  Anything else is
    placed flat inside :data:`EXTERNAL_PATCH_DIR`.
    """
    if base_dir is not None:
        try:
            relative = source.relative_to(base_dir)
        except ValueError:
            relative = None
        if relative is not None and not relative.is_absolute():
            return relative
    return Path(EXTERNAL_PATCH_DIR) / source.name


def copy_patch_entries(
    patch_entries: Sequence[Dict[str, str]],
    export_dir: str,
    base_dir: Optional[str] = None,
) -> List[ExportedPatch]:
    """
    Copy patch files into the export bundle, keeping their repository.

    Args:
        patch_entries: Patch declarations as returned by
                       :meth:`KasManager.collect_patch_entries`, each with a
                       ``path`` and optionally ``repo``/``repo_path`` keys
        export_dir: Bundle directory the patches are copied into
        base_dir: Registry root used to preserve the relative patch layout

    Returns:
        List of the copied patches with their bundle-relative path
    """
    export_path = Path(export_dir).resolve()
    base_path = Path(base_dir).resolve() if base_dir else None

    copied: List[ExportedPatch] = []
    seen = set()

    for entry in patch_entries:
        source = Path(entry["path"])
        if not source.is_absolute():
            source = (base_path or export_path) / source
        source = source.resolve()

        if not source.is_file():
            logging.warning(f"Patch file not found, skipping: {source}")
            continue

        relative = _safe_relative_path(source, base_path)
        if relative in seen:
            continue
        seen.add(relative)

        destination = export_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        repo = entry.get("repo") or ""
        copied.append(
            ExportedPatch(
                path=relative,
                repo=repo,
                repo_path=entry.get("repo_path") or repo,
            )
        )

    if copied:
        logging.info(f"Copied {len(copied)} patch file(s) to {export_path}")
    else:
        logging.debug("No patch files to copy")

    return copied


def copy_patches(
    patch_files: List[str],
    export_dir: str,
    base_dir: Optional[str] = None,
) -> List[Path]:
    """
    Copy patch files into the export bundle.

    Args:
        patch_files: Absolute paths of the patch files to copy
        export_dir: Bundle directory the patches are copied into
        base_dir: Registry root used to preserve the relative patch layout

    Returns:
        List of bundle-relative paths of the copied patches
    """
    copied = copy_patch_entries(
        [{"path": patch_file} for patch_file in patch_files],
        export_dir,
        base_dir=base_dir,
    )
    return [patch.path for patch in copied]


def copy_container(
    container: Optional[Docker],
    export_dir: str,
    base_dir: Optional[str] = None,
) -> Optional[ExportedContainer]:
    """
    Copy the container definition of the exported build into the bundle.

    The Dockerfile referenced by the registry container is copied into the
    bundle's :data:`CONTAINER_DIR` so that the image can be rebuilt on the
    target machine.  Containers that only reference a pre-built image (no
    ``file``) are recorded without copying anything.

    Args:
        container: Resolved container configuration of the build
        export_dir: Bundle directory the Dockerfile is copied into
        base_dir: Registry root used to resolve a relative Dockerfile path

    Returns:
        The recorded container information, or ``None`` when the build does
        not use a container at all
    """
    if container is None:
        return None

    export_path = Path(export_dir).resolve()
    base_path = Path(base_dir).resolve() if base_dir else None

    exported = ExportedContainer(
        image=container.image,
        args=[(arg.name, arg.value) for arg in container.args],
        privileged=container.privileged,
        runtime_args=container.runtime_args,
    )

    if container.file:
        source = Path(container.file)
        if not source.is_absolute():
            source = (base_path or export_path) / source
        source = source.resolve()

        if source.is_file():
            relative = Path(CONTAINER_DIR) / source.name
            destination = export_path / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            exported.dockerfile = relative
            logging.info(f"Copied container definition to {destination}")
        else:
            logging.warning(f"Dockerfile not found, skipping: {source}")

    if exported.image is None and exported.dockerfile is None:
        logging.debug("Container has neither an image nor a Dockerfile")
        return None

    return exported


def _shell_value(value: str) -> str:
    """
    Return *value* as a double-quoted shell string.

    ``$ENV{VAR}`` placeholders used by the registry are translated into
    ``${VAR}`` shell references so that the exported environment resolves
    against the target machine instead of the exporting one.
    """
    placeholders: List[str] = []

    def _stash(match: "re.Match") -> str:
        placeholders.append(match.group(1))
        return f"\0{len(placeholders) - 1}\0"

    stashed = _ENV_PLACEHOLDER.sub(_stash, str(value))
    escaped = _escape_double_quoted(stashed)
    for index, name in enumerate(placeholders):
        escaped = escaped.replace(f"\0{index}\0", f"${{{name}}}")
    return f'"{escaped}"'


def write_environment_file(
    variables: List[EnvironmentVariable],
    export_dir: str,
    label: str = "",
) -> Optional[Path]:
    """
    Write the build environment variables into the bundle.

    The generated file is a POSIX shell snippet sourced by the setup script.
    Variables already present in the caller's environment take precedence so
    that the exported defaults can be overridden without editing the bundle.

    Args:
        variables: Environment variables from the BSP registry model, in
                   increasing order of precedence (later entries win)
        export_dir: Bundle directory the environment file is written to
        label: Human readable description of the exported configuration

    Returns:
        Path to the generated file, or ``None`` when there is nothing to write
    """
    merged = {}
    for variable in variables:
        merged[variable.name] = variable.value

    if not merged:
        logging.debug("No environment variables to export")
        return None

    export_path = Path(export_dir)
    export_path.mkdir(parents=True, exist_ok=True)
    env_path = export_path / ENVIRONMENT_FILE_NAME

    lines = [
        "# Build environment for: " + (_sanitize_comment(label) or "BSP export"),
        "# Generated by bsp-registry-tools -- edit freely.",
        "# Values already set in the environment take precedence.",
        "",
    ]
    for name, value in merged.items():
        lines.append(f": ${{{name}:={_shell_value(value)}}}")
        lines.append(f"export {name}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logging.info(f"Environment file generated: {env_path}")
    return env_path


def _escape_double_quoted(text: str) -> str:
    """Escape *text* for safe interpolation inside a double-quoted shell string."""
    for char in ("\\", '"', "$", "`"):
        text = text.replace(char, "\\" + char)
    return text


def _sanitize_comment(text: str) -> str:
    """Collapse *text* into a single line safe for a shell comment."""
    return " ".join(str(text).split())


def _environment_snippet(env_file_name: str) -> str:
    """Return the shell snippet sourcing the exported environment file."""
    return f"""
if [ -f "$SCRIPT_DIR/{env_file_name}" ]; then
    . "$SCRIPT_DIR/{env_file_name}"
fi
"""


def _container_snippet(container: ExportedContainer) -> str:
    """Return the shell snippet preparing the build container image."""
    image = _escape_double_quoted(container.image or DEFAULT_CONTAINER_IMAGE)
    lines = [
        "",
        f'KAS_CONTAINER_IMAGE="${{KAS_CONTAINER_IMAGE:-{image}}}"',
        "export KAS_CONTAINER_IMAGE",
        'CONTAINER_ENGINE="${KAS_CONTAINER_ENGINE:-docker}"',
        "export KAS_CONTAINER_ENGINE=\"$CONTAINER_ENGINE\"",
    ]

    if container.dockerfile is not None:
        dockerfile = _escape_double_quoted(container.dockerfile.as_posix())
        build_args = "".join(
            f' \\\n        --build-arg "{_escape_double_quoted(name)}={_escape_double_quoted(value)}"'
            for name, value in container.args
        )
        lines.append(
            f"""
DOCKERFILE="$SCRIPT_DIR/{dockerfile}"
if [ -f "$DOCKERFILE" ] \\
   && ! "$CONTAINER_ENGINE" image inspect "$KAS_CONTAINER_IMAGE" >/dev/null 2>&1; then
    echo "Building container image $KAS_CONTAINER_IMAGE ..."
    "$CONTAINER_ENGINE" build -f "$DOCKERFILE" -t "$KAS_CONTAINER_IMAGE"{build_args} \\
        "$SCRIPT_DIR"
fi"""
        )

    return "\n".join(lines) + "\n"


def _kas_command(container: Optional[ExportedContainer]) -> Tuple[str, str, str]:
    """
    Return the KAS command details used by the setup script.

    Returns:
        Tuple of (default command, install hint, extra command flags)
    """
    if container is None:
        return "kas", "Install it with: pip install kas", ""

    flags = " --isar" if container.privileged else ""
    if container.runtime_args:
        flags += f' --runtime-args "{_escape_double_quoted(container.runtime_args)}"'
    return (
        "kas-container",
        "It is part of the kas distribution: https://github.com/siemens/kas",
        flags,
    )


def _kas_setup_script(
    config_name: str,
    label: str,
    container: Optional[ExportedContainer] = None,
    environment_file: Optional[str] = None,
) -> str:
    """Return the contents of the KAS flavour of the setup script."""
    kas_default, kas_hint, kas_flags = _kas_command(container)
    # The hint is printed from a double-quoted echo, so swap the escaped
    # double quotes of the runtime arguments for single quotes.
    kas_hint_flags = kas_flags.replace('\\"', "'").replace('"', "'")
    env_block = _environment_snippet(environment_file) if environment_file else ""
    container_block = _container_snippet(container) if container is not None else ""
    return f"""#!/bin/sh
# Initial build setup for: {label}
# Generated by bsp-registry-tools -- edit freely.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
KAS_CONFIG="${{KAS_CONFIG:-$SCRIPT_DIR/{config_name}}}"
BUILD_DIR="${{KAS_BUILD_DIR:-$SCRIPT_DIR/build}}"
KAS="${{KAS:-{kas_default}}}"
{env_block}{container_block}
if ! command -v "$KAS" >/dev/null 2>&1; then
    echo "$KAS is not installed. {kas_hint}" >&2
    exit 1
fi

if [ ! -f "$KAS_CONFIG" ]; then
    echo "KAS configuration not found: $KAS_CONFIG" >&2
    exit 1
fi

mkdir -p "$BUILD_DIR"
export KAS_BUILD_DIR="$BUILD_DIR"

run_kas() {{
    "$KAS"{kas_flags} "$@"
}}

echo "Fetching layers for $KAS_CONFIG ..."
run_kas checkout "$KAS_CONFIG"

if [ "${{1:-}}" = "--build" ]; then
    shift
    echo "Starting build ..."
    run_kas build "$KAS_CONFIG" "$@"
else
    echo "Layers are ready. Start a build with:"
    echo "  $0 --build"
    echo "or open a shell with:"
    echo "  $KAS{kas_hint_flags} shell $KAS_CONFIG"
fi
"""


def _patch_snippet(patches: Sequence[ExportedPatch]) -> str:
    """
    Return the shell snippet applying the bundled patches.

    The patches are applied to the checkouts created by ``repo sync``; KAS
    applies them itself for KAS exports.  Patches that are already applied are
    skipped so that the script stays re-runnable.
    """
    lines = [
        "",
        "apply_patch() {",
        '    patch_repo_path="$1"',
        '    patch_file="$2"',
        '    patch_target="$SCRIPT_DIR/$patch_repo_path"',
        '    if [ ! -d "$patch_target" ]; then',
        '        echo "Skipping $patch_file: $patch_repo_path is not checked out" >&2',
        "        return 0",
        "    fi",
        '    if git -C "$patch_target" apply --reverse --check "$SCRIPT_DIR/$patch_file" '
        ">/dev/null 2>&1; then",
        '        echo "Already applied, skipping: $patch_file"',
        "        return 0",
        "    fi",
        '    echo "Applying $patch_file to $patch_repo_path ..."',
        '    git -C "$patch_target" apply "$SCRIPT_DIR/$patch_file"',
        "}",
        "",
        'echo "Applying patches ..."',
    ]
    for patch in patches:
        repo_path = _escape_double_quoted(str(patch.repo_path or patch.repo or "."))
        patch_path = _escape_double_quoted(patch.path.as_posix())
        lines.append(f'apply_patch "{repo_path}" "{patch_path}"')
    return "\n".join(lines) + "\n"


def _repo_setup_script(
    config_name: str,
    label: str,
    environment_file: Optional[str] = None,
    patches: Optional[Sequence[ExportedPatch]] = None,
) -> str:
    """Return the contents of the Android ``repo`` flavour of the setup script."""
    env_block = _environment_snippet(environment_file) if environment_file else ""
    patch_block = _patch_snippet(patches) if patches else ""
    return f"""#!/bin/sh
# Initial build setup for: {label}
# Generated by bsp-registry-tools -- edit freely.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
MANIFEST="${{REPO_MANIFEST:-$SCRIPT_DIR/{config_name}}}"
MANIFEST_REPO="$SCRIPT_DIR/.manifest-repo"
{env_block}
if ! command -v repo >/dev/null 2>&1; then
    echo "repo is not installed. See https://gerrit.googlesource.com/git-repo" >&2
    exit 1
fi

if [ ! -f "$MANIFEST" ]; then
    echo "Repo manifest not found: $MANIFEST" >&2
    exit 1
fi

# `repo init` requires a git repository holding the manifest, so wrap the
# exported manifest in a local one.
rm -rf "$MANIFEST_REPO"
mkdir -p "$MANIFEST_REPO"
cp "$MANIFEST" "$MANIFEST_REPO/default.xml"
git -C "$MANIFEST_REPO" init -q
git -C "$MANIFEST_REPO" add default.xml
git -C "$MANIFEST_REPO" -c user.email=bsp@example.com -c user.name=bsp \\
    commit -q -m "Exported manifest"

echo "Initializing repo workspace ..."
repo init -u "$MANIFEST_REPO" -m default.xml
repo sync "$@"
{patch_block}
echo "Sources are ready in $SCRIPT_DIR"
"""


def generate_setup_script(
    export_dir: str,
    config_name: str,
    repo_manifest: bool = False,
    label: str = "",
    container: Optional[ExportedContainer] = None,
    environment_file: Optional[str] = None,
    patches: Optional[Sequence[ExportedPatch]] = None,
) -> Path:
    """
    Write the initial build setup shell script into the bundle.

    Args:
        export_dir: Bundle directory the script is written to
        config_name: Bundle-relative name of the exported configuration
        repo_manifest: Whether the configuration is an Android repo manifest
        label: Human readable description of the exported configuration
        container: Container information of the exported build.  When given,
                   the script builds the image (if a Dockerfile was bundled)
                   and drives the build through ``kas-container``.
        environment_file: Bundle-relative name of the exported environment
                          file sourced by the script
        patches: Patches bundled with the export.  Only used for
                 ``repo_manifest`` exports, where the script applies them to
                 the checkouts created by ``repo sync``.

    Returns:
        Path to the generated script
    """
    export_path = Path(export_dir)
    export_path.mkdir(parents=True, exist_ok=True)
    script_path = export_path / SETUP_SCRIPT_NAME

    label = _sanitize_comment(label) or "BSP export"
    safe_config_name = _escape_double_quoted(config_name)
    safe_env_name = (
        _escape_double_quoted(environment_file) if environment_file else None
    )
    content = (
        _repo_setup_script(
            safe_config_name,
            label,
            environment_file=safe_env_name,
            patches=patches,
        )
        if repo_manifest
        else _kas_setup_script(
            safe_config_name,
            label,
            container=container,
            environment_file=safe_env_name,
        )
    )

    script_path.write_text(content, encoding="utf-8")
    script_path.chmod(0o755)

    logging.info(f"Setup script generated: {script_path}")
    return script_path


def _readme_contents_section(
    config_name: str,
    repo_manifest: bool,
    patches: Optional[Sequence[Path]],
    container: Optional[ExportedContainer],
    environment_file: Optional[str],
    setup_script: bool,
) -> List[str]:
    """Return the markdown lines describing the files of the bundle."""
    config_description = (
        "Exported Android `repo` manifest"
        if repo_manifest
        else "Exported KAS configuration"
    )
    lines = [
        "## Contents",
        "",
        "| Path | Description |",
        "| --- | --- |",
        f"| `{config_name}` | {config_description} |",
    ]

    if patches:
        directories = sorted({(patch.parent.as_posix() or ".") for patch in patches})
        listed = ", ".join(f"`{directory}/`" for directory in directories)
        lines.append(
            f"| {listed} | {len(patches)} patch file(s) referenced by the configuration |"
        )
    if container is not None and container.dockerfile is not None:
        lines.append(
            f"| `{container.dockerfile.as_posix()}` | Dockerfile of the build container |"
        )
    if environment_file:
        lines.append(
            f"| `{environment_file}` | Build environment variables of the exported build |"
        )
    if setup_script:
        lines.append(f"| `{SETUP_SCRIPT_NAME}` | Initial build setup script |")

    lines.append("")
    return lines


def _readme_container_section(container: ExportedContainer) -> List[str]:
    """Return the markdown lines describing the build container."""
    lines = ["## Build container", ""]
    image = container.image or DEFAULT_CONTAINER_IMAGE
    lines.append(f"* Image: `{image}`")
    if container.dockerfile is not None:
        lines.append(
            f"* Dockerfile: `{container.dockerfile.as_posix()}` (the image is built "
            "automatically when it is not available locally)"
        )
    else:
        lines.append("* The image is pulled from the registry, no Dockerfile is bundled.")
    for name, value in container.args:
        lines.append(f"* Build argument: `{name}={value}`")
    if container.privileged:
        lines.append("* Runs in privileged mode (`kas-container --isar`).")
    if container.runtime_args:
        lines.append(f"* Runtime arguments: `{container.runtime_args}`")
    lines.append("")
    return lines


def _readme_usage_section(
    config_name: str,
    repo_manifest: bool,
    container: Optional[ExportedContainer],
    setup_script: bool,
    has_patches: bool = False,
) -> List[str]:
    """Return the markdown lines describing how to use the bundle."""
    lines = ["## Usage", ""]

    if repo_manifest:
        lines.extend([
            "Requires the [`repo`](https://gerrit.googlesource.com/git-repo) tool.",
            "",
        ])
        if setup_script:
            lines.extend([
                "```sh",
                f"./{SETUP_SCRIPT_NAME}",
                "```",
                "",
                "The script wraps the exported manifest in a local git repository and "
                "runs `repo init` / `repo sync` in the bundle directory.  Additional "
                "arguments are forwarded to `repo sync`.",
                "",
            ])
            if has_patches:
                lines.extend([
                    "After the sync the bundled patches are applied to the checked out "
                    "repositories with `git apply`.  Patches that are already applied "
                    "are skipped, so the script can be re-run safely.",
                    "",
                ])
        else:
            lines.extend([
                "Initialize a workspace from the exported manifest with `repo init` "
                f"pointing at `{config_name}`, then run `repo sync`.",
                "",
            ])
            if has_patches:
                lines.extend([
                    "Afterwards apply the bundled patches to the checked out "
                    "repositories with `git apply`.",
                    "",
                ])
        return lines

    kas_default, kas_hint, kas_flags = _kas_command(container)
    hint_flags = kas_flags.replace('\\"', "'").replace('"', "'")
    lines.extend([
        f"Requires `{kas_default}`. {kas_hint}",
        "",
    ])
    if setup_script:
        lines.extend([
            "```sh",
            f"./{SETUP_SCRIPT_NAME}           # fetch the layers",
            f"./{SETUP_SCRIPT_NAME} --build   # fetch the layers and start the build",
            "```",
            "",
            "The build directory defaults to `build/` inside the bundle and can be "
            "changed with `KAS_BUILD_DIR`.  Open a shell in the build environment "
            f"with `{kas_default}{hint_flags} shell {config_name}`.",
            "",
        ])
    else:
        lines.extend([
            "```sh",
            f"{kas_default}{hint_flags} checkout {config_name}",
            f"{kas_default}{hint_flags} build {config_name}",
            "```",
            "",
        ])
    return lines


def write_readme(
    export_dir: str,
    config_name: str,
    repo_manifest: bool = False,
    label: str = "",
    patches: Optional[Sequence[Union[Path, ExportedPatch]]] = None,
    container: Optional[ExportedContainer] = None,
    environment_file: Optional[str] = None,
    setup_script: bool = True,
) -> Path:
    """
    Write the documentation of the export bundle.

    The generated ``README.md`` describes which files the bundle contains and
    how to reproduce the exported build from it.

    Args:
        export_dir: Bundle directory the readme is written to
        config_name: Bundle-relative name of the exported configuration
        repo_manifest: Whether the configuration is an Android repo manifest
        label: Human readable description of the exported configuration
        patches: Bundle-relative paths of the copied patch files
        container: Container information of the exported build
        environment_file: Bundle-relative name of the exported environment file
        setup_script: Whether the bundle contains the setup script

    Returns:
        Path to the generated readme
    """
    export_path = Path(export_dir)
    export_path.mkdir(parents=True, exist_ok=True)
    readme_path = export_path / README_FILE_NAME

    patch_paths = [
        patch.path if isinstance(patch, ExportedPatch) else patch
        for patch in (patches or [])
    ]

    title = _sanitize_comment(label) or "BSP export"
    lines = [
        f"# {title}",
        "",
        "Self-contained build bundle generated by "
        "[bsp-registry-tools](https://github.com/miketsukerman/bsp-registry-tools).",
        "It contains everything needed to reproduce the exported build on "
        "another machine.",
        "",
    ]
    lines.extend(
        _readme_contents_section(
            config_name,
            repo_manifest,
            patch_paths,
            container,
            environment_file,
            setup_script,
        )
    )
    lines.extend(
        _readme_usage_section(
            config_name, repo_manifest, container, setup_script, bool(patch_paths)
        )
    )
    if container is not None and not repo_manifest:
        lines.extend(_readme_container_section(container))
    if environment_file:
        lines.extend([
            "## Environment",
            "",
            f"`{environment_file}` holds the environment variables of the exported "
            "build.  Every variable is only assigned when it is not already set, so "
            "the values can be overridden from the calling environment.",
            "",
        ])

    readme_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    logging.info(f"Bundle readme generated: {readme_path}")
    return readme_path
