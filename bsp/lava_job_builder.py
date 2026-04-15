"""
LAVA job YAML builder.

Generates a LAVA job definition from a :class:`~bsp.resolver.ResolvedConfig`
and an optional Jinja2 template.  When no template is provided a minimal
built-in template is used that boots the image via QEMU (useful as a starting
point for real devices).

Template context variables
--------------------------
All variables below are available in the Jinja2 template:

``device_type``
    LAVA device type label (from the preset's ``testing.lava.device_type``).
``job_name``
    Human-readable job name composed from device slug, release slug, and any
    active feature slugs.
``image_url``
    Full URL to the primary image artifact.  Resolution priority:

    1. ``artifact_url`` when provided (full override, no composition).
    2. ``artifact_server_url`` + ``artifact_name`` when both are non-empty
       (``<artifact_server_url>/<artifact_name>``).
    3. ``artifact_server_url`` + ``build_path`` when ``artifact_name`` is
       absent (legacy behaviour, same as before).
    4. Empty string when nothing is configured.

    Templates may use ``image_url`` directly or build custom URLs from the
    individual context variables below.
``artifact_url``
    Full override URL (from ``testing.lava.artifact_url`` or ``--artifact-url``
    on the CLI).  When non-empty this value is used as-is for ``image_url``.
``artifact_server_url``
    Base URL of the artifact server (from ``lava.artifact_server_url`` at
    registry level or ``testing.lava.artifact_server_url`` at preset level).
``artifact_name``
    Image file name (from ``testing.lava.artifact_name``).  Combined with
    ``artifact_server_url`` to form ``image_url``.
``build_path``
    Relative build output directory path (from :attr:`ResolvedConfig.build_path`).
``device_slug``
    Device slug (e.g. ``"qemu-arm64"``).
``release_slug``
    Release slug (e.g. ``"scarthgap"``).
``feature_slugs``
    List of active feature slugs (may be empty).
``lava_tags``
    List of LAVA device tags required by this job.
``robot_suites``
    List of Robot Framework ``.robot`` file paths to execute (may be empty).
``robot_variables``
    Dict of Robot Framework variables passed via ``--variable`` (may be empty).
``timeout_minutes``
    Overall job timeout in minutes (derived from *wait_timeout // 60*).
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional

try:
    from jinja2 import Environment, FileSystemLoader, BaseLoader, TemplateNotFound
    JINJA2_AVAILABLE = True
except ImportError:  # pragma: no cover
    JINJA2_AVAILABLE = False

from .resolver import ResolvedConfig

# =============================================================================
# Built-in fallback job template
# =============================================================================

_BUILTIN_TEMPLATE = """\
job_name: "{{ job_name }}"
device_type: {{ device_type }}
{% if lava_tags %}
tags:
{% for tag in lava_tags %}
  - {{ tag }}
{% endfor %}
{% endif %}

