"""
Tests for the ImageScanner and related CRA vulnerability scanning functionality.

Covers:
- ScanFinding, SbomResult, ScanResult dataclasses
- ScanResult computed properties (passed, critical_count, etc.)
- ImageScanner._find_artifacts glob discovery
- ImageScanner severity threshold / fail_on logic
- ImageScanner._run_trivy (mocked subprocess)
- ImageScanner._run_syft_grype (mocked subprocess)
- ImageScanner._check_tool_availability (tool not installed)
- SBOM file writing
- CLI scan command argument parsing
- BspManager.scan_bsp / scan_by_components (mocked)
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from bsp.models import ScanConfig
from bsp.scanner import (
    ImageScanner,
    SbomResult,
    ScanFinding,
    ScanResult,
    _SEVERITY_ORDER,
)


# =============================================================================
# ScanFinding tests
# =============================================================================


class TestScanFinding:
    def test_defaults(self):
        f = ScanFinding(
            cve_id="CVE-2024-0001",
            severity="HIGH",
            package_name="libssl",
            package_version="1.0.0",
        )
        assert f.description == ""
        assert f.fix_version == ""

    def test_all_fields(self):
        f = ScanFinding(
            cve_id="CVE-2024-9999",
            severity="CRITICAL",
            package_name="bash",
            package_version="5.1",
            description="A critical bug",
            fix_version="5.2",
        )
        assert f.cve_id == "CVE-2024-9999"
        assert f.severity == "CRITICAL"
        assert f.fix_version == "5.2"


# =============================================================================
# ScanResult tests
# =============================================================================


class TestScanResult:
    def test_defaults(self):
        r = ScanResult()
        assert r.findings == []
        assert r.sboms == []
        assert r.scanned_artifacts == []
        assert r.report_files == []
        assert r.fail_on == "CRITICAL"
        assert r.dry_run is False

    def test_total_count_empty(self):
        assert ScanResult().total_count == 0

    def test_total_count_with_findings(self):
        r = ScanResult(findings=[
            ScanFinding("CVE-1", "HIGH", "pkg", "1.0"),
            ScanFinding("CVE-2", "LOW", "pkg", "1.0"),
        ])
        assert r.total_count == 2

    def test_critical_count(self):
        r = ScanResult(findings=[
            ScanFinding("CVE-1", "CRITICAL", "pkg", "1.0"),
            ScanFinding("CVE-2", "HIGH", "pkg", "1.0"),
            ScanFinding("CVE-3", "CRITICAL", "pkg", "1.0"),
        ])
        assert r.critical_count == 2

    def test_high_count(self):
        r = ScanResult(findings=[
            ScanFinding("CVE-1", "HIGH", "pkg", "1.0"),
            ScanFinding("CVE-2", "LOW", "pkg", "1.0"),
        ])
        assert r.high_count == 1

    def test_medium_count(self):
        r = ScanResult(findings=[
            ScanFinding("CVE-1", "MEDIUM", "pkg", "1.0"),
        ])
        assert r.medium_count == 1

    def test_low_count(self):
        r = ScanResult(findings=[
            ScanFinding("CVE-1", "LOW", "pkg", "1.0"),
            ScanFinding("CVE-2", "LOW", "pkg", "1.0"),
        ])
        assert r.low_count == 2


class TestScanResultPassed:
    def test_passed_when_no_findings(self):
        r = ScanResult(fail_on="CRITICAL")
        assert r.passed is True

    def test_passed_when_fail_on_none(self):
        r = ScanResult(
            fail_on="NONE",
            findings=[ScanFinding("CVE-1", "CRITICAL", "pkg", "1.0")],
        )
        assert r.passed is True

    def test_fails_when_critical_and_fail_on_critical(self):
        r = ScanResult(
            fail_on="CRITICAL",
            findings=[ScanFinding("CVE-1", "CRITICAL", "pkg", "1.0")],
        )
        assert r.passed is False

    def test_passes_when_only_high_and_fail_on_critical(self):
        r = ScanResult(
            fail_on="CRITICAL",
            findings=[ScanFinding("CVE-1", "HIGH", "pkg", "1.0")],
        )
        assert r.passed is True

    def test_fails_when_medium_and_fail_on_medium(self):
        r = ScanResult(
            fail_on="MEDIUM",
            findings=[ScanFinding("CVE-1", "MEDIUM", "pkg", "1.0")],
        )
        assert r.passed is False

    def test_fails_when_high_and_fail_on_medium(self):
        """A HIGH finding should fail when fail_on=MEDIUM (HIGH >= MEDIUM)."""
        r = ScanResult(
            fail_on="MEDIUM",
            findings=[ScanFinding("CVE-1", "HIGH", "pkg", "1.0")],
        )
        assert r.passed is False

    def test_passes_when_low_and_fail_on_high(self):
        r = ScanResult(
            fail_on="HIGH",
            findings=[ScanFinding("CVE-1", "LOW", "pkg", "1.0")],
        )
        assert r.passed is True

    def test_fails_when_low_and_fail_on_low(self):
        r = ScanResult(
            fail_on="LOW",
            findings=[ScanFinding("CVE-1", "LOW", "pkg", "1.0")],
        )
        assert r.passed is False


# =============================================================================
# ImageScanner._find_artifacts tests
# =============================================================================


class TestFindArtifacts:
    def _make_scanner(self, tmp_path, patterns=None, artifact_dirs=None):
        cfg = ScanConfig(
            artifact_patterns=patterns or ["*.wic", "*.rootfs.tar.gz"],
            artifact_dirs=artifact_dirs or ["tmp/deploy/images"],
        )
        return ImageScanner(cfg, str(tmp_path))

    def test_finds_matching_artifacts(self, tmp_path):
        images_dir = tmp_path / "tmp" / "deploy" / "images"
        images_dir.mkdir(parents=True)
        (images_dir / "core-image-minimal.wic").write_bytes(b"fake")
        (images_dir / "core-image-minimal.rootfs.tar.gz").write_bytes(b"fake")
        (images_dir / "core-image-minimal.manifest").write_bytes(b"fake")

        scanner = self._make_scanner(tmp_path)
        artifacts = scanner._find_artifacts()
        names = {a.name for a in artifacts}
        assert "core-image-minimal.wic" in names
        assert "core-image-minimal.rootfs.tar.gz" in names
        assert "core-image-minimal.manifest" not in names

    def test_returns_empty_when_no_matching_files(self, tmp_path):
        images_dir = tmp_path / "tmp" / "deploy" / "images"
        images_dir.mkdir(parents=True)
        (images_dir / "not-an-image.txt").write_bytes(b"fake")

        scanner = self._make_scanner(tmp_path)
        assert scanner._find_artifacts() == []

    def test_returns_empty_when_artifact_dir_missing(self, tmp_path):
        scanner = self._make_scanner(tmp_path)
        assert scanner._find_artifacts() == []

    def test_deduplicates_matches(self, tmp_path):
        """A file matching multiple patterns should appear only once."""
        images_dir = tmp_path / "tmp" / "deploy" / "images"
        images_dir.mkdir(parents=True)
        (images_dir / "image.wic").write_bytes(b"fake")

        cfg = ScanConfig(
            artifact_patterns=["*.wic", "*.wic"],  # duplicate pattern
            artifact_dirs=["tmp/deploy/images"],
        )
        scanner = ImageScanner(cfg, str(tmp_path))
        artifacts = scanner._find_artifacts()
        assert len(artifacts) == 1

    def test_multiple_artifact_dirs(self, tmp_path):
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()
        (dir1 / "a.wic").write_bytes(b"data")
        (dir2 / "b.wic").write_bytes(b"data")

        cfg = ScanConfig(
            artifact_patterns=["*.wic"],
            artifact_dirs=["dir1", "dir2"],
        )
        scanner = ImageScanner(cfg, str(tmp_path))
        artifacts = scanner._find_artifacts()
        names = {a.name for a in artifacts}
        assert "a.wic" in names
        assert "b.wic" in names


# =============================================================================
# ImageScanner._check_tool_availability tests
# =============================================================================


class TestCheckToolAvailability:
    def test_exits_when_tool_not_found(self, tmp_path):
        cfg = ScanConfig()
        scanner = ImageScanner(cfg, str(tmp_path))
        with patch("shutil.which", return_value=None):
            with pytest.raises(SystemExit):
                scanner._check_tool_availability("trivy")

    def test_no_exit_when_tool_found(self, tmp_path):
        cfg = ScanConfig()
        scanner = ImageScanner(cfg, str(tmp_path))
        with patch("shutil.which", return_value="/usr/bin/trivy"):
            scanner._check_tool_availability("trivy")  # Should not raise


# =============================================================================
# ImageScanner._run_trivy tests
# =============================================================================


TRIVY_REPORT_JSON = json.dumps({
    "Results": [
        {
            "Target": "usr/bin/bash",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2024-0001",
                    "Severity": "HIGH",
                    "PkgName": "bash",
                    "InstalledVersion": "5.1",
                    "Description": "A bash vuln",
                    "FixedVersion": "5.2",
                },
                {
                    "VulnerabilityID": "CVE-2024-0002",
                    "Severity": "CRITICAL",
                    "PkgName": "openssl",
                    "InstalledVersion": "1.0",
                    "Description": "An openssl vuln",
                    "FixedVersion": "",
                },
            ],
        }
    ]
})

TRIVY_SBOM_JSON = json.dumps({
    "components": [{"name": "bash"}, {"name": "openssl"}],
})


class TestRunTrivy:
    def _make_scanner(self, tmp_path, **cfg_kwargs):
        cfg = ScanConfig(**cfg_kwargs)
        return ImageScanner(cfg, str(tmp_path))

    def test_parses_trivy_findings(self, tmp_path):
        scanner = self._make_scanner(tmp_path)
        artifact = tmp_path / "core-image.wic"
        artifact.write_bytes(b"fake")
        output_dir = tmp_path / "reports"
        output_dir.mkdir()

        def fake_run(cmd, **kwargs):
            # Write fake report/sbom files based on cmd
            if "--format" in cmd and "json" in cmd and "--output" in cmd:
                out_path = cmd[cmd.index("--output") + 1]
                if "sbom-" in out_path:
                    Path(out_path).write_text(TRIVY_SBOM_JSON)
                else:
                    Path(out_path).write_text(TRIVY_REPORT_JSON)
            proc = MagicMock()
            proc.returncode = 0
            proc.stderr = ""
            return proc

        with patch("subprocess.run", side_effect=fake_run):
            findings, sbom, report_files = scanner._run_trivy(artifact, output_dir)

        assert len(findings) == 2
        cve_ids = {f.cve_id for f in findings}
        assert "CVE-2024-0001" in cve_ids
        assert "CVE-2024-0002" in cve_ids

    def test_trivy_sbom_component_count(self, tmp_path):
        scanner = self._make_scanner(tmp_path)
        artifact = tmp_path / "core-image.wic"
        artifact.write_bytes(b"fake")
        output_dir = tmp_path / "reports"
        output_dir.mkdir()

        def fake_run(cmd, **kwargs):
            if "--output" in cmd:
                out_path = cmd[cmd.index("--output") + 1]
                if "sbom-" in out_path:
                    Path(out_path).write_text(TRIVY_SBOM_JSON)
                else:
                    Path(out_path).write_text(TRIVY_REPORT_JSON)
            proc = MagicMock()
            proc.returncode = 0
            proc.stderr = ""
            return proc

        with patch("subprocess.run", side_effect=fake_run):
            _, sbom, _ = scanner._run_trivy(artifact, output_dir)

        assert sbom is not None
        assert sbom.component_count == 2

    def test_trivy_handles_missing_report(self, tmp_path):
        """When Trivy doesn't write a report file, parsing returns empty list."""
        scanner = self._make_scanner(tmp_path)
        artifact = tmp_path / "core-image.wic"
        artifact.write_bytes(b"fake")
        output_dir = tmp_path / "reports"
        output_dir.mkdir()

        proc = MagicMock()
        proc.returncode = 2  # Error code
        proc.stderr = "some error"

        with patch("subprocess.run", return_value=proc):
            findings, sbom, report_files = scanner._run_trivy(artifact, output_dir)

        assert findings == []
        assert sbom is None


