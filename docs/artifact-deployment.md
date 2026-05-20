# Artifact Deployment Guide

`bsp-registry-tools` can upload Yocto/Isar build artifacts produced by
`bsp build` to **Azure Blob Storage** (default) or **AWS S3** after the build
completes.  It also supports uploading and restoring **Yocto build caches**
(`DL_DIR` and `SSTATE_DIR`) so team members and CI agents can seed their local
caches without running a full build.

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
  - [Yocto cache upload](#yocto-cache-upload)
- [Authentication](#authentication)
  - [Azure](#azure)
  - [AWS](#aws)
- [CLI Reference](#cli-reference)
  - [`bsp deploy`](#bsp-deploy)
  - [`bsp build --deploy`](#bsp-build---deploy)
  - [`bsp gather`](#bsp-gather)
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

When `yocto_cache` is enabled, the tool also packs the Yocto download cache
(`DL_DIR`) and/or the shared-state cache (`SSTATE_DIR`) as `tar.gz` archives
and uploads them under a `cache/` sub-directory of the same prefix.

`bsp gather` downloads previously deployed artifacts.  With `--gather-cache` it
also downloads and extracts the cache archives back to the configured local
directories — no full build needed to warm up the cache on a fresh machine.

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

# Also upload Yocto caches (DL_DIR / SSTATE_DIR) after a build
bsp deploy poky-qemuarm64-scarthgap --container bsp-artifacts --deploy-cache

# Download artifacts from cloud storage
bsp gather poky-qemuarm64-scarthgap --dest-dir ./downloads

# Download artifacts AND restore Yocto caches
bsp gather poky-qemuarm64-scarthgap \
    --dest-dir ./downloads \
    --gather-cache \
    --cache-downloads-dir /mnt/yocto/downloads \
    --cache-sstate-dir /mnt/yocto/sstate

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
| `yocto_cache`      | object (opt.) | —       | Upload / restore Yocto DL_DIR / SSTATE_DIR caches. See [Yocto cache upload](#yocto-cache-upload). |

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

## Yocto cache upload

Enable `yocto_cache` in your `deploy:` block to also upload the Yocto build
caches (`DL_DIR` downloads cache and `SSTATE_DIR` shared-state cache) to the
same cloud prefix.  Cache upload is **opt-in** (disabled by default) and has
no impact on the regular artifact upload pipeline.

```yaml
deploy:
  provider: azure
  container: bsp-artifacts
  prefix: "{vendor}/{device}/{release}/{date}"

  yocto_cache:
    enabled: true          # master switch — must be true to upload anything
    downloads: true        # upload DL_DIR  (default: true)
    sstate: true           # upload SSTATE_DIR (default: true)
    # Optional: hard-code paths instead of using DL_DIR / SSTATE_DIR env vars
    # downloads_path: /mnt/yocto/downloads
    # sstate_path: /mnt/yocto/sstate
```

### How it works

1. **Deploy** – the tool reads `DL_DIR` / `SSTATE_DIR` from the environment
   (or from `downloads_path` / `sstate_path` if set in the registry).  Each
   enabled cache directory is packed into a `tar.gz` archive and uploaded under
   `{prefix}/cache/`:

   ```
   {prefix}/cache/downloads.tar.gz
   {prefix}/cache/sstate.tar.gz
   ```

   Cache metadata (remote URL, size, SHA-256) is recorded in the
   `yocto_cache` section of `manifest.json`.

2. **Gather** – run `bsp gather --gather-cache` to download and extract the
   cache archives back to the local filesystem.  A missing cache archive is
   treated as a soft warning — the rest of the gather succeeds normally.

### `yocto_cache` fields

| Field            | Type          | Default | Description |
|------------------|---------------|---------|-------------|
| `enabled`        | bool          | `false` | Master switch.  Must be `true` to upload any caches. |
| `downloads`      | bool          | `true`  | Include `DL_DIR` in the upload / restore. |
| `sstate`         | bool          | `true`  | Include `SSTATE_DIR` in the upload / restore. |
| `downloads_path` | string (opt.) | —       | Override the local `DL_DIR` path. Falls back to the `DL_DIR` environment variable. |
| `sstate_path`    | string (opt.) | —       | Override the local `SSTATE_DIR` path. Falls back to the `SSTATE_DIR` environment variable. |

### CLI flags

**Upload caches with `bsp deploy`:**

```bash
# Upload artifacts + DL_DIR + SSTATE_DIR
bsp deploy my-preset --deploy-cache

# Upload artifacts + DL_DIR only (skip sstate)
bsp deploy my-preset --deploy-cache --no-deploy-cache-sstate

# Upload artifacts + SSTATE_DIR only (skip downloads)
bsp deploy my-preset --deploy-cache --no-deploy-cache-downloads

# Same flags work with bsp build --deploy
bsp build my-preset --deploy --deploy-cache
```

**Restore caches with `bsp gather`:**

```bash
# Download artifacts + restore both caches to default dirs
bsp gather my-preset --gather-cache

# Specify exact restore paths
bsp gather my-preset \
    --gather-cache \
    --cache-downloads-dir /mnt/yocto/downloads \
    --cache-sstate-dir /mnt/yocto/sstate

# Dry run – see what would be downloaded/restored
bsp gather my-preset --dry-run --gather-cache
```

> **Note**: `bsp gather --gather-cache` uses the `DL_DIR` and `SSTATE_DIR`
> environment variables as default restore destinations (same as the deploy
> side).  Explicit `--cache-downloads-dir` / `--cache-sstate-dir` flags take
> priority.  If neither is set, archives are extracted into `downloads/` and
> `sstate/` sub-directories inside `--dest-dir`.

### Manifest with cache entries

When caches are uploaded the `manifest.json` gains a `yocto_cache` section:

```json
{
  "schema_version": "1",
  "artifacts": [ ... ],
  "yocto_cache": {
    "downloads": {
      "name": "downloads.tar.gz",
      "remote_url": "https://<account>.blob.core.windows.net/bsp-artifacts/acme/myboard/scarthgap/2025-01-15/cache/downloads.tar.gz",
      "size_bytes": 2147483648,
      "sha256": "..."
    },
    "sstate": {
      "name": "sstate.tar.gz",
      "remote_url": "https://...",
      "size_bytes": 5368709120,
      "sha256": "..."
    }
  }
}
```

`bsp gather --gather-cache` uses this section to locate the correct archive
URLs.  Old manifests that lack the `yocto_cache` section are handled
gracefully — the gatherer falls back to the heuristic
`{prefix}/cache/{type}.tar.gz` path.

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
| `--deploy-cache` | Also upload Yocto DL_DIR / SSTATE_DIR caches |
| `--no-deploy-cache-downloads` | Skip uploading the DL_DIR downloads cache (use with `--deploy-cache`) |
| `--no-deploy-cache-sstate` | Skip uploading the SSTATE_DIR sstate cache (use with `--deploy-cache`) |
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

# Deploy artifacts + Yocto caches
bsp deploy poky-qemuarm64-scarthgap --deploy-cache
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
| `--deploy-cache` | Also upload Yocto DL_DIR / SSTATE_DIR caches after a successful build |
| `--no-deploy-cache-downloads` | Skip uploading the DL_DIR downloads cache |
| `--no-deploy-cache-sstate` | Skip uploading the SSTATE_DIR sstate cache |

**Examples:**

```bash
# Build and deploy in one step
bsp build poky-qemuarm64-scarthgap --deploy

# Build and deploy to a specific AWS bucket
bsp build poky-qemuarm64-scarthgap \
    --deploy \
    --deploy-provider aws \
    --deploy-container my-s3-bucket

# Build, deploy artifacts and caches in one step
bsp build poky-qemuarm64-scarthgap --deploy --deploy-cache
```

### `bsp gather`

Download previously deployed artifacts from cloud storage.

```
bsp gather <bsp_name> [OPTIONS]
bsp gather --device <d> --release <r> [--feature <f>] [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--provider PROVIDER` | Override provider: `azure` or `aws` |
| `--container CONTAINER` / `--bucket CONTAINER` | Override Azure container or AWS bucket name |
| `--prefix PREFIX` | Override remote path prefix template |
| `--dest-dir PATH` | Local directory to write artifacts into |
| `--date DATE` | Date override for `{date}` placeholder (`YYYY-MM-DD`) |
| `--gather-cache` | Also restore Yocto caches from cloud storage if available |
| `--cache-downloads-dir PATH` | Local directory to restore the DL_DIR cache into |
| `--cache-sstate-dir PATH` | Local directory to restore the SSTATE_DIR cache into |
| `--dry-run` | List what would be downloaded without downloading (no credentials needed) |

**Examples:**

```bash
# Download latest artifacts for a preset
bsp gather poky-qemuarm64-scarthgap --dest-dir ./artifacts

# Download artifacts from a specific date
bsp gather poky-qemuarm64-scarthgap --dest-dir ./artifacts --date 2025-01-15

# Download artifacts + restore Yocto caches
bsp gather poky-qemuarm64-scarthgap \
    --dest-dir ./artifacts \
    --gather-cache

# Restore caches to explicit directories
bsp gather poky-qemuarm64-scarthgap \
    --gather-cache \
    --cache-downloads-dir /mnt/yocto/downloads \
    --cache-sstate-dir /mnt/yocto/sstate

# Dry run
bsp gather poky-qemuarm64-scarthgap --dry-run --gather-cache
```

---

## Dry-run mode

`--dry-run` lists all artifacts that would be uploaded and where they would go,
without performing any uploads and **without requiring cloud credentials or
installed cloud SDKs**.

```bash
bsp deploy poky-qemuarm64-scarthgap --dry-run
bsp deploy poky-qemuarm64-scarthgap --dry-run --deploy-cache
```

Example output:

```
[dry-run] Would upload 3 artifact(s):
  core-image-minimal-qemuarm64.rootfs.wic.gz → dry-run:qemu/qemuarm64/scarthgap/2025-03-15/core-image-minimal-qemuarm64.rootfs.wic.gz
  core-image-minimal-qemuarm64.rootfs.tar.bz2 → dry-run:qemu/qemuarm64/scarthgap/2025-03-15/core-image-minimal-qemuarm64.rootfs.tar.bz2
  manifest.json → dry-run:qemu/qemuarm64/scarthgap/2025-03-15/manifest.json

[dry-run] Would upload 2 Yocto cache archive(s):
  downloads: downloads.tar.gz → dry-run:qemu/qemuarm64/scarthgap/2025-03-15/cache/downloads.tar.gz
  sstate: sstate.tar.gz → dry-run:qemu/qemuarm64/scarthgap/2025-03-15/cache/sstate.tar.gz
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
  "total_size_bytes": 35651584,
  "yocto_cache": {
    "downloads": {
      "name": "downloads.tar.gz",
      "remote_url": "https://myaccount.blob.core.windows.net/bsp-artifacts/qemu/qemuarm64/scarthgap/2025-03-15/cache/downloads.tar.gz",
      "size_bytes": 2147483648,
      "sha256": "..."
    },
    "sstate": {
      "name": "sstate.tar.gz",
      "remote_url": "https://myaccount.blob.core.windows.net/bsp-artifacts/qemu/qemuarm64/scarthgap/2025-03-15/cache/sstate.tar.gz",
      "size_bytes": 5368709120,
      "sha256": "..."
    }
  }
}
```

The `yocto_cache` section is only present when caches were uploaded.

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

# Gather artifacts
result = manager.gather_bsp(
    "poky-qemuarm64-scarthgap",
    dest_dir="./artifacts",
)
print(f"Downloaded {result.total_count} artifact(s)")

# Gather artifacts + restore Yocto caches
result = manager.gather_bsp(
    "poky-qemuarm64-scarthgap",
    dest_dir="./artifacts",
    gather_cache=True,
    cache_downloads_dest="/mnt/yocto/downloads",
    cache_sstate_dest="/mnt/yocto/sstate",
)
print(f"Restored {len(result.cache_artifacts)} cache(s)")

# Use the lower-level deployer + storage backend directly
from bsp.storage import create_backend
from bsp.deployer import ArtifactDeployer
from bsp.models import ArchiveConfig, DeployConfig, YoctoCacheConfig

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
    yocto_cache=YoctoCacheConfig(
        enabled=True,
        downloads=True,
        sstate=True,
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
    downloads_path="/mnt/yocto/downloads",
    sstate_path="/mnt/yocto/sstate",
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

      - name: Deploy artifacts (+ caches)
        env:
          AZURE_STORAGE_ACCOUNT_URL: ${{ secrets.AZURE_STORAGE_ACCOUNT_URL }}
        run: |
          bsp deploy poky-qemuarm64-scarthgap \
            --container bsp-artifacts \
            --prefix "ci/{device}/{release}/${{ github.sha }}" \
            --deploy-cache
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

      - name: Deploy artifacts (+ caches)
        run: |
          bsp deploy poky-qemuarm64-scarthgap \
            --provider aws \
            --bucket my-bsp-artifacts \
            --prefix "ci/{device}/{release}/${{ github.sha }}" \
            --deploy-cache
```


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
