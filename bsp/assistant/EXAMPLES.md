# BSP Assistant - Example Scenarios & Walkthroughs

This document provides realistic walkthrough scenarios showing how **BSP Assistant** solves real-world embedded Linux development tasks.

---

## Scenario 1: User has RSB-3720, needs OTA + Secure Boot

### Context
A developer is building a secure industrial edge gateway on an Advantech **RSB-3720** (NXP i.MX8M Plus). Requirements:
- Dual-partition atomic over-the-air (OTA) updates using RAUC
- Hardware-backed secure boot (NXP HAB / AHAB signing)
- High production stability

### Step 1: Discover Recommendation
```bash
$ bsp assistant discover --board=RSB-3720 --features=rauc,secure-boot --release-pref=latest --non-interactive
```

### Output
```
🤖 Welcome to BSP Assistant!
   Let me help you find the perfect Board Support Package.

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

### Step 2: Validate Preset Details
```bash
$ bsp assistant explain modular-bsp-rauc-rsb3720-wrynose
```
The developer verifies that `modular-bsp-rauc-rsb3720-wrynose` has `risk_level: LOW`, stable status, and HAB signing hooks pre-configured.

### Step 3: Trigger Build
```bash
$ bsp build modular-bsp-rauc-rsb3720-wrynose
```

---

## Scenario 2: New Developer Exploring Feature Compatibility

### Context
A new junior engineer joins the team and wants to understand what features and boards are available in the registry without reading hundreds of lines of YAML.

### Step 1: List Available Boards
```bash
$ bsp assistant list-devices
```
**Output:**
```
                           Available Hardware Devices                           
┏━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┓
┃ Slug       ┃ Name                ┃ Vendor ┃ SoC Family ┃ Supported Features  ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━┩
│ rsb3720    │ Advantech RSB-3720  │ nxp    │ imx8mp     │ rauc, secure-boot,  │
│            │                     │        │            │ systemd, ssh        │
│ rom5720    │ Advantech ROM-5720  │ nxp    │ imx8m      │ swupdate, systemd,  │
│            │                     │        │            │ ssh                 │
│ qemuarm64  │ QEMU ARM64          │ qemu   │ arm64      │ systemd, debug,     │
│            │ (emulated)          │        │            │ yocto-ssh           │
└────────────┴─────────────────────┴────────┴────────────┴─────────────────────┘
```

### Step 2: List Features
```bash
$ bsp assistant list-features
```
**Output:**
```
                               Available Features                               
┏━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┓
┃ Slug      ┃ Name      ┃ Description                     ┃ Compatible Vendors ┃
┡━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━┩
│ rauc      │ RAUC OTA  │ Robust Auto-Update Controller   │ nxp, qemu          │
│           │           │ dual-partition atomic OTA       │                    │
│ ostree    │ OSTree    │ Atomic filesystem updates       │ nxp, qemu          │
│ secure-b… │ Secure B… │ Cryptographic image signing and │ nxp                │
│           │           │ boot verification               │                    │
│ systemd   │ Systemd   │ Systemd init system integration │ all                │
└───────────┴───────────┴─────────────────────────────────┴────────────────────┘
```

### Result
In under 10 seconds, the engineer discovers the exact hardware platforms, supported features, and vendor constraints without opening a code editor.

---

## Scenario 3: User Needs Long-Term Support (LTS) Release Only

### Context
An enterprise customer requires a 5-year Long-Term Support (LTS) Yocto release for medical device compliance. They cannot use bleeding-edge or short-term releases like `wrynose` or `styhead`.

### Step 1: Run Discovery with LTS Preference
```bash
$ bsp assistant discover --board=rsb3720 --release-pref=lts --non-interactive
```

### Output
```
╭──────────────────────────────────────────────────────────╮
│ 🎯 Recommended Preset:                                   │
│                                                          │
│   modular-bsp-rauc-rsb3720-scarthgap                     │
│                                                          │
│ 📊 Why this choice?                                      │
│   ✓ RSB-3720 fully supported (i.MX8MP)                   │
│   ✓ Release 'scarthgap' selected (LTS Long-Term Support) │
│   ✓ Confidence: 100% (proven combination)                │
│                                                          │
│ ⏱️  Estimated build time: 2-3 hours                      │
│                                                          │
│ 💡 Alternative options:                                  │
│   • modular-bsp-rsb3720-scarthgap                        │
│                                                          │
│ ⚠️  Known issues / Warnings:                             │
│   • None for this combination                            │
│                                                          │
│ 🚀 Ready to build?                                       │
│    bsp build modular-bsp-rauc-rsb3720-scarthgap          │
╰──────────────────────────────────────────────────────────╯
```

### Result
The assistant automatically prioritizes `scarthgap` (Yocto 5.0 LTS) over newer non-LTS releases, guaranteeing LTS compliance.

---

## Scenario 4: User Exploring Alternative Features (RAUC vs OSTree vs SWUpdate)

### Context
A systems architect is evaluating update mechanisms for a fleet of devices and wants to see which presets provide `ostree` vs `rauc`.

### Step 1: Find Presets Supporting OSTree
```bash
$ bsp assistant find --feature=ostree
```

### Output
```
Presets providing feature 'ostree':
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┓
┃ Preset Name                ┃ Device     ┃ Releases     ┃ Features            ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━┩
│ modular-bsp-ostree-rsb3720 │ rsb3720    │ scarthgap,   │ ostree, systemd     │
│                            │            │ wrynose      │                     │
└────────────────────────────┴────────────┴──────────────┴─────────────────────┘

Hardware devices compatible with 'ostree':
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┓
┃ Device Slug ┃ Name               ┃ Vendor ┃ SoC Family ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━┩
│ rsb3720     │ Advantech RSB-3720 │ nxp    │ imx8mp     │
│ qemuarm64   │ QEMU ARM64         │ qemu   │ arm64      │
└─────────────┴────────────────────┴────────┴────────────┘
```

### Step 2: Compare with RAUC
```bash
$ bsp assistant find --feature=rauc
```
The architect immediately sees all presets and boards supporting either technology and can compare build estimates and alternatives.

---

## Scenario 5: User Needs to Check Board / Feature Compatibility

### Context
A developer wants to know if `secure-boot` can be enabled on a QEMU emulated board or an unverified hardware target before modifying layer recipes.

### Step 1: Run Compatibility Check
```bash
$ bsp assistant check --board=qemuarm64 --features=secure-boot,systemd
```

### Output
```
              Compatibility Check: qemuarm64              
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Feature     ┃ Status         ┃ Note                         ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ systemd     │ ✓ Compatible   │ Supported by device hardware │
│ secure-boot │ ✗ Incompatible │ Vendor/SoC constraints not   │
│             │                │ met                          │
└─────────────┴────────────────┴──────────────────────────────┘

✗ Overall Status: Compatibility Issues Detected
  • Feature 'secure-boot' requires hardware cryptographic support not available on 'qemuarm64'.
```

### Result
The developer is immediately informed that `secure-boot` requires hardware signing keys (NXP HAB/AHAB or Qualcomm secure boot) not supported by the QEMU target, saving hours of failed bitbake builds.
