"""
Google Repo manifest importer: converts a repo manifest XML file into
KAS YAML + BSP registry YAML artefacts.

The importer:

1. Parses the manifest XML and **follows all ``<include>`` directives
   recursively** (cycle-safe), producing a flat list of
   :class:`ManifestProject` objects with fully resolved URLs and revisions.

2. Respects the ``revision`` attribute on every ``<project>`` — including
   **pinned commit SHAs**.  A 40-hex-character (SHA-1) or 64-hex-character
   (SHA-256) revision is emitted as ``commit:`` in the KAS repo entry.  The
   optional ``upstream`` attribute (the branch that was current when the SHA
   was pinned) is emitted as ``branch:``.

3. Generates a self-contained **KAS YAML file** containing every repo from
   the manifest with its resolved URL, path, branch / commit, and a default
   layer mapping.  The output path mirrors the Advantech registry layout::

       vendors/<vendor>/<vendor-release>-<codename>.yml
       # or, when --soc-vendor is given:
       vendors/<board-vendor>/<soc-vendor>/<vendor-release>-<codename>.yml

4. Creates or **merges** a ``bsp-registry.yml``:

   * **Create** (default) — writes a minimal skeleton registry; errors if
     the file already exists.
   * **Merge** — reads the existing file and appends the new release /
     vendor-override / device entries without removing anything.

5. Supports an optional **hints YAML file** (``--hints``) that lets users
   override classification for specific projects (e.g. mark a project as
   ``skip`` so it is excluded from the generated KAS file) and inject extra
   device entries into the registry that cannot be inferred from the manifest
   alone (since board / machine names are highly vendor-specific).

Hints file format::

    # hints.yml
    projects:
      meta-myboard:
        role: skip            # exclude from KAS output
      meta-confidential:
        role: skip

    devices:
      - slug: myboard
        description: "My Board"
        vendor: myvendor
        soc_vendor: nxp
        includes:
          - vendors/myvendor/nxp/machine/myboard.yml
"""

import hashlib
import logging
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

logger = logging.getLogger(__name__)

# =============================================================================
# Remote manifest fetcher
# =============================================================================

DEFAULT_MANIFEST_CACHE_DIR = Path.home() / ".cache" / "bsp" / "manifests"
_URL_SCHEMES = ("http://", "https://", "git://", "ssh://", "git@")


def _looks_like_url(source: str) -> bool:
    """Return True if *source* looks like a remote Git URL."""
    return any(source.startswith(scheme) for scheme in _URL_SCHEMES)