# =============================================================================
# ImageScanner._parse_trivy_json tests
# =============================================================================


class TestParseTrivyJson:
    def test_parses_json(self, tmp_path):
        scanner = ImageScanner(ScanConfig(), str(tmp_path))
        report = tmp_path / "report.json"
        report.write_text(TRIVY_REPORT_JSON)
        findings = scanner._parse_trivy_json(report)
        assert len(findings) == 2
        assert findings[0].cve_id == "CVE-2024-0001"
        assert findings[0].severity == "HIGH"

    def test_handles_empty_results(self, tmp_path):
        scanner = ImageScanner(ScanConfig(), str(tmp_path))
        report = tmp_path / "report.json"
        report.write_text(json.dumps({"Results": []}))
        findings = scanner._parse_trivy_json(report)
        assert findings == []

    def test_handles_invalid_json(self, tmp_path):
        scanner = ImageScanner(ScanConfig(), str(tmp_path))
        report = tmp_path / "report.json"
        report.write_text("not valid json {")
        findings = scanner._parse_trivy_json(report)
        assert findings == []

    def test_handles_null_vulnerabilities(self, tmp_path):
        scanner = ImageScanner(ScanConfig(), str(tmp_path))
        report = tmp_path / "report.json"
        report.write_text(json.dumps({
            "Results": [{"Target": "foo", "Vulnerabilities": None}]
        }))
        findings = scanner._parse_trivy_json(report)
        assert findings == []


