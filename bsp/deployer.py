"""
Artifact deployer: discovers and uploads Yocto build artifacts to cloud storage.
"""

import datetime
import fnmatch
import hashlib
import html
import json
import logging
import re
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import DeployConfig, IndexConfig
from .storage.base import CloudStorageBackend

#: Name of the build manifest written by ``bsp build`` into the build path.
BUILD_MANIFEST_FILENAME = "build-manifest.json"

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
    :root {
      --bg: #ffffff;
      --surface: #f7f8fa;
      --fg: #1f2328;
      --muted: #656d76;
      --accent: #0366d6;
      --accent-fg: #ffffff;
      --border: #d8dee4;
      --border-soft: #eef1f4;
      --radius: 6px;
      --radius-pill: 999px;
      --space-1: 0.25rem;
      --space-2: 0.5rem;
      --space-3: 1rem;
      --space-4: 1.5rem;
      --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    }
    :root[data-theme="dark"] {
      --bg: #0d1117;
      --surface: #161b22;
      --fg: #e6edf3;
      --muted: #8b949e;
      --accent: #4493f8;
      --accent-fg: #0d1117;
      --border: #30363d;
      --border-soft: #21262d;
    }
    @media (prefers-color-scheme: dark) {
      :root[data-theme="auto"] {
        --bg: #0d1117;
        --surface: #161b22;
        --fg: #e6edf3;
        --muted: #8b949e;
        --accent: #4493f8;
        --accent-fg: #0d1117;
        --border: #30363d;
        --border-soft: #21262d;
      }
    }
    * { box-sizing: border-box; }
    body {
      font-family: var(--font);
      background: var(--bg);
      color: var(--fg);
      margin: 0;
      line-height: 1.5;
    }
    .page { max-width: 72rem; margin: 0 auto; padding: 0 var(--space-3) var(--space-4); }
    .page-header {
      position: sticky; top: 0; z-index: 10;
      background: var(--bg);
      border-bottom: 1px solid var(--border);
      padding: var(--space-3) 0 var(--space-2);
    }
    h1 { font-size: 1.35rem; margin: 0 0 var(--space-2); overflow-wrap: anywhere; }
    a { color: var(--accent); }
    .breadcrumb {
      font-family: var(--mono); font-size: 0.85em; color: var(--muted);
      margin: 0 0 var(--space-2); overflow-wrap: anywhere;
    }
    .breadcrumb a { text-decoration: none; }
    .breadcrumb a:hover { text-decoration: underline; }
    .badges { display: flex; flex-wrap: wrap; gap: var(--space-1); margin: 0 0 var(--space-2); padding: 0; }
    .badge {
      display: inline-flex; gap: 0.35rem; align-items: baseline;
      border: 1px solid var(--border); border-radius: var(--radius-pill);
      background: var(--surface); padding: 0.1rem 0.65rem; font-size: 0.8em;
    }
    .badge dt { font-weight: 600; color: var(--muted); margin: 0; }
    .badge dd { margin: 0; font-family: var(--mono); overflow-wrap: anywhere; }
    table { border-collapse: collapse; width: 100%; }
    th, td { padding: 0.35rem 0.8rem; border-bottom: 1px solid var(--border-soft); text-align: left; }
    td.size { text-align: right; font-variant-numeric: tabular-nums; }
    code { font-family: var(--mono); font-size: 0.9em; }
    .controls { display: flex; flex-wrap: wrap; gap: var(--space-2); margin: var(--space-2) 0; }
    .controls input[type="search"] {
      flex: 1 1 20rem; padding: 0.35rem 0.6rem; border-radius: var(--radius);
      border: 1px solid var(--border); background: var(--bg); color: var(--fg);
    }
    button {
      font: inherit; color: inherit; border: 1px solid var(--border);
      background: var(--surface); border-radius: var(--radius);
      padding: 0.25rem 0.7rem; cursor: pointer;
    }
    button:hover { border-color: var(--accent); }
    :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    .facets { display: flex; flex-direction: column; gap: var(--space-1); margin-bottom: var(--space-2); }
    .facet-group { border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
    .facet-group > summary {
      cursor: pointer; padding: 0.3rem 0.7rem; font-size: 0.85em;
      font-weight: 600; color: var(--muted); list-style: revert;
    }
    .facet-group .chips { padding: 0 0.7rem 0.5rem; margin: 0; }
    .chips { display: flex; flex-wrap: wrap; gap: var(--space-1); margin-bottom: var(--space-2); }
    .chip {
      cursor: pointer; border: 1px solid var(--border); background: var(--bg);
      border-radius: var(--radius-pill); padding: 0.1rem 0.7rem; font-size: 0.8em;
    }
    .chip .count { color: var(--muted); margin-left: 0.35rem; font-variant-numeric: tabular-nums; }
    .chip.active { background: var(--accent); border-color: var(--accent); color: var(--accent-fg); }
    .chip.active .count { color: inherit; }
    .chip.empty { opacity: 0.45; }
    .date-range { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center;
                  padding: 0 0.7rem 0.5rem; font-size: 0.85em; color: var(--muted); }
    .date-range input {
      font: inherit; padding: 0.2rem 0.4rem; border: 1px solid var(--border);
      border-radius: var(--radius); background: var(--bg); color: var(--fg);
    }
    .sorters { display: flex; flex-wrap: wrap; gap: 0; margin: var(--space-2) 0; font-size: 0.85em; }
    .sorters button { border-radius: 0; margin: 0; border-left-width: 0; }
    .sorters button:first-of-type { border-left-width: 1px;
      border-radius: var(--radius) 0 0 var(--radius); }
    .sorters button:last-of-type { border-radius: 0 var(--radius) var(--radius) 0; }
    .sorters button[aria-pressed="true"] {
      background: var(--accent); border-color: var(--accent); color: var(--accent-fg);
    }
    .summary { color: var(--muted); font-size: 0.85em; margin: var(--space-2) 0; }
    #bsp-tree .row {
      display: flex; align-items: center; gap: var(--space-2);
      padding: 0.2rem var(--space-2); border-bottom: 1px solid var(--border-soft);
      border-radius: var(--radius);
    }
    #bsp-tree .row:hover { background: var(--surface); }
    #bsp-tree .icon { flex: 0 0 auto; width: 1.2em; text-align: center; }
    #bsp-tree .name { flex: 1 1 auto; overflow-wrap: anywhere; }
    #bsp-tree .meta {
      color: var(--muted); font-size: 0.8em; white-space: nowrap;
      text-align: right; font-variant-numeric: tabular-nums;
    }
    #bsp-tree .sha { font-family: var(--mono); }
    #bsp-tree .copy { border: none; background: none; padding: 0 0.2rem; cursor: pointer; }
    #bsp-tree .toggle {
      border: none; background: none; cursor: pointer;
      font-family: var(--mono); font-size: 1em; padding: 0 0.2rem;
      transition: transform 0.12s ease-in-out;
    }
    #bsp-tree .dir > .name { font-weight: 600; }
    #bsp-tree .bar { flex: 0 0 4rem; height: 0.35rem; border-radius: var(--radius-pill);
                     background: var(--border-soft); overflow: hidden; }
    #bsp-tree .bar > span { display: block; height: 100%; background: var(--accent); }
    #bsp-empty {
      border: 1px solid var(--border); border-radius: var(--radius);
      background: var(--surface); padding: var(--space-3); text-align: center;
      color: var(--muted);
    }
    @media (prefers-reduced-motion: reduce) {
      * { transition: none !important; animation: none !important; }
    }
    @media (max-width: 640px) {
      .page { padding: 0 var(--space-2) var(--space-3); }
      #bsp-tree .row { flex-wrap: wrap; }
      #bsp-tree .meta { flex: 1 1 100%; text-align: left; white-space: normal; }
      .controls input[type="search"] { flex: 1 1 100%; }
    }
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
  var facetDefs = payload.facets || [];
  var container = document.getElementById('bsp-tree');
  var emptyEl = document.getElementById('bsp-empty');
  var searchEl = document.getElementById('bsp-search');
  var facetsEl = document.getElementById('bsp-facets');
  var summaryEl = document.getElementById('bsp-summary');
  var fromEl = document.getElementById('bsp-date-from');
  var toEl = document.getElementById('bsp-date-to');
  var open = {};
  var query = '';
  var selected = {};
  var dateFrom = '';
  var dateTo = '';
  var sortKey = 'name';
  var sortAsc = true;
  var DAY_MS = 86400000;

  facetDefs.forEach(function (def) { selected[def.key] = {}; });

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

  function iconFor(path) {
    var name = String(path || '').toLowerCase();
    if (/\.(wic|img|bin|sdimg|rootfs)(\.\w+)*$/.test(name)) { return '\u25a3'; }
    if (/\.(tar|tar\.\w+|tgz|zip|xz|gz|bz2|zst)$/.test(name)) { return '\u2637'; }
    if (/\.sh$/.test(name)) { return '\u2699'; }
    if (/\.json$/.test(name)) { return '\u007b\u007d'; }
    if (/\.(log|txt|md)$/.test(name)) { return '\u2261'; }
    if (/\.html?$/.test(name)) { return '\u2b1a'; }
    return '\u2022';
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

  function facetValues(node, key) {
    var facets = node.facets || {};
    var value = facets[key];
    if (value === undefined || value === null || value === '') { return []; }
    return Array.isArray(value) ? value : [value];
  }

  function selectedValues(key) {
    return Object.keys(selected[key] || {}).filter(function (v) { return selected[key][v]; });
  }

  function dateOf(node) {
    var raw = node.last_modified || (node.facets || {}).date || '';
    return String(raw).slice(0, 10);
  }

  function dateVisible(node) {
    if (!dateFrom && !dateTo) { return true; }
    var day = dateOf(node);
    if (!day) { return false; }
    if (dateFrom && day < dateFrom) { return false; }
    if (dateTo && day > dateTo) { return false; }
    return true;
  }

  function facetsVisible(node) {
    for (var i = 0; i < facetDefs.length; i++) {
      var key = facetDefs[i].key;
      var wanted = selectedValues(key);
      if (!wanted.length) { continue; }
      var have = facetValues(node, key);
      var hit = false;
      for (var j = 0; j < wanted.length; j++) {
        if (have.indexOf(wanted[j]) >= 0) { hit = true; break; }
      }
      if (!hit) { return false; }
    }
    return true;
  }

  function fileVisible(node, match) {
    if (match && !match.test(node.path)) { return false; }
    if (!facetsVisible(node)) { return false; }
    if (!dateVisible(node)) { return false; }
    return true;
  }

  function eachFile(node, fn) {
    (node.children || []).forEach(function (child) {
      if (child.type === 'dir') { eachFile(child, fn); } else { fn(child); }
    });
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

  function filtersActive() {
    if (query || dateFrom || dateTo) { return true; }
    for (var i = 0; i < facetDefs.length; i++) {
      if (selectedValues(facetDefs[i].key).length) { return true; }
    }
    return false;
  }

  function isOpen(node, depth) {
    if (filtersActive()) { return true; }
    if (Object.prototype.hasOwnProperty.call(open, node.path)) { return open[node.path]; }
    return depth < (opts.collapseDepth === undefined ? 1 : opts.collapseDepth);
  }

  function renderDir(node, depth, parent, stats) {
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
        row.setAttribute('role', 'treeitem');
        row.setAttribute('tabindex', '-1');
        row.style.paddingLeft = (depth * 1.2) + 'rem';
        var toggle = document.createElement('button');
        toggle.type = 'button';
        toggle.className = 'toggle';
        toggle.setAttribute('aria-label', 'Toggle ' + child.name);
        toggle.textContent = expanded ? '\u25be' : '\u25b8';
        var label = document.createElement('span');
        label.className = 'name';
        label.textContent = child.name + '/';
        var meta = document.createElement('span');
        meta.className = 'meta';
        meta.textContent = child.file_count + ' files, ' + humanSize(child.size_bytes);
        row.appendChild(toggle);
        row.appendChild(label);
        if (opts.sizeBars && (tree.size_bytes || 0) > 0) {
          var bar = document.createElement('span');
          bar.className = 'bar';
          var fill = document.createElement('span');
          var pct = Math.max(2, Math.round(
            ((child.size_bytes || 0) / tree.size_bytes) * 100));
          fill.style.width = pct + '%';
          bar.appendChild(fill);
          row.appendChild(bar);
        }
        row.appendChild(meta);
        var kids = document.createElement('div');
        kids.setAttribute('role', 'group');
        var count = renderDir(child, depth + 1, kids, stats);
        if (count === 0) { return; }
        kids.hidden = !expanded;
        row.setAttribute('aria-expanded', expanded ? 'true' : 'false');
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
        frow.setAttribute('role', 'treeitem');
        frow.setAttribute('tabindex', '-1');
        frow.style.paddingLeft = ((depth * 1.2) + 1.4) + 'rem';
        var icon = document.createElement('span');
        icon.className = 'icon';
        icon.setAttribute('aria-hidden', 'true');
        icon.textContent = iconFor(child.path);
        var link = document.createElement('a');
        link.className = 'name';
        link.href = child.href;
        link.textContent = child.name;
        var fmeta = document.createElement('span');
        fmeta.className = 'meta';
        var bits = [humanSize(child.size_bytes)];
        if (opts.showDates && child.last_modified) { bits.push(child.last_modified); }
        fmeta.textContent = bits.filter(Boolean).join(' \u00b7 ');
        frow.appendChild(icon);
        frow.appendChild(link);
        frow.appendChild(fmeta);
        if (child.sha256) {
          var sha = document.createElement('span');
          sha.className = 'meta sha';
          sha.textContent = child.sha256.slice(0, 12);
          var copy = document.createElement('button');
          copy.type = 'button';
          copy.className = 'copy';
          copy.title = 'Copy SHA-256';
          copy.setAttribute('aria-label', 'Copy SHA-256 of ' + child.name);
          copy.textContent = '\u29c9';
          copy.addEventListener('click', function () {
            if (navigator.clipboard) { navigator.clipboard.writeText(child.sha256); }
          });
          frow.appendChild(sha);
          frow.appendChild(copy);
        }
        parent.appendChild(frow);
        rendered += 1;
        if (stats) {
          stats.files += 1;
          stats.bytes += Number(child.size_bytes) || 0;
        }
      }
    });
    return rendered;
  }

  function syncHash() {
    var parts = [];
    if (query) { parts.push('q=' + encodeURIComponent(query)); }
    facetDefs.forEach(function (def) {
      var values = selectedValues(def.key);
      if (values.length) {
        parts.push(def.key + '=' + encodeURIComponent(values.join('|')));
      }
    });
    if (dateFrom) { parts.push('from=' + encodeURIComponent(dateFrom)); }
    if (dateTo) { parts.push('to=' + encodeURIComponent(dateTo)); }
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
    var known = {};
    facetDefs.forEach(function (def) { known[def.key] = true; });
    hash.split('&').forEach(function (pair) {
      var idx = pair.indexOf('=');
      if (idx < 0) { return; }
      var key = pair.slice(0, idx);
      var value = decodeURIComponent(pair.slice(idx + 1));
      if (key === 'q') { query = value; if (searchEl) { searchEl.value = value; } }
      else if (key === 'from') { dateFrom = value; if (fromEl) { fromEl.value = value; } }
      else if (key === 'to') { dateTo = value; if (toEl) { toEl.value = value; } }
      else if (known[key]) {
        value.split('|').forEach(function (v) { if (v) { selected[key][v] = true; } });
      }
      else if (key === 'open') {
        value.split('|').forEach(function (path) { if (path) { open[path] = true; } });
      }
    });
  }

  function counts(key) {
    var saved = selected[key];
    selected[key] = {};
    var result = {};
    var match = matcher();
    eachFile(tree, function (node) {
      if (!fileVisible(node, match)) { return; }
      facetValues(node, key).forEach(function (value) {
        result[value] = (result[value] || 0) + 1;
      });
    });
    selected[key] = saved;
    return result;
  }

  function refreshFacetChips() {
    if (!facetsEl) { return; }
    facetDefs.forEach(function (def) {
      var current = counts(def.key);
      var chips = facetsEl.querySelectorAll('.chip[data-facet="' + def.key + '"]');
      Array.prototype.forEach.call(chips, function (chip) {
        var value = chip.getAttribute('data-value') || '';
        var isBucket = chip.hasAttribute('data-bucket');
        var active = isBucket
          ? bucketActive(chip.getAttribute('data-bucket'))
          : !!selected[def.key][value];
        chip.classList.toggle('active', active);
        chip.setAttribute('aria-pressed', active ? 'true' : 'false');
        if (!isBucket) {
          var n = current[value] || 0;
          chip.classList.toggle('empty', n === 0 && !active);
          var badge = chip.querySelector('.count');
          if (badge) { badge.textContent = n; }
        }
      });
    });
  }

  function isoDay(offsetDays) {
    var d = new Date(Date.now() - offsetDays * DAY_MS);
    return d.toISOString().slice(0, 10);
  }

  function bucketRange(bucket) {
    if (bucket === 'today') { return [isoDay(0), '']; }
    if (bucket === '7') { return [isoDay(7), '']; }
    if (bucket === '30') { return [isoDay(30), '']; }
    if (bucket === 'older') { return ['', isoDay(30)]; }
    return ['', ''];
  }

  function bucketActive(bucket) {
    var range = bucketRange(bucket);
    return dateFrom === range[0] && dateTo === range[1] && (dateFrom || dateTo);
  }

  function render() {
    container.textContent = '';
    var stats = { files: 0, bytes: 0 };
    var count = renderDir(tree, 0, container, stats);
    if (emptyEl) { emptyEl.hidden = count !== 0; }
    refreshFacetChips();
    if (summaryEl) {
      summaryEl.textContent = stats.files + (stats.files === 1 ? ' file' : ' files')
        + ' \u00b7 ' + humanSize(stats.bytes);
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
  sorters.setAttribute('role', 'group');
  sorters.setAttribute('aria-label', 'Sort artifacts');
  var sortButtons = [];
  [['name', 'Name'], ['size', 'Size'], ['date', 'Modified']].forEach(function (pair) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.setAttribute('data-sort', pair[0]);
    btn.textContent = pair[1];
    btn.addEventListener('click', function () {
      if (sortKey === pair[0]) { sortAsc = !sortAsc; } else { sortKey = pair[0]; sortAsc = true; }
      syncSorters();
      render();
    });
    sorters.appendChild(btn);
    sortButtons.push(btn);
  });
  function syncSorters() {
    sortButtons.forEach(function (btn) {
      var key = btn.getAttribute('data-sort');
      var active = key === sortKey;
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
      btn.textContent = btn.textContent.replace(/[\u2191\u2193]\s*$/, '').trim()
        + (active ? (sortAsc ? ' \u2191' : ' \u2193') : '');
    });
  }
  container.parentNode.insertBefore(sorters, container);

  if (searchEl) {
    var timer = null;
    searchEl.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () { query = searchEl.value.trim(); render(); }, 150);
    });
  }
  if (facetsEl) {
    facetsEl.addEventListener('click', function (event) {
      var chip = event.target.closest('.chip');
      if (!chip) { return; }
      var key = chip.getAttribute('data-facet');
      if (!key) { return; }
      var bucket = chip.getAttribute('data-bucket');
      if (bucket !== null) {
        var range = bucketRange(bucket);
        if (bucketActive(bucket)) { dateFrom = ''; dateTo = ''; }
        else { dateFrom = range[0]; dateTo = range[1]; }
        if (fromEl) { fromEl.value = dateFrom; }
        if (toEl) { toEl.value = dateTo; }
      } else {
        var value = chip.getAttribute('data-value') || '';
        selected[key][value] = !selected[key][value];
      }
      render();
    });
  }
  if (fromEl) {
    fromEl.addEventListener('change', function () { dateFrom = fromEl.value; render(); });
  }
  if (toEl) {
    toEl.addEventListener('change', function () { dateTo = toEl.value; render(); });
  }
  var expandBtn = document.getElementById('bsp-expand');
  var collapseBtn = document.getElementById('bsp-collapse');
  if (expandBtn) { expandBtn.addEventListener('click', function () { setAll(true); }); }
  if (collapseBtn) { collapseBtn.addEventListener('click', function () { setAll(false); }); }

  container.addEventListener('keydown', function (event) {
    var keys = ['ArrowDown', 'ArrowUp', 'ArrowRight', 'ArrowLeft'];
    if (keys.indexOf(event.key) < 0) { return; }
    var rows = Array.prototype.slice.call(
      container.querySelectorAll('.row[role="treeitem"]')
    ).filter(function (row) { return row.offsetParent !== null; });
    if (!rows.length) { return; }
    var active = document.activeElement;
    while (active && rows.indexOf(active) < 0) { active = active.parentElement; }
    var idx = rows.indexOf(active);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      var next = event.key === 'ArrowDown' ? idx + 1 : idx - 1;
      if (next < 0) { next = 0; }
      if (next >= rows.length) { next = rows.length - 1; }
      rows[next].focus();
    } else if (idx >= 0 && rows[idx].classList.contains('dir')) {
      event.preventDefault();
      var toggle = rows[idx].querySelector('.toggle');
      var expanded = rows[idx].getAttribute('aria-expanded') === 'true';
      if (toggle && expanded !== (event.key === 'ArrowRight')) { toggle.click(); }
    }
  });

  readHash();
  syncSorters();
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