class ManifestFetcher:
    """Clone or update a remote Git repository hosting a repo manifest.

    The clone is cached under *cache_dir* (default:
    ``~/.cache/bsp/manifests/``) so that subsequent invocations only need a
    lightweight ``git fetch``.  Each ``(url, branch)`` pair maps to a unique
    subdirectory so multiple remotes never collide.

    Example::

        fetcher = ManifestFetcher()
        manifest_path = fetcher.fetch(
            "https://github.com/nxp-imx/imx-manifest",
            branch="imx-linux-scarthgap",
            manifest_file="imx-6.6.52-2.2.0.xml",
        )
        # manifest_path is a Path to the local .xml file
    """

    def __init__(self, cache_dir: Path = DEFAULT_MANIFEST_CACHE_DIR) -> None:
        self.cache_dir = Path(cache_dir)

    # ------------------------------------------------------------------

    def fetch(
        self,
        url: str,
        branch: str = "main",
        manifest_file: str = "default.xml",
        update: bool = True,
    ) -> Path:
        """Ensure a local clone of the manifest repository is up-to-date.

        If the repository has not been cloned before it is cloned with
        ``--depth 1`` for speed.  If it has been cloned and *update* is
        ``True``, the branch is fetched and reset to the remote HEAD.

        Args:
            url:           Git repository URL.
            branch:        Branch (or tag) to check out.
            manifest_file: Name of the manifest XML file within the repo root
                           (default: ``"default.xml"``).
            update:        When ``True`` (default) synchronise an existing
                           clone with the remote.

        Returns:
            Path to the local copy of *manifest_file*.

        Raises:
            ValueError: If *manifest_file* is not found in the cloned repo.
            RuntimeError: If any git operation fails.
        """
        clone_dir = self._clone_dir(url, branch)

        if self._is_cloned(clone_dir):
            if update:
                logger.info(
                    "Updating manifest repo %s (branch: %s)", url, branch
                )
                self._update(clone_dir, branch)
            else:
                logger.info(
                    "Using cached manifest repo %s (branch: %s)", url, branch
                )
        else:
            logger.info(
                "Cloning manifest repo %s (branch: %s)", url, branch
            )
            self._clone(url, branch, clone_dir)

        manifest_path = clone_dir / manifest_file
        if not manifest_path.is_file():
            raise ValueError(
                f"Manifest file '{manifest_file}' not found in cloned repo "
                f"at {clone_dir}.  "
                f"Use --manifest-file to specify the correct file name."
            )
        return manifest_path

    def clear_cache(self, url: str, branch: str = "main") -> None:
        """Remove the cached clone for *(url, branch)*."""
        clone_dir = self._clone_dir(url, branch)
        if clone_dir.exists():
            shutil.rmtree(clone_dir)
            logger.info("Removed cached clone: %s", clone_dir)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _clone_dir(self, url: str, branch: str) -> Path:
        """Return the deterministic cache subdirectory for *(url, branch)*."""
        key = f"{url}#{branch}"
        url_hash = hashlib.sha1(key.encode()).hexdigest()[:12]
        # Derive a human-readable prefix from the repo name
        clean_url = url.rstrip("/")
        if clean_url.endswith(".git"):
            clean_url = clean_url[:-4]
        repo_name = clean_url.rsplit("/", 1)[-1] or "manifest"
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", repo_name)
        return self.cache_dir / f"{safe_name}-{url_hash}"

    def _is_cloned(self, clone_dir: Path) -> bool:
        return (clone_dir / ".git").is_dir()

    def _clone(self, url: str, branch: str, clone_dir: Path) -> None:
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "git", "clone",
            "--depth", "1",
            "--branch", branch,
            url,
            str(clone_dir),
        ]
        self._run(cmd)

    def _update(self, clone_dir: Path, branch: str) -> None:
        """Fetch and reset the clone to the remote HEAD of *branch*."""
        cmds = [
            ["git", "-C", str(clone_dir), "fetch", "--depth", "1",
             "origin", branch],
            ["git", "-C", str(clone_dir), "checkout", branch],
            ["git", "-C", str(clone_dir), "reset", "--hard",
             f"origin/{branch}"],
        ]
        for cmd in cmds:
            self._run(cmd)

    @staticmethod
    def _run(cmd: List[str]) -> None:
        logger.debug("Running: %s", " ".join(cmd))
        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Git command failed ({' '.join(cmd[:3])}, exit {exc.returncode}):\n"
                f"{exc.stderr.strip()}"
            ) from exc


# =============================================================================
# Constants
# =============================================================================

# Known Yocto LTS/stable codenames mapped to their version strings.
YOCTO_CODENAMES: Dict[str, str] = {
    "dunfell":   "3.1",
    "gatesgarth": "3.2",
    "hardknott": "3.3",
    "honister":  "3.4",
    "kirkstone": "4.0",
    "langdale":  "4.1",
    "mickledore": "4.2",
    "nanbield":  "4.3",
    "scarthgap": "5.0",
    "styhead":   "5.1",
    "walnascar": "5.2",
    "whinlatter": "5.3",
    "wrynose":   "5.4",
}

KAS_HEADER_VERSION = 14

# Matches 40-char SHA-1 or 64-char SHA-256 hashes.
_SHA_RE = re.compile(r'^[0-9a-f]{40}$|^[0-9a-f]{64}$', re.IGNORECASE)


def _is_sha(value: Optional[str]) -> bool:
    """Return True if *value* looks like a Git commit SHA."""
    return bool(value and _SHA_RE.match(value))


# =============================================================================
# Data classes
# =============================================================================

@dataclass
class ManifestRemote:
    """A ``<remote>`` element in a repo manifest."""
    name: str
    fetch: str   # base URL (trailing slash stripped)
    alias: Optional[str] = None