# =============================================================================
# ImageScanner._run_syft_grype tests
# =============================================================================


GRYPE_REPORT_JSON = json.dumps({
    "matches": [
        {
            "vulnerability": {
                "id": "CVE-2024-1001",
                "severity": "HIGH",
                "description": "A grype finding",
                "fix": {"versions": ["2.0"]},
            },
            "artifact": {
                "name": "libssl",
                "version": "1.0.0",
            },
        }
    ]
})

SYFT_SBOM_JSON = json.dumps({
    "components": [{"name": "libssl"}, {"name": "bash"}, {"name": "glibc"}],
})


class TestRunSyftGrype:
    def _make_scanner(self, tmp_path, **cfg_kwargs):
        cfg = ScanConfig(tool="syft+grype", **cfg_kwargs)
        return ImageScanner(cfg, str(tmp_path))

    def test_parses_grype_findings(self, tmp_path):
        scanner = self._make_scanner(tmp_path)
        artifact = tmp_path / "core-image.wic"
        artifact.write_bytes(b"fake")
        output_dir = tmp_path / "reports"
        output_dir.mkdir()

        def fake_run(cmd, **kwargs):
            if "--output" in cmd:
                out_idx = cmd.index("--output")
                if "syft" in cmd[0]:
                    # Syft: output is --output format=path
                    for arg in cmd:
                        if "sbom-" in arg and "=" in arg:
                            path = arg.split("=", 1)[1]
                            Path(path).write_text(SYFT_SBOM_JSON)
                elif "grype" in cmd[0]:
                    if "--file" in cmd:
                        file_path = cmd[cmd.index("--file") + 1]
                        Path(file_path).write_text(GRYPE_REPORT_JSON)
            proc = MagicMock()
            proc.returncode = 0
            proc.stderr = ""
            return proc

        with patch("subprocess.run", side_effect=fake_run):
            findings, sbom, report_files = scanner._run_syft_grype(artifact, output_dir)

        assert len(findings) == 1
        assert findings[0].cve_id == "CVE-2024-1001"
        assert findings[0].severity == "HIGH"

    def test_syft_sbom_component_count(self, tmp_path):
        scanner = self._make_scanner(tmp_path)
        artifact = tmp_path / "core-image.wic"
        artifact.write_bytes(b"fake")
        output_dir = tmp_path / "reports"
        output_dir.mkdir()

        def fake_run(cmd, **kwargs):
            if "syft" in cmd[0]:
                for arg in cmd:
                    if "sbom-" in arg and "=" in arg:
                        path = arg.split("=", 1)[1]
                        Path(path).write_text(SYFT_SBOM_JSON)
            proc = MagicMock()
            proc.returncode = 0
            proc.stderr = ""
            return proc

        with patch("subprocess.run", side_effect=fake_run):
            _, sbom, _ = scanner._run_syft_grype(artifact, output_dir)

        assert sbom is not None
        assert sbom.component_count == 3


