# BSP Assistant Architecture

## Overview

**BSP Assistant** is an intelligent, interactive discovery and recommendation system integrated into the `bsp` tool suite. Designed for embedded Linux developers, it bridges the gap between hardware boards, feature requirements, and complex Yocto/Isar layer configurations by providing conversational discovery, multi-criteria preset scoring, compatibility validation, and risk assessment.

---

## 1. System Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                             USER TERMINAL / CI                              │
│                                                                             │
│   $ bsp assistant discover       $ bsp assistant explain <preset>           │
│   $ bsp assistant check ...      $ bsp assistant find --feature=...         │
│   $ bsp assistant list-devices   $ bsp assistant suggest --board=...        │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            BSP CLI (bsp/cli.py)                             │
│                                                                             │
│  • Argument parsing (`assistant` subparser)                                 │
│  • BspManager initialization (local registry, git remotes, cache)           │
│  • RegistryRoot pass-through to Assistant subsystem                         │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Assistant CLI (bsp/assistant/cli.py)                  │
│                                                                             │
│  • Multi-step interactive prompt workflows (Rich console / prompt)          │
│  • Non-interactive / CI automation execution paths                          │
│  • Colorized tables, panels, build recommendations, and warnings            │
└──────────────┬───────────────────────────────────────────────┬──────────────┘
               │                                               │
               ▼                                               ▼
┌───────────────────────────────┐               ┌─────────────────────────────┐
│     Recommendation Engine     │               │       Registry Parser       │
│(bsp/assistant/recommendation_ │               │(bsp/assistant/registry_     │
│          engine.py)           │               │          parser.py)         │
│                               │               │                             │
│  • Multi-criteria scoring     │◄──────────────┤  • YAML resolution &        │
│  • Feature compatibility      │  RegistryData │    recursive include:       │
│  • Release maturity assessment│  Matrix       │  • Extraction of Presets,   │
│  • Risk & warning analysis    │               │    Devices, Features        │
│  • Alternative finder         │               │  • Vendor override & sub-rel│
└──────────────┬────────────────┘               └──────────────┬──────────────┘
               │                                               │
               │           ┌───────────────────────┐           │
               └──────────►│  Data Models & Cache  │◄──────────┘
                           │ (bsp/assistant/       │
                           │   models.py, cache.py)│
                           │                       │
                           │ • PresetInfo          │
                           │ • DeviceInfo          │
                           │ • FeatureInfo         │
                           │ • CompatibilityMatrix │
                           │ • Thread-Safe Cache   │
                           └───────────────────────┘
```

---

## 2. User Workflow Diagram

The end-to-end journey from requirement discovery to successful image artifact deployment:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. DISCOVERY & SELECTION                                                    │
│                                                                             │
│   User executes: `bsp assistant discover`                                   │
│                                                                             │
│   [Step 1: Board Prompt]     ──► Select hardware (e.g. RSB-3720, ROM-5720)  │
│   [Step 2: Feature Prompt]   ──► Choose capabilities (e.g. RAUC, HAB boot)  │
│   [Step 3: Release Prompt]   ──► Preference: Latest / LTS / Specific        │
│                                                                             │
│   Assistant performs multi-criteria scoring & risk evaluation               │
│   Outputs: Recommended Preset, Confidence Score, Build Estimate,            │
│            Reasoning Breakdown, and Alternative Options                     │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 2. CONFIGURATION VALIDATION & INSPECTION                                    │
│                                                                             │
│   User inspects chosen preset:                                              │
│     `bsp assistant explain modular-bsp-rauc-rsb3720-wrynose`                │
│                                                                             │
│   User validates custom feature set against hardware constraints:           │
│     `bsp assistant check --board=rsb3720 --features=rauc,secure-boot`       │
│                                                                             │
│   Outputs: Compatibility report, vendor SoC compatibility, known issues     │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 3. BUILD EXECUTION                                                          │
│                                                                             │
│   User runs recommended command:                                            │
│     `bsp build modular-bsp-rauc-rsb3720-wrynose`                            │
│                                                                             │
│   • KAS configuration composed with selected layers and distro fragments    │
│   • Containerized build environment spawned (Docker/Podman)                 │
│   • BitBake compilation produces bootloader, kernel, rootfs, and bundles    │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 4. POST-BUILD VERIFICATION & DEPLOYMENT                                     │
│                                                                             │
│   • Security & SBOM scan:     `bsp scan <preset>` (CRA compliance)          │
│   • SD Card / eMMC flashing:  `bsp flash <preset> --target=/dev/sdX`        │
│   • Hardware test execution:  `bsp test <preset> --backend=lava`            │
│   • Artifact publishing:      `bsp deploy <preset>` (Cloud storage / index) │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Data Model Diagram

```
                 ┌────────────────────────────────┐
                 │          RegistryData          │
                 ├────────────────────────────────┤
                 │ + presets: List[PresetInfo]    │
                 │ + devices: List[DeviceInfo]    │
                 │ + features: List[FeatureInfo]  │
                 │ + releases: List[str]          │
                 │ + matrix: CompatibilityMatrix  │
                 │ + raw_root: RegistryRoot       │
                 └───────────────┬────────────────┘
                                 │
         ┌───────────────────────┼────────────────────────┐
         │ 1..*                  │ 1..*                   │ 1..*
         ▼                       ▼                        ▼