@dataclass
class ManifestProject:
    """A ``<project>`` element with all attributes resolved."""
    name: str
    path: str
    url: str
    revision: Optional[str] = None    # branch, tag, or SHA
    upstream: Optional[str] = None    # branch name when revision is a SHA
    groups: List[str] = field(default_factory=list)
    copyfiles: List[Tuple[str, str]] = field(default_factory=list)
    linkfiles: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class RepoManifest:
    """Result of parsing a repo manifest (may be a merged tree after includes)."""
    projects: List[ManifestProject] = field(default_factory=list)
    default_revision: Optional[str] = None
    default_remote: Optional[str] = None


@dataclass
class ImportHints:
    """Parsed content of an optional hints YAML file."""
    # project-name → role dict (keys: role, slug, vendor)
    projects: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Extra device entries to inject into the registry unconditionally.
    devices: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ImportResult:
    """Summary of what the importer produced."""
    kas_files: List[Tuple[str, str]] = field(default_factory=list)
    # (relative path, YAML string)
    registry_path: Optional[Path] = None
    warnings: List[str] = field(default_factory=list)


# =============================================================================
# ManifestParser
# =============================================================================

class ManifestParser:
    """Parse a Google Repo XML manifest, following ``<include>`` directives.

    Include chains are traversed depth-first.  Cycles are detected and logged
    as warnings; the duplicate file is silently skipped.

    The ``<default>`` element's ``revision`` and ``remote`` apply to every
    ``<project>`` that does not override them explicitly.  Per-project
    ``revision`` always wins over the default.
    """

    def parse(
        self,
        manifest_path: Path,
        base_dir: Optional[Path] = None,
        visited: Optional[Set[Path]] = None,
    ) -> RepoManifest:
        """Parse *manifest_path* and return a :class:`RepoManifest`.

        Args:
            manifest_path: Path to the ``.xml`` manifest file.
            base_dir:      Directory used to resolve relative ``<include>``
                           names.  Defaults to the directory of
                           *manifest_path*.
            visited:       Set of already-visited absolute paths — prevents
                           infinite include loops.  Allocated automatically
                           on the first call.

        Returns:
            A :class:`RepoManifest` with all projects fully resolved.

        Raises:
            ValueError: If the file cannot be parsed or the root element is
                        not ``<manifest>``.
        """
        manifest_path = Path(manifest_path).resolve()
        if base_dir is None:
            base_dir = manifest_path.parent
        if visited is None:
            visited = set()

        if manifest_path in visited:
            logger.warning(
                "Cycle detected in manifest includes: %s (skipping)", manifest_path
            )
            return RepoManifest()
        visited.add(manifest_path)

        try:
            tree = ET.parse(str(manifest_path))
        except (ET.ParseError, OSError) as exc:
            raise ValueError(
                f"Cannot parse manifest '{manifest_path}': {exc}"
            ) from exc

        root = tree.getroot()
        if root.tag != "manifest":
            raise ValueError(
                f"Root element must be <manifest>, got <{root.tag}>"
            )

        # ── Collect remotes ────────────────────────────────────────────────
        remotes: Dict[str, ManifestRemote] = {}
        for elem in root.findall("remote"):
            name = elem.get("name", "")
            fetch = elem.get("fetch", "")
            if not name or not fetch:
                logger.warning("<remote> missing name or fetch attribute; skipping")
                continue
            remotes[name] = ManifestRemote(
                name=name,
                fetch=fetch.rstrip("/"),
                alias=elem.get("alias"),
            )

        # ── Collect default ────────────────────────────────────────────────
        default_elem = root.find("default")
        default_revision: Optional[str] = None
        default_remote_name: Optional[str] = None
        if default_elem is not None:
            default_revision = default_elem.get("revision") or None
            default_remote_name = default_elem.get("remote") or None

        # ── Process includes depth-first ───────────────────────────────────
        included = RepoManifest()
        for inc_elem in root.findall("include"):
            inc_name = inc_elem.get("name")
            if not inc_name:
                continue
            inc_path = (Path(base_dir) / inc_name).resolve()
            sub = self.parse(inc_path, base_dir=inc_path.parent, visited=visited)
            included.projects.extend(sub.projects)
            if sub.default_revision and not included.default_revision:
                included.default_revision = sub.default_revision
            if sub.default_remote and not included.default_remote:
                included.default_remote = sub.default_remote

        # Effective defaults: local <default> wins over any included default.
        eff_revision = default_revision or included.default_revision
        eff_remote = default_remote_name or included.default_remote

        # ── Collect projects ───────────────────────────────────────────────
        projects: List[ManifestProject] = list(included.projects)
        for elem in root.findall("project"):
            name = elem.get("name", "")
            if not name:
                logger.warning("<project> missing name attribute; skipping")
                continue

            path = elem.get("path") or name
            revision = elem.get("revision") or eff_revision
            upstream = elem.get("upstream") or None
            remote_name = elem.get("remote") or eff_remote

            # Resolve absolute URL
            direct_url = elem.get("url")
            if direct_url:
                url = direct_url
            elif remote_name and remote_name in remotes:
                url = f"{remotes[remote_name].fetch}/{name}"
            else:
                logger.warning(
                    "Project '%s' has no resolvable remote URL; "
                    "using name as placeholder",
                    name,
                )
                url = name

            groups_str = elem.get("groups", "")
            groups = [g.strip() for g in groups_str.split(",") if g.strip()]

            copyfiles: List[Tuple[str, str]] = [
                (cf.get("src", ""), cf.get("dest", ""))
                for cf in elem.findall("copyfile")
                if cf.get("src") and cf.get("dest")
            ]
            linkfiles: List[Tuple[str, str]] = [
                (lf.get("src", ""), lf.get("dest", ""))
                for lf in elem.findall("linkfile")
                if lf.get("src") and lf.get("dest")
            ]

            projects.append(ManifestProject(
                name=name,
                path=path,
                url=url,
                revision=revision,
                upstream=upstream,
                groups=groups,
                copyfiles=copyfiles,
                linkfiles=linkfiles,
            ))

        return RepoManifest(
            projects=projects,
            default_revision=eff_revision,
            default_remote=eff_remote,
        )


