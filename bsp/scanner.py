"""
CRA image scanner: runs Trivy, Syft+Grype, or EMBA against Yocto build artifacts.

This module supports the EU Cyber Resilience Act (CRA) requirements for:
- Software Bill of Materials (SBOM) generation
- CVE/vulnerability assessment of embedded Linux images

Three scanner backends are supported:
- **Trivy** (default): ``trivy fs``/``trivy rootfs`` for CVE scanning plus
  ``trivy sbom`` for SBOM generation.
- **Syft + Grype**: ``syft`` for SBOM generation and ``grype`` for CVE
  matching against the generated SBOM.
- **EMBA**: comprehensive firmware security analyser that performs binary-level
  CVE detection without requiring a package-manager database.  Produces a
  CycloneDX SBOM and CSV CVE reports.

Trivy and Syft/Grype are external CLI tools; neither is a Python dependency.
Install instructions for Trivy: https://trivy.dev/latest/getting-started/installation/
Install instructions for Syft/Grype: https://github.com/anchore/syft / https://github.com/anchore/grype

EMBA must be cloned from GitHub and set up separately; it is not a pip package.
Install instructions: https://github.com/e-m-b-a/emba/wiki/Installation
Docker image: embeddedanalyzer/emba
"""

import csv
import json
import logging
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .models import ScanConfig


# =============================================================================
# Result dataclasses
# =============================================================================


# Severity levels in increasing order (used for threshold comparisons).
_SEVERITY_ORDER = ["NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"]

# Artifact name suffixes that ``trivy rootfs`` cannot extract.
#
# WIC images (.wic and any compressed variant) are raw disk images containing a
# partition table; ``trivy rootfs`` has no partition-table parser and produces an
# empty SBOM without any error or warning.
#
# Zstd-compressed tarballs (.tar.zst, .rootfs.tar.zst) are not supported by the
# archive extractor shipped with Trivy ≤ 0.70; the file is treated as a plain
# filesystem path rather than an archive, again yielding empty results.
#
# Use ``**/*.rootfs.tar.gz`` or ``**/*.rootfs.tar.bz2`` as the scan target instead.
_TRIVY_UNSUPPORTED_SUFFIXES: frozenset = frozenset([
    ".wic",
    ".wic.gz",
    ".wic.bz2",
    ".wic.xz",
    ".wic.zst",
    ".rootfs.tar.zst",
    ".tar.zst",
])

# Package-manager database paths that Trivy recognises inside a rootfs.
# Keyed by a short human-readable manager name; values are the files/dirs
# Trivy uses.  The *required* entry is what Trivy actually reads; the
# *indicator* entry is something that may be present even when *required* is
# missing (used to produce a more specific diagnostic message).
_PKGDB_REQUIRED: Dict[str, str] = {
    "dpkg":  "var/lib/dpkg/status",
    "opkg":  "var/lib/opkg/status",
    "apk":   "lib/apk/db/installed",
    "rpm":   "var/lib/rpm/Packages",
}
# Paths whose presence signals a package manager is *configured* even if the
# main database file is absent (e.g. dpkg info dir without status file).
_PKGDB_INDICATORS: Dict[str, str] = {
    "dpkg": "var/lib/dpkg/info",
    "opkg": "var/lib/opkg/info",
}

# Trivy's --os-family value to auto-apply when the corresponding package
# manager database is detected but no OS markers (e.g. /etc/os-release) are
# present.  Without this, Trivy skips OS-package analyzers entirely and
# produces an empty SBOM even when the database file is readable.
# opkg is intentionally absent: Trivy has no opkg support regardless of
# --os-family, so switch to syft+grype for opkg images.
_PKGDB_TO_OS_FAMILY: Dict[str, str] = {
    "dpkg": "debian",
    "apk":  "alpine",
    "rpm":  "centos",
}


@dataclass
class _TarballPkgDbInfo:
    """Result of inspecting a tarball for package-manager database files."""
    # Manager names whose *required* database file is present and non-empty.
    present: List[str]
    # Manager names where an *indicator* dir/file is present but the required
    # database file is absent or empty (missing or zero-byte status file).
    indicator_only: List[str]
    # True when the tarball could not be opened for inspection.
    unreadable: bool = False


@dataclass
class ScanFinding:
    """A single CVE/vulnerability finding from a scanner."""
    cve_id: str
    severity: str
    package_name: str
    package_version: str
    description: str = ""
    fix_version: str = ""


@dataclass
class SbomResult:
    """Result of SBOM generation for a single artifact."""
    path: Path
    sbom_format: str
    component_count: int = 0