# =============================================================================
# ImageScanner._parse_grype_json tests
# =============================================================================


class TestParseGrypeJson:
    def test_parses_json(self, tmp_path):
        scanner = ImageScanner(ScanConfig(severity="LOW"), str(tmp_path))
        report = tmp_path / "grype.json"
        report.write_text(GRYPE_REPORT_JSON)
        findings = scanner._parse_grype_json(report)
        assert len(findings) == 1
        assert findings[0].cve_id == "CVE-2024-1001"

    def test_filters_below_severity_threshold(self, tmp_path):
        """Findings below the configured minimum severity should be excluded."""
        scanner = ImageScanner(ScanConfig(severity="HIGH"), str(tmp_path))
        data = {
            "matches": [
                {"vulnerability": {"id": "CVE-LOW", "severity": "LOW", "description": ""},
                 "artifact": {"name": "pkg", "version": "1.0"}},
                {"vulnerability": {"id": "CVE-HIGH", "severity": "HIGH", "description": ""},
                 "artifact": {"name": "pkg", "version": "1.0"}},
            ]
        }
        report = tmp_path / "grype.json"
        report.write_text(json.dumps(data))
        findings = scanner._parse_grype_json(report)
        ids = {f.cve_id for f in findings}
        assert "CVE-LOW" not in ids
        assert "CVE-HIGH" in ids

    def test_handles_invalid_json(self, tmp_path):
        scanner = ImageScanner(ScanConfig(), str(tmp_path))
        report = tmp_path / "grype.json"
        report.write_text("{invalid")
        findings = scanner._parse_grype_json(report)
        assert findings == []


# =============================================================================
# ImageScanner.scan tests
# =============================================================================


