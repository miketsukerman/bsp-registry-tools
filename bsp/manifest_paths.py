"""
Path sanitisation helpers for ``build-manifest.json``.

The build manifest is uploaded next to the build artifacts and is meant to be
consumed on machines other than the one that produced it.  It must therefore
never contain absolute host paths.  This module turns host paths into paths
that are relative to a small set of well-known anchors (the registry root and
the build root), and replaces anything outside those anchors with symbolic
placeholders.
"""

import os
import re
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

# Placeholder tokens used when a path cannot be expressed relative to one of
# the manifest anchors.
HOME_TOKEN = "${HOME}"
EXTERNAL_TOKEN = "<external>"

# Tokens used inside free-form text (local.conf lines, runtime args, ...) where
# a bare relative path would be ambiguous.
REGISTRY_TOKEN = "${registry}"
BUILD_TOKEN = "${build}"

# Absolute path enclosed in single or double quotes, e.g. DL_DIR = "/data/dl".
_QUOTED_ABS_PATH = re.compile(r"""(["'])(/[^"']*)\1""")
# Absolute path directly after an '=' sign, e.g. DL_DIR=/data/dl.
_ASSIGNED_ABS_PATH = re.compile(r"""(?<==)(/[^\s"';,]+)""")
# Host side of a container bind mount, e.g. -v /data/dl:/downloads[:ro].
_MOUNT_HOST_PATH = re.compile(r"""(?<![\w:$}>])(/[^\s:]+)(?=:)""")


def _normalize(path: Path) -> Path:
    """Return an absolute, symlink-resolved path (tolerating missing files)."""
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError):
        return Path(os.path.normpath(os.path.expanduser(str(path))))


def _relative_to(path: Path, anchor: Path) -> Optional[str]:
    """Return *path* relative to *anchor* as POSIX text, or ``None``.

    Never returns a path that escapes *anchor* with ``..`` segments.
    """
    try:
        relative = path.relative_to(anchor)
    except ValueError:
        return None
    return relative.as_posix()


class ManifestPathSanitizer:
    """Rewrite host paths so the build manifest stays host independent.

    Paths below the registry root or the build root become plain relative
    POSIX paths.  Paths below the user's home directory become
    ``${HOME}/<relative>``.  Anything else is reduced to
    ``<external>/<basename>`` so no host directory layout is disclosed.
    """

    def __init__(
        self,
        registry_root: Optional[Path] = None,
        build_root: Optional[Path] = None,
        home: Optional[Path] = None,
    ) -> None:
        self.registry_root = _normalize(registry_root) if registry_root else None
        self.build_root = _normalize(build_root) if build_root else None
        self.home = _normalize(home) if home else _normalize(Path.home())

    # -- anchors ---------------------------------------------------------
    def _anchors(self) -> List[Path]:
        """Return known anchors ordered from the most specific to the least."""
        anchors = [a for a in (self.registry_root, self.build_root) if a is not None]
        return sorted(anchors, key=lambda p: len(p.parts), reverse=True)

    def _text_anchors(self) -> List[Tuple[str, str]]:
        """Return ``(prefix, token)`` pairs for free-form text substitution."""
        pairs: List[Tuple[Path, str]] = []
        if self.registry_root is not None:
            pairs.append((self.registry_root, REGISTRY_TOKEN))
        if self.build_root is not None:
            pairs.append((self.build_root, BUILD_TOKEN))
        pairs.append((self.home, HOME_TOKEN))
        pairs.sort(key=lambda item: len(str(item[0])), reverse=True)
        return [(str(path), token) for path, token in pairs]

    # -- structured paths ------------------------------------------------
    def relativize(self, value: Any, anchors: Optional[Sequence[Path]] = None) -> Any:
        """Return *value* as a path relative to the best matching anchor.

        Non-string values, empty strings and paths that are already relative
        are returned unchanged (with ``\\`` normalised to ``/``).
        """
        if not isinstance(value, str) or not value.strip():
            return value

        raw = value.strip()
        expanded = Path(os.path.expanduser(raw))
        if not expanded.is_absolute():
            return raw.replace(os.sep, "/") if os.sep != "/" else raw

        resolved = _normalize(expanded)
        candidates = list(anchors) if anchors is not None else self._anchors()
        for anchor in candidates:
            relative = _relative_to(resolved, anchor)
            if relative is not None:
                return relative or "."

        home_relative = _relative_to(resolved, self.home)
        if home_relative is not None:
            return f"{HOME_TOKEN}/{home_relative}" if home_relative else HOME_TOKEN

        return f"{EXTERNAL_TOKEN}/{resolved.name}" if resolved.name else EXTERNAL_TOKEN

    def relativize_to_registry(self, value: Any) -> Any:
        """Relativize *value* against the registry root only.

        Used for values (such as the build directory itself) that would
        otherwise collapse to ``.`` against their own anchor.
        """
        anchors = [self.registry_root] if self.registry_root is not None else []
        return self.relativize(value, anchors=anchors)

    # -- free-form text --------------------------------------------------
    def scrub_text(self, value: Any) -> Any:
        """Remove absolute host paths embedded in free-form text.

        Whole-value paths are relativized.  Otherwise known anchor prefixes are
        replaced by their placeholder token and remaining absolute paths that
        appear in recognisable positions (quoted values, ``KEY=/path``
        assignments and bind-mount host paths) are relativized as well.
        """
        if not isinstance(value, str) or not value.strip():
            return value

        stripped = value.strip()
        if _looks_like_single_path(stripped):
            return self.relativize(stripped)

        text = value
        for prefix, token in self._text_anchors():
            text = text.replace(prefix + os.sep, token + "/")
            if os.sep != "/":
                text = text.replace(prefix + "/", token + "/")
            text = text.replace(prefix, token)

        def _quoted(match: "re.Match") -> str:
            quote, path = match.group(1), match.group(2)
            return f"{quote}{self.relativize(path)}{quote}"

        text = _QUOTED_ABS_PATH.sub(_quoted, text)
        text = _ASSIGNED_ABS_PATH.sub(lambda m: str(self.relativize(m.group(1))), text)
        text = _MOUNT_HOST_PATH.sub(lambda m: str(self.relativize(m.group(1))), text)
        return text

    def scrub_argv(self, argv: Sequence[str]) -> List[str]:
        """Sanitize a command line: bare program name plus scrubbed arguments."""
        if not argv:
            return []
        program = Path(str(argv[0])).name or str(argv[0])
        return [program] + [str(self.scrub_text(str(arg))) for arg in argv[1:]]


def _looks_like_single_path(value: str) -> bool:
    """Return ``True`` when *value* is a single absolute path token."""
    if not value.startswith(("/", "~")) or any(ch.isspace() for ch in value):
        return False
    return not any(ch in value for ch in ('"', "'", "=", ":"))
