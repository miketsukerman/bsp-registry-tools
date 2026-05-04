# `bsp import` — Importing Google Repo Manifests

`bsp import` converts a **Google Repo Yocto manifest** (`.xml`) into the two
artefacts used by the BSP registry:

* A **KAS YAML file** containing every repo from the manifest, with resolved
  URLs, branch names, and pinned commit SHAs.
* A **`bsp-registry.yml`** entry (created from scratch or merged into an
  existing one) that references the generated KAS file.

The command does **not** require an existing registry; it can bootstrap a
complete registry from a manifest or add to one that is already partially
populated.

---

## Quick Start

```bash
# Create a new registry from an NXP manifest
bsp import default.xml \
    --output-dir ./my-registry \
    --vendor advantech \
    --soc-vendor nxp \
    --vendor-release imx-6.6.52-2.2.0

# Merge into the Advantech BSP registry
bsp import default.xml \
    --output-dir /path/to/bsp-registry \
    --vendor advantech \
    --soc-vendor nxp \
    --vendor-release imx-6.6.52-2.2.0 \
    --distro fsl-imx-xwayland \
    --merge
```

---

## How It Works

### Step 1 — Parse the manifest

The parser reads the manifest XML and follows **every `<include>` directive
recursively** (depth-first, cycle-safe).  All `<remote>`, `<default>`, and
`<project>` elements from all included files are merged into a single flat
project list.

Resolution rules:

| Manifest attribute | KAS field |
|---|---|
| `<remote fetch="…">` + `<project name="…">` | `url:` (fetch URL + project name joined) |
| `<project path="…">` | `path:` |
| `<project revision="…">` (branch / tag) | `branch:` |
| `<project revision="<40-hex-sha>">` | `commit:` |
| `<project revision="<sha>" upstream="…">` | both `commit:` and `branch:` |
| `<default revision="…">` | fallback `branch:` for all projects without their own revision |

### Step 2 — Detect the Yocto codename

The importer inspects every `revision`, `upstream`, and the manifest-level
`<default revision="…">` value for a known Yocto codename
(`scarthgap`, `kirkstone`, `mickledore`, …).  The first match is used as the
release slug.

Use `--release` to override this when the codename cannot be detected
automatically (e.g. when all revisions are pinned SHAs).

### Step 3 — Generate the KAS file

A single self-contained KAS YAML file is generated containing every repo
from the manifest.  The file is placed at:

```
vendors/<vendor>/<vendor-release>-<codename>.yml          # --vendor only
vendors/<vendor>/<soc-vendor>/<vendor-release>-<codename>.yml   # with --soc-vendor
```

Well-known repos (`poky`, `bitbake`, `meta-openembedded`, `meta-arm`, …) get
sensible default layer lists.  All other repos default to a single layer
named after the last path component.

### Step 4 — Generate or update `bsp-registry.yml`

An entry like the following is added to `bsp-registry.yml`:

```yaml
registry:
  vendors:
    - slug: advantech
      name: Advantech

  releases:
    - slug: scarthgap
      description: "Yocto 5.0 (Scarthgap)"
      includes:
        - yocto/releases/scarthgap.yml
      vendor_overrides:
        - vendor: advantech
          soc_vendors:
            - vendor: nxp
              distro: fsl-imx-xwayland
              releases:
                - slug: imx-6.6.52-2.2.0
                  description: imx-6.6.52-2.2.0
                  includes:
                    - vendors/advantech/nxp/imx-6.6.52-2.2.0-scarthgap.yml
```

When `--soc-vendor` is omitted the `soc_vendors` nesting is skipped:

```yaml
      vendor_overrides:
        - vendor: myvendor
          releases:
            - slug: my-bsp-1.0
              includes:
                - vendors/myvendor/my-bsp-1.0-scarthgap.yml
```

---

## Options Reference

| Flag | Default | Description |
|---|---|---|
| `MANIFEST` | *(required)* | Path to the repo manifest XML file |
| `--output-dir PATH` | `.` | Directory to write generated files |
| `--vendor SLUG` | `imported` | Board / software vendor slug |
| `--soc-vendor SLUG` | *(none)* | SoC vendor slug; enables nested `soc_vendors` in vendor_overrides |
| `--vendor-release SLUG` | manifest filename stem | Vendor BSP release identifier |
| `--release SLUG` | *(auto-detected)* | Yocto codename to use as release slug |
| `--distro SLUG` | *(none)* | Distro slug to attach to the vendor override |
| `--merge` | `false` | Merge into an existing `bsp-registry.yml`; error if the file exists and this flag is absent |
| `--dry-run` | `false` | Print what would be generated without writing files |
| `--hints PATH` | *(none)* | Path to a YAML hints file (see below) |

---

## Hints File

Board and machine names are **highly vendor-specific** and cannot be reliably
inferred from the manifest alone.  The hints file lets you:

1. **Exclude projects** from the generated KAS file (e.g. proprietary layers
   that should not be committed).