# =============================================================================
# Hints loader
# =============================================================================

def load_hints(hints_path: Optional[Path]) -> ImportHints:
    """Load an optional classification hints YAML file.

    Returns an empty :class:`ImportHints` if *hints_path* is ``None`` or the
    file does not exist.
    """
    if hints_path is None or not Path(hints_path).is_file():
        return ImportHints()
    with open(hints_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    projects_raw = raw.get("projects") or {}
    if not isinstance(projects_raw, dict):
        projects_raw = {}
    devices_raw = raw.get("devices") or []
    if not isinstance(devices_raw, list):
        devices_raw = []
    return ImportHints(projects=projects_raw, devices=devices_raw)


# =============================================================================
# KAS YAML generation
# =============================================================================

# Well-known layer sub-directory names for common repos.  Each value is an
# ordered list of layer names (relative to the repo root); None means the
# layer is enabled without a path override.  Missing entry → default to
# a single layer named after the last component of the project path.
_KNOWN_LAYERS: Dict[str, List[Tuple[str, Optional[str]]]] = {
    "poky": [
        ("meta", None),
        ("meta-poky", None),
        ("meta-yocto-bsp", None),
    ],
    "bitbake": [
        ("bitbake", "disabled"),
    ],
    "meta-openembedded": [
        ("meta-oe", None),
        ("meta-python", None),
        ("meta-networking", None),
        ("meta-filesystems", None),
        ("meta-multimedia", None),
        ("meta-webserver", None),
        ("meta-gnome", None),
        ("meta-xfce", None),
    ],
    "meta-imx": [
        ("meta-imx-bsp", None),
        ("meta-imx-sdk", None),
        ("meta-imx-ml", None),
        ("meta-imx-cockpit", None),
        ("meta-imx-v2x", None),
    ],
    "meta-arm": [
        ("meta-arm", None),
        ("meta-arm-toolchain", None),
    ],
    "meta-security": [
        (".", None),
        ("meta-parsec", "disabled"),
        ("meta-tpm", "disabled"),
    ],
    "meta-virtualization": [(".", "disabled")],
    "meta-selinux":       [(".", None)],
    "meta-freescale":     [(".", None)],
    "meta-freescale-3rdparty": [(".", None)],
    "meta-freescale-distro":   [(".", None)],
}


def _repo_key(project: ManifestProject) -> str:
    """Return the KAS repos dict key for a project (basename of path)."""
    return Path(project.path).name


def _project_to_kas_repo(
    project: ManifestProject,
    default_branch: Optional[str],
) -> dict:
    """Convert a :class:`ManifestProject` to a KAS repo entry dict.

    Revision handling:
    * If the ``revision`` field is a SHA (40 or 64 hex chars), it is emitted
      as ``commit:``.  If the project also has an ``upstream`` field (the
      branch that was current when the commit was pinned), it is emitted as
      ``branch:`` alongside ``commit:``.
    * Otherwise the revision is treated as a branch / tag name and emitted
      as ``branch:``.
    * If there is no revision at all, ``default_branch`` (the manifest-level
      ``<default revision="...">`` value) is used as a fallback branch.
    """
    repo: dict = {}
    repo["url"] = project.url
    if project.path:
        repo["path"] = project.path

    rev = project.revision or default_branch
    if rev:
        if _is_sha(rev):
            repo["commit"] = rev
            if project.upstream:
                repo["branch"] = project.upstream
        else:
            repo["branch"] = rev

    # Layer entries
    key = _repo_key(project)
    known = _KNOWN_LAYERS.get(key)
    if known is not None:
        repo["layers"] = {name: value for name, value in known}
    else:
        repo["layers"] = {key: None}

    return repo


# Custom YAML dumper that writes None as bare keys (`` key:``) instead of
# ``key: null``, matching the Advantech registry style.
class _BspDumper(yaml.Dumper):
    pass


def _represent_none(dumper: yaml.Dumper, _: None) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:null", "")


_BspDumper.add_representer(type(None), _represent_none)


def _dump_yaml(data: Any) -> str:
    """Serialise *data* to a YAML string using the BSP registry style."""
    return yaml.dump(
        data,
        Dumper=_BspDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )


def _build_kas_dict(
    projects: List[ManifestProject],
    default_branch: Optional[str],
    skip_names: Optional[Set[str]] = None,
) -> dict:
    """Build a KAS YAML dict from *projects*.

    Args:
        projects:      List of manifest projects.
        default_branch: Fallback branch / codename used when a project has no
                        revision.
        skip_names:    Set of project names to exclude from the output.

    Returns:
        A ``dict`` suitable for serialisation to a KAS YAML file.
    """
    skip_names = skip_names or set()
    repos: dict = {}
    for p in projects:
        if p.name in skip_names:
            continue
        key = _repo_key(p)
        if key not in repos:   # first occurrence wins
            repos[key] = _project_to_kas_repo(p, default_branch)

    result: dict = {"header": {"version": KAS_HEADER_VERSION}}
    if repos:
        result["repos"] = repos
    return result


# =============================================================================
# BSP registry helpers
# =============================================================================

def _detect_codename(manifest: RepoManifest) -> Optional[str]:
    """Try to detect the Yocto codename from manifest revisions."""
    candidates: List[str] = []
    if manifest.default_revision:
        candidates.append(manifest.default_revision)
    for p in manifest.projects:
        if p.revision and not _is_sha(p.revision):
            candidates.append(p.revision)
        if p.upstream:
            candidates.append(p.upstream)
    for candidate in candidates:
        lower = candidate.lower()
        for codename in YOCTO_CODENAMES:
            if codename in lower:
                return codename
    return None


def _slugify(text: str) -> str:
    """Return a lower-case, hyphen-separated slug from *text*."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


# =============================================================================
# RegistryMerger — load / update / save bsp-registry.yml
# =============================================================================

class RegistryMerger:
    """Load, update, and save a ``bsp-registry.yml`` file.

    All mutating helpers follow an *upsert* semantics: they add an entry if
    it does not already exist and silently return without modifying the file
    if it does.  This makes ``--merge`` idempotent.
    """

    def load(self, path: Path) -> dict:
        """Return the parsed registry dict, or an empty skeleton."""
        if path.is_file():
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            # Ensure the registry key exists
            data.setdefault("registry", {})
            return data
        return {"specification": {"version": "2.0"}, "registry": {}}

    def save(self, path: Path, data: dict) -> None:
        """Write *data* back to *path*, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_dump_yaml(data))

    # ── Vendor ────────────────────────────────────────────────────────────

    def upsert_vendor(
        self,
        data: dict,
        slug: str,
        name: str = "",
        description: str = "",
    ) -> bool:
        """Add a vendor entry if one with *slug* does not already exist.

        Returns True if added, False if already present.
        """
        registry = data.setdefault("registry", {})
        vendors: List[dict] = registry.setdefault("vendors", [])
        if any(v.get("slug") == slug for v in vendors):
            return False
        entry: dict = {"slug": slug, "name": name or slug.title()}
        if description:
            entry["description"] = description
        vendors.append(entry)
        return True

    # ── Device ────────────────────────────────────────────────────────────

    def upsert_device(self, data: dict, device: dict) -> bool:
        """Add a device entry if one with the same slug does not exist.

        Returns True if added, False if already present.
        """
        registry = data.setdefault("registry", {})
        devices: List[dict] = registry.setdefault("devices", [])
        slug = device.get("slug", "")
        if any(d.get("slug") == slug for d in devices):
            return False
        devices.append(device)
        return True

    # ── Release ───────────────────────────────────────────────────────────

    def upsert_release(
        self,
        data: dict,
        slug: str,
        description: str,
        includes: List[str],
    ) -> dict:
        """Find or create a release entry, returning the dict.

        If the release already exists its ``includes`` and ``vendor_overrides``
        are left untouched — only the new vendor override added by the caller
        will be appended.
        """
        registry = data.setdefault("registry", {})
        releases: List[dict] = registry.setdefault("releases", [])
        for rel in releases:
            if rel.get("slug") == slug:
                return rel
        rel: dict = {
            "slug": slug,
            "description": description,
            "includes": includes,
        }
        releases.append(rel)
        return rel

    # ── Vendor override ───────────────────────────────────────────────────

    def add_vendor_override(
        self,
        release: dict,
        vendor_slug: str,
        vendor_release_slug: str,
        vendor_release_description: str,
        kas_include_path: str,
        soc_vendor_slug: Optional[str] = None,
        distro_slug: Optional[str] = None,
    ) -> bool:
        """Append a vendor-release entry inside a release's vendor_overrides.

        Two structures are supported:

        * **Flat** (no ``--soc-vendor``): ``vendor_overrides[vendor].releases``
        * **Nested** (``--soc-vendor`` given):
          ``vendor_overrides[vendor].soc_vendors[soc_vendor].releases``

        Returns True if the entry was added, False if it already existed.
        """
        vendor_overrides: List[dict] = release.setdefault("vendor_overrides", [])
        vr_entry: dict = {
            "slug": vendor_release_slug,
            "description": vendor_release_description,
            "includes": [kas_include_path],
        }

        if soc_vendor_slug:
            vo = self._find_or_create_vo(vendor_overrides, vendor_slug)
            soc_vendors: List[dict] = vo.setdefault("soc_vendors", [])
            svo = self._find_or_create_svo(soc_vendors, soc_vendor_slug)
            if distro_slug:
                svo["distro"] = distro_slug
            svo_releases: List[dict] = svo.setdefault("releases", [])
            if any(r.get("slug") == vendor_release_slug for r in svo_releases):
                return False
            svo_releases.append(vr_entry)
        else:
            vo = self._find_or_create_vo(vendor_overrides, vendor_slug)
            if distro_slug:
                vo["distro"] = distro_slug
            vo_releases: List[dict] = vo.setdefault("releases", [])
            if any(r.get("slug") == vendor_release_slug for r in vo_releases):
                return False
            vo_releases.append(vr_entry)

        return True

    def _find_or_create_vo(
        self, vendor_overrides: List[dict], vendor_slug: str
    ) -> dict:
        for vo in vendor_overrides:
            if vo.get("vendor") == vendor_slug:
                return vo
        vo: dict = {"vendor": vendor_slug}
        vendor_overrides.append(vo)
        return vo

    def _find_or_create_svo(
        self, soc_vendors: List[dict], soc_vendor_slug: str
    ) -> dict:
        for svo in soc_vendors:
            if svo.get("vendor") == soc_vendor_slug:
                return svo
        svo: dict = {"vendor": soc_vendor_slug}
        soc_vendors.append(svo)
        return svo

    # ── BSP preset ────────────────────────────────────────────────────────

    def upsert_bsp_preset(self, data: dict, preset: dict) -> bool:
        """Add a BSP preset if one with the same name does not exist.

        Returns True if added, False if already present.
        """
        registry = data.setdefault("registry", {})
        bsp: List[dict] = registry.setdefault("bsp", [])
        name = preset.get("name", "")
        if any(b.get("name") == name for b in bsp):
            return False
        bsp.append(preset)
        return True