@dataclass
class ScanResult:
    """Result of a full image scan run."""
    findings: List[ScanFinding] = field(default_factory=list)
    sboms: List[SbomResult] = field(default_factory=list)
    scanned_artifacts: List[Path] = field(default_factory=list)
    report_files: List[Path] = field(default_factory=list)
    fail_on: str = "CRITICAL"
    dry_run: bool = False

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "CRITICAL")

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "HIGH")

    @property
    def medium_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "MEDIUM")

    @property
    def low_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "LOW")

    @property
    def total_count(self) -> int:
        return len(self.findings)

    @property
    def passed(self) -> bool:
        """Return True when no findings at or above ``fail_on`` severity exist."""
        if self.fail_on == "NONE":
            return True
        threshold_idx = _SEVERITY_ORDER.index(self.fail_on) if self.fail_on in _SEVERITY_ORDER else len(_SEVERITY_ORDER)
        for finding in self.findings:
            sev_idx = _SEVERITY_ORDER.index(finding.severity) if finding.severity in _SEVERITY_ORDER else 0
            if sev_idx >= threshold_idx:
                return False
        return True


# =============================================================================
# ImageScanner
# =============================================================================


class ImageScanner:
    """
    Scans Yocto build artifacts for CVEs and generates SBOMs.

    Provider-agnostic: the scanner backend (Trivy or Syft+Grype) is
    selected via ``scan_config.tool``.

    Args:
        scan_config: Scanning configuration (tool, severity, format, etc.)
        build_path: Top-level build output directory.  Used as the default
                    location to search for artifacts when no explicit
                    artifact paths are supplied to :meth:`scan`.
    """

    def __init__(self, scan_config: ScanConfig, build_path: str):
        self.config = scan_config
        self.build_path = Path(build_path)
        self.logger = logging.getLogger(self.__class__.__name__)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self, artifact_paths: Optional[List[Path]] = None) -> ScanResult:
        """
        Scan image artifacts for CVEs and generate SBOMs.

        If *artifact_paths* is ``None`` the method discovers artifacts
        automatically using :meth:`_find_artifacts`.

        Args:
            artifact_paths: Explicit list of artifact files to scan.
                            When ``None``, artifacts are auto-discovered
                            via the configured patterns and artifact dirs.

        Returns:
            :class:`ScanResult` with all findings and SBOM metadata.
        """
        result = ScanResult(fail_on=self.config.fail_on)

        artifacts = artifact_paths if artifact_paths is not None else self._find_artifacts()

        if not artifacts:
            self.logger.warning(
                "No artifacts found to scan under '%s'. "
                "Check artifact_patterns and artifact_dirs in the scan config.",
                self.build_path,
            )
            return result

        result.scanned_artifacts = list(artifacts)

        # Resolve output directory
        if self.config.output_dir:
            output_dir = Path(self.config.output_dir)
        else:
            output_dir = self.build_path / "reports"
        output_dir.mkdir(parents=True, exist_ok=True)

        tool = self.config.tool.lower()
        if tool == "trivy":
            self._check_tool_availability("trivy")
            for artifact in artifacts:
                findings, sbom, report_files = self._run_trivy(artifact, output_dir)
                result.findings.extend(findings)
                if sbom:
                    result.sboms.append(sbom)
                result.report_files.extend(report_files)
        elif tool in ("syft+grype", "syft_grype"):
            self._check_tool_availability("syft")
            self._check_tool_availability("grype")
            for artifact in artifacts:
                findings, sbom, report_files = self._run_syft_grype(artifact, output_dir)
                result.findings.extend(findings)
                if sbom:
                    result.sboms.append(sbom)
                result.report_files.extend(report_files)
        elif tool == "emba":
            for artifact in artifacts:
                findings, sbom, report_files = self._run_emba(artifact, output_dir)
                result.findings.extend(findings)
                if sbom:
                    result.sboms.append(sbom)
                result.report_files.extend(report_files)
        else:
            self.logger.error(
                "Unknown scan tool '%s'. Supported tools: trivy, syft+grype, emba.",
                self.config.tool,
            )
            sys.exit(1)

        return result

    # ------------------------------------------------------------------
    # Artifact discovery
    # ------------------------------------------------------------------

    def _find_artifacts(self) -> List[Path]:
        """Find image artifacts under ``build_path`` using configured patterns."""
        found: List[Path] = []
        seen: set = set()

        for artifact_dir in self.config.artifact_dirs:
            search_dir = self.build_path / artifact_dir
            if not search_dir.is_dir():
                self.logger.debug("Artifact dir not found, skipping: %s", search_dir)
                continue
            for pattern in self.config.artifact_patterns:
                for match in sorted(search_dir.glob(pattern)):
                    if match.is_file() and match not in seen:
                        found.append(match)
                        seen.add(match)

        self.logger.info("Found %d artifact(s) to scan in %s", len(found), self.build_path)
        return found

    # ------------------------------------------------------------------
    # Tool availability check
    # ------------------------------------------------------------------

    def _check_tool_availability(self, tool: str) -> None:
        """
        Verify that *tool* is available on ``$PATH``.

        Exits with a descriptive install hint when the tool is missing.
        """
        if shutil.which(tool) is None:
            install_hints = {
                "trivy": (
                    "https://trivy.dev/latest/getting-started/installation/\n"
                    "  Quick install: curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh -s -- -b /usr/local/bin"
                ),
                "syft": "https://github.com/anchore/syft#installation",
                "grype": "https://github.com/anchore/grype#installation",
            }
            hint = install_hints.get(tool, f"https://github.com/search?q={tool}")
            self.logger.error(
                "Required tool '%s' is not installed or not on PATH.\n"
                "Install it from: %s",
                tool,
                hint,
            )
            sys.exit(1)

    # ------------------------------------------------------------------
    # Trivy backend
    # ------------------------------------------------------------------

    @staticmethod
    def _trivy_unsupported_suffix(artifact_path: Path) -> Optional[str]:
        """Return the matched unsupported suffix string, or ``None`` if the format is fine."""
        name = artifact_path.name
        for suffix in _TRIVY_UNSUPPORTED_SUFFIXES:
            if name.endswith(suffix):
                return suffix
        return None

    @staticmethod
    def _inspect_tarball_pkgdb(artifact_path: Path) -> "_TarballPkgDbInfo":
        """
        Peek inside *artifact_path* (a tar.gz or tar.bz2) to detect which
        package-manager database files are present.

        This is a lightweight index-only walk (no full extraction) using
        ``tarfile.getmembers()``.  It is called before invoking Trivy so
        that precise, actionable warnings can be emitted when Trivy would
        produce an empty SBOM.

        Returns a :class:`_TarballPkgDbInfo` describing what was found.
        The paths inside the archive are normalised by stripping a single
        leading ``./`` so they match the entries in :data:`_PKGDB_REQUIRED`.
        """
        try:
            with tarfile.open(artifact_path, "r:*") as tf:
                members = {
                    m.name.lstrip("./")
                    for m in tf.getmembers()
                }
                # Separate index of members with their sizes (to detect empty files)
                sizes: Dict[str, int] = {
                    m.name.lstrip("./"): m.size
                    for m in tf.getmembers()
                }
        except (tarfile.TarError, OSError):
            return _TarballPkgDbInfo(present=[], indicator_only=[], unreadable=True)

        present: List[str] = []
        indicator_only: List[str] = []

        for mgr, required in _PKGDB_REQUIRED.items():
            if required in members and sizes.get(required, 0) > 0:
                present.append(mgr)
            else:
                # Check if an indicator path signals the manager is installed
                # but the required file is missing or zero-byte.
                indicator = _PKGDB_INDICATORS.get(mgr)
                if indicator and any(
                    name == indicator or name.startswith(indicator + "/")
                    for name in members
                ):
                    indicator_only.append(mgr)

        return _TarballPkgDbInfo(present=present, indicator_only=indicator_only)

    def _warn_pkgdb(self, artifact_path: Path, pkgdb: "_TarballPkgDbInfo") -> None:
        """
        Emit targeted log warnings based on *pkgdb* inspection results.

        Called before running Trivy so the user sees the reason for an
        empty SBOM immediately, without having to wait for Trivy to finish.
        """
        if pkgdb.unreadable:
            self.logger.warning(
                "Could not inspect '%s' to check for package-manager databases. "
                "Trivy will attempt to scan it anyway.",
                artifact_path.name,
            )
            return

        for mgr in pkgdb.indicator_only:
            required = _PKGDB_REQUIRED[mgr]
            indicator = _PKGDB_INDICATORS.get(mgr, "")
            self.logger.warning(
                "The rootfs '%s' contains the %s package directory (%s) but "
                "the package database (%s) is absent or empty. "
                "Trivy reads only %s to enumerate packages; without it the "
                "SBOM will be empty. "
                "To fix: ensure the %s database file is populated during the "
                "Yocto build. For dpkg-based images, verify that the bitbake "
                "run_do_rootfs step merges per-package status files into "
                "%s (this is done automatically but can be skipped on "
                "incremental builds — run a clean build to confirm).",
                artifact_path.name,
                mgr,
                indicator,
                required,
                required,
                mgr,
                required,
            )

        if not pkgdb.present and not pkgdb.indicator_only:
            self.logger.warning(
                "The rootfs '%s' contains no recognisable package-manager "
                "database (checked: %s). "
                "Trivy will produce an empty SBOM. "
                "Consider switching to 'tool: syft+grype', which has broader "
                "Yocto package support.",
                artifact_path.name,
                ", ".join(_PKGDB_REQUIRED.keys()),
            )

    def _resolve_trivy_os_family(
        self,
        pkgdb: "_TarballPkgDbInfo",
    ) -> Optional[str]:
        """
        Determine the ``--os-family`` value to pass to Trivy.

        Trivy requires OS detection (typically via ``/etc/os-release``) to
        activate OS-package analyzers (dpkg, apk, rpm).  Yocto images usually
        lack standard Debian/Alpine/RPM OS markers, so Trivy never runs the
        analyzer and the SBOM is empty even when the package database is
        present and fully populated.

        Resolution order:
        1. Explicit ``scan.trivy_os_family`` in the registry config.
        2. Auto-inferred from the package database detected in the tarball.
        3. ``None`` (no flag added — Trivy relies on its own OS detection).

        Returns the resolved os-family string, or ``None`` if nothing could
        be determined.
        """
        if self.config.trivy_os_family:
            return self.config.trivy_os_family

        for mgr in pkgdb.present:
            inferred = _PKGDB_TO_OS_FAMILY.get(mgr)
            if inferred:
                self.logger.info(
                    "Auto-inferred '--os-family %s' from detected %s package database. "
                    "Set 'trivy_os_family' in the scan config to override.",
                    inferred,
                    mgr,
                )
                return inferred

        return None

    def _run_trivy(
        self,
        artifact_path: Path,
        output_dir: Path,
    ) -> tuple:
        """
        Run Trivy against *artifact_path*.

        Produces a JSON vulnerability report and an SBOM file in *output_dir*.

        Returns:
            Tuple of ``(findings, sbom_result_or_None, report_files)``.
        """
        bad_suffix = self._trivy_unsupported_suffix(artifact_path)
        if bad_suffix is not None:
            self.logger.warning(
                "Skipping '%s': 'trivy rootfs' cannot scan '*%s' artifacts "
                "(WIC disk images require partition extraction; zstd-compressed "
                "tarballs are not supported by the Trivy archive extractor). "
                "Use a '**/*.rootfs.tar.gz' or '**/*.rootfs.tar.bz2' artifact instead.",
                artifact_path.name,
                bad_suffix,
            )
            return [], None, []

        # Inspect the tarball for package-manager databases before invoking
        # Trivy.  This allows us to:
        # 1. Emit a precise, actionable warning when a required database file
        #    is absent (e.g. dpkg info/ present but status file missing).
        # 2. Auto-infer --os-family so Trivy activates OS-package analyzers
        #    even when the image lacks standard OS-detection markers.
        pkgdb = _TarballPkgDbInfo(present=[], indicator_only=[])
        # Check for compound tarball extensions (.tar.gz, .rootfs.tar.gz, etc.)
        # by looking for a .tar component anywhere before the compression suffix.
        _name = artifact_path.name
        _is_tarball = (
            _name.endswith(".tar.gz")
            or _name.endswith(".tar.bz2")
            or _name.endswith(".tar.xz")
        )
        if _is_tarball:
            pkgdb = self._inspect_tarball_pkgdb(artifact_path)
            self._warn_pkgdb(artifact_path, pkgdb)

        # Resolve OS family — may be explicit (config) or auto-inferred.
        os_family = self._resolve_trivy_os_family(pkgdb)
        os_version = self.config.trivy_os_version or None

        stem = artifact_path.stem.replace(".", "_")
        report_path = output_dir / f"trivy-{stem}.json"
        sbom_path = output_dir / f"sbom-{stem}.{self._sbom_extension()}"

        # Map internal sbom_format to Trivy's format flag
        trivy_sbom_format_map = {
            "cyclonedx": "cyclonedx",
            "spdx-json": "spdx-json",
            "spdx-tag-value": "spdx",
        }
        trivy_sbom_format = trivy_sbom_format_map.get(
            self.config.sbom_format.lower(), "cyclonedx"
        )

        findings: List[ScanFinding] = []
        report_files: List[Path] = []
        sbom_result: Optional[SbomResult] = None

        def _os_flags() -> List[str]:
            """Return ``--os-family`` / ``--os-version`` flags when set."""
            flags: List[str] = []
            if os_family:
                flags += ["--os-family", os_family]
            if os_version:
                flags += ["--os-version", os_version]
            return flags

        # -- CVE scan -------------------------------------------------------
        scan_cmd = [
            "trivy",
            "rootfs",
            "--format", "json",
            "--severity", self._severity_filter_str(),
            "--output", str(report_path),
            "--quiet",
            *_os_flags(),
            str(artifact_path),
        ]
        self.logger.info("Running Trivy CVE scan: %s", " ".join(scan_cmd))
        try:
            proc = subprocess.run(
                scan_cmd,
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode not in (0, 1):
                # Trivy exits 1 when vulnerabilities are found; >1 is an error.
                self.logger.warning(
                    "Trivy exited with code %d for %s.\nstderr: %s",
                    proc.returncode,
                    artifact_path,
                    proc.stderr,
                )
            if report_path.exists():
                findings = self._parse_trivy_json(report_path)
                report_files.append(report_path)
        except (OSError, subprocess.SubprocessError) as exc:
            self.logger.error("Failed to run Trivy: %s", exc)

        # -- SBOM generation ------------------------------------------------
        sbom_cmd = [
            "trivy",
            "rootfs",
            "--format", trivy_sbom_format,
            "--output", str(sbom_path),
            # Without --list-all-pkgs Trivy omits packages that have no known
            # CVEs, producing an incomplete SBOM that does not meet CRA
            # requirements for a full Software Bill of Materials.
            "--list-all-pkgs",
            "--quiet",
            *_os_flags(),
            str(artifact_path),
        ]
        self.logger.info("Running Trivy SBOM generation: %s", " ".join(sbom_cmd))
        try:
            proc = subprocess.run(
                sbom_cmd,
                capture_output=True,
                text=True,
                check=False,
            )
            if sbom_path.exists():
                component_count = self._count_trivy_sbom_components(sbom_path, trivy_sbom_format)
                if component_count == 0:
                    self.logger.warning(
                        "Trivy found 0 packages in '%s'. "
                        "If a pre-scan database check did not emit a more specific "
                        "warning above, the rootfs may use a package manager not "
                        "supported by Trivy (e.g. opkg). "
                        "Consider switching to 'tool: syft+grype' for broader "
                        "Yocto package-manager support.",
                        artifact_path.name,
                    )
                sbom_result = SbomResult(
                    path=sbom_path,
                    sbom_format=self.config.sbom_format,
                    component_count=component_count,
                )
                report_files.append(sbom_path)
        except (OSError, subprocess.SubprocessError) as exc:
            self.logger.error("Failed to run Trivy SBOM generation: %s", exc)

        return findings, sbom_result, report_files

    def _parse_trivy_json(self, report_path: Path) -> List[ScanFinding]:
        """Parse a Trivy JSON vulnerability report into ``ScanFinding`` objects."""
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.logger.warning("Could not parse Trivy report %s: %s", report_path, exc)
            return []

        findings: List[ScanFinding] = []
        for result in data.get("Results", []):
            for vuln in result.get("Vulnerabilities") or []:
                findings.append(ScanFinding(
                    cve_id=vuln.get("VulnerabilityID", ""),
                    severity=vuln.get("Severity", "UNKNOWN"),
                    package_name=vuln.get("PkgName", ""),
                    package_version=vuln.get("InstalledVersion", ""),
                    description=vuln.get("Description", ""),
                    fix_version=vuln.get("FixedVersion", ""),
                ))
        return findings

    def _count_trivy_sbom_components(self, sbom_path: Path, fmt: str) -> int:
        """Return the number of SBOM components in *sbom_path* (best-effort)."""
        try:
            text = sbom_path.read_text(encoding="utf-8")
            if fmt in ("cyclonedx", "spdx-json"):
                data = json.loads(text)
                # CycloneDX uses "components", SPDX-JSON uses "packages"
                return len(data.get("components", data.get("packages", [])))
            # spdx tag-value: count PackageName occurrences
            return text.count("PackageName:")
        except (OSError, json.JSONDecodeError):
            return 0

    # ------------------------------------------------------------------
    # Syft + Grype backend
    # ------------------------------------------------------------------

    def _run_syft_grype(
        self,
        artifact_path: Path,
        output_dir: Path,
    ) -> tuple:
        """
        Run Syft (SBOM generation) + Grype (CVE matching) against *artifact_path*.

        Returns:
            Tuple of ``(findings, sbom_result_or_None, report_files)``.
        """
        stem = artifact_path.stem.replace(".", "_")
        sbom_path = output_dir / f"sbom-{stem}.{self._sbom_extension()}"
        grype_report_path = output_dir / f"grype-{stem}.json"

        findings: List[ScanFinding] = []
        report_files: List[Path] = []
        sbom_result: Optional[SbomResult] = None

        # Map internal sbom_format to Syft's output format flag
        syft_format_map = {
            "cyclonedx": "cyclonedx-json",
            "spdx-json": "spdx-json",
            "spdx-tag-value": "spdx-tag-value",
        }
        syft_format = syft_format_map.get(self.config.sbom_format.lower(), "cyclonedx-json")

        # -- SBOM generation via Syft ----------------------------------------
        syft_cmd = [
            "syft",
            str(artifact_path),
            "--output", f"{syft_format}={sbom_path}",
            "--quiet",
        ]
        self.logger.info("Running Syft SBOM generation: %s", " ".join(syft_cmd))
        try:
            proc = subprocess.run(
                syft_cmd,
                capture_output=True,
                text=True,
                check=False,
            )
            if sbom_path.exists():
                component_count = self._count_syft_sbom_components(sbom_path, syft_format)
                if component_count == 0:
                    self.logger.warning(
                        "Syft found 0 packages in '%s'. "
                        "This usually means the rootfs does not contain a recognisable "
                        "package-manager database (e.g. the opkg status file was stripped "
                        "from the Yocto image). "
                        "To fix: add 'IMAGE_FEATURES += \"package-management\"' to your "
                        "Yocto image recipe to retain the package database in the final image.",
                        artifact_path.name,
                    )
                sbom_result = SbomResult(
                    path=sbom_path,
                    sbom_format=self.config.sbom_format,
                    component_count=component_count,
                )
                report_files.append(sbom_path)
            else:
                self.logger.warning(
                    "Syft did not produce an SBOM for %s. stderr: %s",
                    artifact_path,
                    proc.stderr,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            self.logger.error("Failed to run Syft: %s", exc)

        # -- CVE scan via Grype (from the SBOM) --------------------------------
        if sbom_path.exists():
            grype_cmd = [
                "grype",
                f"sbom:{sbom_path}",
                "--output", "json",
                "--file", str(grype_report_path),
                "--fail-on", self.config.fail_on.lower(),
                "--only-fixed",
            ]
            # Severity filter: grype doesn't have a --severity filter, so we
            # filter in-process after parsing. We keep --fail-on in the command
            # to let grype set its own exit code as a cross-check.
            self.logger.info("Running Grype CVE scan: %s", " ".join(grype_cmd))
            try:
                proc = subprocess.run(
                    grype_cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if grype_report_path.exists():
                    findings = self._parse_grype_json(grype_report_path)
                    report_files.append(grype_report_path)
            except (OSError, subprocess.SubprocessError) as exc:
                self.logger.error("Failed to run Grype: %s", exc)

        return findings, sbom_result, report_files

    def _parse_grype_json(self, report_path: Path) -> List[ScanFinding]:
        """Parse a Grype JSON report into ``ScanFinding`` objects."""
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.logger.warning("Could not parse Grype report %s: %s", report_path, exc)
            return []

        min_severity_idx = (
            _SEVERITY_ORDER.index(self.config.severity.upper())
            if self.config.severity.upper() in _SEVERITY_ORDER
            else 0
        )

        findings: List[ScanFinding] = []
        for match in data.get("matches", []):
            vuln = match.get("vulnerability", {})
            severity = vuln.get("severity", "UNKNOWN").upper()
            sev_idx = _SEVERITY_ORDER.index(severity) if severity in _SEVERITY_ORDER else 0
            if sev_idx < min_severity_idx:
                continue
            artifact = match.get("artifact", {})
            fix_info = vuln.get("fix") or {}
            fix_versions = fix_info.get("versions") or []
            fix_version = fix_versions[0] if fix_versions else ""
            findings.append(ScanFinding(
                cve_id=vuln.get("id", ""),
                severity=severity,
                package_name=artifact.get("name", ""),
                package_version=artifact.get("version", ""),
                description=vuln.get("description", ""),
                fix_version=fix_version,
            ))
        return findings

    def _count_syft_sbom_components(self, sbom_path: Path, fmt: str) -> int:
        """Return component count from a Syft SBOM (best-effort)."""
        try:
            text = sbom_path.read_text(encoding="utf-8")
            if fmt in ("cyclonedx-json", "spdx-json"):
                data = json.loads(text)
                return len(data.get("components", data.get("packages", [])))
            # spdx-tag-value
            return text.count("PackageName:")
        except (OSError, json.JSONDecodeError):
            return 0

    # ------------------------------------------------------------------
    # EMBA backend
    # ------------------------------------------------------------------

    def _run_emba(
        self,
        artifact_path: Path,
        output_dir: Path,
    ) -> tuple:
        """
        Run EMBA against *artifact_path*.

        EMBA is a comprehensive Bash-based firmware security analyser
        (https://github.com/e-m-b-a/emba) that performs binary-level CVE
        detection without requiring a package-manager database in the image.

        Unlike Trivy and Grype, EMBA:
        - Is **not** available as a ``$PATH`` tool; its install path must be
          configured via ``scan.emba_path``.
        - Produces output in a **log directory** (``-l``), not a single
          report file.  The CycloneDX SBOM is at
          ``<log_dir>/sbom/EMBA_cyclonedx_sbom.json`` and CVE findings are
          in ``<log_dir>/csv_logs/f20_vul_aggregator.csv``.
        - Takes significantly longer than Trivy (15–60+ minutes for a full
          scan).

        Returns:
            Tuple of ``(findings, sbom_result_or_None, report_files)``.
        """
        emba_path = self.config.emba_path
        if not emba_path:
            self.logger.error(
                "EMBA backend requires 'emba_path' to be set in the scan config. "
                "Example: scan:\n"
                "  tool: emba\n"
                "  emba_path: /opt/emba"
            )
            sys.exit(1)

        emba_script = Path(emba_path) / "emba"
        if not emba_script.is_file():
            self.logger.error(
                "EMBA script not found at '%s'. "
                "Clone EMBA from https://github.com/e-m-b-a/emba and set "
                "'emba_path' to the directory containing the 'emba' script.",
                emba_script,
            )
            sys.exit(1)

        # EMBA writes all output to its own log directory; use a
        # per-artifact subdirectory under output_dir.
        stem = artifact_path.stem.replace(".", "_")
        emba_log_dir = output_dir / f"emba-{stem}"
        emba_log_dir.mkdir(parents=True, exist_ok=True)

        cmd: List[str] = []
        if self.config.emba_use_sudo:
            cmd.append("sudo")
        cmd += [
            str(emba_script),
            "-f", str(artifact_path),
            "-l", str(emba_log_dir),
            # Suppress interactive output
            "-z",
            # Generate grepable log (used internally for diagnostics)
            "-g",
        ]
        if self.config.emba_no_docker:
            cmd.append("-D")
        if self.config.emba_profile:
            cmd += ["-p", str(self.config.emba_profile)]
        if self.config.emba_extra_args:
            cmd += self.config.emba_extra_args.split()

        self.logger.info("Running EMBA firmware scan: %s", " ".join(cmd))
        timeout_seconds = self.config.emba_timeout_minutes * 60
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
            if proc.returncode not in (0, 1):
                self.logger.warning(
                    "EMBA exited with code %d for %s.\nstderr: %s",
                    proc.returncode,
                    artifact_path,
                    proc.stderr,
                )
        except subprocess.TimeoutExpired:
            self.logger.error(
                "EMBA scan timed out after %d minutes for '%s'. "
                "Increase 'emba_timeout_minutes' in the scan config or use "
                "a lighter scan profile.",
                self.config.emba_timeout_minutes,
                artifact_path.name,
            )
            return [], None, []
        except (OSError, subprocess.SubprocessError) as exc:
            self.logger.error("Failed to run EMBA: %s", exc)
            return [], None, []

        findings: List[ScanFinding] = []
        report_files: List[Path] = []
        sbom_result: Optional[SbomResult] = None

        # -- Parse CVE findings from EMBA's vulnerability aggregator CSV ------
        # EMBA writes f20_vul_aggregator.csv (primary) or falls back to
        # f17_cve_bin_tool.csv for binary-level findings.
        cve_csv_candidates = [
            emba_log_dir / "csv_logs" / "f20_vul_aggregator.csv",
            emba_log_dir / "csv_logs" / "f17_cve_bin_tool.csv",
        ]
        for cve_csv in cve_csv_candidates:
            if cve_csv.exists() and cve_csv.stat().st_size > 0:
                findings = self._parse_emba_csv(cve_csv)
                report_files.append(cve_csv)
                self.logger.info(
                    "Parsed %d CVE findings from '%s'.",
                    len(findings),
                    cve_csv.name,
                )
                break
        else:
            self.logger.warning(
                "EMBA produced no CVE CSV report under '%s'. "
                "The scan may have completed without findings, or the chosen "
                "profile does not include the vulnerability aggregator module.",
                emba_log_dir / "csv_logs",
            )

        # -- Parse SBOM from EMBA's CycloneDX output --------------------------
        sbom_path = emba_log_dir / "sbom" / "EMBA_cyclonedx_sbom.json"
        if sbom_path.exists() and sbom_path.stat().st_size > 0:
            component_count = self._count_emba_sbom_components(sbom_path)
            # Copy the SBOM into the output_dir alongside other reports.
            dest_sbom = output_dir / f"sbom-emba-{stem}.cdx.json"
            try:
                dest_sbom.write_bytes(sbom_path.read_bytes())
                sbom_result = SbomResult(
                    path=dest_sbom,
                    sbom_format="cyclonedx",
                    component_count=component_count,
                )
                report_files.append(dest_sbom)
            except OSError as exc:
                self.logger.warning("Could not copy EMBA SBOM to output dir: %s", exc)
        else:
            self.logger.warning(
                "EMBA did not produce a CycloneDX SBOM at '%s'. "
                "Ensure the scan profile includes the F15 SBOM module.",
                sbom_path,
            )

        return findings, sbom_result, report_files

    def _parse_emba_csv(self, csv_path: Path) -> List[ScanFinding]:
        """
        Parse an EMBA vulnerability CSV report into :class:`ScanFinding` objects.

        EMBA writes two relevant CSV files:

        * ``f20_vul_aggregator.csv`` — consolidated aggregator.  The columns
          used here are (in order): ``PACKAGE``, ``VERSION``, ``CVE_ID``,
          ``CVSS_SCORE``, ``SEVERITY``, ``EXPLOIT``.  All fields after the
          first six are ignored.
        * ``f17_cve_bin_tool.csv`` — binary-level findings from cve-bin-tool.
          Same column layout.

        Severity values in EMBA CSVs may be textual (``"HIGH"``, ``"MEDIUM"``,
        etc.) or numeric CVSS scores.  Numeric scores are converted using the
        standard CVSS v3 ranges:

        * 0.0 → ``"NONE"``
        * 0.1–3.9 → ``"LOW"``
        * 4.0–6.9 → ``"MEDIUM"``
        * 7.0–8.9 → ``"HIGH"``
        * 9.0–10.0 → ``"CRITICAL"``

        Findings below the configured minimum severity are filtered out.
        """
        min_severity_idx = (
            _SEVERITY_ORDER.index(self.config.severity.upper())
            if self.config.severity.upper() in _SEVERITY_ORDER
            else 0
        )

        findings: List[ScanFinding] = []
        try:
            text = csv_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self.logger.warning("Could not read EMBA CSV report %s: %s", csv_path, exc)
            return []

        try:
            reader = csv.reader(text.splitlines(), delimiter=";")
            for row in reader:
                if not row:
                    continue
                # Skip header rows: first cell is "PACKAGE" / "package" / etc.
                if row[0].strip().upper() in ("PACKAGE", "BINARY", "#"):
                    continue
                if len(row) < 3:
                    continue

                package_name = row[0].strip()
                package_version = row[1].strip() if len(row) > 1 else ""
                cve_id = row[2].strip() if len(row) > 2 else ""

                # Column 4 (index 3) is CVSS score; column 5 (index 4) is
                # textual severity.  Either or both may be absent.
                cvss_raw = row[3].strip() if len(row) > 3 else ""
                sev_raw = row[4].strip().upper() if len(row) > 4 else ""

                severity = self._emba_resolve_severity(sev_raw, cvss_raw)

                sev_idx = (
                    _SEVERITY_ORDER.index(severity)
                    if severity in _SEVERITY_ORDER
                    else 0
                )
                if sev_idx < min_severity_idx:
                    continue

                if not cve_id:
                    continue

                findings.append(ScanFinding(
                    cve_id=cve_id,
                    severity=severity,
                    package_name=package_name,
                    package_version=package_version,
                ))
        except csv.Error as exc:
            self.logger.warning("CSV parse error in %s: %s", csv_path, exc)

        return findings

    @staticmethod
    def _emba_resolve_severity(sev_text: str, cvss_raw: str) -> str:
        """
        Resolve a textual or numeric CVSS severity to a canonical level.

        Prefers the textual severity when it is a recognised level.  Falls
        back to deriving the level from the numeric CVSS score.
        """
        canonical = {"NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
        if sev_text in canonical:
            return sev_text

        # Try to parse a numeric CVSS score.
        try:
            score = float(cvss_raw)
        except (ValueError, TypeError):
            return "UNKNOWN"

        if score >= 9.0:
            return "CRITICAL"
        if score >= 7.0:
            return "HIGH"
        if score >= 4.0:
            return "MEDIUM"
        if score > 0.0:
            return "LOW"
        return "NONE"

    def _count_emba_sbom_components(self, sbom_path: Path) -> int:
        """Return the number of components in an EMBA CycloneDX SBOM (best-effort)."""
        try:
            data = json.loads(sbom_path.read_text(encoding="utf-8"))
            return len(data.get("components", []))
        except (OSError, json.JSONDecodeError):
            return 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _severity_filter_str(self) -> str:
        """Build the Trivy ``--severity`` value from the configured minimum."""
        min_idx = (
            _SEVERITY_ORDER.index(self.config.severity.upper())
            if self.config.severity.upper() in _SEVERITY_ORDER
            else 0
        )
        levels = [s for i, s in enumerate(_SEVERITY_ORDER) if i >= min_idx and s != "NONE"]
        return ",".join(levels)

    def _sbom_extension(self) -> str:
        """Return the file extension for the configured SBOM format."""
        ext_map = {
            "cyclonedx": "cdx.json",
            "spdx-json": "spdx.json",
            "spdx-tag-value": "spdx",
        }
        return ext_map.get(self.config.sbom_format.lower(), "json")
