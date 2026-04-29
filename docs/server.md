# BSP Registry HTTP Server

`bsp-registry-tools` ships an optional HTTP server that exposes the full BSP registry via both a **REST API** and a **GraphQL API**, giving you the same capabilities as the CLI from any HTTP client.

---

## Requirements

The server feature requires extra dependencies not installed by default:

```bash
pip install "bsp-registry-tools[server]"
```

This installs:

| Package | Version | Purpose |
|---------|---------|---------|
| [FastAPI](https://fastapi.tiangolo.com/) | >= 0.100.0 | REST framework |
| [uvicorn](https://www.uvicorn.org/) | >= 0.23.0 | ASGI server |
| [strawberry-graphql](https://strawberry.rocks/) | >= 0.200.0 | GraphQL framework |

---

## Starting the server

### Via CLI

```bash
# Default: http://127.0.0.1:8080
bsp server

# Bind to all interfaces, custom port
bsp server --host 0.0.0.0 --port 9000

# Use a specific registry file
bsp --registry /path/to/bsp-registry.yaml server --host 0.0.0.0 --port 8080

# Remote registry on a non-default branch
bsp --remote https://github.com/my-org/bsp-registry.git --branch dev server

# Development mode (auto-reload on file changes)
bsp server --reload
```

### CLI options for `server`

| Option | Default | Description |
|--------|---------|-------------|
| `--host HOST` | `127.0.0.1` | Host address to listen on |
| `--port PORT` | `8080` | TCP port to listen on |
| `--reload` | disabled | Enable Uvicorn auto-reload (development only) |

All global [registry resolution options](../README.md#registry-resolution-priority) (`--registry`, `--remote`, `--branch`, `--update`, `--local`) apply to the `server` command exactly as they do for other subcommands.

### Via Python

```python
import uvicorn
from bsp.server import create_app

# Create the app from a registry file path
app = create_app(registry_path="/path/to/bsp-registry.yaml")
uvicorn.run(app, host="0.0.0.0", port=8080)
```

Reuse an already-initialised `BspManager` to avoid loading the registry twice:

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

## Available interfaces

Once the server is running:

| URL | Description |
|-----|-------------|
| `http://localhost:8080/` | Redirects to Swagger UI |
| `http://localhost:8080/docs` | Swagger / OpenAPI interactive docs |
| `http://localhost:8080/redoc` | ReDoc documentation |
| `http://localhost:8080/openapi.json` | OpenAPI schema (JSON) |
| `http://localhost:8080/graphql` | GraphiQL interactive editor |
| `http://localhost:8080/api/v1/…` | REST API endpoints |

---

## REST API reference (`/api/v1/`)

### Query endpoints

#### `GET /api/v1/bsp` — List BSP presets

Returns all named BSP presets defined in the registry.

```bash
curl http://localhost:8080/api/v1/bsp
```

```json
[
  {
    "name": "poky-qemuarm64-scarthgap",
    "description": "Poky QEMU ARM64 Scarthgap",
    "device": "qemuarm64",
    "release": "scarthgap",
    "releases": [],
    "vendor_release": null,
    "override": null,
    "features": [],
    "targets": []
  }
]
```

#### `GET /api/v1/devices` — List devices

Returns all hardware device definitions.

```bash
curl http://localhost:8080/api/v1/devices
```

#### `GET /api/v1/releases` — List releases

Returns all release definitions.  Use the optional `device` query parameter to filter to releases compatible with a specific device.

```bash
# All releases
curl http://localhost:8080/api/v1/releases

# Only releases compatible with qemuarm64
curl "http://localhost:8080/api/v1/releases?device=qemuarm64"
```

#### `GET /api/v1/features` — List features

Returns all optional BSP feature definitions.

#### `GET /api/v1/distros` — List distros

Returns all Linux distribution / build-system definitions.

#### `GET /api/v1/frameworks` — List frameworks

Returns all build-system framework definitions.

#### `GET /api/v1/containers` — List containers

Returns all Docker container definitions.

---

### Action endpoints

All action endpoints accept a JSON body.  Use either `bsp_name` **or** both `device` + `release` (mutually exclusive).

#### `POST /api/v1/export` — Export BSP configuration

Returns the resolved BSP KAS configuration as a YAML string.

**Request body:**

```json
{
  "bsp_name": "poky-qemuarm64-scarthgap"
}
```

or

```json
{
  "device": "qemuarm64",
  "release": "scarthgap",
  "features": ["ota"]
}
```

**Response:**

```json
{
  "yaml_content": "header:\n  version: 14\n  ..."
}
```

**Example:**

```bash
curl -X POST http://localhost:8080/api/v1/export \
     -H "Content-Type: application/json" \
     -d '{"bsp_name": "poky-qemuarm64-scarthgap"}'
```

#### `POST /api/v1/build` — Trigger a BSP build

Triggers a BSP build and **blocks until it completes**.  Use `checkout_only: true` to validate the configuration without running a full build.

**Request body:**

```json
{
  "bsp_name": "poky-qemuarm64-scarthgap",
  "checkout_only": false
}
```

**Response:**

```json
{
  "status": "ok",
  "message": "Build completed successfully"
}
```

#### `POST /api/v1/shell` — Run a command in the build container

Executes a non-interactive command inside the BSP build container and returns its output.

**Request body:**

```json
{
  "bsp_name": "poky-qemuarm64-scarthgap",
  "command": "bitbake -e core-image-minimal | grep ^MACHINE="
}
```

**Response:**

```json
{
  "return_code": 0,
  "output": "MACHINE=\"qemuarm64\"\n"
}
```

---

## GraphQL API reference (`/graphql`)

Navigate to **`http://localhost:8080/graphql`** for the interactive GraphiQL editor with schema introspection and autocomplete.

### Queries

```graphql
# All BSP presets
{
  bsp {
    name
    description
    device
    release
    features
    targets
  }
}

# All devices
{
  devices {
    slug
    description
    vendor
    socVendor
    socFamily
    includes
  }
}

# Releases, optionally filtered by device
{
  releases(device: "qemuarm64") {
    slug
    description
    yoctoVersion
    isarVersion
    distro
    environment
  }
}

# Features
{
  features {
    slug
    description
    compatibleWith
    compatibility {
      vendor
      socVendor
      socFamily
    }
  }
}

# Distros, frameworks, containers
{
  distros { slug description framework vendor }
  frameworks { slug description vendor }
  containers { name image file privileged }
}
```

### Mutations

#### `exportBsp` — Resolve and export a BSP configuration

```graphql
mutation {
  exportBsp(bspName: "poky-qemuarm64-scarthgap") {
    yamlContent
  }
}
```

Or by components:

```graphql
mutation {
  exportBsp(device: "qemuarm64", release: "scarthgap", features: ["ota"]) {
    yamlContent
  }
}
```

#### `buildBsp` — Trigger a BSP build

```graphql
mutation {
  buildBsp(bspName: "poky-qemuarm64-scarthgap", checkoutOnly: false) {
    status
    message
  }
}
```

#### `shellCommand` — Run a command inside the build container

```graphql
mutation {
  shellCommand(
    bspName: "poky-qemuarm64-scarthgap"
    command: "bitbake -e core-image-minimal | grep ^MACHINE="
  ) {
    returnCode
    output
  }
}
```

---

## Error handling

### REST errors

The REST API returns standard HTTP status codes:

| Code | Meaning |
|------|---------|
| `200 OK` | Successful query |
| `404 Not Found` | Device/preset slug not found |
| `400 Bad Request` | Operation failed (e.g. build error) |
| `422 Unprocessable Entity` | Invalid request body (validation error) |

### GraphQL errors

GraphQL errors are returned inside the standard `errors` array in the response body alongside partial data where available.

---

## Security considerations

- The server does **not** include authentication or authorisation.  Do not expose it directly to untrusted networks without a reverse proxy and appropriate access controls.
- The `build` and `shell` endpoints execute system commands inside Docker containers; restrict access accordingly.
- Bind to `127.0.0.1` (the default) unless you explicitly need network access.
