# Registry Editing Reference

`bsp-registry-tools` ships a full read-write editing layer so you can create,
update, and remove registry entries without manually editing YAML.  Two
interfaces are provided:

* **CLI** — `bsp registry <action> <entity> [options]`
* **TUI** — interactive edit panel launched from the `bsp-explorer` screen

---

## TUI Edit Panel

The `bsp-explorer` TUI exposes registry editing through modal dialogs that
appear over the existing BSP tree without disrupting the layout.

![Registry Edit Panel](screenshots/registry-edit-tui.svg)

### Opening the Edit Panel (Ctrl+E)

Select a preset node in the left-hand BSP tree, then press **Ctrl+E** (or use
the footer shortcut).  The Edit Panel slides in as a modal overlay showing a
form for every field of the selected entity.

| Control | Purpose |
|---------|---------|
| `Input` fields | Edit string attributes (`name`, `description`, build path, …) |
| Drop-down selectors | Pick a `device`, `release`, or container from the loaded registry |
| Feature chips | Visual list of enabled features — use **+** / **−** to add or remove |
| `local_conf` textarea | Multi-line free-form `local_conf` entries |

### Buttons

| Button | Key | Description |
|--------|-----|-------------|
| 💾 **Save** | — | Validate, then write changes atomically to the registry file |
| ↩ **Undo** | — | Pop the undo stack and restore the previous state |
| ✕ **Discard** | — | Drop all unsaved in-memory changes |
| ✕ **Close** | Esc | Close the panel (prompts if there are unsaved changes) |

### Add entity (key `a`)

With any tree node selected, press **`a`** to open the **Add Entity** dialog
for the same entity type (e.g. select a Preset node → add a new preset).  Only
the slug / name field is mandatory; all other fields default to empty.

### Remove entity (key `d`)

Press **`d`** on any leaf node.  A confirmation dialog lists all dangling
cross-references (presets that use this device / release, devices that use this
vendor, …) before asking for confirmation.

### Diff view (Ctrl+D)

Press **Ctrl+D** to stream a coloured unified diff of every unsaved in-memory
change versus the on-disk registry file into the bottom log panel.

### Validation feedback

The top of the Edit Panel shows a green **✓ No validation errors** banner while
the form is clean.  On any error (duplicate slug, missing cross-reference, etc.)
the banner turns red and lists the offending issues.  The **Save** button is
disabled while errors exist.

---

## `bsp registry` CLI Sub-command Group

```
bsp registry <action> [<entity>] [options]
```

All `registry` sub-commands accept the global `--registry PATH` option to
target a specific file.

### `registry init`

Create a minimal `bsp-registry.yaml` skeleton in the current directory (or at
`--output PATH`).

```bash
# Create bsp-registry.yaml in the current directory
bsp registry init

# Write to a custom path
bsp registry init --output /path/to/my-registry.yaml

# Overwrite an existing file
bsp registry init --force
```

### `registry validate`

Load the registry and report all errors and warnings.  Exits non-zero when
there are errors.

```bash
bsp registry validate
bsp --registry /path/to/bsp-registry.yaml registry validate
```

Sample output:

```
[ERROR] Device slug 'board-a' is defined more than once
[WARNING] Preset 'my-preset' references container 'undefined-container' which is not declared
✓ 0 errors, 1 warning
```

### `registry diff`

Show what has changed in the working-directory registry versus its last
committed state.  Uses `git diff` when the file lives inside a git working
tree, and a Python unified diff otherwise.

```bash
bsp registry diff
bsp --registry ./bsp-registry.yaml registry diff
```

### `registry add <entity>`

Add a new entry of the given type to the registry.

```bash
# Add a hardware device
bsp registry add device \
  --slug myboard \
  --vendor advantech \
  --soc-vendor nxp \
  --description "My Custom Board"

# Add a Yocto release
bsp registry add release \
  --slug scarthgap \
  --description "Yocto 5.0 LTS (Scarthgap)" \
  --yocto-version "5.0"

# Add an optional feature
bsp registry add feature \
  --slug ota \
  --description "OTA update support"

# Add a named preset
bsp registry add preset \
  --name myboard-scarthgap \
  --description "My Board Scarthgap" \
  --device myboard \
  --release scarthgap

# Add a vendor
bsp registry add vendor \
  --slug acme \
  --name "ACME Corp"

# Add a distro
bsp registry add distro \
  --slug poky \
  --description "Poky (Yocto reference distro)"

# Add a container
bsp registry add container \
  --container-name debian-bookworm \
  --image "registry.example.com/debian/kas:5.1"
```

After adding, `validate()` is run automatically.  Any warnings are printed
before the file is written.

### `registry edit <entity> <slug-or-name>`

Update specific fields on an existing entry.  Only the flags you supply are
changed; everything else is left untouched.

```bash
# Update just the description
bsp registry edit device myboard --description "Updated description"

# Change the container used by a preset
bsp registry edit preset myboard-scarthgap --container debian-trixie

# Open the entity YAML block in $EDITOR for free-form editing
bsp registry edit device myboard --editor

# Auto-commit the change to git after saving
bsp registry edit release scarthgap --description "Yocto 5.0 LTS" --commit
```

