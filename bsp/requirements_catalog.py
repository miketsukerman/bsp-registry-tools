"""Requirement catalogue support for direct test reports.

A requirement catalogue carries the *static* metadata of a test case
(description, expected specification, requirement version, category) so that
test scripts only have to emit the *runtime* outcome via
``<LAVA_SIGNAL_TESTCASE ...>`` lines.

Three catalogue forms are supported:

``requirements.yaml``-style shared file
    A file in the test-definition repository listing requirements for all
    suites.  Either a top-level ``requirements:`` mapping/list or a bare
    mapping of requirement id to entry.

Remote ``requirements.yaml`` URL
    The same file referenced by an ``http(s)://`` URL, so the descriptions
    maintained in a test-definition repository (for example
    ``https://github.com/miketsukerman/modular-bsp-test-definitions/blob/main/requirements.yaml``)
    can be reused for runs whose definitions come from elsewhere.  GitHub
    ``blob``/``raw`` web URLs are converted to their raw content URL
    automatically.

Inline ``metadata.test_cases`` block
    Declared inside a single Lava-Test definition YAML.  Inline entries take
    precedence over shared entries for the suite that declares them.

Catalogue sources are always optional and never fatal: malformed content and
failing downloads are logged and skipped so that a broken catalogue can never
fail a test run.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import yaml


# Conventional catalogue file names looked up at the root of a test-definition
# source when no explicit catalogue path is configured.
DEFAULT_CATALOG_FILENAMES = (
    "requirements.yaml",
    "requirements.yml",
    "test-requirements.yaml",
    "test-requirements.yml",
)

_INSTANCE_SEPARATORS = ("-", "_", ".", ":", "/")

# Remote catalogues are downloaded with a short timeout: a slow or unreachable
# host must never hold up (or fail) a test run.
CATALOG_DOWNLOAD_TIMEOUT = 15
_GITHUB_WEB_PATH_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?:blob|raw)/(?P<rest>.+)$")

# Downloaded catalogues, keyed by raw URL, so a catalogue shared by all suites
# of a run is fetched only once.
_URL_CATALOG_CACHE: Dict[str, "RequirementCatalog"] = {}


@dataclass
class RequirementEntry:
    """Static metadata for a single requirement (one or more test cases).

    ``description`` states what the test case means (its purpose) while
    ``verifies`` states what is actually asserted and how, so a report reader
    can tell the intent apart from the verification method.
    """

    id: str
    description: str = ""
    verifies: str = ""
    specification: Any = ""
    version: str = ""
    category: str = ""
    manual: bool = False
    remarks: str = ""

    def specification_for(self, instance: str = "") -> str:
        """Return the specification text for *instance* (``""`` for the base case).

        ``specification`` may either be a plain scalar shared by every instance
        of the requirement, or a mapping of instance key to value (e.g.
        ``{cpu0: 1600000, cpu1: 1600000}``) as used by parameterised tests.
        """
        spec = self.specification
        if isinstance(spec, dict):
            if instance and instance in spec:
                return _stringify(spec[instance])
            if not instance:
                # A mapping without an instance key is rendered as a compact
                # "key: value" listing so no information is silently dropped.
                return "; ".join(f"{k}: {_stringify(v)}" for k, v in spec.items())
            return ""
        return _stringify(spec)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return " ".join(_stringify(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_stringify(v)}" for k, v in value.items())
    return str(value)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on", "manual")


class RequirementCatalog:
    """An id-indexed collection of :class:`RequirementEntry` objects."""

    def __init__(self, entries: Optional[Dict[str, RequirementEntry]] = None):
        self._entries: Dict[str, RequirementEntry] = dict(entries or {})

    def __bool__(self) -> bool:
        return bool(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> Dict[str, RequirementEntry]:
        return dict(self._entries)

    def get(self, requirement_id: str) -> Optional[RequirementEntry]:
        return self._entries.get(requirement_id)

    def merged_with(self, other: "RequirementCatalog") -> "RequirementCatalog":
        """Return a new catalogue where entries of *other* override this one."""
        merged = dict(self._entries)
        merged.update(other._entries)
        return RequirementCatalog(merged)

    def resolve(self, test_case_id: str, requirement_id: str = "") -> Tuple[Optional[RequirementEntry], str]:
        """Resolve *test_case_id* to a catalogue entry and its instance key.

        An explicit *requirement_id* (from a ``REQUIREMENT_ID=`` signal
        attribute) is preferred.  Otherwise the test case id is matched against
        catalogue keys using the longest matching prefix, so
        ``L-CPU-FREQ-SCALING-MAX-cpu0`` resolves to the entry
        ``L-CPU-FREQ-SCALING-MAX`` with instance key ``cpu0``.
        """
        if requirement_id:
            entry = self._entries.get(requirement_id)
            if entry is not None:
                return entry, _instance_suffix(test_case_id, requirement_id)
            return None, ""

        exact = self._entries.get(test_case_id)
        if exact is not None:
            return exact, ""

        best: Optional[RequirementEntry] = None
        best_key = ""
        for key, entry in self._entries.items():
            if len(key) >= len(test_case_id) or not test_case_id.startswith(key):
                continue
            if test_case_id[len(key)] not in _INSTANCE_SEPARATORS:
                continue
            if len(key) > len(best_key):
                best, best_key = entry, key
        if best is None:
            return None, ""
        return best, _instance_suffix(test_case_id, best_key)


def _instance_suffix(test_case_id: str, requirement_id: str) -> str:
    """Return the instance part of *test_case_id* relative to *requirement_id*."""
    if not requirement_id or test_case_id == requirement_id:
        return ""
    if not test_case_id.startswith(requirement_id):
        return ""
    remainder = test_case_id[len(requirement_id):]
    if remainder[:1] in _INSTANCE_SEPARATORS:
        remainder = remainder[1:]
    return remainder


def _entry_from_mapping(entry_id: str, raw: Any) -> Optional[RequirementEntry]:
    if isinstance(raw, str):
        return RequirementEntry(id=entry_id, description=raw)
    if not isinstance(raw, dict):
        return None
    return RequirementEntry(
        id=entry_id,
        description=_stringify(
            raw.get("description") or raw.get("desc") or raw.get("purpose") or ""
        ),
        verifies=_stringify(raw.get("verifies") or raw.get("verification") or ""),
        specification=raw.get("specification", raw.get("spec", "")),
        version=_stringify(raw.get("version", raw.get("req_version", ""))),
        category=_stringify(raw.get("category", "")),
        manual=_as_bool(raw.get("manual", False)),
        remarks=_stringify(raw.get("remarks", "")),
    )


def catalog_from_raw(raw: Any, source: str = "", logger: Optional[logging.Logger] = None) -> RequirementCatalog:
    """Build a catalogue from already-parsed YAML data.

    Accepts either a mapping of requirement id to entry, or a list of entries
    each carrying an ``id`` field.  A top-level ``requirements`` (or
    ``test_cases``) key is unwrapped automatically.
    """
    log = logger or logging.getLogger(__name__)

    if isinstance(raw, dict):
        for wrapper in ("requirements", "test_cases", "testcases"):
            if wrapper in raw:
                raw = raw[wrapper]
                break

    entries: Dict[str, RequirementEntry] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            entry = _entry_from_mapping(str(key), value)
            if entry is None:
                log.warning("Skipping malformed requirement entry '%s'%s.", key, f" in {source}" if source else "")
                continue
            entries[entry.id] = entry
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                log.warning("Skipping malformed requirement entry%s.", f" in {source}" if source else "")
                continue
            entry_id = str(item.get("id") or item.get("requirement_id") or item.get("name") or "").strip()
            if not entry_id:
                log.warning("Skipping requirement entry without an id%s.", f" in {source}" if source else "")
                continue
            entry = _entry_from_mapping(entry_id, item)
            if entry is not None:
                entries[entry.id] = entry
    elif raw is not None:
        log.warning("Ignoring requirement catalogue with unsupported structure%s.", f" in {source}" if source else "")

    return RequirementCatalog(entries)


def is_catalog_url(reference: str) -> bool:
    """Return ``True`` when *reference* is an ``http(s)`` catalogue URL."""
    return str(reference).strip().lower().startswith(("http://", "https://"))


def raw_catalog_url(url: str) -> str:
    """Return the raw-content URL for *url*.

    GitHub web URLs (``.../blob/<ref>/<path>`` and ``.../raw/<ref>/<path>``)
    serve an HTML page, so they are rewritten to their
    ``raw.githubusercontent.com`` equivalent.  Any other URL is returned
    unchanged.
    """
    url = str(url).strip()
    parsed = urlparse(url)
    if parsed.netloc.lower() not in ("github.com", "www.github.com"):
        return url
    match = _GITHUB_WEB_PATH_RE.match(parsed.path)
    if not match:
        return url
    return (
        f"https://raw.githubusercontent.com/{match.group('owner')}/"
        f"{match.group('repo')}/{match.group('rest')}"
    )


def load_catalog_url(
    url: str,
    logger: Optional[logging.Logger] = None,
    timeout: int = CATALOG_DOWNLOAD_TIMEOUT,
) -> RequirementCatalog:
    """Download a requirement catalogue from *url*.

    Results are cached per process so a catalogue shared by every suite of a
    run is downloaded only once.  Never raises: unreachable hosts, HTTP errors
    and malformed content log a warning and yield an empty catalogue, so a
    remote catalogue can never fail a test run.
    """
    log = logger or logging.getLogger(__name__)
    fetch_url = raw_catalog_url(url)
    cached = _URL_CATALOG_CACHE.get(fetch_url)
    if cached is not None:
        return cached
    try:
        import requests  # imported lazily so offline runs never need it

        response = requests.get(fetch_url, timeout=timeout)
        response.raise_for_status()
        raw = yaml.safe_load(response.text)
    except Exception as exc:  # noqa: BLE001 - a catalogue is always optional
        log.warning("Failed to download requirement catalogue '%s': %s", fetch_url, exc)
        return RequirementCatalog()
    catalog = catalog_from_raw(raw, source=fetch_url, logger=log)
    _URL_CATALOG_CACHE[fetch_url] = catalog
    return catalog


def clear_catalog_url_cache() -> None:
    """Drop the cache of downloaded catalogues (used by tests)."""
    _URL_CATALOG_CACHE.clear()


def load_catalog_file(path: Path, logger: Optional[logging.Logger] = None) -> RequirementCatalog:
    """Load a requirement catalogue from *path*.

    Never raises: unreadable or malformed files log a warning and yield an
    empty catalogue.
    """
    log = logger or logging.getLogger(__name__)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        log.warning("Failed to read requirement catalogue '%s': %s", path, exc)
        return RequirementCatalog()
    return catalog_from_raw(raw, source=str(path), logger=log)


def discover_catalog(
    repo_dir: Path,
    explicit_paths: Optional[List[str]] = None,
    logger: Optional[logging.Logger] = None,
) -> RequirementCatalog:
    """Load the shared catalogue for a test-definition source.

    *explicit_paths* entries may be ``http(s)`` URLs — which are downloaded, so
    a catalogue maintained in another repository can be reused — or file paths,
    resolved relative to *repo_dir* first, then as absolute/CWD-relative paths.
    When no explicit reference is given, conventional file names at the
    repository root are used.
    """
    log = logger or logging.getLogger(__name__)
    catalog = RequirementCatalog()

    if explicit_paths:
        for raw_path in explicit_paths:
            if is_catalog_url(raw_path):
                catalog = catalog.merged_with(load_catalog_url(raw_path, logger=log))
                continue
            candidate = (repo_dir / raw_path)
            if not candidate.is_file():
                candidate = Path(raw_path).expanduser()
            if not candidate.is_file():
                log.warning("Requirement catalogue not found: '%s'.", raw_path)
                continue
            catalog = catalog.merged_with(load_catalog_file(candidate, logger=log))
        return catalog

    for name in DEFAULT_CATALOG_FILENAMES:
        candidate = repo_dir / name
        if candidate.is_file():
            catalog = catalog.merged_with(load_catalog_file(candidate, logger=log))
            break
    return catalog


def inline_catalog(definition: Dict[str, Any], logger: Optional[logging.Logger] = None) -> RequirementCatalog:
    """Build a catalogue from a definition's ``metadata.test_cases`` block."""
    metadata = definition.get("metadata")
    if not isinstance(metadata, dict):
        return RequirementCatalog()
    raw = metadata.get("test_cases") or metadata.get("requirements")
    if raw is None:
        return RequirementCatalog()
    return catalog_from_raw(raw, source="metadata.test_cases", logger=logger)


def humanize_test_case_id(test_case_id: str) -> str:
    """Return a readable fallback description derived from a test case id."""
    text = re.sub(r"[-_.]+", " ", test_case_id).strip()
    if not text:
        return ""
    return text[0].upper() + text[1:]