timeouts:
  job:
    minutes: {{ timeout_minutes }}
  action:
    minutes: {{ [timeout_minutes // 2, 1] | max }}

priority: medium
visibility: public

actions:
{% if image_url %}
  - deploy:
      timeout:
        minutes: {{ [timeout_minutes // 4, 5] | max }}
      to: tmpfs
      images:
        rootfs:
          url: {{ image_url }}
          image_arg: "-drive file={rootfs},format=raw"

  - boot:
      timeout:
        minutes: 5
      method: qemu
      media: tmpfs
      auto_login:
        login_prompt: "login:"
        username: root
      prompts:
        - "root@"
{% endif %}

{% if robot_suites %}
  - test:
      timeout:
        minutes: {{ [timeout_minutes // 2, 10] | max }}
      definitions:
{% for suite in robot_suites %}
        - repository: {{ suite }}
          from: file
          name: "{{ suite | basename | replace('.robot', '') }}"
          path: {{ suite }}
{% if robot_variables %}
          parameters:
{% for var_name, var_value in robot_variables.items() %}
            {{ var_name }}: "{{ var_value }}"
{% endfor %}
{% endif %}
{% endfor %}
{% else %}
  - test:
      timeout:
        minutes: {{ [timeout_minutes // 2, 5] | max }}
      definitions:
        - repository: https://git.linaro.org/qa/test-definitions.git
          from: git
          path: automated/linux/smoke/smoke.yaml
          name: smoke
{% endif %}
"""

logger = logging.getLogger(__name__)


# =============================================================================
# Jinja2 helpers
# =============================================================================

def _basename_filter(path: str) -> str:
    """Jinja2 filter that returns the basename of a path string."""
    return Path(path).name


def _make_jinja_env(template_dir: Optional[str] = None) -> "Environment":
    """
    Create a Jinja2 Environment.

    When *template_dir* is given, a :class:`FileSystemLoader` is used so that
    templates can include or extend other templates located in the same
    directory.  Otherwise a :class:`BaseLoader` backed by the built-in
    template string is used.

    Args:
        template_dir: Optional directory containing the job template file.

    Returns:
        Configured :class:`jinja2.Environment` instance.
    """
    if template_dir:
        loader = FileSystemLoader(template_dir)
    else:
        loader = BaseLoader()

    # keep_trailing_newline ensures rendered YAML ends with a newline, as
    # required by LAVA's job submission API which rejects definitions without it.
    env = Environment(loader=loader, keep_trailing_newline=True)
    env.filters["basename"] = _basename_filter
    env.filters["max"] = max
    return env


# =============================================================================
# Public builder function
# =============================================================================


def build_lava_job(
    resolved: ResolvedConfig,
    device_type: str,
    artifact_url: str = "",
    artifact_server_url: str = "",
    artifact_name: str = "",
    job_template_path: Optional[str] = None,
    lava_tags: Optional[List[str]] = None,
    robot_suites: Optional[List[str]] = None,
    robot_variables: Optional[Dict[str, str]] = None,
    wait_timeout: int = 3600,
) -> str:
    """
    Render a LAVA job definition YAML string.

    Args:
        resolved: Resolved BSP build configuration (device, release, features,
                  build_path, etc.).
        device_type: LAVA device-type label (e.g. ``"qemu-aarch64"``).
        artifact_url: Complete URL to the primary image artifact.  When
                      non-empty this value is used as ``image_url`` directly,
                      bypassing ``artifact_server_url`` + ``artifact_name``
                      composition.
        artifact_server_url: Base URL of the artifact server
                             (e.g. ``"http://fileserver/builds"``).  Combined
                             with ``artifact_name`` (or ``build_path`` when
                             ``artifact_name`` is empty) to form ``image_url``
                             when ``artifact_url`` is not provided.
        artifact_name: Image file name
                       (e.g. ``"core-image-minimal-qemu.wic.gz"``).
                       When provided, ``image_url`` is composed as
                       ``<artifact_server_url>/<artifact_name>``.
                       When empty, the legacy ``<artifact_server_url>/<build_path>``
                       composition is used.
        job_template_path: Optional path to a Jinja2 ``.yaml.j2`` template
                           file.  When ``None`` the built-in minimal template
                           is used.
        lava_tags: Optional list of LAVA device tags for scheduler matching.
        robot_suites: Optional list of Robot Framework suite paths.
        robot_variables: Optional dict of Robot Framework ``--variable`` pairs.
        wait_timeout: Overall job timeout in seconds (converted to minutes for
                      the template).

    Returns:
        Rendered LAVA job definition as a YAML string.

    Raises:
        RuntimeError: When Jinja2 is not installed.
        FileNotFoundError: When *job_template_path* points to a missing file.
    """
    if not JINJA2_AVAILABLE:
        raise RuntimeError(  # pragma: no cover
            "The 'jinja2' package is required for LAVA job generation. "
            "Install it with: pip install jinja2"
        )

    # Compose job name from slugs
    feature_slugs = [f.slug for f in (resolved.features or [])]
    job_name_parts = [resolved.device.slug, resolved.release.slug] + feature_slugs
    job_name = "-".join(job_name_parts)

    # Build the image artifact URL (priority: full artifact_url > server+name > server+path)
    build_path = resolved.build_path or ""
    if artifact_url:
        image_url = artifact_url
    elif artifact_server_url and artifact_name:
        image_url = artifact_server_url.rstrip("/") + "/" + artifact_name.lstrip("/")
    elif artifact_server_url and build_path:
        image_url = artifact_server_url.rstrip("/") + "/" + build_path.lstrip("/")
    else:
        image_url = artifact_url or ""

    context: Dict = {
        "device_type": device_type,
        "job_name": job_name,
        "image_url": image_url,
        "artifact_url": artifact_url,
        "artifact_server_url": artifact_server_url,
        "artifact_name": artifact_name,
        "build_path": build_path,
        "device_slug": resolved.device.slug,
        "release_slug": resolved.release.slug,
        "feature_slugs": feature_slugs,
        "lava_tags": lava_tags or [],
        "robot_suites": robot_suites or [],
        "robot_variables": robot_variables or {},
        "timeout_minutes": max(wait_timeout // 60, 1),
    }

    if job_template_path:
        template_file = Path(job_template_path)
        if not template_file.exists():
            raise FileNotFoundError(
                f"LAVA job template not found: {job_template_path}"
            )
        env = _make_jinja_env(str(template_file.parent))
        template = env.get_template(template_file.name)
    else:
        env = _make_jinja_env()
        template = env.from_string(_BUILTIN_TEMPLATE)

    rendered = template.render(**context)
    logger.debug("Rendered LAVA job definition (%d chars)", len(rendered))
    return rendered