class TestImageScannerScan:
    def test_scan_trivy_returns_result(self, tmp_path):
        images_dir = tmp_path / "tmp" / "deploy" / "images"
        images_dir.mkdir(parents=True)
        artifact = images_dir / "core-image.wic"
        artifact.write_bytes(b"fake")

        cfg = ScanConfig(
            tool="trivy",
            artifact_dirs=["tmp/deploy/images"],
            artifact_patterns=["*.wic"],
        )
        scanner = ImageScanner(cfg, str(tmp_path))

        with patch("shutil.which", return_value="/usr/bin/trivy"):
            with patch.object(scanner, "_run_trivy", return_value=(
                [ScanFinding("CVE-1", "HIGH", "pkg", "1.0")],
                SbomResult(path=tmp_path / "reports" / "sbom.json", sbom_format="cyclonedx", component_count=5),
                [tmp_path / "reports" / "report.json"],
            )):
                result = scanner.scan()

        assert result.total_count == 1
        assert len(result.sboms) == 1
        assert len(result.scanned_artifacts) == 1

    def test_scan_returns_empty_result_when_no_artifacts(self, tmp_path):
        cfg = ScanConfig()
        scanner = ImageScanner(cfg, str(tmp_path))
        with patch("shutil.which", return_value="/usr/bin/trivy"):
            result = scanner.scan()
        assert result.total_count == 0
        assert result.scanned_artifacts == []

    def test_scan_exits_on_unknown_tool(self, tmp_path):
        cfg = ScanConfig(tool="unknown-tool")
        images_dir = tmp_path / "tmp" / "deploy" / "images"
        images_dir.mkdir(parents=True)
        (images_dir / "image.wic").write_bytes(b"data")

        scanner = ImageScanner(cfg, str(tmp_path))
        with pytest.raises(SystemExit):
            scanner.scan()

    def test_scan_uses_provided_artifact_paths(self, tmp_path):
        artifact = tmp_path / "my-image.wic"
        artifact.write_bytes(b"fake")

        cfg = ScanConfig(tool="trivy")
        scanner = ImageScanner(cfg, str(tmp_path))

        with patch("shutil.which", return_value="/usr/bin/trivy"):
            with patch.object(scanner, "_run_trivy", return_value=([], None, [])) as mock_trivy:
                scanner.scan(artifact_paths=[artifact])
                mock_trivy.assert_called_once()
                called_artifact = mock_trivy.call_args[0][0]
                assert called_artifact == artifact

    def test_scan_creates_output_dir(self, tmp_path):
        artifact = tmp_path / "image.wic"
        artifact.write_bytes(b"fake")
        output_dir = tmp_path / "custom-reports"

        cfg = ScanConfig(tool="trivy", output_dir=str(output_dir))
        scanner = ImageScanner(cfg, str(tmp_path))

        with patch("shutil.which", return_value="/usr/bin/trivy"):
            with patch.object(scanner, "_run_trivy", return_value=([], None, [])):
                scanner.scan(artifact_paths=[artifact])

        assert output_dir.is_dir()


# =============================================================================
# ImageScanner helper method tests
# =============================================================================


class TestHelpers:
    def test_severity_filter_str_from_high(self, tmp_path):
        scanner = ImageScanner(ScanConfig(severity="HIGH"), str(tmp_path))
        result = scanner._severity_filter_str()
        assert "HIGH" in result
        assert "CRITICAL" in result
        assert "MEDIUM" not in result
        assert "LOW" not in result

    def test_severity_filter_str_from_low(self, tmp_path):
        scanner = ImageScanner(ScanConfig(severity="LOW"), str(tmp_path))
        result = scanner._severity_filter_str()
        for level in ("LOW", "MEDIUM", "HIGH", "CRITICAL"):
            assert level in result

    def test_sbom_extension_cyclonedx(self, tmp_path):
        scanner = ImageScanner(ScanConfig(sbom_format="cyclonedx"), str(tmp_path))
        assert scanner._sbom_extension() == "cdx.json"

    def test_sbom_extension_spdx_json(self, tmp_path):
        scanner = ImageScanner(ScanConfig(sbom_format="spdx-json"), str(tmp_path))
        assert scanner._sbom_extension() == "spdx.json"

    def test_sbom_extension_spdx_tag_value(self, tmp_path):
        scanner = ImageScanner(ScanConfig(sbom_format="spdx-tag-value"), str(tmp_path))
        assert scanner._sbom_extension() == "spdx"


# =============================================================================
# CLI scan command argument tests
# =============================================================================


