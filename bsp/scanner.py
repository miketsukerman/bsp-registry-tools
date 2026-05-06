"""
CRA image scanner: runs Trivy or Syft+Grype against Yocto build artifacts.

This module supports the EU Cyber Resilience Act (CRA) requirements for:
- Software Bill of Materials (SBOM) generation
- CVE/vulnerability assessment of embedded Linux images

Two scanner backends are supported:
- **Trivy** (default): ``trivy fs``/``trivy rootfs`` for CVE scanning plus
  ``trivy sbom`` for SBOM generation.
- **Syft + Grype**: ``syft`` for SBOM generation and ``grype`` for CVE
  matching against the generated SBOM.

Both backends are external CLI tools; neither is a Python dependency.
Install instructions for Trivy: https://trivy.dev/latest/getting-started/installation/
Install instructions for Syft/Grype: https://github.com/anchore/syft / https://github.com/anchore/grype
"""

import json
import logging
import shutil
import subprocess
import sys
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
        else:
            self.logger.error(
                "Unknown scan tool '%s'. Supported tools: trivy, syft+grype.",
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

        # -- CVE scan -------------------------------------------------------
        scan_cmd = [
            "trivy",
            "rootfs",
            "--format", "json",
            "--severity", self._severity_filter_str(),
            "--output", str(report_path),
            "--quiet",
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
                        "This usually means the rootfs does not contain a recognisable "
                        "package-manager database (e.g. the opkg status file was stripped "
                        "from the Yocto image). "
                        "To fix: add 'IMAGE_FEATURES += \"package-management\"' to your "
                        "Yocto image recipe, or switch to 'tool: syft+grype' which can "
                        "read the Yocto package manifest without requiring a live database.",
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
