# Artifact Deployment Guide

`bsp-registry-tools` can upload Yocto/Isar build artifacts produced by
`bsp build` to **Azure Blob Storage** (default) or **AWS S3** after the build
completes.

---

## Table of Contents

- [Overview](#overview)
- [Quick Start](#quick-start)
- [Installation](#installation)
- [Registry Configuration](#registry-configuration)
  - [Global `deploy:` block](#global-deploy-block)
  - [Per-preset override](#per-preset-override)
  - [Field reference](#field-reference)
  - [Prefix template placeholders](#prefix-template-placeholders)
  - [Archive bundling](#archive-bundling)
- [Authentication](#authentication)
  - [Azure](#azure)
  - [AWS](#aws)
- [CLI Reference](#cli-reference)
  - [`bsp deploy`](#bsp-deploy)
  - [`bsp build --deploy`](#bsp-build---deploy)
- [Dry-run mode](#dry-run-mode)
- [Artifact manifest](#artifact-manifest)
- [Partial failures](#partial-failures)
- [Python API](#python-api)
- [CI/CD integration](#cicd-integration)
  - [GitHub Actions – Azure](#github-actions--azure)
  - [GitHub Actions – AWS](#github-actions--aws)

---

## Overview

After a Yocto build, images and SDKs land under:

```
<build_path>/tmp/deploy/images/
<build_path>/tmp/deploy/sdk/
```

`bsp deploy` finds all files that match the configured glob patterns in those
directories and uploads them to your cloud storage provider.  An optional JSON
manifest (with artifact names, sizes, and SHA-256 checksums) is uploaded
alongside the artifacts.

Config can live either in the registry YAML (checked in, shared by the team) or
be overridden entirely from the command line.

---

## Quick Start

```bash
# 1. Install cloud SDK extras (one-time)
pip install "bsp-registry-tools[azure]"   # Azure
pip install "bsp-registry-tools[aws]"     # AWS
pip install "bsp-registry-tools[deploy]"  # both

# 2. Authenticate (one-time)
az login                          # Azure (interactive)
aws configure                     # AWS (interactive)

# 3. Build and deploy in one step
bsp build poky-qemuarm64-scarthgap --deploy --deploy-container bsp-artifacts

# — or — deploy separately after a successful build
bsp deploy poky-qemuarm64-scarthgap --container bsp-artifacts

# Preview what would be uploaded (no credentials required)
bsp deploy poky-qemuarm64-scarthgap --dry-run
```

---

## Installation

Cloud SDK dependencies are **optional** to avoid forcing them on users who do
not need deployment.

```bash
# Azure Blob Storage support
pip install "bsp-registry-tools[azure]"
# installs: azure-storage-blob>=12.0, azure-identity>=1.0

# AWS S3 support
pip install "bsp-registry-tools[aws]"
# installs: boto3>=1.20

# Both providers
pip install "bsp-registry-tools[deploy]"
```

`--dry-run` mode works **without any cloud SDK installed**.

---

## Registry Configuration

### Global `deploy:` block

Add a top-level `deploy:` block to your registry YAML.  It applies to every
build by default.

```yaml
specification:
  version: "2.0"

deploy:
  provider: azure
  account_url: $ENV{AZURE_STORAGE_ACCOUNT_URL}   # supports $ENV{} expansion
  container: bsp-artifacts
  prefix: "{vendor}/{device}/{release}/{date}"
  patterns:
    - "**/*.wic.gz"
    - "**/*.wic.bz2"
    - "**/*.tar.bz2"
    - "**/*.ext4"
    - "**/*.sdimg"
  artifact_dirs:
    - tmp/deploy/images
    - tmp/deploy/sdk
  include_manifest: true
  # Optional: bundle all artifacts into a single archive before uploading
  archive:
    name: "firmware-{device}-{release}-{date}"
    format: tar.gz

registry:
  # ...
```

**AWS variant:**

```yaml
deploy:
  provider: aws
  bucket: my-s3-bucket
  region: eu-west-1
  prefix: "{device}/{release}/{date}"
  patterns:
    - "**/*.wic.gz"
  artifact_dirs:
    - tmp/deploy/images
```

### Per-preset override

An individual `BspPreset` entry can include its own `deploy:` block.  Only the
fields that differ from the `DeployConfig` defaults override the global config;
all other fields keep their global values.

**Merge order** (later entries win):
1. **Global `deploy:`** — baseline for every build
2. **Preset `deploy:`** — overrides only fields that differ from their defaults
3. **CLI flags** (`--provider`, `--container`, …) — highest priority

```yaml
deploy:                               # global: Azure, shared container
  provider: azure
  account_url: $ENV{AZURE_STORAGE_ACCOUNT_URL}
  container: bsp-artifacts
  prefix: "{vendor}/{device}/{release}/{date}"

registry:
  bsp:
    # Uses global settings unchanged.
    - name: qemuarm64-scarthgap
      device: qemuarm64
      release: scarthgap
      features: []

    # Overrides only container and prefix; provider and account_url come from global.
    - name: imx8mp-adv-scarthgap-release
      description: "Advantech i.MX8MP Scarthgap – release artefacts"
      device: imx8mp-adv
      release: scarthgap
      features: []
      deploy:
        container: imx8mp-release-artifacts           # ← override
        prefix: "release/{device}/{release}/{date}"   # ← override
        patterns:                                     # ← override
          - "**/*.wic.gz"

    # Switches to AWS entirely for this preset only.
    - name: aws-build-scarthgap
      device: qemuarm64
      release: scarthgap
      features: []
      deploy:
        provider: aws                 # ← override: switch provider
        container: my-s3-bucket       # ← override: bucket name
```

### Field reference

| Field              | Type          | Default | Description |
|--------------------|---------------|---------|-------------|
| `provider`         | string        | `"azure"` | Cloud provider: `"azure"` or `"aws"` |
| `container`        | string (opt.) | —       | Azure Blob container name |
| `bucket`           | string (opt.) | —       | AWS S3 bucket name |
| `account_url`      | string (opt.) | —       | Azure account URL; supports `$ENV{VAR}` expansion. Falls back to the `AZURE_STORAGE_ACCOUNT_URL` env var. |
| `prefix`           | string (opt.) | `"{vendor}/{device}/{release}/{date}"` | Remote path prefix template (see [placeholders](#prefix-template-placeholders)) |
| `patterns`         | list[str]     | `["**/*.wic*", "**/*.tar.gz", "**/*.ext4", "**/*.sdimg"]` | Glob patterns for artifact files |
| `artifact_dirs`    | list[str]     | `["tmp/deploy/images", "tmp/deploy/sdk"]` | Subdirectories under the build path to scan |
| `include_manifest` | bool          | `true`  | Upload a JSON manifest alongside artifacts |
| `archive`          | object (opt.) | —       | Bundle all artifacts into a single archive before uploading. See [Archive bundling](#archive-bundling). |
| `region`           | string (opt.) | —       | AWS region (optional; boto3 default otherwise) |
| `profile`          | string (opt.) | —       | AWS credentials profile (optional) |

### Prefix template placeholders

The `prefix` field is a Python format string.  The following variables are
available at deploy time:

| Placeholder  | Example value    | Description |
|--------------|------------------|-------------|
| `{device}`   | `qemuarm64`      | Device slug |
| `{release}`  | `scarthgap`      | Release slug |
| `{distro}`   | `poky`           | Effective distro slug |
| `{vendor}`   | `qemu`           | Device vendor slug |
| `{date}`     | `2025-03-15`     | Build date (UTC, `YYYY-MM-DD`) |
| `{datetime}` | `20250315-143022` | Build date + time (UTC, `YYYYMMDD-HHMMSS`) |

**Example prefixes:**

```
{vendor}/{device}/{release}/{date}
→  qemu/qemuarm64/scarthgap/2025-03-15/

builds/{device}/{date}
→  builds/qemuarm64/2025-03-15/

release/{release}/{device}
→  release/scarthgap/qemuarm64/
```

---

## Archive bundling

By default every matching artifact file is uploaded individually.  Set the
`archive:` sub-object inside `deploy:` to collect all artifacts into a single
compressed archive **before** uploading.  Only the archive (plus the manifest
when `include_manifest: true`) is uploaded.

```yaml
deploy:
  provider: azure
  container: bsp-artifacts
  archive:
    name: "firmware-{device}-{release}-{date}"
    format: tar.gz
```

### `archive` fields

| Field    | Type   | Default                       | Description |
|----------|--------|-------------------------------|-------------|
| `name`   | string | `"artifacts-{device}-{date}"` | Archive filename template (without extension).  Supports the same placeholders as `prefix`: `{device}`, `{release}`, `{distro}`, `{vendor}`, `{date}`, `{datetime}`. |
| `format` | string | `"tar.gz"`                    | Compression format: `tar.gz`, `tar.bz2`, `tar.xz`, or `zip`. |

The appropriate file extension is appended automatically (e.g. `.tar.gz` for
`tar.gz`).

**CLI equivalents:**

```bash
# bsp deploy
bsp deploy my-preset \
    --archive-name "firmware-{device}-{release}-{date}" \
    --archive-format tar.gz

# bsp build --deploy
bsp build my-preset --deploy \
    --deploy-archive-name "firmware-{device}-{release}-{date}" \
    --deploy-archive-format tar.gz
```

---

## Authentication

### Azure

Credentials are resolved in the following order:

1. **`AZURE_STORAGE_CONNECTION_STRING`** environment variable — if set, the
   connection string is used directly (no `account_url` needed).
2. **`deploy.account_url`** (or `AZURE_STORAGE_ACCOUNT_URL` env var) +
   `DefaultAzureCredential` — supports any of the methods below transparently:

| Method | Required setup |
|--------|---------------|
| Azure CLI | `az login` |
| Service principal | `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID` env vars |
| Managed Identity | Automatic on Azure VMs / AKS / App Service |
| Workload Identity | Automatic in AKS with OIDC |

**Minimal local setup:**

```bash
export AZURE_STORAGE_ACCOUNT_URL=https://myaccount.blob.core.windows.net
az login
bsp deploy my-preset --container bsp-artifacts
```

**Service principal (CI):**

```bash
export AZURE_CLIENT_ID=...
export AZURE_CLIENT_SECRET=...
export AZURE_TENANT_ID=...
export AZURE_STORAGE_ACCOUNT_URL=https://myaccount.blob.core.windows.net
bsp deploy my-preset --container bsp-artifacts
```

### AWS

Credentials are resolved using the standard **boto3 credential chain**:

1. Environment variables: `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`
2. Shared credentials file: `~/.aws/credentials` (set up with `aws configure`)
3. AWS config file: `~/.aws/config`
4. IAM role (EC2 instance profile, ECS task role, Lambda execution role)

**Minimal local setup:**

```bash
aws configure        # interactive prompts for key, secret, region
bsp deploy my-preset --provider aws --bucket my-s3-bucket
```

**Environment variables (CI):**

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=eu-west-1
bsp deploy my-preset --provider aws --bucket my-s3-bucket
```

---

## CLI Reference

### `bsp deploy`

Upload artifacts from a previous build to cloud storage.

```
bsp deploy <bsp_name> [OPTIONS]
bsp deploy --device <d> --release <r> [--feature <f>] [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--provider PROVIDER` | Override provider: `azure` or `aws` |
| `--container CONTAINER` / `--bucket CONTAINER` | Override Azure container or AWS bucket name |
| `--prefix PREFIX` | Override remote path prefix template |
| `--pattern PATTERN` | Override glob patterns (repeatable; replaces registry config) |
| `--archive-name NAME` | Bundle artifacts into a single archive with this name (supports `{device}`, `{release}`, `{distro}`, `{vendor}`, `{date}`, `{datetime}`) |
| `--archive-format FORMAT` | Archive format: `tar.gz` (default), `tar.bz2`, `tar.xz`, `zip` |
| `--dry-run` | List what would be uploaded without uploading (no credentials needed) |

**Examples:**

```bash
# Deploy using registry settings
bsp deploy poky-qemuarm64-scarthgap

# Dry run – see what would be uploaded
bsp deploy poky-qemuarm64-scarthgap --dry-run

# Override container at runtime
bsp deploy poky-qemuarm64-scarthgap --container my-other-container

# Deploy to AWS with a custom prefix
bsp deploy poky-qemuarm64-scarthgap \
    --provider aws \
    --bucket my-s3-bucket \
    --prefix "builds/{device}/{release}/{date}"

# Upload only *.wic.gz files
bsp deploy poky-qemuarm64-scarthgap --pattern "**/*.wic.gz"

# Deploy by components (no preset required)
bsp deploy --device qemuarm64 --release scarthgap --container bsp-artifacts
```

### `bsp build --deploy`

Deploy artifacts automatically after a successful build.  All `--deploy-*`
flags mirror the `bsp deploy` options.

```
bsp build <bsp_name> --deploy [--deploy-provider PROVIDER]
    [--deploy-container CONTAINER] [--deploy-prefix PREFIX]
```

| Option | Description |
|--------|-------------|
| `--deploy` | Deploy artifacts after a successful build |
| `--deploy-provider PROVIDER` | Override storage provider |
| `--deploy-container CONTAINER` | Override container or bucket name |
| `--deploy-prefix PREFIX` | Override path prefix template |
| `--deploy-archive-name NAME` | Bundle artifacts into a single archive with this name (supports `{device}`, `{release}`, `{distro}`, `{vendor}`, `{date}`, `{datetime}`) |
| `--deploy-archive-format FORMAT` | Archive format: `tar.gz` (default), `tar.bz2`, `tar.xz`, `zip` |

**Examples:**

```bash
# Build and deploy in one step
bsp build poky-qemuarm64-scarthgap --deploy

# Build and deploy to a specific AWS bucket
bsp build poky-qemuarm64-scarthgap \
    --deploy \
    --deploy-provider aws \
    --deploy-container my-s3-bucket
```

---

## Dry-run mode

`--dry-run` lists all artifacts that would be uploaded and where they would go,
without performing any uploads and **without requiring cloud credentials or
installed cloud SDKs**.

```bash
bsp deploy poky-qemuarm64-scarthgap --dry-run
```

Example output:

```
[dry-run] Would upload 3 artifact(s):
  core-image-minimal-qemuarm64.rootfs.wic.gz → dry-run:qemu/qemuarm64/scarthgap/2025-03-15/core-image-minimal-qemuarm64.rootfs.wic.gz
  core-image-minimal-qemuarm64.rootfs.tar.bz2 → dry-run:qemu/qemuarm64/scarthgap/2025-03-15/core-image-minimal-qemuarm64.rootfs.tar.bz2
  manifest.json → dry-run:qemu/qemuarm64/scarthgap/2025-03-15/manifest.json
```

---

## Artifact manifest

When `include_manifest: true` (default), a `manifest.json` file is uploaded
alongside the artifacts.  It contains:

```json
{
  "schema_version": "1",
  "generated_at": "2025-03-15T14:30:22+00:00",
  "provider": "azure",
  "build": {
    "device": "qemuarm64",
    "release": "scarthgap",
    "distro": "poky",
    "vendor": "qemu"
  },
  "artifacts": [
    {
      "name": "core-image-minimal-qemuarm64.rootfs.wic.gz",
      "remote_url": "https://myaccount.blob.core.windows.net/bsp-artifacts/qemu/qemuarm64/scarthgap/2025-03-15/core-image-minimal-qemuarm64.rootfs.wic.gz",
      "size_bytes": 35651584,
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    }
  ],
  "total_size_bytes": 35651584
}
```

---

## Partial failures

If an individual file upload fails, the tool **continues uploading the
remaining files** and reports a summary at the end:

```
Uploaded 2 artifact(s):
  core-image-minimal-qemuarm64.rootfs.tar.bz2 → https://...
  manifest.json → https://...

WARNING: 1 artifact(s) failed to upload:
  core-image-minimal-qemuarm64.rootfs.wic.gz: [Errno 32] Broken pipe
```

The process exits with code 0 when at least one file succeeded, or 1 when all
uploads fail.

---

## Python API

```python
from bsp import BspManager

manager = BspManager("bsp-registry.yaml")
manager.initialize()

# Dry-run deploy for a preset
result = manager.deploy_bsp("poky-qemuarm64-scarthgap", dry_run=True)
print(f"Would upload {result.success_count} artifact(s)")

# Deploy with runtime overrides
result = manager.deploy_bsp(
    "poky-qemuarm64-scarthgap",
    deploy_overrides={
        "provider": "aws",
        "container": "my-s3-bucket",
        "prefix": "builds/{device}/{release}/{date}",
    },
)
for artifact in result.artifacts:
    print(f"  {artifact.local_path.name} → {artifact.remote_url}")
    print(f"  sha256: {artifact.sha256}")

# Deploy by components
result = manager.deploy_by_components(
    device_slug="qemuarm64",
    release_slug="scarthgap",
    deploy_overrides={"container": "bsp-artifacts"},
)

# Use the lower-level deployer + storage backend directly
from bsp.storage import create_backend
from bsp.deployer import ArtifactDeployer
from bsp.models import ArchiveConfig, DeployConfig

config = DeployConfig(
    provider="azure",
    container="bsp-artifacts",
    prefix="{device}/{release}/{date}",
    patterns=["**/*.wic.gz"],
    artifact_dirs=["tmp/deploy/images"],
    archive=ArchiveConfig(
        name="firmware-{device}-{release}-{date}",
        format="tar.gz",
    ),
)
backend = create_backend("azure", container_name="bsp-artifacts")
deployer = ArtifactDeployer(config, backend)

result = deployer.deploy(
    build_path="build/poky-qemuarm64-scarthgap",
    device="qemuarm64",
    release="scarthgap",
    distro="poky",
    vendor="qemu",
)
print(deployer.generate_manifest(result, device="qemuarm64", release="scarthgap"))
```

---

## CI/CD integration

### GitHub Actions – Azure

```yaml
name: Build and Deploy BSP

on:
  push:
    branches: [main]

jobs:
  build-deploy:
    runs-on: ubuntu-latest
    permissions:
      id-token: write   # required for OIDC / Workload Identity federation
      contents: read

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install bsp-registry-tools with Azure support
        run: pip install "bsp-registry-tools[azure]"

      - name: Azure Login (OIDC)
        uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}

      - name: Build BSP
        run: bsp build poky-qemuarm64-scarthgap

      - name: Deploy artifacts
        env:
          AZURE_STORAGE_ACCOUNT_URL: ${{ secrets.AZURE_STORAGE_ACCOUNT_URL }}
        run: |
          bsp deploy poky-qemuarm64-scarthgap \
            --container bsp-artifacts \
            --prefix "ci/{device}/{release}/${{ github.sha }}"
```

### GitHub Actions – AWS

```yaml
name: Build and Deploy BSP (AWS)

on:
  push:
    branches: [main]

jobs:
  build-deploy:
    runs-on: ubuntu-latest
    permissions:
      id-token: write   # required for OIDC
      contents: read

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install bsp-registry-tools with AWS support
        run: pip install "bsp-registry-tools[aws]"

      - name: Configure AWS Credentials (OIDC)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN }}
          aws-region: eu-west-1

      - name: Build BSP
        run: bsp build poky-qemuarm64-scarthgap

      - name: Deploy artifacts
        run: |
          bsp deploy poky-qemuarm64-scarthgap \
            --provider aws \
            --bucket my-bsp-artifacts \
            --prefix "ci/{device}/{release}/${{ github.sha }}"
```
