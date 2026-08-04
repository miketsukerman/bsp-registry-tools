"""
Artifact deployer: discovers and uploads Yocto build artifacts to cloud storage.
"""

import datetime
import fnmatch
import hashlib
import html
import json
import logging
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import DeployConfig, IndexConfig
from .storage.base import CloudStorageBackend

# Meta tags copied from the reference implementation so browsers and proxies
# never serve a stale index with expired signed links.
_NO_CACHE_META = (
    '  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">\n'
    '  <meta http-equiv="Pragma" content="no-cache">\n'
    '  <meta http-equiv="Expires" content="0">\n'
)


def human_size(num_bytes: int) -> str:
    """Return *num_bytes* formatted as a short human-readable string."""
    size = float(num_bytes or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


_INDEX_CSS = """\
    body { font-family: sans-serif; margin: 2rem; }
    table { border-collapse: collapse; }
    th, td { padding: 0.3rem 0.8rem; border-bottom: 1px solid #ddd; }
    td.size { text-align: right; }
    code { font-size: 0.9em; }
    .controls { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 1rem 0; }
    .controls input { flex: 1 1 20rem; padding: 0.35rem 0.5rem; }
    .chips { display: flex; flex-wrap: wrap; gap: 0.3rem; margin-bottom: 1rem; }
    .chip { cursor: pointer; border: 1px solid #bbb; background: #f6f6f6;
            border-radius: 999px; padding: 0.15rem 0.7rem; font-size: 0.85em; }
    .chip.active { background: #0366d6; border-color: #0366d6; color: #fff; }
    #bsp-tree .row { display: flex; align-items: center; gap: 0.5rem;
                     padding: 0.15rem 0; border-bottom: 1px solid #f0f0f0; }
    #bsp-tree .name { flex: 1 1 auto; overflow-wrap: anywhere; }
    #bsp-tree .meta { color: #666; font-size: 0.85em; white-space: nowrap; }
    #bsp-tree .toggle { border: none; background: none; cursor: pointer;
                        font-family: monospace; font-size: 1em; padding: 0 0.2rem; }
    #bsp-tree .dir > .name { font-weight: 600; }
    .sorters { margin: 0.5rem 0; font-size: 0.9em; }
    .sorters button { margin-right: 0.3rem; }
"""


#: Vanilla-JS tree renderer inlined into every generated index page.  It has no
#: external dependencies so the page works from a private container reached
#: through a single signed URL.
_INDEX_JS = r"""
(function () {
  var dataEl = document.getElementById('bsp-index-data');
  if (!dataEl) { return; }
  var payload = JSON.parse(dataEl.textContent);
  var tree = payload.tree;
  var opts = payload.options || {};
  var container = document.getElementById('bsp-tree');
  var emptyEl = document.getElementById('bsp-empty');
  var searchEl = document.getElementById('bsp-search');
  var chipsEl = document.getElementById('bsp-chips');
  var open = {};
  var query = '';
  var ext = '';
  var sortKey = 'name';
  var sortAsc = true;

  function humanSize(n) {
    if (n === null || n === undefined) { return ''; }
    var units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    var size = Number(n) || 0;
    for (var i = 0; i < units.length; i++) {
      if (size < 1024 || i === units.length - 1) {
        return (i === 0 ? Math.round(size) : size.toFixed(1)) + ' ' + units[i];
      }
      size /= 1024;
    }
    return size + ' TiB';
  }

  function globToRegExp(pattern) {
    var escaped = pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&');
    escaped = escaped.replace(/\*/g, '.*').replace(/\?/g, '.');
    return new RegExp(escaped, 'i');
  }

  function matcher() {
    if (!query) { return null; }
    if (query.indexOf('*') >= 0 || query.indexOf('?') >= 0) {
      try { return globToRegExp(query); } catch (e) { return null; }
    }
    var needle = query.toLowerCase();
    return { test: function (value) { return value.toLowerCase().indexOf(needle) >= 0; } };
  }

  function extensionOf(path) {
    var name = path.split('/').pop();
    var dot = name.indexOf('.');
    return dot > 0 ? name.slice(dot).toLowerCase() : '';
  }

  function fileVisible(node, match) {
    if (ext && extensionOf(node.path).indexOf(ext) < 0) { return false; }
    if (match && !match.test(node.path)) { return false; }
    return true;
  }

  function compare(a, b) {
    var res = 0;
    if (sortKey === 'size') {
      res = (a.size_bytes || 0) - (b.size_bytes || 0);
    } else if (sortKey === 'date') {
      res = String(a.last_modified || '').localeCompare(String(b.last_modified || ''));
    } else {
      res = a.name.toLowerCase().localeCompare(b.name.toLowerCase());
    }
    return sortAsc ? res : -res;
  }

  function isOpen(node, depth) {
    if (query || ext) { return true; }
    if (Object.prototype.hasOwnProperty.call(open, node.path)) { return open[node.path]; }
    return depth < (opts.collapseDepth === undefined ? 1 : opts.collapseDepth);
  }

  function renderDir(node, depth, parent) {
    var children = node.children.slice().sort(function (a, b) {
      if ((a.type === 'dir') !== (b.type === 'dir')) { return a.type === 'dir' ? -1 : 1; }
      return compare(a, b);
    });
    var match = matcher();
    var rendered = 0;
    children.forEach(function (child) {
      if (child.type === 'dir') {
        var holder = document.createElement('div');
        var expanded = isOpen(child, depth);
        var row = document.createElement('div');
        row.className = 'row dir';
        row.style.paddingLeft = (depth * 1.2) + 'rem';
        var toggle = document.createElement('button');
        toggle.type = 'button';
        toggle.className = 'toggle';
        toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        toggle.textContent = expanded ? '\u25be' : '\u25b8';
        var label = document.createElement('span');
        label.className = 'name';
        label.textContent = child.name + '/';
        var meta = document.createElement('span');
        meta.className = 'meta';
        meta.textContent = child.file_count + ' files, ' + humanSize(child.size_bytes);
        row.appendChild(toggle);
        row.appendChild(label);
        row.appendChild(meta);
        var kids = document.createElement('div');
        kids.setAttribute('role', 'group');
        var count = renderDir(child, depth + 1, kids);
        if (count === 0) { return; }
        kids.hidden = !expanded;
        function flip() {
          open[child.path] = kids.hidden;
          render();
        }
        toggle.addEventListener('click', flip);
        label.addEventListener('click', flip);
        holder.appendChild(row);
        holder.appendChild(kids);
        parent.appendChild(holder);
        rendered += count;
      } else if (fileVisible(child, match)) {
        var frow = document.createElement('div');
        frow.className = 'row file';
        frow.style.paddingLeft = ((depth * 1.2) + 1.4) + 'rem';
        var link = document.createElement('a');
        link.className = 'name';
        link.href = child.href;
        link.textContent = child.name;
        var fmeta = document.createElement('span');
        fmeta.className = 'meta';
        var bits = [humanSize(child.size_bytes)];
        if (opts.showDates && child.last_modified) { bits.push(child.last_modified); }
        if (child.sha256) { bits.push(child.sha256.slice(0, 12)); }
        fmeta.textContent = bits.filter(Boolean).join(' \u00b7 ');
        frow.appendChild(link);
        frow.appendChild(fmeta);
        parent.appendChild(frow);
        rendered += 1;
      }
    });
    return rendered;
  }

  function syncHash() {
    var parts = [];
    if (query) { parts.push('q=' + encodeURIComponent(query)); }
    if (ext) { parts.push('ext=' + encodeURIComponent(ext)); }
    var opened = Object.keys(open).filter(function (k) { return open[k]; });
    if (opened.length) { parts.push('open=' + encodeURIComponent(opened.join('|'))); }
    var hash = parts.join('&');
    if (hash !== location.hash.replace(/^#/, '')) {
      history.replaceState(null, '', hash ? '#' + hash : location.pathname);
    }
  }

  function readHash() {
    var hash = location.hash.replace(/^#/, '');
    if (!hash) { return; }
    hash.split('&').forEach(function (pair) {
      var idx = pair.indexOf('=');
      if (idx < 0) { return; }
      var key = pair.slice(0, idx);
      var value = decodeURIComponent(pair.slice(idx + 1));
      if (key === 'q') { query = value; if (searchEl) { searchEl.value = value; } }
      else if (key === 'ext') { ext = value; }
      else if (key === 'open') {
        value.split('|').forEach(function (path) { if (path) { open[path] = true; } });
      }
    });
  }

  function render() {
    container.textContent = '';
    var count = renderDir(tree, 0, container);
    if (emptyEl) { emptyEl.hidden = count !== 0; }
    if (chipsEl) {
      Array.prototype.forEach.call(chipsEl.querySelectorAll('.chip'), function (chip) {
        chip.classList.toggle('active', (chip.getAttribute('data-ext') || '') === ext);
      });
    }
    syncHash();
  }

  function setAll(state) {
    (function walk(node) {
      node.children.forEach(function (child) {
        if (child.type === 'dir') { open[child.path] = state; walk(child); }
      });
    })(tree);
    render();
  }

  var sorters = document.createElement('div');
  sorters.className = 'sorters';
  [['name', 'Name'], ['size', 'Size'], ['date', 'Modified']].forEach(function (pair) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = 'Sort by ' + pair[1];
    btn.addEventListener('click', function () {
      if (sortKey === pair[0]) { sortAsc = !sortAsc; } else { sortKey = pair[0]; sortAsc = true; }
      render();
    });
    sorters.appendChild(btn);
  });
  container.parentNode.insertBefore(sorters, container);

  if (searchEl) {
    var timer = null;
    searchEl.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () { query = searchEl.value.trim(); render(); }, 150);
    });
  }
  if (chipsEl) {
    chipsEl.addEventListener('click', function (event) {
      var chip = event.target.closest('.chip');
      if (!chip) { return; }
      ext = chip.getAttribute('data-ext') || '';
      render();
    });
  }
  var expandBtn = document.getElementById('bsp-expand');
  var collapseBtn = document.getElementById('bsp-collapse');
  if (expandBtn) { expandBtn.addEventListener('click', function () { setAll(true); }); }
  if (collapseBtn) { collapseBtn.addEventListener('click', function () { setAll(false); }); }

  readHash();
  render();
})();
"""


def _json_for_html(payload) -> str:
    """
    Serialize *payload* for embedding in an HTML ``<script>`` data island.

    ``<``, ``>`` and ``&`` are escaped as unicode sequences so a hostile blob
    name can neither terminate the script element nor inject markup.
    """
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return (
        text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _artifact_extension(path: str) -> str:
    """Return the (possibly compound) extension of *path*, e.g. ``".tar.gz"``."""
    name = (path or "").rsplit("/", 1)[-1]
    dot = name.find(".")
    return name[dot:].lower() if dot > 0 else ""


#: Emit a warning when an index would embed more than this many files.
_INDEX_SOFT_LIMIT = 5000


def _relative_to_prefix(remote_path: str, base_prefix: str = "") -> str:
    """Return *remote_path* expressed relative to *base_prefix*."""
    path = (remote_path or "").lstrip("/")
    base = (base_prefix or "").strip("/")
    if base and path.startswith(base + "/"):
        return path[len(base) + 1:]
    return path


def _remote_path_from_url(url: str, prefix: str, fallback_name: str) -> str:
    """
    Recover the remote object path from an upload URL.

    Backends return provider-specific URLs (``https://…/container/a/b.wic``,
    ``fake://a/b.wic`` or ``dry-run:a/b.wic``).  The path is recovered by
    locating *prefix* inside the URL so nested layouts keep their directory
    component; otherwise ``{prefix}/{fallback_name}`` is assumed.
    """
    base = (prefix or "").strip("/")
    text = (url or "").split("?", 1)[0]
    if base:
        marker = base + "/"
        idx = text.find(marker)
        if idx >= 0:
            rel = text[idx + len(marker):].strip("/")
            if rel:
                return f"{base}/{rel}"
        return f"{base}/{fallback_name}" if fallback_name else base
    return fallback_name


def _matches_any(rel_path: str, patterns: Optional[List[str]]) -> bool:
    """Return ``True`` when *rel_path* matches any of the glob *patterns*."""
    if not patterns:
        return False
    name = rel_path.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(name, pattern)
        for pattern in patterns
    )


def build_index_tree(files: List[Dict]) -> Dict:
    """
    Build a nested directory tree from a flat list of file records.

    Args:
        files: One dict per file with at least a ``path`` key holding the path
               relative to the indexed prefix.  Other keys (``href``,
               ``size_bytes``, ``last_modified``, ``sha256``) are carried
               through to the file nodes untouched.

    Returns:
        The root directory node.  Directory nodes carry ``name``, ``path``,
        ``type`` (``"dir"``), ``children``, ``file_count`` and ``size_bytes``
        (aggregated over the whole subtree); file nodes carry ``type``
        (``"file"``) plus the record fields.
    """
    root: Dict = {
        "type": "dir", "name": "", "path": "",
        "children": [], "file_count": 0, "size_bytes": 0,
    }

    def _child_dir(parent: Dict, name: str) -> Dict:
        for child in parent["children"]:
            if child["type"] == "dir" and child["name"] == name:
                return child
        path = f"{parent['path']}/{name}" if parent["path"] else name
        node = {
            "type": "dir", "name": name, "path": path,
            "children": [], "file_count": 0, "size_bytes": 0,
        }
        parent["children"].append(node)
        return node

    for record in files:
        rel = str(record.get("path") or "").strip("/")
        if not rel:
            continue
        parts = rel.split("/")
        node = root
        chain = [root]
        for part in parts[:-1]:
            node = _child_dir(node, part)
            chain.append(node)
        size = int(record.get("size_bytes") or 0)
        node["children"].append({
            "type": "file",
            "name": parts[-1],
            "path": rel,
            "href": record.get("href", ""),
            "size_bytes": record.get("size_bytes"),
            "last_modified": record.get("last_modified"),
            "sha256": record.get("sha256") or "",
        })
        for ancestor in chain:
            ancestor["file_count"] += 1
            ancestor["size_bytes"] += size

    def _sort(node: Dict) -> None:
        node["children"].sort(
            key=lambda c: (c["type"] != "dir", c["name"].lower())
        )
        for child in node["children"]:
            if child["type"] == "dir":
                _sort(child)

    _sort(root)
    return root


def flatten_index_tree(node: Dict) -> List[Dict]:
    """Return every file node of *node* in depth-first order."""
    files: List[Dict] = []
    for child in node.get("children", []):
        if child["type"] == "dir":
            files.extend(flatten_index_tree(child))
        else:
            files.append(child)
    return files


# =============================================================================
# Deploy result
# =============================================================================


@dataclass
class UploadedArtifact:
    """Metadata for a single uploaded artifact."""
    local_path: Path
    remote_url: str
    size_bytes: int
    sha256: str


@dataclass
class UploadedCache:
    """Metadata for a single uploaded Yocto cache archive."""
    cache_type: str       # "downloads" or "sstate"
    local_archive: Path
    remote_url: str
    size_bytes: int
    sha256: str


@dataclass
class DeployResult:
    """Result of a full deployment run."""
    artifacts: List[UploadedArtifact] = field(default_factory=list)
    cache_uploads: List[UploadedCache] = field(default_factory=list)
    manifest_url: Optional[str] = None
    index_url: Optional[str] = None
    dry_run: bool = False

    @property
    def total_bytes(self) -> int:
        return sum(a.size_bytes for a in self.artifacts)

    @property
    def success_count(self) -> int:
        return len(self.artifacts)


# =============================================================================
# ArtifactDeployer
# =============================================================================


class ArtifactDeployer:
    """
    Discovers Yocto build artifacts and uploads them to a cloud storage backend.

    This class is provider-agnostic; all cloud interaction is delegated to the
    ``CloudStorageBackend`` instance passed to the constructor.

    Args:
        deploy_config: Deployment configuration (patterns, dirs, prefix, etc.)
        storage_backend: Concrete ``CloudStorageBackend`` to use for uploads.
    """

    def __init__(
        self,
        deploy_config: DeployConfig,
        storage_backend: CloudStorageBackend,
    ):
        self.config = deploy_config
        self.backend = storage_backend
        self.logger = logging.getLogger(self.__class__.__name__)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def collect_artifacts(self, build_path: str) -> List[Path]:
        """
        Find all artifact files under *build_path* that match the configured
        patterns and artifact directories.

        Args:
            build_path: Top-level build output directory (e.g.
                        ``"build/poky/my-device/scarthgap"``).

        Returns:
            Deduplicated, sorted list of matching ``Path`` objects.
        """
        build_root = Path(build_path)
        found: List[Path] = []
        seen = set()

        for artifact_dir in self.config.artifact_dirs:
            search_dir = build_root / artifact_dir
            if not search_dir.is_dir():
                self.logger.debug("Artifact dir not found, skipping: %s", search_dir)
                continue
            for pattern in self.config.patterns:
                for match in sorted(search_dir.glob(pattern)):
                    if match.is_file() and match not in seen:
                        found.append(match)
                        seen.add(match)

        self.logger.info("Collected %d artifact(s) from %s", len(found), build_path)
        return found

    def compose_remote_prefix(
        self,
        device: str = "",
        release: str = "",
        distro: str = "",
        vendor: str = "",
    ) -> str:
        """
        Build the remote path prefix from the ``DeployConfig.prefix`` template.

        Supported placeholders: ``{device}``, ``{release}``, ``{distro}``,
        ``{vendor}``, ``{date}`` (``YYYY-MM-DD``), ``{datetime}``
        (``YYYYMMDD-HHMMSS``).

        Args:
            device: Device slug.
            release: Release slug.
            distro: Effective distro slug.
            vendor: Board vendor slug.

        Returns:
            Resolved prefix string (no leading or trailing slash).
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        template = self.config.prefix or "{vendor}/{device}/{release}/{date}"
        prefix = template.format(
            device=device or "unknown",
            release=release or "unknown",
            distro=distro or "unknown",
            vendor=vendor or "unknown",
            date=now.strftime("%Y-%m-%d"),
            datetime=now.strftime("%Y%m%d-%H%M%S"),
        )
        return prefix.strip("/")

    def compose_archive_name(
        self,
        device: str = "",
        release: str = "",
        distro: str = "",
        vendor: str = "",
    ) -> str:
        """
        Build the archive filename (without extension) from the
        ``DeployConfig.archive.name`` template.

        Supports the same placeholders as :meth:`compose_remote_prefix`.

        Args:
            device: Device slug.
            release: Release slug.
            distro: Effective distro slug.
            vendor: Board vendor slug.

        Returns:
            Resolved archive base-name string.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        archive = self.config.archive
        template = archive.name if archive else "artifacts-{device}-{date}"
        name = template.format(
            device=device or "unknown",
            release=release or "unknown",
            distro=distro or "unknown",
            vendor=vendor or "unknown",
            date=now.strftime("%Y-%m-%d"),
            datetime=now.strftime("%Y%m%d-%H%M%S"),
        )
        return name.strip("/")

    def deploy(
        self,
        build_path: str,
        device: str = "",
        release: str = "",
        distro: str = "",
        vendor: str = "",
        downloads_path: Optional[str] = None,
        sstate_path: Optional[str] = None,
        update_index: Optional[bool] = None,
    ) -> DeployResult:
        """
        Collect and upload all matching artifacts.

        When ``DeployConfig.yocto_cache`` is enabled and *downloads_path* /
        *sstate_path* point to existing directories the corresponding cache
        directories are packed into ``tar.gz`` archives and uploaded under
        ``{prefix}/cache/``.

        Args:
            build_path: Top-level Yocto build output directory.
            device: Device slug (used for prefix expansion).
            release: Release slug.
            distro: Effective distro slug.
            vendor: Board vendor slug.
            downloads_path: Optional absolute path to the ``DL_DIR`` cache
                            directory.  Passed by :class:`~bsp.bsp_manager.BspManager`
                            when Yocto cache upload is enabled.
            sstate_path: Optional absolute path to the ``SSTATE_DIR`` cache
                         directory.
            update_index: Force-enable (``True``) or disable (``False``) HTML
                          index generation, overriding
                          ``DeployConfig.index.enabled``.  ``None`` (default)
                          uses the configured value.

        Returns:
            ``DeployResult`` with metadata for every uploaded artifact and,
            when ``include_manifest`` is enabled, the manifest URL.
        """
        result = DeployResult(dry_run=self.backend.dry_run)
        artifacts = self.collect_artifacts(build_path)

        if not artifacts:
            print(f"No artifacts found in '{build_path}'. Nothing to deploy.")
            return result

        prefix = self.compose_remote_prefix(
            device=device, release=release, distro=distro, vendor=vendor
        )
        action_verb = "[dry-run] Would upload" if self.backend.dry_run else "Deploying"
        print(
            f"{action_verb} {len(artifacts)} artifact(s) to {self.config.provider} "
            f"under prefix '{prefix}'..."
        )

        failed: List[Tuple[Path, Exception]] = []

        if self.config.archive:
            # Bundle all artifacts into a single compressed archive before upload.
            archive_basename = self.compose_archive_name(
                device=device, release=release, distro=distro, vendor=vendor
            )
            print(f"  Creating archive {archive_basename}...")
            tmp_archive = self._create_archive(
                artifacts,
                archive_basename,
                self.config.archive.format,
            )
            try:
                archive_remote = f"{prefix}/{tmp_archive.name}"
                print(f"  Uploading {tmp_archive.name}...")
                url = self.backend.upload_file(tmp_archive, archive_remote)
                size = tmp_archive.stat().st_size if not self.backend.dry_run else 0
                sha = self._sha256(tmp_archive) if not self.backend.dry_run else ""
                result.artifacts.append(
                    UploadedArtifact(
                        local_path=tmp_archive,
                        remote_url=url,
                        size_bytes=size,
                        sha256=sha,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.error("Failed to upload archive %s: %s", tmp_archive, exc)
                failed.append((tmp_archive, exc))
            finally:
                tmp_archive.unlink(missing_ok=True)
                shutil.rmtree(tmp_archive.parent, ignore_errors=True)
        else:
            for local_path in artifacts:
                rel = local_path.name
                remote_path = f"{prefix}/{rel}"
                print(f"  Uploading {rel}...")
                try:
                    url = self.backend.upload_file(local_path, remote_path)
                    size = local_path.stat().st_size if not self.backend.dry_run else 0
                    sha = self._sha256(local_path) if not self.backend.dry_run else ""
                    result.artifacts.append(
                        UploadedArtifact(
                            local_path=local_path,
                            remote_url=url,
                            size_bytes=size,
                            sha256=sha,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    self.logger.error("Failed to upload %s: %s", local_path, exc)
                    failed.append((local_path, exc))

        if failed:
            self.logger.warning(
                "%d upload(s) failed out of %d total.",
                len(failed),
                len(artifacts),
            )

        # --- Yocto cache upload ---
        cache_cfg = self.config.yocto_cache
        if cache_cfg and cache_cfg.enabled:
            result.cache_uploads = self._upload_caches(
                prefix=prefix,
                downloads_path=downloads_path,
                sstate_path=sstate_path,
                include_downloads=cache_cfg.downloads,
                include_sstate=cache_cfg.sstate,
            )

        if self.config.include_manifest and result.artifacts:
            manifest_url = self._upload_manifest(result, prefix, device, release, distro, vendor)
            result.manifest_url = manifest_url

        index_cfg = self.config.index or IndexConfig()
        index_enabled = index_cfg.enabled if update_index is None else update_index
        if index_enabled and result.artifacts:
            result.index_url = self._upload_index(
                result, prefix, device, release, distro, vendor,
                index_config=index_cfg,
            )
            if index_cfg.root_index:
                self._upload_root_index(index_config=index_cfg)

        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def generate_manifest(
        self,
        result: DeployResult,
        device: str = "",
        release: str = "",
        distro: str = "",
        vendor: str = "",
    ) -> str:
        """
        Build a JSON manifest describing all uploaded artifacts and, when
        present, any uploaded Yocto cache archives.

        Args:
            result: Completed ``DeployResult``.
            device: Device slug.
            release: Release slug.
            distro: Effective distro slug.
            vendor: Board vendor slug.

        Returns:
            JSON string.
        """
        manifest: Dict = {
            "schema_version": "1",
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "build": {
                "device": device,
                "release": release,
                "distro": distro,
                "vendor": vendor,
            },
            "provider": self.config.provider,
            "dry_run": result.dry_run,
            "artifacts": [
                {
                    "name": a.local_path.name,
                    "remote_url": a.remote_url,
                    "size_bytes": a.size_bytes,
                    "sha256": a.sha256,
                }
                for a in result.artifacts
            ],
            "total_size_bytes": result.total_bytes,
        }

        if result.cache_uploads:
            manifest["yocto_cache"] = {
                uc.cache_type: {
                    "name": uc.local_archive.name,
                    "remote_url": uc.remote_url,
                    "size_bytes": uc.size_bytes,
                    "sha256": uc.sha256,
                }
                for uc in result.cache_uploads
            }

        return json.dumps(manifest, indent=2)

    def _upload_caches(
        self,
        prefix: str,
        downloads_path: Optional[str],
        sstate_path: Optional[str],
        include_downloads: bool = True,
        include_sstate: bool = True,
    ) -> List["UploadedCache"]:
        """
        Pack and upload Yocto cache directories.

        Each enabled cache directory that exists on disk is packed into a
        ``tar.gz`` archive and uploaded under ``{prefix}/cache/``.  Missing or
        disabled cache directories are silently skipped.

        Args:
            prefix: Resolved remote path prefix (e.g. ``"acme/myboard/scarthgap/2025-01-15"``).
            downloads_path: Local path to ``DL_DIR``.
            sstate_path: Local path to ``SSTATE_DIR``.
            include_downloads: Whether to upload the downloads cache.
            include_sstate: Whether to upload the sstate cache.

        Returns:
            List of :class:`UploadedCache` entries for each successfully
            uploaded cache archive.
        """
        cache_prefix = f"{prefix}/cache"
        uploaded: List[UploadedCache] = []

        candidates = []
        if include_downloads and downloads_path:
            candidates.append(("downloads", downloads_path))
        if include_sstate and sstate_path:
            candidates.append(("sstate", sstate_path))

        for cache_type, local_dir in candidates:
            dir_path = Path(local_dir)
            if not dir_path.is_dir():
                self.logger.info(
                    "Yocto cache dir not found, skipping upload: %s (%s)",
                    local_dir, cache_type,
                )
                continue

            archive_name = f"{cache_type}.tar.gz"
            remote_path = f"{cache_prefix}/{archive_name}"

            if self.backend.dry_run:
                print(
                    f"[dry-run] Would pack and upload Yocto {cache_type} cache: "
                    f"{local_dir} → {remote_path}"
                )
                uploaded.append(
                    UploadedCache(
                        cache_type=cache_type,
                        local_archive=dir_path / archive_name,
                        remote_url=f"dry-run:{remote_path}",
                        size_bytes=0,
                        sha256="",
                    )
                )
                continue

            tmp_dir = Path(tempfile.mkdtemp(prefix="bsp_cache_"))
            archive_path = tmp_dir / archive_name
            try:
                print(f"  Packing Yocto {cache_type} cache: {local_dir} → {archive_path}")
                with tarfile.open(archive_path, "w:gz") as tar:
                    tar.add(str(dir_path), arcname=cache_type)

                print(
                    f"  Uploading Yocto {cache_type} cache archive "
                    f"({archive_path.stat().st_size} bytes) → {remote_path}"
                )
                url = self.backend.upload_file(archive_path, remote_path)
                size = archive_path.stat().st_size
                sha = self._sha256(archive_path)
                uploaded.append(
                    UploadedCache(
                        cache_type=cache_type,
                        local_archive=archive_path,
                        remote_url=url,
                        size_bytes=size,
                        sha256=sha,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.error(
                    "Failed to upload Yocto %s cache: %s", cache_type, exc
                )
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        return uploaded

    # ------------------------------------------------------------------
    # HTML index generation
    # ------------------------------------------------------------------

    def compose_index_title(
        self,
        index_config: Optional[IndexConfig] = None,
        device: str = "",
        release: str = "",
        distro: str = "",
        vendor: str = "",
    ) -> str:
        """
        Expand the ``IndexConfig.title`` template.

        Supports the same placeholders as :meth:`compose_remote_prefix`.
        """
        cfg = index_config or self.config.index or IndexConfig()
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            return cfg.title.format(
                device=device or "unknown",
                release=release or "unknown",
                distro=distro or "unknown",
                vendor=vendor or "unknown",
                date=now.strftime("%Y-%m-%d"),
                datetime=now.strftime("%Y%m%d-%H%M%S"),
            )
        except (KeyError, IndexError):
            return cfg.title

    def _artifact_href(
        self,
        remote_path: str,
        index_config: IndexConfig,
        base_prefix: str = "",
    ) -> str:
        """
        Resolve the ``href`` used for *remote_path* in a generated index.

        With ``sign_urls`` enabled a read-only signed URL is requested from
        the backend.  Otherwise (or when the backend cannot sign) a href
        relative to *base_prefix* (the prefix the page itself lives under) is
        emitted so the page also works behind a CDN or custom domain, and so
        nested artifacts keep their directory component.
        """
        relative = _relative_to_prefix(remote_path, base_prefix)
        if not index_config.sign_urls:
            return relative
        try:
            return self.backend.get_signed_url(
                remote_path, expiry=index_config.sas_expiry
            )
        except NotImplementedError:
            self.logger.warning(
                "Storage backend does not support signed URLs; "
                "falling back to relative links in the index."
            )
            return relative
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Failed to sign URL for %s: %s", remote_path, exc)
            return relative

    def generate_index_html(
        self,
        entries: List[Dict],
        title: str,
        metadata: Optional[Dict] = None,
        manifest_href: Optional[str] = None,
        tree: Optional[Dict] = None,
        index_config: Optional[IndexConfig] = None,
    ) -> str:
        """
        Render a self-contained HTML page listing *entries*.

        Args:
            entries: One dict per artifact with the keys ``name``, ``href``,
                     ``size_bytes`` and (optionally) ``path``, ``sha256`` and
                     ``last_modified``.  Used for the flat table and for the
                     ``<noscript>`` fallback of the tree view.
            title: Page title (already expanded).
            metadata: Optional build metadata rendered above the table.
            manifest_href: Optional link to the JSON manifest.
            tree: Optional directory tree (see :func:`build_index_tree`).  When
                  given and ``index_config.tree`` is enabled the page renders a
                  collapsible tree with search, filter and sort controls.
            index_config: Index configuration controlling the tree view.

        Returns:
            Complete HTML document as a string.  All interpolated values are
            HTML-escaped and the embedded JSON is escaped so it cannot break
            out of its ``<script>`` element.
        """
        esc = html.escape
        cfg = index_config or self.config.index or IndexConfig()
        use_tree = bool(tree) and cfg.tree
        generated = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
        show_dates = cfg.show_dates and any(
            e.get("last_modified") for e in entries
        )
        lines = [
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '  <meta charset="utf-8">',
            '  <meta name="viewport" content="width=device-width, initial-scale=1">',
            _NO_CACHE_META.rstrip("\n"),
            f"  <title>{esc(title)}</title>",
            "  <style>",
            _INDEX_CSS.rstrip("\n"),
            "  </style>",
            "</head>",
            "<body>",
            f"  <h1>{esc(title)}</h1>",
        ]

        meta_items = list((metadata or {}).items())
        meta_items.append(("generated", generated))
        lines.append("  <ul>")
        for key, value in meta_items:
            if value in (None, ""):
                continue
            lines.append(f"    <li><b>{esc(str(key))}:</b> {esc(str(value))}</li>")
        lines.append("  </ul>")

        if use_tree:
            lines.extend(self._render_tree_controls(cfg, entries))
            lines.append('  <div id="bsp-tree" role="tree"></div>')
            lines.append('  <p id="bsp-empty" hidden>No matching artifacts.</p>')
            lines.append("  <noscript>")
            lines.extend(self._render_flat_table(entries, show_dates))
            lines.append("  </noscript>")
        else:
            lines.extend(self._render_flat_table(entries, show_dates))

        if manifest_href:
            lines.append(
                f'  <p><a href="{esc(str(manifest_href), quote=True)}">manifest.json</a></p>'
            )

        if use_tree:
            payload = {
                "tree": tree,
                "options": {
                    "collapseDepth": max(0, int(cfg.collapse_depth or 0)),
                    "search": bool(cfg.search),
                    "filters": bool(cfg.filters),
                    "showDates": bool(show_dates),
                },
            }
            lines.append(
                '  <script id="bsp-index-data" type="application/json">'
                + _json_for_html(payload)
                + "</script>"
            )
            lines.append("  <script>")
            lines.append(_INDEX_JS.rstrip("\n"))
            lines.append("  </script>")

        lines.extend(["</body>", "</html>", ""])
        return "\n".join(lines)

    @staticmethod
    def _render_flat_table(entries: List[Dict], show_dates: bool = False) -> List[str]:
        """Render the legacy flat artifact table (also used as noscript fallback)."""
        esc = html.escape
        date_header = "<th>Modified</th>" if show_dates else ""
        lines = [
            "  <table>",
            f"    <tr><th>Name</th><th>Size</th>{date_header}<th>SHA-256</th></tr>",
        ]
        for entry in entries:
            name = esc(str(entry.get("path") or entry.get("name", "")))
            href = esc(str(entry.get("href", "")), quote=True)
            size = esc(human_size(entry.get("size_bytes") or 0))
            sha = str(entry.get("sha256") or "")
            short_sha = esc(sha[:12])
            date_cell = (
                f'<td>{esc(str(entry.get("last_modified") or ""))}</td>'
                if show_dates else ""
            )
            lines.append(
                f'    <tr><td><a href="{href}">{name}</a></td>'
                f'<td class="size">{size}</td>{date_cell}'
                f"<td><code>{short_sha}</code></td></tr>"
            )
        lines.append("  </table>")
        return lines

    @staticmethod
    def _render_tree_controls(cfg: IndexConfig, entries: List[Dict]) -> List[str]:
        """Render the search box, type filter chips and expand/collapse buttons."""
        esc = html.escape
        lines = ['  <div class="controls">']
        if cfg.search:
            lines.append(
                '    <input id="bsp-search" type="search" '
                'placeholder="Filter by name or path (supports *)" '
                'aria-label="Filter artifacts">'
            )
        lines.append(
            '    <button type="button" id="bsp-expand">Expand all</button>'
            '    <button type="button" id="bsp-collapse">Collapse all</button>'
        )
        lines.append("  </div>")
        if cfg.filters:
            extensions = sorted({
                _artifact_extension(str(e.get("path") or e.get("name", "")))
                for e in entries
            } - {""})
            if extensions:
                lines.append('  <div class="chips" id="bsp-chips">')
                lines.append(
                    '    <button type="button" class="chip active" data-ext="">all</button>'
                )
                for ext in extensions:
                    lines.append(
                        f'    <button type="button" class="chip" '
                        f'data-ext="{esc(ext, quote=True)}">{esc(ext)}</button>'
                    )
                lines.append("  </div>")
        return lines

    def _upload_html(self, html_text: str, remote_path: str) -> Optional[str]:
        """Upload *html_text* as an ``.html`` blob; return its URL or ``None``."""
        if self.backend.dry_run:
            print(f"[dry-run] Would generate and upload index → {remote_path}")
            return f"dry-run:{remote_path}"

        tmp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".html", delete=False, prefix="bsp_index_",
                encoding="utf-8",
            ) as fh:
                fh.write(html_text)
                tmp_path = Path(fh.name)
            return self.backend.upload_file(tmp_path, remote_path)
        except Exception as exc:  # noqa: BLE001
            # A failing index upload must never fail an otherwise good deploy.
            self.logger.warning("Failed to upload index %s: %s", remote_path, exc)
            return None
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    def _index_entries(
        self,
        records: List[Dict],
        prefix: str,
        cfg: IndexConfig,
    ) -> Tuple[List[Dict], Optional[str]]:
        """
        Turn raw listing *records* into index entries relative to *prefix*.

        ``index.html`` pages are always skipped (at any depth) and paths
        matching ``IndexConfig.exclude`` are dropped.  The manifest href is
        returned separately so it can be rendered as a footer link.
        """
        entries: List[Dict] = []
        manifest_href: Optional[str] = None
        for record in records:
            remote_path = str(record.get("path") or "")
            rel = _relative_to_prefix(remote_path, prefix)
            if not rel:
                continue
            name = rel.rsplit("/", 1)[-1]
            if name.lower() == "index.html":
                continue
            if _matches_any(rel, cfg.exclude):
                continue
            if rel == "manifest.json":
                manifest_href = self._artifact_href(remote_path, cfg, prefix)
                continue
            entries.append({
                "name": name,
                "path": rel,
                "href": self._artifact_href(remote_path, cfg, prefix),
                "size_bytes": record.get("size"),
                "last_modified": record.get("last_modified"),
                "sha256": record.get("sha256") or "",
            })

        entries.sort(key=lambda e: e["path"])
        if len(entries) > _INDEX_SOFT_LIMIT:
            self.logger.warning(
                "Index for prefix '%s' contains %d files (soft limit %d); "
                "the generated page may be large and slow to render.",
                prefix or "/", len(entries), _INDEX_SOFT_LIMIT,
            )
        return entries, manifest_href

    def _upload_index(
        self,
        result: DeployResult,
        prefix: str,
        device: str = "",
        release: str = "",
        distro: str = "",
        vendor: str = "",
        index_config: Optional[IndexConfig] = None,
    ) -> Optional[str]:
        """Generate and upload ``{prefix}/index.html``; return its remote URL."""
        cfg = index_config or self.config.index or IndexConfig()
        records: List[Dict] = []
        for art in result.artifacts:
            remote_path = self._artifact_remote_path(art, prefix)
            records.append({
                "path": remote_path,
                "size": art.size_bytes,
                "sha256": art.sha256,
                "last_modified": None,
            })
        for cache in getattr(result, "cache_uploads", None) or []:
            remote_path = _remote_path_from_url(
                getattr(cache, "remote_url", ""), prefix,
                Path(getattr(cache, "local_archive", "") or "").name,
            )
            if remote_path:
                records.append({
                    "path": remote_path,
                    "size": getattr(cache, "size_bytes", None),
                    "sha256": getattr(cache, "sha256", ""),
                    "last_modified": None,
                })

        entries, discovered_manifest = self._index_entries(records, prefix, cfg)
        manifest_href = None
        if result.manifest_url:
            manifest_href = discovered_manifest or self._artifact_href(
                f"{prefix}/manifest.json", cfg, prefix
            )

        html_text = self.generate_index_html(
            entries,
            title=self.compose_index_title(
                cfg, device=device, release=release, distro=distro, vendor=vendor
            ),
            metadata={
                "device": device,
                "release": release,
                "distro": distro,
                "vendor": vendor,
                "prefix": prefix,
            },
            manifest_href=manifest_href,
            tree=build_index_tree(entries),
            index_config=cfg,
        )
        return self._upload_html(html_text, f"{prefix}/index.html")

    @staticmethod
    def _artifact_remote_path(artifact: "UploadedArtifact", prefix: str) -> str:
        """Best-effort remote path of *artifact* under *prefix*."""
        return _remote_path_from_url(
            artifact.remote_url, prefix, artifact.local_path.name
        )

    def rebuild_index(
        self,
        prefix: str,
        index_config: Optional[IndexConfig] = None,
    ) -> Optional[str]:
        """
        Rebuild ``{prefix}/index.html`` purely from the live container listing.

        This requires no local build and is what lets a scheduled job refresh
        expiring signed links.  The remote directory structure below *prefix*
        is preserved in the generated tree.

        Args:
            prefix: Remote prefix to index.
            index_config: Optional index configuration override.

        Returns:
            Remote URL of the uploaded index, or ``None`` on failure.
        """
        cfg = index_config or self.config.index or IndexConfig()
        prefix = prefix.strip("/")
        records = self.backend.list_artifacts_detailed(prefix)

        entries, manifest_href = self._index_entries(records, prefix, cfg)

        html_text = self.generate_index_html(
            entries,
            title=self.compose_index_title(cfg) if not prefix else prefix,
            metadata={"prefix": prefix or "/"},
            manifest_href=manifest_href,
            tree=build_index_tree(entries),
            index_config=cfg,
        )
        remote = f"{prefix}/index.html" if prefix else "index.html"
        return self._upload_html(html_text, remote)

    def _upload_root_index(
        self,
        index_config: Optional[IndexConfig] = None,
    ) -> Optional[str]:
        """
        Generate and upload a container-root ``index.html`` presenting every
        indexed prefix as a navigable tree, newest first.
        """
        cfg = index_config or self.config.index or IndexConfig()
        if self.backend.dry_run:
            print("[dry-run] Would generate and upload root index → index.html")
            return "dry-run:index.html"

        try:
            blobs = self.backend.list_artifacts("")
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Failed to list container for root index: %s", exc)
            return None

        prefixes = set()
        for blob in blobs:
            name = blob.rsplit("/", 1)[-1]
            if name.lower() == "index.html":
                continue
            if _matches_any(blob, cfg.exclude):
                continue
            if "/" in blob:
                prefixes.add(blob.rsplit("/", 1)[0])

        entries = [
            {
                "name": p.rsplit("/", 1)[-1],
                "path": f"{p}/index.html",
                "href": f"{p}/index.html" if not cfg.sign_urls
                else self._artifact_href(f"{p}/index.html", cfg),
                "size_bytes": 0,
                "sha256": "",
            }
            for p in sorted(prefixes, reverse=True)
        ]

        html_text = self.generate_index_html(
            entries,
            title="Build artifacts",
            metadata={"prefixes": len(entries)},
            tree=build_index_tree(entries),
            index_config=cfg,
        )
        return self._upload_html(html_text, "index.html")

    def _upload_manifest(
        self,
        result: DeployResult,
        prefix: str,
        device: str,
        release: str,
        distro: str,
        vendor: str,
    ) -> Optional[str]:
        """Generate and upload the JSON manifest; return its remote URL."""
        manifest_json = self.generate_manifest(
            result, device=device, release=release, distro=distro, vendor=vendor
        )
        remote_manifest = f"{prefix}/manifest.json"

        if self.backend.dry_run:
            self.logger.info("[dry-run] Would upload manifest → %s", remote_manifest)
            return f"dry-run:{remote_manifest}"

        try:
            # Write to a temp file then upload
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, prefix="bsp_manifest_"
            ) as fh:
                fh.write(manifest_json)
                tmp_path = Path(fh.name)
            url = self.backend.upload_file(tmp_path, remote_manifest)
            tmp_path.unlink(missing_ok=True)
            return url
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Failed to upload manifest: %s", exc)
            return None

    @staticmethod
    def _sha256(path: Path) -> str:
        """Return the hex SHA-256 digest of a file."""
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _create_archive(
        files: List[Path],
        basename: str,
        fmt: str,
    ) -> Path:
        """
        Pack *files* into a temporary compressed archive and return its path.

        The caller is responsible for deleting the returned file **and its
        parent temporary directory** when done.

        Args:
            files: Ordered list of files to include.
            basename: Archive file name without extension
                      (e.g. ``"firmware-my-device-2024-01-15"``).
            fmt: Archive format.  Supported: ``"tar.gz"``,
                 ``"tar.bz2"``, ``"tar.xz"``, ``"zip"``.

        Returns:
            ``Path`` to the created archive file inside a temporary directory.

        Raises:
            ValueError: If *fmt* is not a recognised format.
        """
        _TAR_MODES = {
            "tar.gz": ("gz", ".tar.gz"),
            "tar.bz2": ("bz2", ".tar.bz2"),
            "tar.xz": ("xz", ".tar.xz"),
        }

        fmt_lower = fmt.lower()
        tmp_dir = Path(tempfile.mkdtemp(prefix="bsp_archive_"))
        try:
            if fmt_lower in _TAR_MODES:
                mode, ext = _TAR_MODES[fmt_lower]
                archive_path = tmp_dir / f"{basename}{ext}"
                with tarfile.open(archive_path, f"w:{mode}") as tar:
                    for file_path in files:
                        tar.add(file_path, arcname=file_path.name)
            elif fmt_lower == "zip":
                archive_path = tmp_dir / f"{basename}.zip"
                with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for file_path in files:
                        zf.write(file_path, arcname=file_path.name)
            else:
                raise ValueError(
                    f"Unsupported archive format '{fmt}'. "
                    "Choose one of: tar.gz, tar.bz2, tar.xz, zip."
                )
        except Exception:
            # Clean up the temp dir if archive creation fails.
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        return archive_path
