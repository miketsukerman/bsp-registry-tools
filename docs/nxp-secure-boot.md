# NXP Secure Boot — Registry Configuration, Key Generation, SoC Fusing, and Image Verification

This guide covers **both** the BSP registry configuration for NXP secure-boot
and the operational steps required to generate keys, fuse the SoC, and verify
signed images.

> **Security notice**: Private keys, SRK tables, and fuse values are
> **never** stored in the registry.  Always keep private key material in
> an offline HSM, a CI/CD secret store (Vault, GitHub Encrypted Secrets,
> Azure Key Vault, AWS Secrets Manager), or equivalent — never in source
> control.

---

## Table of Contents

- [Overview](#overview)
- [Registry Configuration](#registry-configuration)
  - [Background](#background)
  - [Assumptions / scope](#assumptions--scope)
  - [Registry snippet](#registry-snippet)
  - [KAS file include order](#kas-file-include-order)
  - [Environment variables](#environment-variables)
  - [Operator guide](#operator-guide)
- [Prerequisites](#prerequisites)
- [HABv4 (i.MX6 / i.MX7 / i.MX8)](#habv4-imx6--imx7--imx8)
  - [1. Install NXP CST](#1-install-nxp-cst)
  - [2. Generate the HABv4 PKI tree](#2-generate-the-habv4-pki-tree)
  - [3. Generate the SRK fuse table](#3-generate-the-srk-fuse-table)
  - [4. Build a signed image](#4-build-a-signed-image)
  - [5. Verify a signed image (host)](#5-verify-a-signed-image-host)
  - [6. Fuse the SoC](#6-fuse-the-soc)
  - [7. Verify secure boot on the target](#7-verify-secure-boot-on-the-target)
- [AHAB (i.MX8M+ / i.MX9x)](#ahab-imx8m--imx9x)
  - [1. Install NXP CST (v3.3.x or later)](#1-install-nxp-cst-v33x-or-later)
  - [2. Generate the AHAB PKI tree](#2-generate-the-ahab-pki-tree)
  - [3. Build a signed container image](#3-build-a-signed-container-image)
  - [4. Verify a signed container image (host)](#4-verify-a-signed-container-image-host)
  - [5. Fuse the SoC](#5-fuse-the-soc)
  - [6. Verify secure boot on the target](#6-verify-secure-boot-on-the-target)
- [CI/CD key injection](#cicd-key-injection)
- [Key rotation](#key-rotation)
- [Troubleshooting](#troubleshooting)

---

## Overview

NXP provides two code-signing architectures:

| Architecture | SoC generations | Trust anchor | Fuse bank |
|---|---|---|---|
| **HABv4** | i.MX6, i.MX7, i.MX8 (non-M family) | Super Root Key (SRK) table hash | `SRK_HASH` fuses |
| **AHAB** | i.MX8M, i.MX8M Plus, i.MX8M Mini, i.MX8M Nano, i.MX9x | SRKH (Super Root Key Hash) | `SRKH` fuses |

Both flows use the **NXP Code Signing Tool (CST)** to create keys, sign
images, and produce the fuse values that permanently bind the SoC to a
specific public-key root.

---

## Registry Configuration

### Background

NXP offers two code-signing flows depending on the SoC generation:

| Flow  | SoC families           | Yocto class / tool          |
|-------|------------------------|-----------------------------|
| HABv4 | i.MX6, i.MX7, i.MX8   | `imx-hab` / `imx-boot-hab`  |
| AHAB  | i.MX8M+, i.MX9x       | `imx-ahab` / NXP CST tools  |

A single `secure-boot` feature entry covers both flows.  The correct
signing-specific KAS layers are selected by the `vendor_release` field on the
BSP preset (`habv4` or `ahab`).  The **same** slug drives both the NXP BSP
sub-release selection at the release level *and* the signing-flow selection at
the feature level.

### Assumptions / scope

* Presets are the supported workflow (no component-based CLI change required).
* Private keys are **never** stored in the registry.  They are injected at
  build time via environment variables that use `$ENV{}` placeholders.
* Support is initially limited to Yocto (`compatible_with: [yocto]`).

### Registry snippet

```yaml
specification:
  version: "2.0"

registry:

  frameworks:
    - slug: yocto
      description: "Yocto Project build system"
      vendor: "Yocto Project"
      includes:
        - kas/yocto/yocto.yaml

  distro:
    - slug: poky
      description: "Poky (Yocto Project reference distro)"
      vendor: yocto
      framework: yocto
      includes:
        - kas/poky/distro/poky.yaml

    # NXP-specific distro required for the i.MX BSP meta-layers
    - slug: fsl-imx-xwayland
      description: "NXP i.MX Wayland distro"
      vendor: nxp
      framework: yocto
      includes:
        - vendors/nxp/distro/fsl-imx-xwayland.yaml

  devices:
    # HABv4 device (i.MX8 family)
    - slug: imx8-hab4-board
      description: "Advantech i.MX8 Board"
      vendor: advantech
      soc_vendor: nxp
      soc_family: imx8
      includes:
        - kas/boards/imx8-hab4-board.yaml

    # AHAB device (i.MX8M Plus)
    - slug: imx8mp-ahab-board
      description: "Advantech i.MX8M Plus Board"
      vendor: advantech
      soc_vendor: nxp
      soc_family: imx8m
      includes:
        - kas/boards/imx8mp-ahab-board.yaml

    # AHAB device (i.MX93)
    - slug: imx93-ahab-board
      description: "Advantech i.MX93 Board"
      vendor: advantech
      soc_vendor: nxp
      soc_family: imx9
      includes:
        - kas/boards/imx93-ahab-board.yaml

  releases:
    - slug: scarthgap
      distro: poky
      description: "Yocto 5.0 LTS (Scarthgap)"
      yocto_version: "5.0"
      includes:
        - kas/poky/scarthgap.yaml
      vendor_overrides:
        - vendor: advantech
          includes:
            - kas/yocto/vendors/advantech/scarthgap.yaml   # Advantech BSP common
          soc_vendors:
            - vendor: nxp
              distro: fsl-imx-xwayland                     # override distro for NXP
              includes:
                - kas/yocto/vendors/advantech/nxp/scarthgap.yaml   # NXP SoC common
              releases:
                - slug: habv4
                  description: "Scarthgap NXP BSP for i.MX8 (HABv4)"
                  includes:
                    - kas/yocto/vendors/advantech/nxp/habv4.yaml
                - slug: ahab
                  description: "Scarthgap NXP BSP for i.MX8M+/i.MX9 (AHAB)"
                  includes:
                    - kas/yocto/vendors/advantech/nxp/ahab.yaml

  features:
    - slug: secure-boot
      description: "NXP Secure Boot (HABv4 / AHAB)"
      # Reject the feature for non-NXP devices at resolve time
      compatibility:
        soc_vendor:
          - nxp
      compatible_with: [yocto]           # Yocto only
      includes:
        - kas/features/secure-boot/common.yaml   # always added for any NXP device
      env:
        # Keys are injected at build time — never stored in the registry
        - name: "SIGNING_KEY"
          value: "$ENV{SIGNING_KEY}"
        - name: "NXP_HAB_PKI_DIR"
          value: "$ENV{NXP_HAB_PKI_DIR}"
      local_conf:
        - 'INHERIT += "image-sign"'
      vendor_overrides:
        - vendor: advantech
          includes:
            - kas/features/secure-boot/advantech-base.yaml  # board-vendor common signing config
          soc_vendors:
            - vendor: nxp
              includes:
                - kas/features/secure-boot/nxp-common.yaml  # NXP SoC-vendor common signing config
              releases:
                # vendor_release slug is shared with the release-level soc_vendors releases
                - slug: habv4
                  description: "HABv4 signing layers"
                  includes:
                    - kas/features/secure-boot/nxp-habv4.yaml
                - slug: ahab
                  description: "AHAB signing layers"
                  includes:
                    - kas/features/secure-boot/nxp-ahab.yaml

  bsp:
    # Signed presets — include the secure-boot feature
    - name: imx8-hab4-scarthgap-secure-boot
      description: "Advantech i.MX8 Scarthgap — HABv4 secure boot"
      device: imx8-hab4-board
      release: scarthgap
      vendor_release: habv4
      features:
        - secure-boot
      build:
        container: "debian-bookworm"
        path: build/imx8-hab4-scarthgap-secure-boot

    - name: imx8mp-ahab-scarthgap-secure-boot
      description: "Advantech i.MX8M Plus Scarthgap — AHAB secure boot"
      device: imx8mp-ahab-board
      release: scarthgap
      vendor_release: ahab
      features:
        - secure-boot
      build:
        container: "debian-bookworm"
        path: build/imx8mp-ahab-scarthgap-secure-boot

    - name: imx93-ahab-scarthgap-secure-boot
      description: "Advantech i.MX93 Scarthgap — AHAB secure boot"
      device: imx93-ahab-board
      release: scarthgap
      vendor_release: ahab
      features:
        - secure-boot
      build:
        container: "debian-bookworm"
        path: build/imx93-ahab-scarthgap-secure-boot

    # Unsigned presets — NXP BSP loaded but no signing feature
    - name: imx8-hab4-scarthgap-unsigned
      description: "Advantech i.MX8 Scarthgap — unsigned (development)"
      device: imx8-hab4-board
      release: scarthgap
      vendor_release: habv4
      features: []
      build:
        container: "debian-bookworm"
        path: build/imx8-hab4-scarthgap-unsigned
```

### KAS file include order

For a HABv4 secure-boot build (`imx8-hab4-scarthgap-secure-boot`):

```
kas/yocto/yocto.yaml                              # framework
vendors/nxp/distro/fsl-imx-xwayland.yaml         # distro (NXP override)
kas/poky/scarthgap.yaml                          # release
kas/yocto/vendors/advantech/scarthgap.yaml       # vendor-common (board)
kas/yocto/vendors/advantech/nxp/scarthgap.yaml   # soc-vendor-common
kas/yocto/vendors/advantech/nxp/habv4.yaml       # soc-vendor-release (BSP)
kas/boards/imx8-hab4-board.yaml                  # device
kas/features/secure-boot/common.yaml             # feature base
kas/features/secure-boot/advantech-base.yaml     # feature vendor-common
kas/features/secure-boot/nxp-common.yaml         # feature soc-vendor-common
kas/features/secure-boot/nxp-habv4.yaml          # feature soc-vendor-release (signing)
```

For the AHAB variant (`imx8mp-ahab-scarthgap-secure-boot`) the order is
identical except that `habv4.yaml` is replaced by `ahab.yaml` at both the BSP
release level and the feature level.

### Environment variables

Keys are **never** stored in the registry.  Export them before running
`bsp build`:

```bash
export SIGNING_KEY=/path/to/signing.key
export NXP_HAB_PKI_DIR=/path/to/hab-pki/   # HABv4
# or
export NXP_HAB_PKI_DIR=/path/to/ahab-pki/  # AHAB
```

The `$ENV{VAR}` placeholders in the feature `env:` block are expanded at build
time by the environment manager.  The build will fail early if a required
variable is unset.

### Operator guide

| Goal | Preset to use |
|------|---------------|
| Production image with HABv4 signing (i.MX8) | `imx8-hab4-scarthgap-secure-boot` |
| Production image with AHAB signing (i.MX8M+) | `imx8mp-ahab-scarthgap-secure-boot` |
| Production image with AHAB signing (i.MX93) | `imx93-ahab-scarthgap-secure-boot` |
| Development / CI image without signing | `imx8-hab4-scarthgap-unsigned` |

Build a signed image:

```bash
export SIGNING_KEY=/secure/keys/imx8-signing.key
export NXP_HAB_PKI_DIR=/secure/keys/hab-pki/
bsp build imx8-hab4-scarthgap-secure-boot
```

Build an unsigned development image (no key required):

```bash
bsp build imx8-hab4-scarthgap-unsigned
```

> **Security note**: Never commit private keys or SRK tables to the registry
> repository.  Use CI/CD secret management (Vault, GitHub Encrypted Secrets,
> Azure Key Vault, AWS Secrets Manager, etc.) to inject key paths at build time.

---

## Prerequisites

| Tool | Version | Source |
|---|---|---|
| NXP CST | ≥ 3.3.2 (AHAB); ≥ 3.1 (HABv4) | [NXP Software Center](https://www.nxp.com/design/software/development-software/manufacturing-tool-suite/code-signing-tool:SW-CST) |
| OpenSSL | ≥ 1.1.1 | Distribution package |
| `uuu` (Universal Update Utility) | latest | [NXP GitHub](https://github.com/nxp-imx/mfgtools) |
| `imx-mkimage` | matching BSP release | BSP Yocto build |

Install CST by extracting the archive from the NXP Software Center and
adding the `linux64/bin/` directory to your `PATH`:

```bash
export PATH="$PATH:/opt/cst/linux64/bin"
cst --version   # should print CST version
```

---

## HABv4 (i.MX6 / i.MX7 / i.MX8)

### 1. Install NXP CST

Download CST from the NXP Software Center and extract it.

```bash
tar -xf cst-3.3.2.tar.gz -C /opt/cst
export PATH="$PATH:/opt/cst/linux64/bin"
```

### 2. Generate the HABv4 PKI tree

CST ships with a helper script that creates the full four-level PKI hierarchy
(CA → SRK → CSF → IMG keys).

```bash
cd /opt/cst/keys

# Interactive: answer the prompts (key length, expiry, CA passphrase).
# For automation, set the environment variable CA_PASSWORD and call
# hab4_pki_tree.sh in non-interactive mode.
export CA_PASSWORD="$(openssl rand -hex 32)"
./hab4_pki_tree.sh
```

After the script completes you will have:

```
keys/
  CA1_sha256_2048_65537_v3_ca_key.pem      ← Root CA private key
  SRK1_sha256_2048_65537_v3_ca_key.pem     ← SRK private key
  CSF1_1_sha256_2048_65537_v3_usr_key.pem  ← CSF private key
  IMG1_1_sha256_2048_65537_v3_usr_key.pem  ← image private key
crts/
  CA1_sha256_2048_65537_v3_ca_crt.pem
  SRK1_sha256_2048_65537_v3_ca_crt.pem
  CSF1_1_sha256_2048_65537_v3_usr_crt.pem
  IMG1_1_sha256_2048_65537_v3_usr_crt.pem
```

> Use at least 4 SRK slots (`SRK1`–`SRK4`) to retain the ability to revoke a
> key after fusing without bricking the device.

### 3. Generate the SRK fuse table

```bash
cd /opt/cst/keys
srktool \
  --hab_ver 4 \
  --certs crts/SRK1_sha256_2048_65537_v3_ca_crt.pem \
          crts/SRK2_sha256_2048_65537_v3_ca_crt.pem \
          crts/SRK3_sha256_2048_65537_v3_ca_crt.pem \
          crts/SRK4_sha256_2048_65537_v3_ca_crt.pem \
  --table SRK_1_2_3_4_table.bin \
  --efuses SRK_1_2_3_4_fuse.bin \
  --digest sha256
```

Keep `SRK_1_2_3_4_fuse.bin` — this is the 32-byte SHA-256 hash of the SRK
table that will be programmed into the SoC fuses.

### 4. Build a signed image

Point the registry `secure-boot` feature at your PKI directory:

```bash
export NXP_HAB_PKI_DIR=/opt/cst   # directory containing keys/ and crts/
export SIGNING_KEY=/opt/cst/keys/IMG1_1_sha256_2048_65537_v3_usr_key.pem

bsp build imx8-hab4-scarthgap-secure-boot
```

The Yocto `imx-hab` class uses these paths to invoke `cst` during the
`do_deploy_hab` task and embeds the CSF binary into the boot image.

### 5. Verify a signed image (host)

```bash
# Extract the IVT / HAB data from the boot image and check with CST:
cst --o /tmp/hab-verify.log \
    --i /path/to/build/imx8-hab4-scarthgap-secure-boot/tmp/deploy/images/\
imx8-hab4-board/imx-boot-imx8-hab4-board.bin-flash_evk \
    verify

grep -E "HAB|Command Sequence|overall" /tmp/hab-verify.log
# Expected: "Command Sequence File verified successfully"
```

Alternatively, use `habtool` from the `python-imx` package:

```bash
pip install imx
habtool info /path/to/imx-boot.bin
```

### 6. Fuse the SoC

> **⚠ IRREVERSIBLE**: Programming fuses is permanent.  Test on a non-production
> board first.  A wrong SRK hash will permanently lock the board.

Connect the board in Serial Download Protocol (SDP) mode and use `uuu`:

```bash
# Write the SRK hash to the fuse shadow registers (dry-run in RAM):
uuu -b qspi /path/to/imx-boot.bin

# Program the actual fuses (permanent):
uuu_imx_fat_image.uuu \
    SRK_1_2_3_4_fuse.bin \
    imx8-hab4-board

# Enable secure-boot mode (HAB closed) — IRREVERSIBLE:
# After this, unsigned images will be refused at boot.
# Do this ONLY after verifying that a signed image boots successfully.
uuu -b qspi /path/to/signed-imx-boot.bin
# Burn SEC_CONFIG fuse word to 0x2 (eFUSE bank 0, word 6 on i.MX8):
uuu FB: ucmd fuse prog 0 6 0x2
```

Board-specific fuse addresses and bank/word numbers differ between SoC
variants.  Always consult the **Security Reference Manual** for your exact
SoC:

| SoC | Document |
|-----|----------|
| i.MX8M / i.MX8M Mini / i.MX8M Nano | i.MX 8M Security Reference Manual (Rev E+) |
| i.MX8M Plus | i.MX 8M Plus Security Reference Manual |
| i.MX8 QuadXPlus / Quad | i.MX 8 Security Reference Manual |

### 7. Verify secure boot on the target

After fusing and rebooting with a signed image, check the HAB status in
U-Boot:

```
=> hab_status

Secure boot enabled

Current SW Version: 0
Failure Analysis:
No failure record present!
```

A response of `Failure Analysis` with any records means the image signature
was rejected — the board is in a locked state and will refuse to boot unsigned
images going forward.

---

## AHAB (i.MX8M+ / i.MX9x)

### 1. Install NXP CST (v3.3.x or later)

AHAB support requires CST 3.3.2 or newer.

```bash
tar -xf cst-3.3.2.tar.gz -C /opt/cst
export PATH="$PATH:/opt/cst/linux64/bin"
cst --version   # must be ≥ 3.3.2
```

### 2. Generate the AHAB PKI tree

```bash
cd /opt/cst/keys
export CA_PASSWORD="$(openssl rand -hex 32)"
./ahab_pki_tree.sh
```

Output layout (per root key slot, up to four):

```
keys/
  SRK1_prime256v1_v3_ca_key.pem      ← SRK private key (ECDSA P-256)
  SGK1_1_prime256v1_v3_usr_key.pem   ← image-signing private key
crts/
  SRK1_prime256v1_v3_ca_crt.pem
  SGK1_1_prime256v1_v3_usr_crt.pem
```

AHAB uses ECDSA P-256 or P-384 by default.  RSA-2048/4096 is also supported
by the CST for HABv4-migration scenarios.

### 3. Build a signed container image

```bash
export NXP_HAB_PKI_DIR=/opt/cst      # directory containing keys/ and crts/
export SIGNING_KEY=/opt/cst/keys/SGK1_1_prime256v1_v3_usr_key.pem

bsp build imx8mp-ahab-scarthgap-secure-boot
# or:
bsp build imx93-ahab-scarthgap-secure-boot
```

The Yocto `imx-ahab` class calls `cst` to produce signed container images
and embeds authentication data in the boot container.

### 4. Verify a signed container image (host)

```bash
ahab_signature_block_parser \
    /path/to/build/imx8mp-ahab-scarthgap-secure-boot/tmp/deploy/images/\
imx8mp-ahab-board/imx-boot-imx8mp-ahab-board.bin-flash_evk_flexspi \
    | grep -E "SRK|SGK|Verified"
# Expected: "Container verified successfully"
```

Alternatively use `imx-mkimage`'s built-in checker:

```bash
mkimage -l /path/to/imx-boot.bin   # prints container header and hash info
```

### 5. Fuse the SoC

Generate the SRKH (Super Root Key Hash) from the SRK certificates:

```bash
# Produces an 8-word (32-byte) SHA-256 hash of the four SRK public keys.
ahab-container-tool srk-hash \
    --srk crts/SRK1_prime256v1_v3_ca_crt.pem \
    --srk crts/SRK2_prime256v1_v3_ca_crt.pem \
    --srk crts/SRK3_prime256v1_v3_ca_crt.pem \
    --srk crts/SRK4_prime256v1_v3_ca_crt.pem \
    -o srkh.bin
xxd srkh.bin   # 32 bytes = 8 fuse words
```

Program the fuses via U-Boot (i.MX8M Plus example — check your SRM for exact
bank/word addresses):

```
# In U-Boot prompt after booting a *signed* image:
# SRKH occupies fuse bank 6, words 0–7 on i.MX8M Plus.
=> fuse prog 6 0 0xAABBCCDD   # word 0 from srkh.bin
=> fuse prog 6 1 0xEEFF0011   # word 1
...
=> fuse prog 6 7 0x99887766   # word 7

# Close the device (AHAB closed mode) — IRREVERSIBLE:
# SEC_CONFIG fuse: bank 2, word 0, bit 25 on i.MX8M Plus.
=> fuse prog 2 0 0x02000000
```

> **⚠ IRREVERSIBLE**: Double-check every fuse word against `srkh.bin` before
> programming.  Use `fuse read` to verify after each write.

### 6. Verify secure boot on the target

In U-Boot after rebooting with a signed image and closed fuses:

```
=> ahab_status

Lifecycle: OEM Closed
No AHAB events found!
```

Any AHAB events reported indicate signature failures.  The board will stop
executing unsigned images.

---

## CI/CD key injection

Keys must never be committed to the repository.  Inject them at build time
using your CI/CD platform's secret store:

**GitHub Actions example:**

```yaml
jobs:
  build-signed:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Restore signing keys from secret store
        env:
          PKI_TAR_B64: ${{ secrets.NXP_PKI_TAR_B64 }}
        run: |
          echo "$PKI_TAR_B64" | base64 -d | tar -xz -C /opt/cst
          chmod 600 /opt/cst/keys/*.pem

      - name: Build signed image
        env:
          NXP_HAB_PKI_DIR: /opt/cst
          SIGNING_KEY: /opt/cst/keys/IMG1_1_sha256_2048_65537_v3_usr_key.pem
        run: |
          bsp build imx8-hab4-scarthgap-secure-boot
```

**GitLab CI example:**

```yaml
build-signed:
  stage: build
  before_script:
    - echo "$NXP_PKI_TAR_B64" | base64 -d | tar -xz -C /opt/cst
    - chmod 600 /opt/cst/keys/*.pem
  script:
    - NXP_HAB_PKI_DIR=/opt/cst
      SIGNING_KEY=/opt/cst/keys/SGK1_1_prime256v1_v3_usr_key.pem
      bsp build imx8mp-ahab-scarthgap-secure-boot
  variables:
    NXP_PKI_TAR_B64: $NXP_PKI_TAR_B64   # from GitLab CI/CD Variables
```

**Azure DevOps example:**

```yaml
- task: DownloadSecureFile@1
  name: NxpPki
  inputs:
    secureFile: 'nxp-pki.tar.gz'

- script: |
    tar -xz -C /opt/cst -f $(NxpPki.secureFilePath)
    chmod 600 /opt/cst/keys/*.pem
    NXP_HAB_PKI_DIR=/opt/cst \
    SIGNING_KEY=/opt/cst/keys/IMG1_1_sha256_2048_65537_v3_usr_key.pem \
    bsp build imx8-hab4-scarthgap-secure-boot
```

---

## Key rotation

HABv4 and AHAB both support up to **four SRK slots**.  To rotate a
compromised key:

1. Program a replacement SRK certificate into one of the unused slots
   **before** revoking the current one.
2. Revoke the compromised key by blowing the corresponding `SRK_REVOKE`
   fuse bits (HABv4) or `SRKH_REVOKE` bits (AHAB).
3. Re-sign images with the new key.

> Once a key slot is revoked it cannot be reinstated.  Plan your SRK
> allocation carefully (recommend: two production slots + two recovery slots).

---

## Troubleshooting

| Symptom | Likely cause | Resolution |
|---|---|---|
| `cst: command not found` | CST not on PATH | `export PATH="$PATH:/opt/cst/linux64/bin"` |
| HAB status: failure records present | Image not signed or wrong key | Re-sign with the correct SRK/IMG key pair |
| U-Boot stops after SPL | SPL not signed or signed with wrong key | Verify SPL signing step in `imx-mkimage` |
| AHAB events: `0x22 - CMD_VIOLATION` | Container header tampered | Rebuild and re-sign from scratch |
| Board does not boot after fusing | Wrong SRK hash written | Hardware is permanently locked — cannot recover |
| `NXP_HAB_PKI_DIR` not set | Environment variable missing | Set before calling `bsp build` |
| Yocto `do_deploy_hab` fails | CST binary not found inside container | Mount or install CST inside the build container |

---

## See also

- [registry-v2.md — NXP Secure Boot](registry-v2.md#nxp-secure-boot) — brief
  registry-v2 schema reference entry that points here
- [NXP Code Signing Tool User Guide](https://www.nxp.com/design/software/development-software/manufacturing-tool-suite/code-signing-tool:SW-CST)
  — official NXP documentation for CST
- [AN12596 — i.MX 8 Secure Boot](https://www.nxp.com/docs/en/application-note/AN12596.pdf)
  — application note for HABv4 on i.MX8
- [AN13709 — i.MX 8M Plus AHAB Secure Boot](https://www.nxp.com/docs/en/application-note/AN13709.pdf)
  — application note for AHAB on i.MX8M Plus