#: Facet groups that may be offered in the generated index filter bar, in the
#: order they are rendered.  Maps the facet key to its human-readable label.
FACET_LABELS = {
    "preset": "Preset",
    "machine": "Machine",
    "release": "Yocto release",
    "distro": "Distro",
    "vendor": "Vendor",
    "date": "Upload date",
}

#: Title used when no facet value is known, so the generated page never shows
#: a placeholder-only heading such as ``unknown unknown — unknown``.
DEFAULT_INDEX_TITLE = "BSP Registry Binary Artifacts"


def _title_needs_facets(template: str) -> bool:
    """Return ``True`` when *template* references any facet placeholder."""
    return any(
        f"{{{name}}}" in template
        for name in ("device", "release", "distro", "vendor", "preset")
    )

#: Sidecar file storing the facet values of a deployed prefix so later index
#: rebuilds do not have to guess them from the prefix layout.
INDEX_META_NAME = "index-meta.json"

#: Discrete upload-date buckets rendered as chips next to the date range.
_DATE_BUCKETS = (("today", "Today"), ("7", "Last 7 days"),
                 ("30", "Last 30 days"), ("older", "Older"))


def parse_prefix_facets(template: str, prefix: str) -> Dict[str, str]:
    """
    Recover facet values from *prefix* by inverting a prefix *template*.

    The template is the ``DeployConfig.prefix`` string (default
    ``"{vendor}/{device}/{release}/{date}"``).  Its literal separators are
    matched against *prefix* to recover each placeholder value.

    Args:
        template: Prefix template containing ``{placeholder}`` fields.
        prefix: Concrete remote prefix to parse.

    Returns:
        Mapping of facet key to value.  ``device`` is also exposed as
        ``machine``.  An empty dict is returned when *prefix* does not match
        the template.
    """
    template = (template or "{vendor}/{device}/{release}/{date}").strip("/")
    prefix = (prefix or "").strip("/")
    if not template or not prefix:
        return {}

    names: List[str] = []
    pattern_parts: List[str] = []
    for piece in re.split(r"(\{[a-zA-Z_][a-zA-Z0-9_]*\})", template):
        if not piece:
            continue
        if piece.startswith("{") and piece.endswith("}"):
            names.append(piece[1:-1])
            pattern_parts.append("([^/]+)")
        else:
            pattern_parts.append(re.escape(piece))
    if not names:
        return {}

    match = re.fullmatch("".join(pattern_parts), prefix)
    if not match:
        return {}

    facets: Dict[str, str] = {}
    for name, value in zip(names, match.groups()):
        if not value:
            continue
        facets[name] = value
        if name == "device":
            facets["machine"] = value
        if name == "datetime":
            facets.setdefault("date", value[:8])
    return facets