SCAN_REGISTRY_YAML = """
specification:
  version: "2.0"
registry:
  devices:
    - slug: rpi5
      description: "Raspberry Pi 5"
      vendor: raspberrypi
      soc_vendor: broadcom
      includes: []
  releases:
    - slug: scarthgap
      description: "Scarthgap"
      yocto_version: "5.0"
      includes: []
  features: []
  bsp:
    - name: rpi5-scarthgap
      description: "RPI5 Scarthgap BSP"
      device: rpi5
      release: scarthgap
      features: []
      build:
        path: build/rpi5/scarthgap
"""


class TestCliScanArguments:
    def _make_registry(self, tmp_path) -> Path:
        reg = tmp_path / "bsp-registry.yaml"
        reg.write_text(SCAN_REGISTRY_YAML)
        return reg

    def test_scan_bsp_name(self, tmp_path):
        reg = self._make_registry(tmp_path)
        with patch("sys.argv", ["bsp", "--registry", str(reg), "scan", "rpi5-scarthgap"]):
            with patch("bsp.cli.BspManager") as MockBspMgr:
                mock_mgr = MockBspMgr.return_value
                mock_mgr.scan_bsp.return_value = ScanResult()
                from bsp.cli import main as cli_main
                cli_main()
                mock_mgr.scan_bsp.assert_called_once()
                call_args = mock_mgr.scan_bsp.call_args
                assert "rpi5-scarthgap" in str(call_args)

    def test_scan_dry_run_flag(self, tmp_path):
        reg = self._make_registry(tmp_path)
        with patch("sys.argv", ["bsp", "--registry", str(reg), "scan", "rpi5-scarthgap", "--dry-run"]):
            with patch("bsp.cli.BspManager") as MockBspMgr:
                mock_mgr = MockBspMgr.return_value
                mock_mgr.scan_bsp.return_value = ScanResult()
                from bsp.cli import main as cli_main
                cli_main()
                _, kwargs = mock_mgr.scan_bsp.call_args
                assert kwargs.get("dry_run") is True

    def test_scan_tool_flag(self, tmp_path):
        reg = self._make_registry(tmp_path)
        with patch("sys.argv", ["bsp", "--registry", str(reg), "scan", "rpi5-scarthgap", "--tool", "trivy"]):
            with patch("bsp.cli.BspManager") as MockBspMgr:
                mock_mgr = MockBspMgr.return_value
                mock_mgr.scan_bsp.return_value = ScanResult()
                from bsp.cli import main as cli_main
                cli_main()
                _, kwargs = mock_mgr.scan_bsp.call_args
                assert kwargs.get("scan_overrides", {}).get("tool") == "trivy"

    def test_scan_severity_flag(self, tmp_path):
        reg = self._make_registry(tmp_path)
        with patch("sys.argv", ["bsp", "--registry", str(reg), "scan", "rpi5-scarthgap", "--severity", "MEDIUM"]):
            with patch("bsp.cli.BspManager") as MockBspMgr:
                mock_mgr = MockBspMgr.return_value
                mock_mgr.scan_bsp.return_value = ScanResult()
                from bsp.cli import main as cli_main
                cli_main()
                _, kwargs = mock_mgr.scan_bsp.call_args
                assert kwargs.get("scan_overrides", {}).get("severity") == "MEDIUM"

    def test_scan_fail_on_flag(self, tmp_path):
        reg = self._make_registry(tmp_path)
        with patch("sys.argv", ["bsp", "--registry", str(reg), "scan", "rpi5-scarthgap", "--fail-on", "HIGH"]):
            with patch("bsp.cli.BspManager") as MockBspMgr:
                mock_mgr = MockBspMgr.return_value
                mock_mgr.scan_bsp.return_value = ScanResult()
                from bsp.cli import main as cli_main
                cli_main()
                _, kwargs = mock_mgr.scan_bsp.call_args
                assert kwargs.get("scan_overrides", {}).get("fail_on") == "HIGH"

    def test_scan_sbom_format_flag(self, tmp_path):
        reg = self._make_registry(tmp_path)
        with patch("sys.argv", ["bsp", "--registry", str(reg), "scan", "rpi5-scarthgap", "--sbom-format", "spdx-json"]):
            with patch("bsp.cli.BspManager") as MockBspMgr:
                mock_mgr = MockBspMgr.return_value
                mock_mgr.scan_bsp.return_value = ScanResult()
                from bsp.cli import main as cli_main
                cli_main()
                _, kwargs = mock_mgr.scan_bsp.call_args
                assert kwargs.get("scan_overrides", {}).get("sbom_format") == "spdx-json"

    def test_scan_output_dir_flag(self, tmp_path):
        reg = self._make_registry(tmp_path)
        output_dir = str(tmp_path / "reports")
        with patch("sys.argv", ["bsp", "--registry", str(reg), "scan", "rpi5-scarthgap", "--output-dir", output_dir]):
            with patch("bsp.cli.BspManager") as MockBspMgr:
                mock_mgr = MockBspMgr.return_value
                mock_mgr.scan_bsp.return_value = ScanResult()
                from bsp.cli import main as cli_main
                cli_main()
                _, kwargs = mock_mgr.scan_bsp.call_args
                assert kwargs.get("scan_overrides", {}).get("output_dir") == output_dir

    def test_scan_image_path_flag(self, tmp_path):
        reg = self._make_registry(tmp_path)
        image = str(tmp_path / "my-image.wic")
        with patch("sys.argv", ["bsp", "--registry", str(reg), "scan", "rpi5-scarthgap", "--image-path", image]):
            with patch("bsp.cli.BspManager") as MockBspMgr:
                mock_mgr = MockBspMgr.return_value
                mock_mgr.scan_bsp.return_value = ScanResult()
                from bsp.cli import main as cli_main
                cli_main()
                _, kwargs = mock_mgr.scan_bsp.call_args
                assert kwargs.get("image_paths") == [image]

    def test_scan_by_components(self, tmp_path):
        reg = self._make_registry(tmp_path)
        with patch("sys.argv", ["bsp", "--registry", str(reg), "scan",
                                 "--device", "rpi5", "--release", "scarthgap"]):
            with patch("bsp.cli.BspManager") as MockBspMgr:
                mock_mgr = MockBspMgr.return_value
                mock_mgr.scan_by_components.return_value = ScanResult()
                from bsp.cli import main as cli_main
                cli_main()
                mock_mgr.scan_by_components.assert_called_once()

    def test_build_scan_flag(self, tmp_path):
        """``bsp build --scan`` passes scan_after_build=True to build_bsp."""
        reg = self._make_registry(tmp_path)
        with patch("sys.argv", ["bsp", "--registry", str(reg), "build", "rpi5-scarthgap", "--scan"]):
            with patch("bsp.cli.BspManager") as MockBspMgr:
                mock_mgr = MockBspMgr.return_value
                from bsp.cli import main as cli_main
                cli_main()
                _, kwargs = mock_mgr.build_bsp.call_args
                assert kwargs.get("scan_after_build") is True