#### `--editor` flag

When `--editor` is specified, the tool serialises the entity to a temporary
YAML file and opens it in `$EDITOR` (falling back to `vi`).  On exit the file
is re-parsed, validated, and saved if there are no errors.

### `registry remove <entity> <slug-or-name>`

Remove an entry from the registry.  By default the tool:

1. Checks for dangling cross-references (presets using this device, etc.) and
   lists them.
2. Prompts for confirmation unless `--force` is given.

```bash
# Interactive remove with reference check
bsp registry remove device myboard

# Skip confirmation prompt
bsp registry remove device myboard --force

# Remove and auto-commit to git
bsp registry remove device myboard --force --commit
```

### `registry show <entity> <slug-or-name>`

Pretty-print the YAML block for a single entity — useful for scripting.

```bash
# Print to stdout
bsp registry show device myboard

# Pipe into clipboard (macOS)
bsp registry show preset myboard-scarthgap | pbcopy
```

Sample output:

```yaml
myboard:
  description: My Custom Board
  includes: []
  local_conf: []
  slug: myboard
  soc_family: null
  soc_vendor: nxp
  vendor: advantech
```

---

## Entity Types and Flags

### `device`

| Flag | Required | Description |
|------|----------|-------------|
| `--slug` | Yes | Unique identifier |
| `--vendor` | Yes | Vendor slug |
| `--soc-vendor` | Yes | SoC vendor slug |
| `--description` | No | Human-readable description |
| `--soc-family` | No | SoC family identifier |
| `--includes` | No | KAS include file paths (repeatable) |
| `--local-conf` | No | Extra `local_conf` entries (repeatable) |

### `release`

| Flag | Required | Description |
|------|----------|-------------|
| `--slug` | Yes | Unique identifier |
| `--description` | No | Human-readable description |
| `--yocto-version` | No | Yocto version string (e.g. `"5.0"`) |
| `--distro` | No | Distro slug cross-reference |
| `--includes` | No | KAS include file paths (repeatable) |

### `feature`

| Flag | Required | Description |
|------|----------|-------------|
| `--slug` | Yes | Unique identifier |
| `--description` | No | Human-readable description |
| `--includes` | No | KAS include file paths (repeatable) |

### `preset`

| Flag | Required | Description |
|------|----------|-------------|
| `--name` | Yes | Unique preset name |
| `--device` | Yes | Device slug cross-reference |
| `--description` | No | Human-readable description |
| `--release` | No | Release slug (mutually exclusive with `--releases`) |
| `--releases` | No | Space-separated release slugs |
| `--features` | No | Space-separated feature slugs |
| `--container` | No | Build container name override |
| `--build-path` | No | Override the output build directory |

### `vendor`

| Flag | Required | Description |
|------|----------|-------------|
| `--slug` | Yes | Unique identifier |
| `--name` | No | Display name |

### `distro`

| Flag | Required | Description |
|------|----------|-------------|
| `--slug` | Yes | Unique identifier |
| `--description` | No | Human-readable description |

### `container`

| Flag | Required | Description |
|------|----------|-------------|
| `--container-name` | Yes | Unique container key |
| `--image` | Yes | Docker image name and tag |
| `--dockerfile` | No | Path to Dockerfile for building the image |
| `--privileged` | No | Run container in privileged mode |

---

## Safety Features

### Atomic writes

`RegistryWriter.save()` writes to a temporary file in the same directory as the
target, then renames it into place.  This guarantees the on-disk file is never
left in a partial state.

### Automatic backup

Before the **first** save in a session, the original registry file is copied to
`<name>.bak`.  Subsequent saves within the same session do not overwrite the
backup, so the original is always recoverable.

### Git integration

When the registry file lives inside a git working tree, `registry edit` and
`registry remove` automatically stage the change (`git add`) without committing.
Pass `--commit` to also create a commit with an auto-generated message:

```
bsp registry: edit device myboard
bsp registry: remove preset old-preset
```

### Multi-level undo (TUI)

Every mutating operation in the TUI's Edit Panel pushes a snapshot of the
serialised registry onto the undo stack.  Use the **↩ Undo** button to step
back through the history one change at a time.

---

## Python API

`RegistryWriter` is also accessible as a Python library:

```python
from bsp import RegistryWriter
from bsp.models import Device
from pathlib import Path

writer = RegistryWriter()
writer.load(Path("bsp-registry.yaml"))

# Add a device
writer.add_device(Device(
    slug="myboard",
    vendor="acme",
    soc_vendor="nxp",
    description="My Board",
))

# Validate
issues = writer.validate()
for issue in issues:
    print(issue)

# Save atomically (creates .bak on first save)
writer.save()

# Undo the last change
writer.undo()

# Show diff vs on-disk
print(writer.diff())
```

See the [full API reference](registry-v2.md) for the data model classes
(`Device`, `Release`, `Feature`, `BspPreset`, `Vendor`, `Distro`, `Docker`).