def _facet_values(node: Dict, key: str) -> List[str]:
    """Return the facet values stored on *node* for *key* as a list."""
    value = (node.get("facets") or {}).get(key)
    if value in (None, "", []):
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def collect_facets(tree: Dict, keys: List[str]) -> List[Dict]:
    """
    Build the facet definitions embedded in the index data island.

    Args:
        tree: Directory tree produced by :func:`build_index_tree`.
        keys: Facet keys to expose, in render order.

    Returns:
        One dict per non-empty facet group with ``key``, ``label`` and a
        ``values`` list of ``{"value": ..., "count": ...}`` entries sorted by
        descending count then value.
    """
    files = flatten_index_tree(tree)
    groups: List[Dict] = []
    for key in keys or []:
        if key not in FACET_LABELS:
            continue
        counts: Dict[str, int] = {}
        for node in files:
            for value in _facet_values(node, key):
                counts[value] = counts.get(value, 0) + 1
        if not counts:
            continue
        if key == "date":
            # Newest uploads first: dates read better in reverse order.
            values = sorted(counts.items(), key=lambda kv: kv[0], reverse=True)
        else:
            values = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        groups.append({
            "key": key,
            "label": FACET_LABELS[key],
            "values": [{"value": v, "count": c} for v, c in values],
        })
    return groups


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
        "children": [], "file_count": 0, "size_bytes": 0, "facets": {},
    }

    def _child_dir(parent: Dict, name: str) -> Dict:
        for child in parent["children"]:
            if child["type"] == "dir" and child["name"] == name:
                return child
        path = f"{parent['path']}/{name}" if parent["path"] else name
        node = {
            "type": "dir", "name": name, "path": path,
            "children": [], "file_count": 0, "size_bytes": 0, "facets": {},
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
        facets = {
            key: value
            for key, value in (record.get("facets") or {}).items()
            if value not in (None, "", [])
        }
        node["children"].append({
            "type": "file",
            "name": parts[-1],
            "path": rel,
            "href": record.get("href", ""),
            "size_bytes": record.get("size_bytes"),
            "last_modified": record.get("last_modified"),
            "sha256": record.get("sha256") or "",
            "facets": facets,
        })
        for ancestor in chain:
            ancestor["file_count"] += 1
            ancestor["size_bytes"] += size
            for key, value in facets.items():
                bucket = ancestor["facets"].setdefault(key, [])
                for item in (value if isinstance(value, list) else [value]):
                    if item not in bucket:
                        bucket.append(item)

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
    build_manifest_url: Optional[str] = None
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
        preset: str = "",
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
            preset: Optional BSP preset name, recorded as an index facet.

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

        if self.config.include_build_manifest and result.artifacts:
            result.build_manifest_url = self._upload_build_manifest(build_path, prefix)

        if self.config.include_manifest and result.artifacts:
            manifest_url = self._upload_manifest(result, prefix, device, release, distro, vendor)
            result.manifest_url = manifest_url

        index_cfg = self.config.index or IndexConfig()
        index_enabled = index_cfg.enabled if update_index is None else update_index
        if index_enabled and result.artifacts:
            result.index_url = self._upload_index(
                result, prefix, device, release, distro, vendor,
                index_config=index_cfg, preset=preset,
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

        if result.build_manifest_url:
            manifest["build_manifest"] = {
                "name": BUILD_MANIFEST_FILENAME,
                "remote_url": result.build_manifest_url,
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
        preset: str = "",
    ) -> str:
        """
        Expand the ``IndexConfig.title`` template.

        Supports the same placeholders as :meth:`compose_remote_prefix` plus
        ``{preset}``.  When none of the facet values are known the configured
        template would expand to a meaningless placeholder-only heading, so
        :data:`DEFAULT_INDEX_TITLE` is returned instead.
        """
        cfg = index_config or self.config.index or IndexConfig()
        now = datetime.datetime.now(datetime.timezone.utc)
        if not any((device, release, distro, vendor, preset)):
            if _title_needs_facets(cfg.title):
                return DEFAULT_INDEX_TITLE
        try:
            return cfg.title.format(
                device=device or "unknown",
                release=release or "unknown",
                distro=distro or "unknown",
                vendor=vendor or "unknown",
                preset=preset or "unknown",
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
        facet_groups = collect_facets(tree, list(cfg.facets or [])) if use_tree else []
        theme = cfg.theme if cfg.theme in ("auto", "light", "dark") else "auto"
        accent = str(cfg.accent or "").strip()
        if any(ch in accent for ch in "<>;{}\"'"):
            accent = ""
        lines = [
            "<!DOCTYPE html>",
            f'<html lang="en" data-theme="{esc(theme, quote=True)}">',
            "<head>",
            '  <meta charset="utf-8">',
            '  <meta name="viewport" content="width=device-width, initial-scale=1">',
            _NO_CACHE_META.rstrip("\n"),
            f"  <title>{esc(title)}</title>",
            "  <style>",
            _INDEX_CSS.rstrip("\n"),
        ]
        if accent:
            lines.append(
                "    :root { --accent: " + esc(accent) + "; }"
            )
        lines.extend([
            "  </style>",
            "</head>",
            "<body>",
            '  <div class="page">',
            '  <header class="page-header">',
            f"  <h1>{esc(title)}</h1>",
        ])

        meta_items = list((metadata or {}).items())
        meta_items.append(("generated", generated))
        prefix_value = str((metadata or {}).get("prefix") or "")
        lines.extend(self._render_breadcrumb(prefix_value))
        lines.append('  <dl class="badges">')
        for key, value in meta_items:
            if value in (None, ""):
                continue
            lines.append(
                f'    <div class="badge"><dt>{esc(str(key))}</dt>'
                f"<dd>{esc(str(value))}</dd></div>"
            )
        lines.append("  </dl>")

        if use_tree:
            lines.extend(self._render_tree_controls(cfg, entries, facet_groups))
            lines.append("  </header>")
            lines.append('  <p class="summary" id="bsp-summary"></p>')
            lines.append('  <div id="bsp-tree" role="tree"></div>')
            lines.append('  <p id="bsp-empty" hidden>No matching artifacts.</p>')
            lines.append("  <noscript>")
            lines.extend(self._render_flat_table(entries, show_dates))
            lines.append("  </noscript>")
        else:
            lines.append("  </header>")
            lines.extend(self._render_flat_table(entries, show_dates))

        if manifest_href:
            lines.append(
                f'  <p><a href="{esc(str(manifest_href), quote=True)}">manifest.json</a></p>'
            )

        if use_tree:
            payload = {
                "tree": tree,
                "facets": facet_groups,
                "options": {
                    "collapseDepth": max(0, int(cfg.collapse_depth or 0)),
                    "search": bool(cfg.search),
                    "showDates": bool(show_dates),
                    "sizeBars": True,
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

        lines.append("  </div>")
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
    def _render_breadcrumb(prefix: str) -> List[str]:
        """Render a monospace breadcrumb linking each parent ``index.html``."""
        esc = html.escape
        segments = [seg for seg in str(prefix or "").strip("/").split("/") if seg]
        if not segments:
            return []
        parts = ['<a href="' + "../" * len(segments) + 'index.html">root</a>']
        for depth, segment in enumerate(segments):
            up = len(segments) - depth - 1
            if up:
                href = "../" * up + "index.html"
                parts.append(f'<a href="{esc(href, quote=True)}">{esc(segment)}</a>')
            else:
                parts.append(esc(segment))
        return ['  <p class="breadcrumb">' + " / ".join(parts) + "</p>"]

    @staticmethod
    def _render_tree_controls(
        cfg: IndexConfig,
        entries: List[Dict],
        facet_groups: Optional[List[Dict]] = None,
    ) -> List[str]:
        """Render the search box, facet bar and buttons."""
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
        if facet_groups:
            lines.append('  <div class="facets" id="bsp-facets">')
            for group in facet_groups:
                key = str(group.get("key", ""))
                label = str(group.get("label", key))
                lines.append(
                    f'    <details class="facet-group" open>'
                    f"<summary>{esc(label)}</summary>"
                )
                lines.append('      <div class="chips">')
                for item in group.get("values", []):
                    value = str(item.get("value", ""))
                    count = int(item.get("count", 0))
                    lines.append(
                        f'        <button type="button" class="chip" '
                        f'aria-pressed="false" '
                        f'data-facet="{esc(key, quote=True)}" '
                        f'data-value="{esc(value, quote=True)}">{esc(value)}'
                        f'<span class="count">{count}</span></button>'
                    )
                lines.append("      </div>")
                if key == "date":
                    lines.append('      <div class="chips">')
                    for bucket, bucket_label in _DATE_BUCKETS:
                        lines.append(
                            f'        <button type="button" class="chip" '
                            f'aria-pressed="false" data-facet="date" '
                            f'data-bucket="{esc(bucket, quote=True)}">'
                            f"{esc(bucket_label)}</button>"
                        )
                    lines.append("      </div>")
                    lines.append(
                        '      <div class="date-range">'
                        '<label for="bsp-date-from">From</label>'
                        '<input type="date" id="bsp-date-from">'
                        '<label for="bsp-date-to">To</label>'
                        '<input type="date" id="bsp-date-to">'
                        "</div>"
                    )
                lines.append("    </details>")
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
        facets: Optional[Dict] = None,
    ) -> Tuple[List[Dict], Optional[str]]:
        """
        Turn raw listing *records* into index entries relative to *prefix*.

        ``index.html`` pages are always skipped (at any depth) and paths
        matching ``IndexConfig.exclude`` are dropped.  The manifest href is
        returned separately so it can be rendered as a footer link.

        *facets* provides default facet values applied to every entry that
        does not carry its own ``facets`` mapping.
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
            if rel == INDEX_META_NAME:
                continue
            last_modified = record.get("last_modified")
            entry_facets = dict(record.get("facets") or facets or {})
            if last_modified and "date" not in entry_facets:
                entry_facets["date"] = str(last_modified)[:10]
            entries.append({
                "name": name,
                "path": rel,
                "href": self._artifact_href(remote_path, cfg, prefix),
                "size_bytes": record.get("size"),
                "last_modified": last_modified,
                "sha256": record.get("sha256") or "",
                "facets": entry_facets,
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
        preset: str = "",
    ) -> Optional[str]:
        """Generate and upload ``{prefix}/index.html``; return its remote URL."""
        cfg = index_config or self.config.index or IndexConfig()
        uploaded_at = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        )
        facets = {
            key: value
            for key, value in (
                ("preset", preset),
                ("machine", device),
                ("device", device),
                ("release", release),
                ("distro", distro),
                ("vendor", vendor),
                ("date", uploaded_at[:10]),
            )
            if value
        }
        records: List[Dict] = []
        for art in result.artifacts:
            remote_path = self._artifact_remote_path(art, prefix)
            records.append({
                "path": remote_path,
                "size": art.size_bytes,
                "sha256": art.sha256,
                "last_modified": uploaded_at,
                "facets": facets,
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
                    "last_modified": uploaded_at,
                    "facets": facets,
                })

        self._upload_index_meta(prefix, facets, uploaded_at)

        entries, discovered_manifest = self._index_entries(
            records, prefix, cfg, facets=facets
        )
        manifest_href = None
        if result.manifest_url:
            manifest_href = discovered_manifest or self._artifact_href(
                f"{prefix}/manifest.json", cfg, prefix
            )

        html_text = self.generate_index_html(
            entries,
            title=self.compose_index_title(
                cfg, device=device, release=release, distro=distro,
                vendor=vendor, preset=preset,
            ),
            metadata={
                "preset": preset,
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

    def _upload_index_meta(
        self,
        prefix: str,
        facets: Dict,
        uploaded_at: str,
    ) -> Optional[str]:
        """
        Persist *facets* as ``{prefix}/index-meta.json``.

        The sidecar lets :meth:`rebuild_index` and :meth:`_upload_root_index`
        recover authoritative facet values instead of guessing them from the
        prefix layout.  Failures are logged and ignored: a missing sidecar
        only degrades filtering.
        """
        remote = f"{prefix}/{INDEX_META_NAME}" if prefix else INDEX_META_NAME
        payload = json.dumps(
            {"schema_version": "1", "uploaded_at": uploaded_at, "facets": facets},
            indent=2, sort_keys=True,
        )
        if self.backend.dry_run:
            self.logger.info("[dry-run] Would upload index metadata → %s", remote)
            return f"dry-run:{remote}"
        tmp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, prefix="bsp_index_meta_"
            ) as fh:
                fh.write(payload)
                tmp_path = Path(fh.name)
            return self.backend.upload_file(tmp_path, remote)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Failed to upload index metadata %s: %s", remote, exc)
            return None
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    def _load_index_meta(self, prefix: str) -> Dict:
        """
        Read the facet sidecar stored under *prefix*.

        Returns an empty dict when the sidecar is missing or unreadable.
        """
        remote = f"{prefix}/{INDEX_META_NAME}" if prefix else INDEX_META_NAME
        if self.backend.dry_run:
            return {}
        tmp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".json", prefix="bsp_index_meta_", delete=False
            ) as fh:
                tmp_path = Path(fh.name)
            self.backend.download_file(remote, tmp_path)
            with open(tmp_path) as fh:
                data = json.load(fh)
            facets = data.get("facets") if isinstance(data, dict) else None
            return {
                str(k): v for k, v in (facets or {}).items()
                if v not in (None, "", [])
            }
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("No index metadata at %s: %s", remote, exc)
            return {}
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    def _prefix_facets(self, prefix: str) -> Dict:
        """
        Resolve the facets of *prefix*, preferring the persisted sidecar and
        falling back to parsing the configured prefix template.
        """
        facets = self._load_index_meta(prefix)
        if facets:
            return facets
        return parse_prefix_facets(self.config.prefix or "", prefix)

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

        facets = self._prefix_facets(prefix)
        entries, manifest_href = self._index_entries(
            records, prefix, cfg, facets=facets
        )

        metadata = {
            key: value for key, value in facets.items()
            if key in FACET_LABELS and key != "date"
        }
        metadata["prefix"] = prefix or "/"
        html_text = self.generate_index_html(
            entries,
            title=self.compose_index_title(cfg) if not prefix else prefix,
            metadata=metadata,
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

        entries = []
        for prefix in sorted(prefixes, reverse=True):
            facets = self._prefix_facets(prefix)
            entries.append({
                "name": prefix.rsplit("/", 1)[-1],
                "path": f"{prefix}/index.html",
                "href": f"{prefix}/index.html" if not cfg.sign_urls
                else self._artifact_href(f"{prefix}/index.html", cfg),
                "size_bytes": 0,
                "sha256": "",
                "last_modified": facets.get("date", ""),
                "facets": facets,
            })
        entries.sort(
            key=lambda e: (str(e.get("last_modified") or ""), e["path"]),
            reverse=True,
        )

        html_text = self.generate_index_html(
            entries,
            title=DEFAULT_INDEX_TITLE,
            metadata={"prefixes": len(entries)},
            tree=build_index_tree(entries),
            index_config=cfg,
        )
        return self._upload_html(html_text, "index.html")

    def _find_build_manifest(self, build_path: str) -> Optional[Path]:
        """
        Locate the ``build-manifest.json`` written by ``bsp build``.

        Looks in the build path root first, then in the ``build/``
        sub-directory used by some Yocto layouts.

        Args:
            build_path: Top-level Yocto build output directory.

        Returns:
            Path to the manifest, or ``None`` when it does not exist.
        """
        root = Path(build_path)
        for candidate in (
            root / BUILD_MANIFEST_FILENAME,
            root / "build" / BUILD_MANIFEST_FILENAME,
        ):
            if candidate.is_file():
                return candidate
        return None

    def _upload_build_manifest(self, build_path: str, prefix: str) -> Optional[str]:
        """
        Upload the build manifest produced by ``bsp build``.

        Args:
            build_path: Top-level Yocto build output directory.
            prefix: Resolved remote prefix.

        Returns:
            Remote URL of the uploaded manifest, or ``None`` when the file is
            missing or the upload failed.
        """
        local_path = self._find_build_manifest(build_path)
        if local_path is None:
            self.logger.warning(
                "No %s found under '%s'; skipping build manifest upload.",
                BUILD_MANIFEST_FILENAME,
                build_path,
            )
            return None

        remote_path = f"{prefix}/{BUILD_MANIFEST_FILENAME}"
        if self.backend.dry_run:
            self.logger.info(
                "[dry-run] Would upload build manifest → %s", remote_path
            )
            print(f"  [dry-run] Would upload {BUILD_MANIFEST_FILENAME}...")
            return f"dry-run:{remote_path}"

        print(f"  Uploading {BUILD_MANIFEST_FILENAME}...")
        try:
            return self.backend.upload_file(local_path, remote_path)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Failed to upload build manifest: %s", exc)
            return None

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