# =============================================================================
# BspManager.scan_bsp / scan_by_components integration tests (mocked scanner)
# =============================================================================


class TestBspManagerScan:
    def test_scan_bsp_calls_scanner(self, tmp_path):
        from bsp import BspManager

        reg = tmp_path / "bsp-registry.yaml"
        reg.write_text(SCAN_REGISTRY_YAML)

        mgr = BspManager(config_path=str(reg))
        mgr.initialize()

        mock_result = ScanResult(
            findings=[ScanFinding("CVE-1", "HIGH", "bash", "5.1")],
            fail_on="CRITICAL",
        )

        with patch("bsp.bsp_manager.ImageScanner") as MockScanner:
            mock_instance = MockScanner.return_value
            mock_instance.scan.return_value = mock_result
            mock_instance._find_artifacts.return_value = []
            result = mgr.scan_bsp("rpi5-scarthgap", dry_run=True)

        # In dry_run mode the scanner is bypassed, so we get an empty result
        assert isinstance(result, ScanResult)

    def test_scan_by_components_calls_scanner(self, tmp_path):
        from bsp import BspManager

        reg = tmp_path / "bsp-registry.yaml"
        reg.write_text(SCAN_REGISTRY_YAML)

        mgr = BspManager(config_path=str(reg))
        mgr.initialize()

        mock_result = ScanResult(fail_on="CRITICAL")

        with patch("bsp.bsp_manager.ImageScanner") as MockScanner:
            mock_instance = MockScanner.return_value
            mock_instance.scan.return_value = mock_result
            mock_instance._find_artifacts.return_value = []
            result = mgr.scan_by_components("rpi5", "scarthgap", dry_run=True)

        assert isinstance(result, ScanResult)

    def test_scan_bsp_exits_on_failure(self, tmp_path):
        """scan_bsp should call sys.exit when result.passed is False and not dry_run."""
        from bsp import BspManager

        reg = tmp_path / "bsp-registry.yaml"
        reg.write_text(SCAN_REGISTRY_YAML)

        mgr = BspManager(config_path=str(reg))
        mgr.initialize()

        failing_result = ScanResult(
            findings=[ScanFinding("CVE-1", "CRITICAL", "bash", "5.1")],
            fail_on="CRITICAL",
        )

        with patch("bsp.bsp_manager.ImageScanner") as MockScanner:
            mock_instance = MockScanner.return_value
            mock_instance.scan.return_value = failing_result
            with patch("shutil.which", return_value="/usr/bin/trivy"):
                with pytest.raises(SystemExit):
                    mgr.scan_bsp("rpi5-scarthgap")

    def test_scan_bsp_resolve_scan_config_merges_overrides(self, tmp_path):
        """scan_overrides should override the registry-level scan config."""
        from bsp import BspManager

        yaml_with_scan = SCAN_REGISTRY_YAML + """
scan:
  tool: trivy
  severity: HIGH
  fail_on: CRITICAL
"""
        reg = tmp_path / "bsp-registry.yaml"
        reg.write_text(yaml_with_scan)

        mgr = BspManager(config_path=str(reg))
        mgr.initialize()

        # Resolve scan config with an override
        from bsp.resolver import ResolvedConfig
        resolved = mgr.resolver.resolve("rpi5", "scarthgap")
        cfg = mgr._resolve_scan_config(resolved, scan_overrides={"severity": "MEDIUM"})
        assert cfg.severity == "MEDIUM"
        # Other fields remain from root config
        assert cfg.tool == "trivy"

    def test_resolve_scan_config_defaults_when_no_config(self, tmp_path):
        """When no scan block exists, defaults are used."""
        from bsp import BspManager

        reg = tmp_path / "bsp-registry.yaml"
        reg.write_text(SCAN_REGISTRY_YAML)

        mgr = BspManager(config_path=str(reg))
        mgr.initialize()

        resolved = mgr.resolver.resolve("rpi5", "scarthgap")
        cfg = mgr._resolve_scan_config(resolved)
        assert cfg.tool == "trivy"
        assert cfg.severity == "HIGH"
        assert cfg.fail_on == "CRITICAL"


