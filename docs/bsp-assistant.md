# BSP Assistant - User Guide

**BSP Assistant** is an interactive, AI-guided discovery and recommendation system built into the `bsp` tool. It helps developers find the optimal Board Support Package (BSP) presets, validate hardware/feature compatibility, and avoid configuration pitfalls before initiating multi-hour Yocto or Isar builds.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Feature Overview](#feature-overview)
3. [Command Reference](#command-reference)
   - [bsp assistant discover](#bsp-assistant-discover)
   - [bsp assistant explain](#bsp-assistant-explain)
   - [bsp assistant check](#bsp-assistant-check)
   - [bsp assistant find](#bsp-assistant-find)
   - [bsp assistant list-devices](#bsp-assistant-list-devices)
   - [bsp assistant list-features](#bsp-assistant-list-features)
   - [bsp assistant suggest](#bsp-assistant-suggest)
4. [Common Use Cases](#common-use-cases)
5. [Troubleshooting & FAQ](#troubleshooting--faq)

---

## Quick Start

Launch the interactive discovery wizard directly from your terminal:

```bash
bsp assistant
```
or
```bash
bsp assistant discover
```

The wizard will ask three simple questions:
1. **Target Board**: Which hardware board you are using (e.g. `RSB-3720`, `ROM-5720`, `qemuarm64`).
2. **Features**: Features you need (e.g. `rauc`, `secure-boot`, `systemd`, `ssh`).
3. **Yocto Release**: Preferred release type (`latest`, `lts`, or a specific release like `scarthgap`).

### Example Session

```
$ bsp assistant discover

🤖 Welcome to BSP Assistant!
   Let me help you find the perfect Board Support Package.

? What board do you have?
  Available: RSB-3720, ROM-5720, ROM-5721, qemuarm64...
  → Enter board name or slug: RSB-3720

? What features do you need?
  Available features: rauc, swupdate, ostree, secure-boot, systemd, ssh
  → Enter features (comma-separated): rauc, secure-boot

? Preferred Yocto release?
  1) Latest
  2) LTS
  3) Specific release...
  → Selection (1): 1

╭──────────────────────────────────────────────────────────╮
│ 🎯 Recommended Preset:                                   │
│                                                          │
│   modular-bsp-rauc-rsb3720-wrynose                       │
│                                                          │
│ 📊 Why this choice?                                      │
│   ✓ RSB-3720 fully supported (i.MX8MP, HAB secure boot)  │
│   ✓ RAUC feature ready (OTA with atomic rollback)        │
│   ✓ Release 'wrynose' selected (latest stable)           │
│   ✓ Confidence: 95% (proven combination)                 │
│                                                          │
│ ⏱️  Estimated build time: 2-3 hours                      │
│                                                          │
│ 💡 Alternative options:                                  │
│   • modular-bsp-rauc-rsb3720-scarthgap                   │
│   • modular-bsp-ostree-rsb3720-wrynose                   │
│                                                          │
│ ⚠️  Known issues / Warnings:                             │
│   • None for this combination                            │
│                                                          │
│ 🚀 Ready to build?                                       │
│    bsp build modular-bsp-rauc-rsb3720-wrynose            │
╰──────────────────────────────────────────────────────────╯
```

---

## Feature Overview

### 1. Intelligent Multi-Criteria Scoring
BSP Assistant evaluates candidate presets using four criteria:
- **Board Compatibility (30 pts)**: Hardware architecture, vendor overrides, and machine constraints.
- **Feature Coverage (35 pts)**: Precise matching against required layer fragments (OTA, container runtimes, security).
- **Release Maturity (25 pts)**: Evaluates release stability (LTS vs. Stable vs. Bleeding-edge / EOL).
- **Testing Confidence (10 pts)**: Proven configurations verified by CI/CD receive highest priority.

### 2. Risk & Lifecycle Assessment
- Flags End-of-Life (EOL) Yocto releases.
- Identifies untested board-feature combinations before build kickoff.
- Warns of potential kernel/layer incompatibilities.

### 3. Alternative Recommendations
- Suggests fallback presets (e.g. LTS alternative when latest is selected, or atomic OSTree update when RAUC is selected).

### 4. Non-Interactive Scripting Mode
Fully scriptable with CLI flags for CI/CD pipelines and automated tooling:
```bash
bsp assistant discover --board=qemuarm64 --features=systemd,security --release-pref=lts --non-interactive
```

---

## Command Reference

### `bsp assistant discover`
Run the interactive discovery wizard or automated recommendation.

**Syntax:**
```bash
bsp assistant discover [OPTIONS]
```

**Options:**
- `--board, -b <NAME>`: Target hardware device or board name.
- `--features, -f <FEAT>`: Required feature slug (can be specified multiple times or comma-separated).
- `--release-pref <PREF>`: Release preference: `latest`, `lts`, or `specific`.
- `--release, -r <SLUG>`: Specific release version slug.
- `--non-interactive`: Disable interactive prompts and output recommendation immediately.

---

### `bsp assistant explain <preset>`
Display full breakdown of a preset, including target hardware, included features, stability status, and risk analysis.

**Syntax:**
```bash
bsp assistant explain <preset_name>
```

**Example:**
```bash
bsp assistant explain poky-qemuarm64
```

---

### `bsp assistant check`
Verify whether a board and a list of features are compatible without running a build.

**Syntax:**
```bash
bsp assistant check --board=<device> --features=<f1,f2> [--release=<r>]
```

**Example:**
```bash
bsp assistant check --board=qemuarm64 --features=systemd,security
```

**Output:**
```
              Compatibility Check: qemuarm64              
┏━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Feature  ┃ Status       ┃ Note                         ┃
┡━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ systemd  │ ✓ Compatible │ Supported by device hardware │
│ security │ ✓ Compatible │ Supported by device hardware │
└──────────┴──────────────┴──────────────────────────────┘

✓ Overall Status: Fully Compatible
  • All requested features are compatible with this hardware configuration.
```

---

### `bsp assistant find`
Search for presets and hardware devices that support a specific feature.

**Syntax:**
```bash
bsp assistant find --feature=<feature> [--release=<r>]
```

**Example:**
```bash
bsp assistant find --feature=systemd
```

---

### `bsp assistant list-devices`
List all supported hardware devices with vendor, SoC family, and supported features.

**Syntax:**
```bash
bsp assistant list-devices
```

---

### `bsp assistant list-features`
List all features defined in the registry along with descriptions and vendor constraints.

**Syntax:**
```bash
bsp assistant list-features
```

---

### `bsp assistant suggest`
Get fast preset recommendations for a specific hardware board.

**Syntax:**
```bash
bsp assistant suggest --board=<device>
```

**Example:**
```bash
bsp assistant suggest --board=qemuarm64
```

---

## Common Use Cases

### 1. New Hardware Setup
When bringing up an unknown Advantech board, run `list-devices` or `suggest`:
```bash
bsp assistant list-devices
bsp assistant suggest --board=RSB-3720
```

### 2. Verifying OTA / Security Capabilities
Check if your target board supports RAUC and secure boot:
```bash
bsp assistant check --board=rsb3720 --features=rauc,secure-boot
```

### 3. Finding LTS Configurations
To build long-term support production images:
```bash
bsp assistant discover --board=rsb3720 --release-pref=lts --non-interactive
```

### 4. CI/CD Pre-flight Validation
Add a validation step to GitHub Actions / GitLab CI before starting a long build:
```bash
bsp assistant check --board=${TARGET_BOARD} --features=${FEATURES} || exit 1
```

---

## Troubleshooting & FAQ

### Q: Why is my confidence score lower than 100%?
The confidence score reflects:
- If a requested feature is partially supported or missing.
- If the combination is marked as `untested`.
- If an EOL (End-of-Life) or bleeding-edge development release is used.

### Q: Can I use BSP Assistant with custom remote registries?
Yes. The assistant honors all standard `bsp` registry options:
```bash
bsp --remote https://github.com/my-org/bsp-registry.git --branch main assistant discover
```

### Q: How is build time estimated?
- Emulated QEMU / Isar builds: ~30-45 minutes
- Standard ARM64 Yocto builds: ~1.5-2.5 hours
- Complex builds with heavy features (GUI/ROS2/Browser): ~3-4 hours
Estimates assume initial build without pre-warmed sstate cache.