┌──────────────────┐    ┌─────────────────┐    ┌─────────────────────┐
│    PresetInfo    │    │   DeviceInfo    │    │     FeatureInfo     │
├──────────────────┤    ├─────────────────┤    ├─────────────────────┤
│ + name: str      │    │ + slug: str     │    │ + slug: str         │
│ + description    │    │ + name: str     │    │ + name: str         │
│ + device: str    │◄───┤ + vendor: str   │    │ + description: str  │
│ + releases: list │    │ + soc_family    │    │ + compatible_vendors│
│ + features: list ├────┼─────────────────┼───►│ + compat_releases   │
│ + stability_stat │    │ + supported_feat│    └─────────────────────┘
│ + tested_combos  │    └─────────────────┘
└────────┬─────────┘
         │
         │ Evaluated By
         ▼
┌────────────────────────────────┐             ┌─────────────────────┐
│      RecommendationEngine      │◄────────────┤  UserRequirements   │
├────────────────────────────────┤             ├─────────────────────┤
│ + score_preset(...)            │             │ + board_name: str?  │
│ + check_compatibility(...)     │             │ + features: list    │
│ + get_risk_assessment(...)     │             │ + release_pref: str │
│ + find_alternatives(...)       │             │ + release_ver: str? │
└────────┬───────────────────────┘             └─────────────────────┘
         │
         ▼ Generates
┌────────────────────────────────┐             ┌─────────────────────┐
│         Recommendation         │             │   RiskAssessment    │
├────────────────────────────────┤             ├─────────────────────┤
│ + preset_name: str             │             │ + preset_name: str  │
│ + confidence_score: float      │             │ + risk_level: str   │
│ + reasoning: List[str]         │             │ + warnings: list    │
│ + alternatives: List[str]      │             │ + is_tested: bool   │
│ + known_issues: List[str]      │             │ + is_eol: bool      │
│ + build_time_estimate: str     │             └─────────────────────┘
└────────────────────────────────┘
```

---

## 4. Registry Parser Flow

The registry parser loads hierarchical YAML trees, resolves `include:` directives recursively, merges components, and constructs the in-memory compatibility matrix:

```
  Path to bsp-registry.yaml
             │
             ▼
    [RegistryCache.get()]
      ├── Cache Hit  ──► Return existing RegistryData (O(1))
      └── Cache Miss ──┐
                       ▼
         [get_registry_from_yaml_file()]
                       │
       ┌───────────────┴───────────────┐
       ▼                               ▼
 [Recursive Resolution]       [Root Registry Object]
   • `include:` tags            • Presets collection
   • Relative file imports      • Devices & Overrides
   • Vendor definitions         • Features & Distros
   • Layers & Kas fragments     • Releases catalog
       └───────────────┬───────────────┘
                       │
                       ▼
       [RegistryParser.load_registry_root()]
                       │
  ┌────────────────────┼────────────────────┐
  ▼                    ▼                    ▼