# =============================================================================
# ManifestImporter — orchestrator
# =============================================================================

class ManifestImporter:
    """Orchestrate the full manifest → BSP registry import pipeline.

    Typical usage::

        importer = ManifestImporter()
        result = importer.run(
            manifest_path=Path("default.xml"),
            output_dir=Path("my-registry"),
            vendor_slug="myvendor",
            soc_vendor_slug="nxp",
            vendor_release_slug="imx-6.6.52-2.2.0",
        )
    """

    def __init__(self) -> None:
        self._parser = ManifestParser()
        self._merger = RegistryMerger()

    def run(
        self,
        manifest_path: Path,
        output_dir: Path,
        vendor_slug: Optional[str] = None,
        soc_vendor_slug: Optional[str] = None,
        vendor_release_slug: Optional[str] = None,
        release_slug: Optional[str] = None,
        distro_slug: Optional[str] = None,
        dry_run: bool = False,
        merge: bool = False,
        hints_path: Optional[Path] = None,
    ) -> ImportResult:
        """Run the import and return an :class:`ImportResult`.

        Args:
            manifest_path:        Path to the repo manifest XML file.
            output_dir:           Directory where generated files are written.
            vendor_slug:          Board / software vendor slug
                                  (e.g. ``"advantech"``).  Falls back to
                                  ``"imported"`` if omitted.
            soc_vendor_slug:      SoC vendor slug (e.g. ``"nxp"``).  When
                                  given the KAS file is placed under
                                  ``vendors/<vendor>/<soc_vendor>/…`` and a
                                  nested ``soc_vendors`` structure is used in
                                  the release's vendor_overrides.
            vendor_release_slug:  Vendor BSP release identifier
                                  (e.g. ``"imx-6.6.52-2.2.0"``).  Defaults
                                  to the manifest file stem.
            release_slug:         Yocto codename override.  Auto-detected
                                  from manifest revisions when omitted.
            distro_slug:          Distro slug to attach to the vendor override
                                  (e.g. ``"fsl-imx-xwayland"``).
            dry_run:              If True, no files are written; the function
                                  logs what would happen and returns normally.
            merge:                If True, merge into an existing
                                  ``bsp-registry.yml``; error if False and the
                                  file already exists.
            hints_path:           Optional path to a hints YAML file.

        Returns:
            :class:`ImportResult` describing the files produced.

        Raises:
            FileExistsError: In *create* mode (``merge=False``) when
                             ``bsp-registry.yml`` already exists.
            ValueError:      When the manifest cannot be parsed.
        """
        result = ImportResult()
        manifest_path = Path(manifest_path).resolve()
        output_dir = Path(output_dir).resolve()

        # ── 1. Parse manifest ──────────────────────────────────────────────
        logger.info("Parsing manifest: %s", manifest_path)
        manifest = self._parser.parse(manifest_path)
        logger.info("Parsed %d project(s)", len(manifest.projects))

        # ── 2. Load hints ──────────────────────────────────────────────────
        hints = load_hints(hints_path) if hints_path else ImportHints()
        skip_names: Set[str] = {
            name
            for name, hint in hints.projects.items()
            if hint.get("role") == "skip"
        }
        if skip_names:
            logger.info("Skipping %d project(s) from hints: %s", len(skip_names), skip_names)

        # ── 3. Determine codename ──────────────────────────────────────────
        detected = _detect_codename(manifest)
        codename = release_slug or detected or _slugify(manifest_path.stem)
        yocto_version = YOCTO_CODENAMES.get(codename, "")
        if not detected and not release_slug:
            msg = (
                f"Could not detect a Yocto codename from manifest revisions; "
                f"using '{codename}' as release slug.  "
                f"Pass --release to set it explicitly."
            )
            logger.warning(msg)
            result.warnings.append(msg)
        logger.info("Yocto codename: %s", codename)

        # ── 4. Resolve vendor / release identifiers ────────────────────────
        effective_vendor = vendor_slug or "imported"
        effective_vendor_release = (
            vendor_release_slug or _slugify(manifest_path.stem)
        )

        # ── 5. Build KAS dict ──────────────────────────────────────────────
        kas_dict = _build_kas_dict(
            manifest.projects,
            default_branch=manifest.default_revision,
            skip_names=skip_names,
        )

        # ── 6. Determine KAS output path ───────────────────────────────────
        if soc_vendor_slug:
            kas_rel_path = (
                f"vendors/{effective_vendor}/{soc_vendor_slug}/"
                f"{effective_vendor_release}-{codename}.yml"
            )
        else:
            kas_rel_path = (
                f"vendors/{effective_vendor}/"
                f"{effective_vendor_release}-{codename}.yml"
            )
        result.kas_files.append((kas_rel_path, _dump_yaml(kas_dict)))

        # ── 7. Load / prepare registry data ───────────────────────────────
        registry_path = output_dir / "bsp-registry.yml"
        if not merge and registry_path.exists():
            raise FileExistsError(
                f"Registry file already exists: {registry_path}\n"
                "Use --merge to add to an existing registry, "
                "or remove the file to create a fresh one."
            )
        registry_data = self._merger.load(registry_path if merge else Path("/nonexistent"))

        # ── 8. Populate registry ───────────────────────────────────────────
        self._merger.upsert_vendor(registry_data, effective_vendor)
        if soc_vendor_slug:
            self._merger.upsert_vendor(registry_data, soc_vendor_slug)

        release_includes = [f"yocto/releases/{codename}.yml"]
        release_description = (
            f"Yocto {yocto_version} ({codename.title()})"
            if yocto_version
            else codename.title()
        )
        release = self._merger.upsert_release(
            registry_data,
            slug=codename,
            description=release_description,
            includes=release_includes,
        )

        self._merger.add_vendor_override(
            release=release,
            vendor_slug=effective_vendor,
            vendor_release_slug=effective_vendor_release,
            vendor_release_description=effective_vendor_release,
            kas_include_path=kas_rel_path,
            soc_vendor_slug=soc_vendor_slug,
            distro_slug=distro_slug,
        )

        # Inject device entries from hints
        for device in hints.devices:
            added = self._merger.upsert_device(registry_data, device)
            if not added:
                logger.debug(
                    "Device '%s' already present; skipping", device.get("slug")
                )

        result.registry_path = registry_path

        # ── 9. Write files (unless --dry-run) ─────────────────────────────
        if dry_run:
            logger.info("[dry-run] Would write KAS file:   %s", kas_rel_path)
            logger.info(
                "[dry-run] Would %s registry: bsp-registry.yml",
                "update" if merge else "create",
            )
            _print_dry_run_summary(
                kas_rel_path,
                kas_dict,
                registry_data,
                merge=merge,
            )
        else:
            kas_abs = output_dir / kas_rel_path
            kas_abs.parent.mkdir(parents=True, exist_ok=True)
            kas_abs.write_text(_dump_yaml(kas_dict), encoding="utf-8")
            logger.info("Wrote KAS file: %s", kas_abs)

            self._merger.save(registry_path, registry_data)
            logger.info(
                "%s registry: %s",
                "Updated" if merge else "Created",
                registry_path,
            )

        return result


# =============================================================================
# Dry-run output helper
# =============================================================================

def _print_dry_run_summary(
    kas_rel_path: str,
    kas_dict: dict,
    registry_data: dict,
    merge: bool,
) -> None:
    """Print a human-readable summary of what would be written."""
    import sys
    prefix = "[dry-run] "

    print(f"\n{prefix}── KAS file: {kas_rel_path} ─────────────────────────")
    for line in _dump_yaml(kas_dict).splitlines():
        print(f"{prefix}  {line}")

    action = "Updated" if merge else "Created"
    print(f"\n{prefix}── bsp-registry.yml ({action}) ─────────────────────")
    for line in _dump_yaml(registry_data).splitlines():
        print(f"{prefix}  {line}")
    print()