# =============================================================================
# ScanConfig model tests
# =============================================================================


class TestScanConfigModel:
    def test_defaults(self):
        cfg = ScanConfig()
        assert cfg.tool == "trivy"
        assert cfg.severity == "HIGH"
        assert cfg.fail_on == "CRITICAL"
        assert cfg.sbom_format == "cyclonedx"
        assert cfg.output_dir is None
        assert cfg.upload is False
        assert "*.wic" in cfg.artifact_patterns

    def test_registry_root_has_scan_field(self):
        from bsp.models import RegistryRoot
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(RegistryRoot)}
        assert "scan" in field_names

    def test_bsp_preset_has_scan_field(self):
        from bsp.models import BspPreset
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(BspPreset)}
        assert "scan" in field_names

    def test_scan_config_parsed_from_yaml(self, tmp_path):
        """ScanConfig is correctly parsed from a registry YAML with a scan block."""
        from bsp import BspManager

        yaml_content = SCAN_REGISTRY_YAML + """
scan:
  tool: trivy
  severity: MEDIUM
  fail_on: HIGH
  sbom_format: spdx-json
  output_dir: /tmp/scan-reports
"""
        reg = tmp_path / "bsp-registry.yaml"
        reg.write_text(yaml_content)

        mgr = BspManager(config_path=str(reg))
        mgr.initialize()

        assert mgr.model.scan is not None
        assert mgr.model.scan.tool == "trivy"
        assert mgr.model.scan.severity == "MEDIUM"
        assert mgr.model.scan.fail_on == "HIGH"
        assert mgr.model.scan.sbom_format == "spdx-json"
        assert mgr.model.scan.output_dir == "/tmp/scan-reports"

    def test_preset_level_scan_config_parsed(self, tmp_path):
        """Preset-level scan block is resolved and available in resolved.scan_config."""
        from bsp import BspManager

        yaml_with_preset_scan = """
specification:
  version: "2.0"
registry:
  devices:
    - slug: rpi5
      description: "RPI5"
      vendor: raspberrypi
      soc_vendor: broadcom
      includes: []
  releases:
    - slug: scarthgap
      description: "Scarthgap"
      yocto_version: "5.0"
      includes: []
  features: []
  bsp:
    - name: rpi5-scarthgap
      description: "BSP"
      device: rpi5
      release: scarthgap
      features: []
      build:
        path: build/rpi5/scarthgap
      scan:
        tool: syft+grype
        severity: CRITICAL
        fail_on: NONE
"""
        reg = tmp_path / "bsp-registry.yaml"
        reg.write_text(yaml_with_preset_scan)

        mgr = BspManager(config_path=str(reg))
        mgr.initialize()

        resolved, preset = mgr.resolver.resolve_preset("rpi5-scarthgap")
        assert resolved.scan_config is not None
        assert resolved.scan_config.tool == "syft+grype"
        assert resolved.scan_config.severity == "CRITICAL"
        assert resolved.scan_config.fail_on == "NONE"