[extract_presets()]  [extract_devices()]  [extract_features()]
  • Name & description • Device slugs       • Feature slugs & names
  • Stability status   • Vendor & SoC       • Compatible vendors
  • Features list      • Hardware features  • Compatible releases
  • Single/multi rels  • Sub-releases       • Included layers
  └────────────────────┼────────────────────┘
                       │
                       ▼
         [get_compatibility_matrix()]
    Computes cross-product compatibility:
      • Device ◄──► Features
      • Feature ◄──► Releases
      • Vendor constraints
                       │
                       ▼
            [RegistryCache.set()]
                       │
                       ▼
             Returns RegistryData
```

---

## 5. Recommendation Engine Decision Tree

When evaluating presets against user requirements, the engine scores each candidate across 4 dimensions totaling up to 100 points:

```
                  Start: Evaluate Candidate Preset
                                 │
     ────────────────────────────┴────────────────────────────
    │                                                         │
    ▼                                                         ▼
[Hardware / Board Match]                     [Feature Coverage & Matching]
  Max: 30 points                               Max: 35 points
  • Exact slug match: +30                      • Ratio: matched / requested
  • Normalized match: +28                      • Full feature match: +35
  • Vendor match: +15                          • Partial match: + (ratio * 35)
  • No match: 0                                • Surplus features: -2 per extra
    │                                                         │
    └────────────────────────────┬────────────────────────────┘
                                 │
     ────────────────────────────┴────────────────────────────
    │                                                         │
    ▼                                                         ▼
[Release Maturity & Match]                   [Testing & Proven Status]
  Max: 25 points                               Max: 10 points
  • Release requested?                         • tested_combinations == True: +10
    - Exact match: +25                         • Untested combination: 0
    - Missing: 0                               • Deduct for risk:
  • No release specified:                        - EOL release: -20
    - LTS release: +25                           - Bleeding-edge/dev: -5
    - Stable latest: +20
    - Bleeding-edge: +10
    │                                                         │
    └────────────────────────────┬────────────────────────────┘
                                 │
                                 ▼
                    [Compute Total Score (0-100)]
                                 │
                                 ▼
                     [Risk Assessment Filter]
            ├── EOL release detected? ──► Add warning & reduce score
            ├── Missing requested features? ──► Flag in reasoning
            └── Untested combination? ──► Mark confidence penalty
                                 │
                                 ▼
                 [Select Highest Scoring Candidate]
            • Formulate reasoning bullet points
            • Find top 3 alternative presets
            • Estimate build duration based on target framework
            • Return Recommendation
```

---

## 6. CLI Integration Points

BSP Assistant seamlessly integrates into the main `bsp` command line interface without breaking existing workflows:

```
bsp [global options] <command> [command options]
  ├── --registry <path>   ──► Custom local registry path
  ├── --remote <url>      ──► Remote git registry specification
  ├── --branch <branch>   ──► Git branch for remote registry
  ├── --verbose / -v      ──► Debug logging
  └── --no-color          ──► Plain text output

Commands:
  ├── build               ──► Existing build subsystem
  ├── deploy              ──► Existing artifact deployment
  ├── scan                ──► Existing CRA vulnerability scanner
  ├── flash               ──► Existing image flashing
  ├── test                ──► Existing LAVA / local test runner
  ├── ...
  │
  └── assistant           ──► [NEW] Assistant Subsystem Entry Point
        ├── discover      ──► Interactive / automated discovery wizard
        ├── explain       ──► Preset deep dive & risk breakdown
        ├── check         ──► Hardware / feature compatibility check
        ├── find          ──► Feature search across boards & presets
        ├── list-devices  ──► Hardware inventory table
        ├── list-features ──► Feature catalog table
        └── suggest       ──► Board-specific preset suggestions
```

### Integration Details
1. **Registry Reuse**: The assistant utilizes the already parsed `BspManager.registry` object initialized by `bsp/cli.py`, eliminating redundant disk I/O and network fetches.
2. **Global Flag Inheritance**: Global flags like `--registry`, `--remote`, `--branch`, and `--verbose` are fully respected by the assistant commands.
3. **Shell Completions**: Subcommands, boards, presets, features, and releases leverage existing `argcomplete` completers (`PresetsCompleter`, `DevicesCompleter`, `FeaturesCompleter`, `ReleasesCompleter`).
4. **Zero Breaking Changes**: All existing CLI subcommands and options operate completely unchanged.