2. **Inject device entries** into `bsp-registry.yml` that point to
   hand-crafted machine KAS files.

### Format

```yaml
# hints.yml

# Override classification for individual manifest projects.
# Currently supported roles:
#   skip  — exclude the project from the generated KAS file entirely
projects:
  meta-proprietary:
    role: skip
  meta-internal-tools:
    role: skip

# Device entries to inject into bsp-registry.yml.
# Each entry is merged verbatim; existing entries with the same slug
# are not overwritten (idempotent).
devices:
  - slug: imx8mprsb3720a1
    description: "RSB-3720 (i.MX8MP, 1 GB RAM)"
    vendor: advantech
    soc_vendor: nxp
    includes:
      - vendors/advantech/nxp/machine/imx8/imx8mprsb3720a1.yml

  - slug: imx8mprsb3720a2
    description: "RSB-3720 (i.MX8MP, 2 GB RAM)"
    vendor: advantech
    soc_vendor: nxp
    includes:
      - vendors/advantech/nxp/machine/imx8/imx8mprsb3720a2.yml
```

Pass the file with `--hints hints.yml`.

---

## Populating the Advantech BSP Registry

The Advantech BSP registry at
[Advantech-EECC/bsp-registry](https://github.com/Advantech-EECC/bsp-registry)
follows this layout:

```
bsp-registry.yml          ← main registry
yocto/
  yocto.yaml              ← framework KAS (repos without pins)
  releases/
    scarthgap.yml         ← release KAS (pinned commits for base repos)
    kirkstone.yml
    ...
vendors/
  nxp/
    nxp.yaml              ← NXP SoC-layer definitions
    common.yml            ← NXP local_conf, targets
    imx-6.6.52-2.2.0-scarthgap.yml  ← NXP BSP release pins
  advantech/
    nxp/
      imx-6.6.52-2.2.0-scarthgap.yml  ← Advantech overrides on top of NXP
      machine/
        imx8/
          imx8mprsb3720a1.yml          ← board-specific KAS
```

A typical import workflow for adding a new NXP BSP release to this registry:

```bash
# 1. Clone the registry
git clone https://github.com/Advantech-EECC/bsp-registry.git
cd bsp-registry

# 2. Prepare a hints file for your board
cat > hints.yml <<'EOF'
devices:
  - slug: imx8mprsb3720a1
    description: "RSB-3720 (i.MX8MP)"
    vendor: advantech
    soc_vendor: nxp
    includes:
      - vendors/advantech/nxp/machine/imx8/imx8mprsb3720a1.yml
EOF

# 3. Import the NXP manifest
bsp import /path/to/imx-manifest/default.xml \
    --output-dir . \
    --vendor advantech \
    --soc-vendor nxp \
    --vendor-release imx-6.6.52-2.2.0 \
    --distro fsl-imx-xwayland \
    --hints hints.yml \
    --merge

# 4. Review the generated files, then commit
git diff
git add vendors/advantech/nxp/imx-6.6.52-2.2.0-scarthgap.yml bsp-registry.yml
git commit -m "feat: add imx-6.6.52-2.2.0 scarthgap via bsp import"
```

### After import — manual steps

The generated KAS file is **self-contained** but may require manual
refinement before it matches the layered Advantech registry style:

1. **Layer lists** — well-known repos (`poky`, `meta-openembedded`, etc.)
   get default layer lists; vendor-specific repos may need adjustment.
2. **Framework repos** — if the registry already has `yocto/releases/scarthgap.yml`
   pinning `poky`, `meta-openembedded`, etc., you can remove those repos
   from the generated vendor file and add an `includes:` header entry
   pointing to the existing release file.
3. **Machine KAS files** — for each device in `hints.yml`, create a
   machine-specific KAS file at the path referenced in `includes:`, setting
   `MACHINE` in `local_conf_header`.
4. **Disabled layers** — some repos (e.g. `meta-security`, `meta-virtualization`)
   ship with layers that should be `"disabled"` by default; the importer
   applies known defaults but vendor-specific repos may need adjustments.

---

## Mapping Reference

| Google Repo concept | BSP Registry concept |
|---|---|
| `<remote fetch="…">` + `<project name="…">` | `repos.<key>.url` in KAS |
| `<project path="…">` | `repos.<key>.path` in KAS |
| `<project revision="branch">` | `repos.<key>.branch` in KAS |
| `<project revision="<sha>">` | `repos.<key>.commit` in KAS |
| `<project revision="<sha>" upstream="branch">` | both `commit` + `branch` |
| `<default revision="…">` | fallback branch for all projects |
| `<include name="…">` | resolved recursively before processing |
| Manifest file name / `--vendor-release` | `vendor_release.slug` |
| `--release` / detected codename | `releases[].slug` |
| `--vendor` | `vendors[].slug` + `vendor_overrides[].vendor` |
| `--soc-vendor` | nested `soc_vendors[].vendor` in vendor_overrides |
| `--distro` | `soc_vendors[].distro` (or `vendor_overrides[].distro`) |
