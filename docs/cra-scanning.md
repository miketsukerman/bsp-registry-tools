# CRA Image Scanning

## Background: EU Cyber Resilience Act

The **EU Cyber Resilience Act (CRA)** mandates that manufacturers of products
with digital elements — including embedded Linux Board Support Packages (BSPs)
— must:

- Maintain a **Software Bill of Materials (SBOM)** for every release
- Perform and document **vulnerability assessments** against known CVEs
- Ensure **no known exploitable CVEs** are present at the time of release
- Report **actively exploited vulnerabilities** within defined timelines

The primary tool for this in the embedded / Yocto ecosystem is
[**Trivy**](https://trivy.dev/) (Aqua Security), which can scan Yocto rootfs
tarballs and WIC images, generate SBOMs (CycloneDX, SPDX), and report CVEs.  A
secondary option is [**Syft + Grype**](https://github.com/anchore/) (Anchore).

Both tools are **external CLI programs** — they are not Python packages.  See
[Prerequisites](#prerequisites) for installation instructions.

---

## Prerequisites

### Trivy (recommended)

```bash
# Linux — official install script
curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh \
  | sh -s -- -b /usr/local/bin

# macOS
brew install aquasecurity/trivy/trivy

# Verify
trivy --version
```

Full instructions: <https://trivy.dev/latest/getting-started/installation/>

### Syft + Grype (alternative)

```bash
# Syft
curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh \
  | sh -s -- -b /usr/local/bin

# Grype
curl -sSfL https://raw.githubusercontent.com/anchore/grype/main/install.sh \
  | sh -s -- -b /usr/local/bin
```

---

## Registry YAML configuration

A `scan:` block may be added at **root level** (global defaults applying to
every build) and/or at **preset level** (overrides the root-level config for
that specific preset):

```yaml
# bsp-registry.yaml

specification:
  version: "2.0"

# -------------------------------------------------------------------
# Global scan defaults (applied to every preset unless overridden)
# -------------------------------------------------------------------
scan:
  tool: trivy                     # "trivy" (default) | "syft+grype"
  severity: HIGH                  # minimum CVE severity to report
                                  # LOW | MEDIUM | HIGH (default) | CRITICAL
  fail_on: CRITICAL               # exit non-zero at this severity level
                                  # NONE | LOW | MEDIUM | HIGH | CRITICAL (default)
  sbom_format: cyclonedx          # cyclonedx (default) | spdx-json | spdx-tag-value
  output_dir: reports/            # output directory for reports and SBOMs
                                  # (default: <build_path>/reports/)
  artifact_patterns:              # glob patterns to select image files
    - "**/*.rootfs.tar.gz"
    - "**/*.rootfs.tar.bz2"
    - "**/*.ext4"
  artifact_dirs:                  # subdirs under build_path to search
    - "tmp/deploy/images"
  upload: false                   # upload reports to cloud storage (optional)

registry:
  devices: [...]
  releases: [...]
  bsp:
    - name: imx8-scarthgap
      description: "i.MX 8 Scarthgap BSP"
      device: imx8
      release: scarthgap
      build:
        path: build/imx8/scarthgap
      # Preset-level scan override: stricter fail_on for this release
      scan:
        fail_on: HIGH
```

### `scan:` field reference

| Field | Default | Description |
|---|---|---|
| `tool` | `trivy` | Scanner backend: `trivy` or `syft+grype` |
| `severity` | `HIGH` | Minimum CVE severity included in report: `LOW`, `MEDIUM`, `HIGH`, `CRITICAL` |
| `fail_on` | `CRITICAL` | Exit non-zero at this severity: `NONE`, `LOW`, `MEDIUM`, `HIGH`, `CRITICAL` |
| `sbom_format` | `cyclonedx` | SBOM format: `cyclonedx`, `spdx-json`, `spdx-tag-value` |
| `output_dir` | `<build_path>/reports/` | Directory for scan reports and SBOMs |
| `artifact_patterns` | `**/*.rootfs.tar.gz`, `**/*.rootfs.tar.bz2`, … | Glob patterns to select image files to scan |
| `artifact_dirs` | `tmp/deploy/images` | Subdirectories under the build path to search |
| `upload` | `false` | Upload reports to cloud storage (same as `deploy`) |

### Supported artifact formats

`trivy rootfs` (the Trivy scanner backend) can only extract **rootfs tarballs** and
**raw ext4 images**. Several Yocto output formats are structurally incompatible and
produce an empty SBOM and zero CVE findings when passed to Trivy.

| Format | Scannable? | Notes |
|---|---|---|
| `*.rootfs.tar.gz` / `*.rootfs.tar.bz2` | ✅ Yes | Recommended scan target |
| `*.tar.gz` / `*.tar.bz2` | ✅ Yes | Generic tarballs |
| `*.ext4` | ✅ Yes | Raw ext4 filesystem image |
| `*.sdimg` / `*.rpi-sdimg` | ✅ Yes | Depends on Trivy version |
| `*.rootfs.tar.zst` / `*.tar.zst` | ❌ No | Zstd not supported by Trivy's archive extractor; silent empty result |
| `*.wic` and `*.wic.*` | ❌ No | Raw disk image with partition table; Trivy has no WIC/GPT parser |

> **WIC and compressed WIC images** (`.wic`, `.wic.gz`, `.wic.bz2`, `.wic.xz`,
> `.wic.zst`) can **never** be scanned directly by `trivy rootfs`.  They are raw disk
> images containing a partition table; Trivy has no partition extractor.  The scanner
> will log a warning and skip these files automatically.
>
> **Zstd-compressed tarballs** (`.tar.zst`, `.rootfs.tar.zst`) are silently accepted
> by Trivy but yield an empty SBOM because the archive extractor does not support
> zstd.  The scanner will log a warning and skip them.  Decompress with
> `zstd -d image.rootfs.tar.zst -o image.rootfs.tar` and add `**/*.rootfs.tar` to
> `artifact_patterns`, or configure Yocto to produce `.tar.gz` output
> (`IMAGE_FSTYPES += "tar.gz"`).

---

## CLI Usage

### `bsp scan` — standalone scan command

Scan the build artifacts for a named BSP preset:

```bash
bsp scan imx8-scarthgap
```

Scan by specifying device, release, and optional features directly:

```bash
bsp scan --device imx8 --release scarthgap
```

#### Common flags

```
bsp scan <preset | --device D --release R [--feature F ...]>
         [--tool trivy|syft+grype]
         [--severity LOW|MEDIUM|HIGH|CRITICAL]
         [--fail-on NONE|LOW|MEDIUM|HIGH|CRITICAL]
         [--sbom-format cyclonedx|spdx-json|spdx-tag-value]
         [--output-dir PATH]
         [--image-path PATH]    # scan a specific artifact (repeatable)
         [--dry-run]            # list what would be scanned
```

#### Examples

```bash
# Scan with MEDIUM as minimum severity threshold, fail on HIGH
bsp scan imx8-scarthgap --severity MEDIUM --fail-on HIGH

# Generate an SPDX-JSON SBOM instead of CycloneDX
bsp scan imx8-scarthgap --sbom-format spdx-json --output-dir /tmp/sboms

# Dry run: list artifacts without scanning
bsp scan imx8-scarthgap --dry-run

# Scan a specific WIC image explicitly
bsp scan imx8-scarthgap \
    --image-path build/imx8/scarthgap/tmp/deploy/images/core-image-minimal-imx8.wic

# Use Syft + Grype instead of Trivy
bsp scan imx8-scarthgap --tool syft+grype
```

### `bsp build --scan` — scan after build

The `--scan` flag on `bsp build` triggers a vulnerability scan immediately
after a successful build (analogous to `--deploy` and `--test`):

```bash
bsp build imx8-scarthgap --scan

# Scan with non-default fail threshold
bsp build imx8-scarthgap --scan --scan-fail-on HIGH

# Combined: build, deploy, then scan
bsp build imx8-scarthgap --deploy --scan
```

#### `--scan` flags on `bsp build`

```
--scan                    Scan artifacts after a successful build
--scan-tool TOOL          Scanner backend (trivy or syft+grype)
--scan-severity LEVEL     Minimum severity to report
--scan-fail-on LEVEL      Exit non-zero at this severity
--scan-output-dir PATH    Directory for scan reports
```

---

## Output

After a scan, the tool prints a summary:

```
Scan completed: 3 finding(s) across 1 artifact(s)
  Severity breakdown: CRITICAL=1  HIGH=2  MEDIUM=0  LOW=0
  SBOM(s) generated:
    build/imx8/scarthgap/reports/sbom-core-image-minimal-imx8_wic.cdx.json (312 components, format: cyclonedx)
  Report(s):
    build/imx8/scarthgap/reports/trivy-core-image-minimal-imx8_wic.json
    build/imx8/scarthgap/reports/sbom-core-image-minimal-imx8_wic.cdx.json
```

Files written to `output_dir` (default: `<build_path>/reports/`):

| File pattern | Contents |
|---|---|
| `trivy-<stem>.json` | Trivy CVE vulnerability report (JSON) |
| `grype-<stem>.json` | Grype CVE vulnerability report (JSON) |
| `sbom-<stem>.cdx.json` | CycloneDX SBOM |
| `sbom-<stem>.spdx.json` | SPDX-JSON SBOM |
| `sbom-<stem>.spdx` | SPDX tag-value SBOM |

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Scan passed — no findings at or above `fail_on` severity |
| `1` | Scan failed — at least one finding at or above `fail_on` severity |
| `1` | Tool not installed, configuration error, or artifact not found |

The exit code is `0` when `fail_on: NONE` regardless of findings.

---

## Supported SBOM formats

| Format | `sbom_format` value | File extension |
|---|---|---|
| CycloneDX (JSON) | `cyclonedx` | `.cdx.json` |
| SPDX (JSON) | `spdx-json` | `.spdx.json` |
| SPDX (tag-value) | `spdx-tag-value` | `.spdx` |

---

## Integration with CI/CD

```yaml
# GitHub Actions example
- name: Scan BSP for CVEs
  run: |
    bsp scan imx8-scarthgap \
      --fail-on CRITICAL \
      --sbom-format cyclonedx \
      --output-dir reports/

- name: Upload SBOM and scan report
  uses: actions/upload-artifact@v4
  with:
    name: cra-scan-reports
    path: reports/
```
