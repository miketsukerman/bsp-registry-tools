# bsp-registry-tools

Python tools to build, fetch, and work with Yocto-based BSPs using the [KAS](https://kas.readthedocs.io/) build system.

## Overview

`bsp-registry-tools` provides a command-line interface and Python API for managing Advantech Board Support Packages (BSPs). It uses YAML-based registry files to define BSP configurations, build environments, and Docker containers, making reproducible Yocto builds straightforward.

### Key Features

- 📋 **BSP registry management** via YAML configuration files
- 🌐 **Automatic remote registry fetching** — clone/update a remote registry with no manual setup
- 🐳 **Docker container support** for reproducible build environments
- 🔧 **KAS integration** for Yocto-based builds (`kas`, `kas-container`)
- 🖥️ **Interactive shell** access to build environments
- 🔄 **Environment variable expansion** (`$ENV{VAR}` syntax)
- 📤 **Configuration export** for sharing and archiving build configs
- ✅ **Comprehensive validation** of configurations before building
- 📂 **Registry splitting** — compose a registry from multiple files using the `include` directive
- 🌍 **HTTP server mode** — expose the full BSP registry via REST and GraphQL APIs
- ☁️ **Cloud artifact deployment** — upload Yocto build artifacts to Azure Blob Storage or AWS S3 with `bsp deploy`

## Installation

### From PyPI

```bash
pip install bsp-registry-tools
```

To also install the optional HTTP server dependencies:

```bash
pip install "bsp-registry-tools[server]"
```

### From Source

```bash
git clone https://github.com/Advantech-EECC/bsp-registry-tools.git
cd bsp-registry-tools
pip install .

# With server extras:
pip install ".[server]"
```

### Dependencies

- Python 3.8+
- [PyYAML](https://pyyaml.org/) >= 6.0
- [dacite](https://github.com/konradhalas/dacite) >= 1.6.0
- [kas](https://kas.readthedocs.io/) >= 4.7
- [colorama](https://github.com/tartley/colorama) >= 0.4.6

**Optional — server mode** (`pip install bsp-registry-tools[server]`):

- [FastAPI](https://fastapi.tiangolo.com/) >= 0.100.0
- [uvicorn](https://www.uvicorn.org/) >= 0.23.0
- [strawberry-graphql](https://strawberry.rocks/) >= 0.200.0

#### Optional extras for cloud deployment

Cloud SDK dependencies are optional and only needed if you use `bsp deploy`:

```bash
# Azure Blob Storage support
pip install "bsp-registry-tools[azure]"

# AWS S3 support
pip install "bsp-registry-tools[aws]"

# Both providers
pip install "bsp-registry-tools[deploy]"
```

## Quick Start

### Zero-Config Usage (Remote Registry)

If you have no local registry file, `bsp` automatically clones the default
[Advantech BSP registry](https://github.com/Advantech-EECC/bsp-registry) into
`~/.cache/bsp/registry` and keeps it up-to-date on every run:

```bash
# First run: clones the registry, then lists BSPs
bsp list

# Subsequent runs: pulls latest changes, then lists BSPs
bsp list

# Skip the network update (useful offline or in CI)
bsp --no-update list

# Use a different remote or branch
bsp --remote https://github.com/my-org/bsp-registry.git --branch dev list
```

### Manual Registry Usage

### 1. Create a BSP Registry File

Create a `bsp-registry.yaml` or `bsp-registry.yml` file (see [examples/bsp-registry.yaml](examples/bsp-registry.yaml)):

```yaml
specification:
  version: "2.0"

environment:
  variables:
    - name: "GITCONFIG_FILE"
      value: "$ENV{HOME}/.gitconfig"

# Named environment: container + variables used for all builds by default
environments:
  default:
    container: "debian-bookworm"
    variables:
      - name: "DL_DIR"
        value: "$ENV{HOME}/yocto-cache/downloads"
      - name: "SSTATE_DIR"
        value: "$ENV{HOME}/yocto-cache/sstate"

containers:
  debian-bookworm:
    image: "bsp/registry/debian/kas:5.1"
    file: Dockerfile
    args:
      - name: "DISTRO"
        value: "debian-bookworm"
      - name: "KAS_VERSION"
        value: "5.1"

registry:
  # frameworks and distro define the build system hierarchy (optional but recommended)
  frameworks:
    - slug: yocto
      description: "Yocto Project build system"
      vendor: "Yocto Project"
      includes:
        - kas/yocto/yocto.yaml

  distro:
    - slug: poky
      description: "Poky (Yocto Project reference distro)"
      framework: yocto     # links distro to a framework for feature compatibility checks
      includes:
        - kas/yocto/distro/poky.yaml

  # devices define hardware targets (KAS includes listed flat, no nested build: block)
  devices:
    - slug: qemuarm64
      description: "QEMU ARM64 (emulated)"
      vendor: qemu
      soc_vendor: arm
      includes:
        - kas/qemu/qemuarm64.yaml

  releases:
    - slug: scarthgap
      description: "Yocto 5.0 LTS (Scarthgap)"
      distro: poky
      yocto_version: "5.0"
      includes:
        - kas/scarthgap.yaml

  # bsp presets name a device + release + features combination.
  # Use "releases" (plural) to target multiple releases without repetition:
  bsp:
    - name: poky-qemuarm64
      description: "Poky QEMU ARM64"
      device: qemuarm64
      releases: [scarthgap, styhead]   # expands to poky-qemuarm64-scarthgap / poky-qemuarm64-styhead
      features: []
      build:
        container: "debian-bookworm"
    # Single-release entry (backward compatible):
    - name: poky-qemuarm64-scarthgap-ota
      description: "Poky QEMU ARM64 Scarthgap with OTA"
      device: qemuarm64
      release: scarthgap
      features: [ota]
      build:
        container: "debian-bookworm"
        path: build/poky-qemuarm64-scarthgap-ota
```

### 2. List Available BSPs

```bash
# With an explicit registry file
bsp --registry bsp-registry.yaml list

# Or simply if bsp-registry.yaml (or bsp-registry.yml) is in the current directory
bsp list
```

```
- poky-qemuarm64-scarthgap: Poky QEMU ARM64 Scarthgap (Yocto 5.0 LTS)
```

### 3. Build a BSP

```bash
bsp build poky-qemuarm64-scarthgap
```

### 4. Enter Interactive Shell

```bash
bsp shell poky-qemuarm64-scarthgap
```

## CLI Reference

```
usage: bsp [-h] [--verbose] [--registry REGISTRY] [--no-color]
           [--remote REMOTE] [--branch BRANCH] [--update | --no-update]
           [--local]
           {build,list,containers,tree,export,shell,server,deploy} ...

Advantech Board Support Package Registry

positional arguments:
  {build,list,containers,tree,export,shell,server,deploy}
                        Command to execute
    build               Build an image for BSP
    list                List available BSPs
    containers          List available containers
    tree                Display a tree view of the BSP registry
    export              Export BSP configuration
    shell               Enter interactive shell for BSP
    server              Start a GraphQL / REST HTTP server
    deploy              Deploy build artifacts to cloud storage

options:
  -h, --help            show this help message and exit
  --verbose, -v         Verbose output
  --registry REGISTRY, -r REGISTRY
                        BSP Registry file (local path; skips remote fetch)
  --no-color            Disable colored output
  --remote REMOTE       Remote registry git URL
                        (default: https://github.com/Advantech-EECC/bsp-registry.git)
  --branch BRANCH       Remote registry branch (default: main)
  --update              Update the cached registry clone before use (default)
  --no-update           Skip updating the cached registry clone
  --local               Force local registry lookup only (do not use remote)
```

### Registry Resolution Priority

The tool determines which registry file to use in the following order:

1. **`--registry <path>`** — explicit local file, remote fetch is skipped entirely.
2. **`--local`** — use `./bsp-registry.yaml` or `./bsp-registry.yml` in the current directory; no network access.
3. **`bsp-registry.yaml` exists in the current directory** — auto-detect (preferred extension).
4. **`bsp-registry.yml` exists in the current directory** — auto-detect (alternate extension).
5. **Otherwise** — clone/update the remote registry into `~/.cache/bsp/registry` via `RegistryFetcher`.

### Global Options

| Option | Description |
|--------|-------------|
| `--verbose`, `-v` | Enable verbose/debug output |
| `--registry REGISTRY`, `-r REGISTRY` | Path to BSP registry file (local override) |
| `--no-color` | Disable colored output |
| `--remote REMOTE` | Remote registry git URL (default: Advantech BSP registry) |
| `--branch BRANCH` | Remote registry branch (default: `main`) |
| `--update` / `--no-update` | Update cached registry clone before use (default: update) |
| `--local` | Force local lookup; never contact remote |

### Commands

#### `list` — List available BSPs

```bash
bsp list
bsp --registry my-registry.yaml list
```

#### `containers` — List available container definitions

```bash
bsp containers
```

#### `tree` — Display a tree view of the BSP registry

```bash
bsp tree
bsp tree --full
bsp tree --compact
bsp --no-color tree
bsp --registry my-registry.yaml tree
```

Renders the full registry as a colored ASCII tree, grouped into sections:
**Frameworks**, **Distros**, **Releases** (with vendor overrides), **Devices**,
**Features** (with vendor overrides in full mode), and **BSP Presets** (with device, release, and feature details).
Use `--no-color` to disable colors (e.g. for scripts or log files).

| Option | Description |
|--------|-------------|
| `--full` | Show full details including includes lists, vendor overrides for releases and features, and override slugs for presets |
| `--compact` | Show compact output with names/slugs only (no sub-items) |

**Example output (`bsp tree`):**

```
BSP Registry
├── Frameworks (1)
│   └── yocto: Yocto Project (vendor: yocto)
├── Distros (1)
│   └── poky: Poky (vendor: yocto, framework: yocto)
├── Releases (1)
│   └── scarthgap: Yocto 5.0 LTS [Yocto 5.0]
│       ├── distro: poky
│       └── vendor override: advantech (sub-releases: imx-6.6.53)
├── Devices (2)
│   ├── qemu-arm64: QEMU ARM64 (vendor: qemu, soc_vendor: arm)
│   └── imx8qm: i.MX8 QM (vendor: advantech, soc_vendor: nxp, soc_family: imx8)
├── Features (2)
│   ├── ota: OTA Update
│   └── secure-boot: Secure Boot [requires vendor: ['advantech']]
└── BSP Presets (2)
    ├── qemu-arm64-scarthgap: QEMU ARM64 Scarthgap
    │   └── device: qemu-arm64  release: scarthgap
    └── imx8qm-scarthgap: i.MX8 QM Scarthgap
        ├── device: imx8qm  release: scarthgap
        ├── vendor release: imx-6.6.53
        └── features: ota, secure-boot
```

**Example output (`bsp tree --full`):**

In `--full` mode all includes lists are expanded and vendor overrides for both releases and features are shown as nested sub-trees:

```
BSP Registry
├── Releases (1)
│   └── scarthgap: Yocto 5.0 LTS [Yocto 5.0]
│       ├── distro: poky
│       ├── includes: kas/poky/scarthgap.yaml
│       └── vendor override: advantech (distro: fsl-imx-xwayland)
│           ├── includes: kas/yocto/vendors/advantech/scarthgap.yaml
│           └── vendor release: imx-6.6.53: Scarthgap for i.MX 6.6.53
│               └── includes:
│                   └── kas/yocto/vendors/advantech/nxp/imx-6.6.53.yaml
└── Features (1)
    └── rauc: Enable RAUC support in the Yocto image [requires compatible_with: yocto]
        ├── includes:
        │   └── features/ota/rauc/rauc.yml
        └── vendor override: advantech
            ├── includes: features/ota/rauc/advantech-rauc.yml
            └── soc vendor: nxp
                ├── includes: features/ota/rauc/modular-bsp-ota-nxp.yml
                └── vendor release: imx-6.6.53: RAUC for i.MX 6.6.53
                    └── includes:
                        └── features/ota/rauc/rauc-imx-6.6.53.yml
```

#### `build` — Build a BSP image

```bash
bsp build <bsp_name> [--clean] [--checkout] [--deploy] [--deploy-provider PROVIDER] [--deploy-container CONTAINER] [--deploy-prefix PREFIX]
bsp build --device <device> --release <release> [--feature <feat>...] [--clean] [--checkout]
bsp build --all [--clean] [--keep-going] [--checkout]
```

| Option | Description |
|--------|-------------|
| `--clean` | Before building, remove previous build artefacts from `<build_path>/tmp/` while preserving `tmp/deploy/` (images and packages) and `tmp/log/` (build logs) to free disk space |
| `--checkout` | Validate configuration and checkout repos without building (fast, implies no Docker build) |
| `--all`, `-a` | Build every BSP preset defined in the registry one by one (sequential) |
| `--keep-going`, `-k` | When used with `--all`, continue building remaining presets after a failure and print a summary at the end (default: stop on first failure) |
| `--deploy` | Deploy artifacts to cloud storage after a successful build |
| `--deploy-provider PROVIDER` | Cloud storage provider: `azure` (default) or `aws` |
| `--deploy-container CONTAINER` | Azure container or AWS bucket name (overrides registry config) |
| `--deploy-prefix PREFIX` | Remote path prefix template (overrides registry config) |
| `--deploy-archive-name NAME` | Bundle artifacts into a single archive with this name before uploading (supports `{device}`, `{release}`, `{distro}`, `{vendor}`, `{date}`, `{datetime}`) |
| `--deploy-archive-format FORMAT` | Archive format: `tar.gz` (default), `tar.bz2`, `tar.xz`, `zip` |

**Examples:**

```bash
# Full build of a named preset
bsp build poky-qemuarm64-scarthgap

# Checkout/validate only (fast, no build)
bsp build poky-qemuarm64-scarthgap --checkout

# Clean before building to free disk space; removes tmp/work/ etc., keeps tmp/deploy/ and tmp/log/
bsp build poky-qemuarm64-scarthgap --clean

# Build by device + release (with optional features)
bsp build --device qemuarm64 --release scarthgap --feature ota

# Build all presets sequentially, stop on first failure (default)
bsp build --all

# Build all presets, continue past failures, clean before each build
bsp build --all --keep-going --clean

# Build and deploy artifacts to Azure automatically
bsp build poky-qemuarm64-scarthgap --deploy

# Build and deploy to a specific AWS bucket
bsp build poky-qemuarm64-scarthgap --deploy --deploy-provider aws --deploy-container my-s3-bucket
```

#### `shell` — Interactive shell in build environment

```bash
bsp shell <bsp_name> [--command COMMAND]
```

| Option | Description |
|--------|-------------|
| `--command COMMAND`, `-c COMMAND` | Execute a specific command instead of starting interactive shell |

**Examples:**

```bash
# Interactive shell
bsp shell poky-qemuarm64-scarthgap

# Execute single command
bsp shell poky-qemuarm64-scarthgap --command "bitbake core-image-minimal"
```

#### `export` — Export BSP configuration

```bash
bsp export <bsp_name> [--output OUTPUT]
```

| Option | Description |
|--------|-------------|
| `--output OUTPUT`, `-o OUTPUT` | Output file path (default: stdout) |

**Examples:**

```bash
# Print to stdout
bsp export poky-qemuarm64-scarthgap

# Save to file
bsp export poky-qemuarm64-scarthgap --output exported-config.yaml
```

#### `server` — Start an HTTP server (REST + GraphQL)

Starts a FastAPI-based HTTP server that exposes the full BSP registry via both a REST API and a GraphQL API.  Requires the `server` optional extras (`pip install "bsp-registry-tools[server]"`).

```bash
bsp server [--host HOST] [--port PORT] [--reload]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--host HOST` | `127.0.0.1` | Host address to bind to |
| `--port PORT` | `8080` | Port to listen on |
| `--reload` | — | Enable auto-reload on code changes (development mode) |

---

#### `deploy` — Upload build artifacts to cloud storage

Deploy Yocto build artifacts (images, SDKs) that were produced by `bsp build`
to Azure Blob Storage or AWS S3.

```bash
bsp deploy <bsp_name> [OPTIONS]
bsp deploy --device <d> --release <r> [--feature <f>] [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--provider PROVIDER` | Storage provider: `azure` (default) or `aws` |
| `--container CONTAINER`, `--bucket CONTAINER` | Azure container or AWS S3 bucket name |
| `--prefix PREFIX` | Remote path prefix template (supports `{device}`, `{release}`, `{distro}`, `{vendor}`, `{date}`, `{datetime}`) |
| `--pattern PATTERN` | Glob pattern for artifacts to upload (repeatable; overrides registry config) |
| `--archive-name NAME` | Bundle artifacts into a single archive with this name before uploading (supports `{device}`, `{release}`, `{distro}`, `{vendor}`, `{date}`, `{datetime}`) |
| `--archive-format FORMAT` | Archive format: `tar.gz` (default), `tar.bz2`, `tar.xz`, `zip` |
| `--dry-run` | List what would be uploaded without uploading (no credentials required) |

**Examples:**

```bash
# Start server on localhost:8080 (default)
bsp server

# Expose on all interfaces on port 9000
bsp server --host 0.0.0.0 --port 9000

# With a specific registry file
bsp --registry /path/to/bsp-registry.yaml server --host 0.0.0.0 --port 8080

# Development mode with auto-reload
bsp server --reload
```

Once started, the following interfaces are available:

| URL | Description |
|-----|-------------|
| `http://localhost:8080/docs` | Swagger / OpenAPI UI (REST) |
| `http://localhost:8080/redoc` | ReDoc UI (REST) |
| `http://localhost:8080/graphql` | GraphiQL interactive editor (GraphQL) |
| `http://localhost:8080/api/v1/…` | REST API endpoints |

## HTTP Server (REST + GraphQL)

The `bsp server` command exposes the entire BSP registry over HTTP.  Both a REST API and a GraphQL API are available simultaneously on the same port.

### Installation

```bash
pip install "bsp-registry-tools[server]"
```

### Starting the server

```bash
# Default: http://127.0.0.1:8080
bsp server

# Custom host/port
bsp server --host 0.0.0.0 --port 9000

# Using a specific registry file
bsp --registry /path/to/bsp-registry.yaml server --host 0.0.0.0 --port 8080
```

### REST API (`/api/v1/`)

#### Query endpoints (GET)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/bsp` | List all BSP presets |
| GET | `/api/v1/devices` | List all hardware devices |
| GET | `/api/v1/releases` | List all releases |
| GET | `/api/v1/releases?device=<slug>` | List releases compatible with a device |
| GET | `/api/v1/features` | List all optional features |
| GET | `/api/v1/distros` | List all distribution definitions |
| GET | `/api/v1/frameworks` | List all framework definitions |
| GET | `/api/v1/containers` | List all Docker container definitions |

**Example:**

```bash
curl http://localhost:8080/api/v1/devices
```

```json
[
  {
    "slug": "qemuarm64",
    "description": "QEMU ARM64 (emulated)",
    "vendor": "qemu",
    "soc_vendor": "arm",
    "soc_family": null,
    "includes": ["kas/qemu/qemuarm64.yaml"],
    "local_conf": []
  }
]
```

#### Action endpoints (POST)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/export` | Resolve and return a BSP config as YAML |
| POST | `/api/v1/build` | Trigger a BSP build (blocking) |
| POST | `/api/v1/shell` | Run a command inside the build container |

All action endpoints accept a JSON body with either `bsp_name` **or** both `device` + `release`:

```bash
# Export by preset name
curl -X POST http://localhost:8080/api/v1/export \
     -H "Content-Type: application/json" \
     -d '{"bsp_name": "poky-qemuarm64-scarthgap"}'

# Export by components
curl -X POST http://localhost:8080/api/v1/export \
     -H "Content-Type: application/json" \
     -d '{"device": "qemuarm64", "release": "scarthgap", "features": []}'

# Validate (checkout only) without building
curl -X POST http://localhost:8080/api/v1/build \
     -H "Content-Type: application/json" \
     -d '{"bsp_name": "poky-qemuarm64-scarthgap", "checkout_only": true}'
```

#### Interactive REST documentation

Navigate to **`http://localhost:8080/docs`** for the full Swagger / OpenAPI UI or **`http://localhost:8080/redoc`** for ReDoc.

### GraphQL API (`/graphql`)

Navigate to **`http://localhost:8080/graphql`** for the interactive GraphiQL editor.

#### Queries

```graphql
# List all devices
{ devices { slug description vendor socVendor } }

# List all BSP presets
{ bsp { name description device release features } }

# List releases compatible with a specific device
{ releases(device: "qemuarm64") { slug description yoctoVersion } }

# List features, distros, frameworks, and containers
{ features { slug description compatibleWith }
  distros { slug description framework }
  frameworks { slug vendor }
  containers { name image } }
```

#### Mutations

```graphql
# Export BSP config by preset name
mutation {
  exportBsp(bspName: "poky-qemuarm64-scarthgap") {
    yamlContent
  }
}

# Export by components
mutation {
  exportBsp(device: "qemuarm64", release: "scarthgap") {
    yamlContent
  }
}

# Validate (checkout only) without building
mutation {
  buildBsp(bspName: "poky-qemuarm64-scarthgap", checkoutOnly: true) {
    status
    message
  }
}

# Run a command in the build container
mutation {
  shellCommand(bspName: "poky-qemuarm64-scarthgap", command: "bitbake -e") {
    returnCode
    output
  }
}
```

### Python API — embedding the server

You can also embed the server directly in Python code:

```python
import uvicorn
from bsp.server import create_app

app = create_app(registry_path="/path/to/bsp-registry.yaml")
uvicorn.run(app, host="0.0.0.0", port=8080)
```

Or reuse an already-initialised `BspManager`:

```python
from bsp import BspManager
from bsp.server import create_app
import uvicorn

manager = BspManager("bsp-registry.yaml")
manager.initialize()

app = create_app(manager=manager)
uvicorn.run(app, host="0.0.0.0", port=8080)
```

---

# Deploy using registry-configured settings (Azure by default)
bsp deploy poky-qemuarm64-scarthgap

# Preview what would be uploaded without uploading
bsp deploy poky-qemuarm64-scarthgap --dry-run

# Deploy to an explicit Azure container
bsp deploy poky-qemuarm64-scarthgap --container bsp-artifacts

# Deploy to AWS S3
bsp deploy poky-qemuarm64-scarthgap --provider aws --bucket my-s3-bucket

# Deploy by components with a custom prefix
bsp deploy --device qemuarm64 --release scarthgap --prefix "builds/{vendor}/{device}/{date}"

# Upload only compressed image files
bsp deploy poky-qemuarm64-scarthgap --pattern "**/*.wic.gz"
```

**Authentication:**

| Provider | Authentication |
|----------|---------------|
| Azure | `AZURE_STORAGE_CONNECTION_STRING` env var, or `AZURE_STORAGE_ACCOUNT_URL` + `DefaultAzureCredential` (supports `az login`, service principal env vars, Managed Identity) |
| AWS | Standard boto3 credential chain: `~/.aws/credentials`, `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` env vars, IAM role, instance profile |

See [docs/artifact-deployment.md](docs/artifact-deployment.md) for full details, YAML configuration, and CI/CD integration examples.

## Registry Configuration Reference

The BSP registry is a YAML file following **schema v2.0**.  See [docs/registry-v2.md](docs/registry-v2.md) for the full reference.  For the HTTP server reference, see [docs/server.md](docs/server.md).  Key top-level sections:

### `specification`

```yaml
specification:
  version: "2.0"
```

### `include` (optional)

Split a large registry across multiple files using the `include` directive.
Paths are relative to the file that contains the directive.

```yaml
include:
  - devices/boards.yaml
  - releases/scarthgap.yaml
```

Each included file is merged before the root file's own content.  Lists
(e.g. `devices`, `releases`, `features`, `environment`) are concatenated; dicts
(e.g. `containers`, `environments`) are merged recursively; scalars use the root
file's value.
Included files can themselves contain further `include` directives, and
circular references are detected at load time.

See [docs/registry-v2.md](docs/registry-v2.md#include-optional) for full details.

### `environment`

Global build environment applied to all builds.  Groups `variables` (supports `$ENV{VAR_NAME}` expansion) and `copy` (file-copy entries executed inside the build environment before every build) under a single key.

```yaml
environment:
  variables:
    - name: "GITCONFIG_FILE"
      value: "$ENV{HOME}/.gitconfig"
    - name: "DL_DIR"
      value: "$ENV{HOME}/yocto-cache/downloads"
    - name: "SSTATE_DIR"
      value: "$ENV{HOME}/yocto-cache/sstate"
  copy:
    - scripts/global-setup.sh: build/
    - config/global.conf: build/conf/
```

Both `variables` and `copy` are optional.  Global copies are merged first, before named-environment and device copies.

### `environments`

Named environments bundle a container reference, environment variables, and optional file-copy entries together.  The special name `"default"` is used by any release that does not explicitly name an environment.

```yaml
environments:
  default:
    container: "debian-bookworm"
    variables:
      - name: "DL_DIR"
        value: "$ENV{HOME}/yocto-cache/downloads"
  isar-build:
    container: "isar-debian-trixie"
    variables:
      - name: "DL_DIR"
        value: "$ENV{HOME}/isar-cache/downloads"
    # Copy the QEMU run script into every Isar build directory
    copy:
      - isar/scripts/isar-runqemu.sh: build/
```

### `containers`

Docker container definitions for build environments:

```yaml
containers:
  debian-bookworm:
    image: "my-registry/debian/kas:5.1"
    file: Dockerfile
    args:
      - name: "DISTRO"
        value: "debian-bookworm"
      - name: "KAS_VERSION"
        value: "5.1"
  isar-container:
    image: "my-registry/isar/kas:5.1"
    file: Dockerfile.isar
    args: []
    privileged: true    # Run container in privileged mode (required for ISAR builds)
    runtime_args: "-p 2222:2222"  # Extra flags passed to the container engine
```

**Container fields:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `image` | string | — | Docker image name/tag |
| `file` | string | — | Path to Dockerfile for building the image |
| `args` | list | `[]` | Docker build arguments (`name`/`value` pairs) |
| `privileged` | boolean | `false` | Run container with elevated privileges. Required for ISAR builds. |
| `runtime_args` | string | — | Extra flags appended to the container engine `run` invocation (e.g. port-forwarding, `--device` access). Forwarded via `KAS_CONTAINER_ARGS`. |

### `registry.devices`

Hardware device/board definitions:

```yaml
registry:
  devices:
    - slug: qemuarm64
      description: "QEMU ARM64 (emulated)"
      vendor: qemu
      soc_vendor: arm
      includes:
        - kas/qemu/qemuarm64.yaml
```

### `registry.releases`

Yocto/Isar release definitions referencing a distro:

```yaml
registry:
  releases:
    - slug: scarthgap
      description: "Yocto 5.0 LTS (Scarthgap)"
      distro: poky
      yocto_version: "5.0"
      includes:
        - kas/scarthgap.yaml
```

### `registry.bsp`

Named presets — device + release(s) + optional features:

```yaml
registry:
  bsp:
    # Single-release preset (backward compatible)
    - name: poky-qemuarm64-scarthgap
      description: "Poky QEMU ARM64 Scarthgap (Yocto 5.0 LTS)"
      device: qemuarm64
      release: scarthgap
      features: []
      build:              # optional: override container and/or output path
        container: "debian-bookworm"
        path: build/poky-qemuarm64-scarthgap

    # Multi-release preset: use "releases" (plural) to avoid repeating the
    # same entry for every Yocto release.  The resolver expands this into one
    # preset per release, named "{name}-{release_slug}":
    #   poky-qemuarm64-scarthgap  (auto-composed path: build/poky-qemuarm64-scarthgap)
    #   poky-qemuarm64-styhead    (auto-composed path: build/poky-qemuarm64-styhead)
    - name: poky-qemuarm64
      description: "Poky QEMU ARM64"
      device: qemuarm64
      releases: [scarthgap, styhead]
      features: [systemd, debug]
      build:              # optional: container override (path is always auto-composed)
        container: "debian-bookworm"
```

> **Note**: `release` (singular) and `releases` (plural) are mutually exclusive.
> Exactly one must be specified per preset entry.

### `deploy` (optional)

Global cloud deployment configuration applied to all builds.  An individual
`BspPreset` can also include a `deploy:` block that overrides specific settings
for that preset (see [Per-preset override](#per-preset-deploy-override) below).

```yaml
deploy:
  provider: azure                                   # "azure" (default) or "aws"
  account_url: $ENV{AZURE_STORAGE_ACCOUNT_URL}      # Azure only; supports $ENV{} expansion
  container: bsp-artifacts                          # Azure container name
  # bucket: my-s3-bucket                           # AWS alternative to container
  prefix: "{vendor}/{device}/{release}/{date}"      # remote path prefix template
  patterns:                                         # glob patterns for files to upload
    - "**/*.wic.gz"
    - "**/*.wic.bz2"
    - "**/*.tar.bz2"
    - "**/*.ext4"
    - "**/*.sdimg"
  artifact_dirs:                                    # subdirs under build_path to search
    - tmp/deploy/images
    - tmp/deploy/sdk
  include_manifest: true                            # upload a JSON manifest of all artifacts
```

**Prefix template variables:**

| Variable | Value |
|----------|-------|
| `{device}` | Device slug |
| `{release}` | Release slug |
| `{distro}` | Effective distro slug |
| `{vendor}` | Device vendor slug |
| `{date}` | Build date in `YYYY-MM-DD` format |
| `{datetime}` | Build datetime in `YYYYMMDD-HHMMSS` format |

#### Per-preset deploy override

Add a `deploy:` block directly on a `BspPreset` to override specific global
deploy settings for that preset.  Only fields that differ from their default
values override the global config; other fields keep the global value.
CLI flags (`--provider`, `--container`, …) are applied last.

```yaml
deploy:                               # global: Azure, shared container
  provider: azure
  account_url: $ENV{AZURE_STORAGE_ACCOUNT_URL}
  container: bsp-artifacts

registry:
  bsp:
    # Uses global settings unchanged.
    - name: qemuarm64-scarthgap
      device: qemuarm64
      release: scarthgap
      features: []

    # Overrides only container and prefix; provider/account_url come from global.
    - name: imx8mp-adv-scarthgap-release
      device: imx8mp-adv
      release: scarthgap
      features: []
      deploy:
        container: imx8mp-release-artifacts         # ← override
        prefix: "release/{device}/{release}/{date}" # ← override
        patterns:
          - "**/*.wic.gz"                           # ← override

    # Switches to AWS entirely for this preset.
    - name: aws-build-scarthgap
      device: qemuarm64
      release: scarthgap
      features: []
      deploy:
        provider: aws                 # ← override: switch provider
        container: my-s3-bucket       # ← override: bucket name
```

See [docs/artifact-deployment.md](docs/artifact-deployment.md) for full details.

## KAS Configuration Files

KAS configuration files define Yocto layer repositories, machine settings, and build targets. See the [examples/kas/](examples/kas/) directory for reference configurations.

### QEMU Example Configurations

The `examples/` directory contains ready-to-use KAS configurations for QEMU targets:

| File | Description |
|------|-------------|
| `examples/kas/scarthgap.yaml` | Yocto Scarthgap (5.0 LTS) base configuration |
| `examples/kas/styhead.yaml` | Yocto Styhead (5.1) base configuration |
| `examples/kas/qemu/qemuarm64.yaml` | QEMU ARM64 machine configuration |
| `examples/kas/qemu/qemux86-64.yaml` | QEMU x86-64 machine configuration |
| `examples/kas/qemu/qemuarm.yaml` | QEMU ARM (32-bit) machine configuration |

### KAS File Structure

```yaml
header:
  version: 14
  includes:            # Optional: include other KAS files
    - base.yaml

distro: poky
machine: qemuarm64

target:
  - core-image-minimal

repos:
  poky:
    url: "https://git.yoctoproject.org/poky"
    commit: "abc123..."
    path: "layers/poky"
    layers:
      meta:
      meta-poky:

local_conf_header:
  my_config: |
    DISTRO_FEATURES += "x11"
```

## Python API

You can also use `bsp-registry-tools` as a Python library:

```python
from bsp import BspManager, EnvironmentManager, KasManager, RegistryFetcher

# Fetch registry from remote (clone on first call, pull on subsequent)
fetcher = RegistryFetcher()
registry_path = fetcher.fetch_registry(
    repo_url="https://github.com/Advantech-EECC/bsp-registry.git",
    branch="main",
    update=True,
)

# Load and manage BSP registry
manager = BspManager(str(registry_path))
manager.initialize()

# List BSPs programmatically
for bsp in manager.model.registry.bsp:
    print(f"{bsp.name}: {bsp.description}")

# Get a specific BSP
bsp = manager.get_bsp_by_name("poky-qemuarm64-scarthgap")

# Environment variable management with $ENV{} expansion
from bsp import EnvironmentVariable
env_vars = [
    EnvironmentVariable(name="DL_DIR", value="$ENV{HOME}/downloads"),
]
env_manager = EnvironmentManager(env_vars)
print(env_manager.get_value("DL_DIR"))  # Expanded path

# Use KasManager directly
kas = KasManager(
    kas_files=["kas/scarthgap.yaml", "kas/qemu/qemuarm64.yaml"],
    build_dir="build/my-bsp",
    use_container=False,
)
kas.validate_kas_files()
```

### Starting the HTTP server programmatically

```python
import uvicorn
from bsp.server import create_app

# Create and run the server (requires bsp-registry-tools[server])
app = create_app(registry_path="bsp-registry.yaml")
uvicorn.run(app, host="0.0.0.0", port=8080)

### Cloud Deployment API

```python
from bsp import BspManager

manager = BspManager("bsp-registry.yaml")
manager.initialize()

# Deploy artifacts from a preset build (dry-run)
result = manager.deploy_bsp("poky-qemuarm64-scarthgap", dry_run=True)
print(f"Would upload {result.success_count} artifact(s)")

# Deploy with overrides
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

# Deploy by components
result = manager.deploy_by_components(
    device_slug="qemuarm64",
    release_slug="scarthgap",
)

# Use the storage backend and deployer directly
from bsp.storage import create_backend
from bsp.deployer import ArtifactDeployer
from bsp.models import DeployConfig

config = DeployConfig(
    provider="azure",
    container="bsp-artifacts",
    prefix="{device}/{release}/{date}",
    patterns=["**/*.wic.gz"],
)
backend = create_backend("azure", container_name="bsp-artifacts", dry_run=True)
deployer = ArtifactDeployer(config, backend)
result = deployer.deploy("build/poky-qemuarm64-scarthgap", device="qemuarm64", release="scarthgap")
```

## Development

### Setup Development Environment

```bash
git clone https://github.com/Advantech-EECC/bsp-registry-tools.git
cd bsp-registry-tools
pip install -e ".[dev]"
```

### Running Tests

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run with coverage report
pytest --cov=bsp --cov-report=term-missing

# Run specific test class
pytest tests/test_bsp.py::TestEnvironmentManager -v
```

### Project Structure

```
bsp-registry-tools/
├── bsp/
│   ├── __init__.py           # Public API exports
│   ├── cli.py                # CLI entry point
│   ├── bsp_manager.py        # Main BSP coordinator
│   ├── registry_fetcher.py   # Remote registry clone/update
│   ├── kas_manager.py        # KAS build system integration
│   ├── environment.py        # Environment variable management
│   ├── path_resolver.py      # Path utilities
│   ├── models.py             # Dataclass models (v2.0 schema)
│   ├── resolver.py           # V2 resolver: device + release + features → ResolvedConfig
│   ├── utils.py              # YAML / Docker utilities
│   ├── exceptions.py         # Custom exceptions
│   └── server/               # Optional HTTP server (requires [server] extras)
│       ├── __init__.py       # Exports create_app
│       ├── app.py            # FastAPI application factory
│       ├── rest.py           # REST router (/api/v1/*)
│       ├── graphql_schema.py # Strawberry GraphQL schema
│       └── types.py          # Pydantic response models
│   ├── deployer.py           # ArtifactDeployer: collect & upload build artifacts
│   └── storage/              # Cloud storage backends
│       ├── __init__.py       # Exports CloudStorageBackend and create_backend()
│       ├── base.py           # Abstract CloudStorageBackend base class
│       ├── azure.py          # AzureStorageBackend (azure-storage-blob)
│       ├── aws.py            # AwsStorageBackend (boto3)
│       └── factory.py        # create_backend() factory function
├── pyproject.toml            # Package configuration
├── README.md                 # This file
├── LICENSE                   # Apache 2.0 License
├── docs/
│   ├── registry-v2.md        # Full v2.0 schema reference
│   ├── registry-v1.md        # Legacy v1.0 schema reference
│   ├── migration-v1-to-v2.md # Migration guide from v1 to v2
│   └── artifact-deployment.md # Cloud deployment guide (Azure / AWS)
├── tests/
│   ├── conftest.py
│   ├── test_bsp_manager.py
│   ├── test_cli.py
│   ├── test_deploy.py        # Deployment tests
│   ├── test_registry_fetcher.py
│   └── ...
├── examples/
│   ├── bsp-registry.yaml      # Sample v2.0 BSP registry for QEMU targets
│   └── kas/
│       └── ...                # KAS configuration files
└── .github/
    └── workflows/
        ├── tests.yaml         # CI: run tests on push/PR
        └── publish.yaml       # CD: publish to PyPI on release
```

## Publishing to PyPI

This repository uses GitHub Actions for automated publishing.

### Setup

1. Create PyPI and TestPyPI accounts and configure [Trusted Publishers](https://docs.pypi.org/trusted-publishers/):
   - **PyPI**: Add GitHub Actions publisher for `Advantech-EECC/bsp-registry-tools`
   - **TestPyPI**: Same configuration on test.pypi.org

2. Create GitHub Environments named `pypi` and `testpypi` in your repository settings.

### Publish Workflow

**Automatic (on GitHub Release):**
- Creating a non-prerelease GitHub Release automatically publishes to both TestPyPI and PyPI.
- Creating a prerelease publishes to TestPyPI only.

**Manual:**
```
GitHub → Actions → "Publish to PyPI" → Run workflow → Select environment
```

### Build Locally

```bash
pip install build
python -m build
# Artifacts are in dist/
```

## Architecture

### Classes

| Class | Description |
|-------|-------------|
| `BspManager` | Main coordinator for BSP operations |
| `KasManager` | Handles KAS build system operations |
| `EnvironmentManager` | Manages build environment variables with `$ENV{}` expansion |
| `PathResolver` | Utility for path resolution and validation |
| `RegistryFetcher` | Clones/updates a remote git-hosted BSP registry to a local cache |
| `bsp.server.create_app` | Factory that creates a FastAPI app with REST + GraphQL endpoints |
| `ArtifactDeployer` | Discovers and uploads Yocto build artifacts to cloud storage |
| `AzureStorageBackend` | Azure Blob Storage backend (requires `azure-storage-blob`) |
| `AwsStorageBackend` | AWS S3 backend (requires `boto3`) |

### Data Classes

| Class | Description |
|-------|-------------|
| `RegistryRoot` | Root registry container (specification, registry, containers, environments, deploy) |
| `Registry` | Contains devices, releases, features, presets, frameworks, and distros |
| `Device` | Hardware device/board definition (slug, vendor, soc_vendor, includes) |
| `Release` | Yocto/Isar release definition (slug, distro reference, includes) |
| `Feature` | Optional BSP feature (slug, includes, compatibility constraints, vendor_overrides) |
| `BspPreset` | Named preset combining device + release + features + optional deploy config |
| `Framework` | Build-system framework definition (e.g. Yocto, Isar) |
| `Distro` | Linux distribution definition (e.g. Poky, Isar distro) |
| `Docker` | Docker image, build arg, privileged mode, and runtime_args configuration |
| `NamedEnvironment` | Named environment bundling a container reference, variables, and optional copy entries |
| `EnvironmentVariable` | Name/value pair with `$ENV{}` expansion support |
| `DeployConfig` | Cloud deployment configuration (provider, container/bucket, prefix, patterns, artifact dirs) |
| `DeployResult` | Result of a deployment run: list of uploaded artifacts with URLs and checksums |

### Exceptions

| Exception | Description |
|-----------|-------------|
| `ScriptError` | Base exception for all script errors |
| `ConfigurationError` | Configuration file issues |
| `BuildError` | Build process failures |
| `DockerError` | Docker operation failures |
| `KasError` | KAS operation failures |

## License

This project is licensed under the Apache 2.0 License — see the [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! Please open an issue or submit a pull request on [GitHub](https://github.com/Advantech-EECC/bsp-registry-tools).
