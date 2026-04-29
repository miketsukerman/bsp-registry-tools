#!/usr/bin/env python3
"""
BSP Registry Tools -- PDF Document Generator
============================================
Generates a formatted A4 PDF document from the BSP Registry Tools content.

Dependencies (auto-installed on first run or pre-installed via pip):
    pip install fpdf2 pillow matplotlib

Usage:
    python3 generate_pdf.py
    # produces bsp_registry_tools.pdf in the same directory
"""
from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

HERE = Path(__file__).parent

# ---------------------------------------------------------------------------
# Architecture diagram
# ---------------------------------------------------------------------------

def generate_architecture_diagram(dest: Path) -> Path:
    """Recreate the BSP Registry Tools architecture diagram as a PNG."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyBboxPatch
    except ImportError:
        print("  matplotlib not available -- skipping architecture diagram")
        return dest

    fig, ax = plt.subplots(1, 1, figsize=(14, 11))
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 11)
    ax.axis("off")
    fig.patch.set_facecolor("white")

    def box(
        ax, x, y, w, h, label, sublabel="",
        color="#ffffff", text_color="#222222",
        fontsize=9, border_color="#555555", bold=False,
    ):
        rect = FancyBboxPatch(
            (x - w / 2, y - h / 2), w, h,
            boxstyle="round,pad=0.05",
            linewidth=1.2, edgecolor=border_color, facecolor=color, zorder=3,
        )
        ax.add_patch(rect)
        weight = "bold" if bold else "normal"
        if sublabel:
            ax.text(x, y + 0.12, label, ha="center", va="center",
                    fontsize=fontsize, fontweight=weight, color=text_color, zorder=4)
            ax.text(x, y - 0.18, sublabel, ha="center", va="center",
                    fontsize=fontsize - 1, color=text_color, zorder=4)
        else:
            ax.text(x, y, label, ha="center", va="center", fontsize=fontsize,
                    fontweight=weight, color=text_color, zorder=4)

    def arrow(ax, x1, y1, x2, y2):
        ax.annotate(
            "", xy=(x2, y2), xytext=(x1, y1),
            arrowprops=dict(arrowstyle="-|>", color="#444444", lw=1.2, mutation_scale=14),
            zorder=2,
        )

    def lbl(ax, x, y, text, fontsize=7.5):
        ax.text(x, y, text, ha="center", va="center", fontsize=fontsize,
                color="#666666", style="italic",
                bbox=dict(boxstyle="square,pad=0.15", facecolor="white",
                          edgecolor="none", alpha=0.9),
                zorder=5)

    C_GREEN  = "#d4edda"
    C_TEAL   = "#a8d5c2"
    C_BLUE   = "#b3d4f5"
    C_YELLOW = "#fef3cd"
    C_PURPLE = "#e2d4f0"
    C_WHITE  = "#ffffff"

    # Level 0 -- user
    box(ax, 7, 10.3, 3.2, 0.7,
        "Customer Engineer / Advantech Engineer / CI",
        color=C_GREEN, fontsize=8.5, bold=True)

    # Level 1 -- interfaces
    box(ax, 3.0, 8.8, 2.6, 0.8, "bsp CLI",
        sublabel="CLI interface to bsp-registry", color=C_GREEN, fontsize=8.5, bold=True)
    box(ax, 7.0, 8.8, 2.8, 0.8, "bsp-explorer",
        sublabel="TUI / GUI interface to bsp-registry", color=C_WHITE, fontsize=8.5, bold=True)
    box(ax, 11.2, 8.8, 2.6, 0.8, "bsp server",
        sublabel="( REST , GraphQL )", color=C_WHITE, fontsize=8.5, bold=True)
    arrow(ax, 5.0, 9.95, 3.8, 9.2)
    arrow(ax, 7.0, 9.95, 7.0, 9.2)
    arrow(ax, 9.0, 9.95, 10.5, 9.2)

    # Level 2 -- BspManager API
    box(ax, 7.0, 7.5, 2.8, 0.7, "BspManager API",
        color=C_TEAL, fontsize=9.5, bold=True)
    arrow(ax, 3.0, 8.4, 5.6, 7.85)
    arrow(ax, 7.0, 8.4, 7.0, 7.85)
    arrow(ax, 11.2, 8.4, 8.4, 7.85)

    # Level 3 -- manager outputs
    box(ax, 1.5, 5.7, 2.4, 0.75, "Cloud storage", sublabel="Azure, AWS, etc.",
        color=C_WHITE, fontsize=8)
    arrow(ax, 4.5, 7.15, 2.7, 6.07)
    lbl(ax, 3.3, 6.7, "deploy / download\nbuilt images", 7)

    box(ax, 4.5, 5.7, 2.4, 0.75, "HIL Test setup",
        sublabel="LAVA master", color=C_WHITE, fontsize=8)
    arrow(ax, 5.8, 7.15, 4.9, 6.07)
    lbl(ax, 5.05, 6.7, "Trigger BSP\nimages testing", 7)

    box(ax, 7.6, 5.7, 2.6, 0.75, "BSP Registry\nConfiguration (YAML)",
        color=C_GREEN, fontsize=8)
    arrow(ax, 7.0, 7.15, 7.4, 6.07)
    lbl(ax, 7.0, 6.7, "reading BSP\nregistry model", 7)

    box(ax, 10.5, 5.7, 2.2, 0.75, "KAS Build System",
        color=C_WHITE, fontsize=8)
    arrow(ax, 8.1, 7.15, 10.0, 6.07)
    lbl(ax, 9.2, 6.75, "build images", 7)

    box(ax, 12.9, 5.7, 2.0, 0.75, "Container Engine\n(Docker, Podman)",
        color=C_WHITE, fontsize=7.5)
    arrow(ax, 8.5, 7.15, 12.3, 6.07)
    lbl(ax, 10.9, 6.65, "build\nenvironment", 7)

    # Level 4
    box(ax, 7.0, 4.1, 3.2, 0.9, "bsp-registry.yaml",
        sublabel="git repository / local folder",
        color=C_BLUE, fontsize=8.5, bold=True)
    arrow(ax, 7.6, 5.32, 7.2, 4.55)

    box(ax, 11.5, 4.1, 2.8, 0.75, "Yocto Project / Isar\n(Build System)",
        color=C_WHITE, fontsize=8)
    arrow(ax, 10.5, 5.32, 11.3, 4.47)
    arrow(ax, 12.9, 5.32, 12.0, 4.47)

    box(ax, 1.5, 4.1, 2.4, 0.75, "Cloud storage\nAzure, AWS",
        color=C_WHITE, fontsize=8)
    arrow(ax, 1.5, 5.32, 1.5, 4.47)

    # Level 5
    box(ax, 2.7, 2.4, 2.4, 0.7, "KAS Yaml\nconfigurations",
        color=C_BLUE, fontsize=7.5)
    box(ax, 5.4, 2.4, 2.0, 0.7, "Docker\nfiles",
        color=C_YELLOW, fontsize=7.5)
    box(ax, 7.8, 2.4, 2.2, 0.7, "LAVA test\nconfigurations",
        color=C_YELLOW, fontsize=7.5)
    box(ax, 10.2, 2.4, 2.2, 0.7, "Cloud storage\nconfiguration",
        color=C_PURPLE, fontsize=7.5)
    box(ax, 12.5, 2.4, 2.4, 0.7, "Source Layers /\nDebian Packages",
        color=C_WHITE, fontsize=7.5)

    arrow(ax, 5.7, 3.65, 3.5, 2.75)
    arrow(ax, 6.5, 3.65, 5.6, 2.75)
    arrow(ax, 7.0, 3.65, 7.8, 2.75)
    arrow(ax, 7.5, 3.65, 9.9, 2.75)
    arrow(ax, 1.5, 3.72, 2.0, 2.75)
    arrow(ax, 11.5, 3.72, 12.2, 2.75)

    ax.set_title("BSP Registry Tools -- System Architecture",
                 fontsize=13, fontweight="bold", pad=8, color="#222222")
    plt.tight_layout(pad=0.3)
    plt.savefig(str(dest), dpi=150, bbox_inches="tight", facecolor="white")
    plt.close()
    print(f"  Generated {dest} ({dest.stat().st_size:,} bytes)")
    return dest


# ---------------------------------------------------------------------------
# PDF builder
# ---------------------------------------------------------------------------

def build_pdf(output: Path, arch_img: Path) -> None:
    from fpdf import FPDF

    # ── Colours ────────────────────────────────────────────────────────────
    TITLE_R, TITLE_G, TITLE_B     = 30, 80, 150
    SECTION_R, SECTION_G, SECTION_B = 0, 100, 80
    SUBSEC_R, SUBSEC_G, SUBSEC_B  = 60, 60, 60
    CODE_BG_R, CODE_BG_G, CODE_BG_B = 245, 245, 245
    CODE_FG_R, CODE_FG_G, CODE_FG_B = 40, 40, 120
    TABLE_HEAD_R, TABLE_HEAD_G, TABLE_HEAD_B = 210, 230, 240
    ACCENT_R, ACCENT_G, ACCENT_B  = 0, 140, 110

    FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
    FONT_SANS         = str(FONT_DIR / "DejaVuSans.ttf")
    FONT_SANS_BOLD    = str(FONT_DIR / "DejaVuSans-Bold.ttf")
    FONT_SANS_OBLIQUE = str(FONT_DIR / "DejaVuSans-Oblique.ttf")
    FONT_MONO         = str(FONT_DIR / "DejaVuSansMono.ttf")
    FONT_MONO_BOLD    = str(FONT_DIR / "DejaVuSansMono-Bold.ttf")

    class BSPDoc(FPDF):
        _toc: list[tuple[str, int, str]]  # (title, level, dest_id)

        def __init__(self):
            super().__init__(orientation="P", unit="mm", format="A4")
            self._toc = []
            self.set_margins(20, 25, 20)
            self.set_auto_page_break(auto=True, margin=20)
            # Register Unicode fonts
            self.add_font("Sans",      style="",  fname=FONT_SANS)
            self.add_font("Sans",      style="B", fname=FONT_SANS_BOLD)
            self.add_font("Sans",      style="I", fname=FONT_SANS_OBLIQUE)
            self.add_font("Mono",      style="",  fname=FONT_MONO)
            self.add_font("Mono",      style="B", fname=FONT_MONO_BOLD)

        # ── helpers ────────────────────────────────────────────────────────
        def hr(self, color=(180, 180, 180)):
            self.set_draw_color(*color)
            self.set_line_width(0.3)
            x0 = self.l_margin
            x1 = self.w - self.r_margin
            y  = self.get_y()
            self.line(x0, y, x1, y)
            self.ln(2)

        def section(self, title: str, level: int = 1) -> None:
            """Numbered heading with auto-TOC registration."""
            self.ln(4)
            dest = title.replace(" ", "_").replace("/", "_")
            self._toc.append((title, level, dest))
            if level == 1:
                self.set_font("Sans", "B", 14)
                self.set_text_color(SECTION_R, SECTION_G, SECTION_B)
                self.set_fill_color(235, 248, 244)
                self.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT", fill=True)
                self.ln(1)
                self.hr((180, 220, 210))
            elif level == 2:
                self.set_font("Sans", "B", 11)
                self.set_text_color(SUBSEC_R, SUBSEC_G, SUBSEC_B)
                self.cell(0, 6, title, new_x="LMARGIN", new_y="NEXT")
                self.ln(1)
            self.set_text_color(0, 0, 0)

        def body(self, text: str, fontsize: int = 10) -> None:
            self.set_font("Sans", "", fontsize)
            self.set_text_color(40, 40, 40)
            self.multi_cell(0, 5.5, text)
            self.ln(1)

        def bullet(self, items: list[str], indent: float = 4) -> None:
            self.set_font("Sans", "", 10)
            self.set_text_color(40, 40, 40)
            for item in items:
                x0 = self.get_x()
                y0 = self.get_y()
                self.set_fill_color(ACCENT_R, ACCENT_G, ACCENT_B)
                self.ellipse(self.l_margin + indent - 1, y0 + 2.2, 1.5, 1.5, "F")
                self.set_x(self.l_margin + indent + 2)
                self.multi_cell(0, 5.5, item)
            self.ln(1)

        def code_block(self, code: str, fontsize: int = 8) -> None:
            self.set_fill_color(CODE_BG_R, CODE_BG_G, CODE_BG_B)
            self.set_draw_color(200, 200, 200)
            self.set_line_width(0.2)
            self.set_font("Mono", "", fontsize)
            self.set_text_color(CODE_FG_R, CODE_FG_G, CODE_FG_B)
            lines = code.strip().splitlines()
            pad_x = 4
            line_h = 4.8
            total_h = len(lines) * line_h + 4
            x0 = self.l_margin
            y0 = self.get_y()
            w  = self.w - self.l_margin - self.r_margin
            if y0 + total_h > self.h - self.b_margin - 5:
                self.add_page()
                y0 = self.get_y()
            self.rect(x0, y0, w, total_h, "FD")
            self.set_y(y0 + 2)
            for line in lines:
                self.set_x(x0 + pad_x)
                self.cell(0, line_h, line, new_x="LMARGIN", new_y="NEXT")
            self.ln(3)
            self.set_text_color(40, 40, 40)

        def table(
            self,
            headers: list[str],
            rows: list[list[str]],
            col_widths: list[float] | None = None,
        ) -> None:
            usable = self.w - self.l_margin - self.r_margin
            if col_widths is None:
                col_widths = [usable / len(headers)] * len(headers)
            # header
            self.set_fill_color(TABLE_HEAD_R, TABLE_HEAD_G, TABLE_HEAD_B)
            self.set_draw_color(160, 180, 200)
            self.set_line_width(0.25)
            self.set_font("Sans", "B", 9)
            self.set_text_color(30, 30, 30)
            for i, h in enumerate(headers):
                self.cell(col_widths[i], 7, h, border=1, fill=True)
            self.ln()
            # rows
            self.set_font("Sans", "", 9)
            for ri, row in enumerate(rows):
                fill = ri % 2 == 0
                self.set_fill_color(252, 252, 252) if fill else self.set_fill_color(243, 248, 253)
                for i, cell in enumerate(row):
                    self.cell(col_widths[i], 6.5, cell, border=1, fill=True)
                self.ln()
            self.ln(3)
            self.set_text_color(40, 40, 40)

        # ── header / footer ────────────────────────────────────────────────
        def header(self):
            if self.page_no() == 1:
                return
            self.set_font("Sans", "I", 8)
            self.set_text_color(130, 130, 130)
            self.cell(0, 8, "BSP Registry Tools -- Technical Reference", align="L")
            self.ln()
            self.hr((200, 200, 200))

        def footer(self):
            if self.page_no() == 1:
                return
            self.set_y(-15)
            self.set_font("Sans", "I", 8)
            self.set_text_color(140, 140, 140)
            self.cell(0, 5, f"Page {self.page_no()}", align="C")

        # ── cover page ─────────────────────────────────────────────────────
        def cover_page(self):
            self.add_page()
            self.set_y(40)
            # Top accent bar
            self.set_fill_color(ACCENT_R, ACCENT_G, ACCENT_B)
            self.rect(0, 35, 210, 5, "F")

            self.set_font("Sans", "B", 30)
            self.set_text_color(TITLE_R, TITLE_G, TITLE_B)
            self.cell(0, 16, "BSP Registry Tools", align="C", new_x="LMARGIN", new_y="NEXT")

            self.set_font("Sans", "", 15)
            self.set_text_color(60, 60, 60)
            self.cell(0, 9,
                      "Streamlined Yocto & Isar BSP Management for Embedded Linux Teams",
                      align="C", new_x="LMARGIN", new_y="NEXT")
            self.ln(4)

            self.set_font("Sans", "I", 11)
            self.set_text_color(90, 90, 90)
            self.cell(0, 7, "From registry to built image \u2014 one command",
                      align="C", new_x="LMARGIN", new_y="NEXT")
            self.ln(12)

            # key facts box
            self.set_fill_color(235, 248, 244)
            self.set_draw_color(ACCENT_R, ACCENT_G, ACCENT_B)
            self.set_line_width(0.5)
            bw, bh = 120, 38
            bx = (210 - bw) / 2
            by = self.get_y()
            self.rect(bx, by, bw, bh, "FD")
            self.set_y(by + 6)
            self.set_font("Sans", "", 10)
            self.set_text_color(40, 40, 40)
            lines = [
                "[pkg]  pip install bsp-registry-tools",
                "->  github.com/Advantech-EECC/bsp-registry",
                "(c)   Apache 2.0 License",
            ]
            for ln_text in lines:
                self.set_x(bx + 8)
                self.cell(bw - 16, 7, ln_text, new_x="LMARGIN", new_y="NEXT")

            # Bottom accent bar
            self.set_fill_color(ACCENT_R, ACCENT_G, ACCENT_B)
            self.rect(0, 282, 210, 5, "F")
            self.set_text_color(255, 255, 255)
            self.set_font("Sans", "", 8)
            self.set_y(283)
            self.cell(0, 4, "Advantech EECC -- BSP Registry Tools Technical Reference",
                      align="C")

        # ── TOC ────────────────────────────────────────────────────────────
        def toc_page(self):
            self.add_page()
            self.set_font("Sans", "B", 16)
            self.set_text_color(SECTION_R, SECTION_G, SECTION_B)
            self.cell(0, 10, "Table of Contents", new_x="LMARGIN", new_y="NEXT")
            self.hr((180, 220, 210))
            self.ln(3)
            for title, level, _ in self._toc:
                indent = 8 if level == 2 else 0
                self.set_font("Sans", "B" if level == 1 else "", 10 if level == 1 else 9.5)
                self.set_text_color(30, 30, 30)
                self.set_x(self.l_margin + indent)
                self.cell(0, 6.5, ("  " if level == 2 else "") + title,
                          new_x="LMARGIN", new_y="NEXT")

    # ── Build document ──────────────────────────────────────────────────────
    pdf = BSPDoc()

    # ── 1. Cover ──────────────────────────────────────────────────────────
    pdf.cover_page()

    # ── 2. TOC placeholder (filled after content) ─────────────────────────
    toc_placeholder = 2  # page 2 is TOC

    # ── 3. Main content starts page 3 ─────────────────────────────────────
    pdf.add_page()

    # ── Overview ──────────────────────────────────────────────────────────
    pdf.section("1. Overview")
    pdf.body(
        "BSP Registry Tools is a Python CLI and library for managing Board Support Packages "
        "(BSPs) for embedded Linux systems. It uses a YAML-based registry (schema v2.0) as the "
        "single source of truth, covering devices, Yocto/Isar releases, composable features, "
        "named presets, Docker container definitions, cloud deployment, and CI/CD integration."
    )
    pdf.bullet([
        "Python CLI tool (bsp) and importable Python API (BspManager, V2Resolver, …)",
        "YAML registry v2.0 -- devices, releases, features, presets, environments",
        "Wraps KAS (kas / kas-container) for reproducible Yocto and Isar builds",
        "Zero-config first run -- auto-clones the Advantech registry on first use",
        "Cloud artifact deploy / gather (Azure Blob Storage, AWS S3)",
        "HTTP server with REST + GraphQL APIs (FastAPI + Strawberry)",
        "Shell tab completions for all arguments",
    ])

    # ── Architecture diagram ───────────────────────────────────────────────
    pdf.section("2. System Architecture")
    pdf.body(
        "The diagram below shows how the BSP Registry Tools components interact with each "
        "other and with external systems. The BspManager API is the central hub connecting "
        "all user-facing interfaces (CLI, TUI/GUI explorer, HTTP server) to the underlying "
        "build infrastructure (KAS, container engines, cloud storage, LAVA test infrastructure)."
    )
    if arch_img.exists():
        pdf.image(str(arch_img), x=15, w=180)
        pdf.ln(4)
    else:
        pdf.body("[Architecture diagram image not available]")

    pdf.section("Component Roles", level=2)
    pdf.table(
        ["Component", "Role"],
        [
            ["bsp CLI", "Primary user interface -- build, list, export, deploy, gather, registry CRUD"],
            ["bsp-explorer", "TUI / GUI interface (interactive terminal explorer)"],
            ["bsp server", "HTTP REST + GraphQL server for automation and dashboards"],
            ["BspManager API", "Core Python library coordinating all operations"],
            ["BSP Registry YAML", "Single source of truth: devices, releases, features, presets"],
            ["KAS Build System", "Orchestrates Yocto BitBake or Isar builds from YAML config files"],
            ["Container Engine", "Docker / Podman provides isolated, reproducible build environments"],
            ["Cloud Storage", "Azure Blob / AWS S3 for artifact upload (deploy) and download (gather)"],
            ["HIL Test / LAVA", "Hardware-in-the-loop test automation via LAVA"],
        ],
        col_widths=[55, 120],
    )

    # ── Installation ──────────────────────────────────────────────────────
    pdf.section("3. Installation")
    pdf.body("Python 3.8 or later is required. A virtual environment is strongly recommended.")
    pdf.section("Core install", level=2)
    pdf.code_block("pip install bsp-registry-tools")

    pdf.section("Optional extras", level=2)
    pdf.table(
        ["Extra", "Installs"],
        [
            ["[azure]",       "Azure Blob Storage upload / download"],
            ["[aws]",         "AWS S3 upload / download"],
            ["[server]",      "FastAPI + uvicorn + Strawberry GraphQL"],
            ["[completions]", "Shell tab completions (argcomplete)"],
            ["[dev]",         "pytest, coverage, ruff, …"],
        ],
        col_widths=[35, 140],
    )
    pdf.code_block('pip install "bsp-registry-tools[azure,completions]"')

    # ── Supported Hardware ────────────────────────────────────────────────
    pdf.section("4. Supported Hardware")

    pdf.section("NXP i.MX Boards (Advantech Europe)", level=2)
    pdf.table(
        ["Board", "SoC", "Yocto Releases", "Status"],
        [
            ["RSB-3720",    "i.MX8M Plus", "scarthgap, styhead, walnascar, whinlatter", "[OK] Stable"],
            ["RSB-3720 4G", "i.MX8M Plus", "walnascar, whinlatter",                     "[OK] Stable"],
            ["RSB-3720 6G", "i.MX8M Plus", "walnascar, whinlatter",                     "[OK] Stable"],
            ["ROM-2620",    "i.MX8",       "scarthgap, styhead, walnascar, whinlatter", "[OK] Stable"],
            ["ROM-2820",    "i.MX93",      "scarthgap, styhead, walnascar, whinlatter", "[OK] Stable"],
            ["ROM-5720",    "i.MX8",       "scarthgap, styhead, walnascar, whinlatter", "[OK] Stable"],
            ["ROM-5721",    "i.MX8",       "scarthgap, styhead, walnascar, whinlatter", "[OK] Stable"],
            ["ROM-5722",    "i.MX8",       "scarthgap, styhead, walnascar, whinlatter", "[OK] Stable"],
            ["AOM-5521",    "i.MX95",      "scarthgap, walnascar",                      "[OK] Stable"],
        ],
        col_widths=[30, 22, 100, 23],
    )

    pdf.section("MediaTek & Qualcomm Boards", level=2)
    pdf.table(
        ["Board", "SoC", "Releases", "Status"],
        [
            ["RSB-3810",           "MediaTek MT8395", "scarthgap", "[WIP] Development"],
            ["Genio 1200 EVK",     "MediaTek MT8395", "scarthgap", "[WIP] Development"],
            ["AOM-2721",           "Qualcomm QCS6490","scarthgap", "[WIP] Development"],
            ["QCS6490 RB3 Gen2",   "Qualcomm QCS6490","scarthgap", "[WIP] Development"],
        ],
        col_widths=[42, 42, 40, 51],
    )

    pdf.section("Emulated Targets (QEMU)", level=2)
    pdf.bullet([
        "qemuarm64 -- 64-bit ARM (AArch64)",
        "qemuarm   -- 32-bit ARM",
        "qemux86   -- x86 32-bit",
        "qemux86-64 / qemuamd64 -- x86-64",
    ])

    # ── Supported Releases ────────────────────────────────────────────────
    pdf.section("5. Supported Releases")
    pdf.section("Yocto Releases", level=2)
    pdf.table(
        ["Slug", "Yocto Version", "LTS", "Notes"],
        [
            ["kirkstone",  "4.0", "LTS", "poky, fsl-imx-xwayland"],
            ["mickledore",  "4.2", "",   "poky, fsl-imx-xwayland"],
            ["nanbield",    "4.3", "",   "poky"],
            ["scarthgap",   "5.0", "LTS","poky, fsl-imx-xwayland, mediatek, qualcomm, ros2"],
            ["styhead",     "5.1", "",   "poky, fsl-imx-xwayland"],
            ["walnascar",   "5.2", "",   "poky, fsl-imx-xwayland"],
            ["whinlatter",  "5.3", "",   "poky, fsl-imx-xwayland"],
            ["wrynose",     "6.0", "",   "poky (upcoming)"],
        ],
        col_widths=[32, 28, 15, 100],
    )
    pdf.section("Isar (Debian-based) Releases", level=2)
    pdf.table(
        ["Slug", "Distribution", "Base Image"],
        [
            ["debian-trixie", "Debian 13 (Trixie)", "isar-debian-13 container"],
            ["ubuntu-noble",  "Ubuntu 24.04 LTS",   "isar-debian-13 container"],
            ["ubuntu-jammy",  "Ubuntu 22.04 LTS",   "isar-debian-13 container"],
        ],
        col_widths=[40, 55, 80],
    )

    # ── Registry Schema ───────────────────────────────────────────────────
    pdf.section("6. Registry Schema v2")
    pdf.body(
        "The registry uses a YAML file (default: bsp-registry.yml) with schema version 2.0. "
        "The file decomposes into independent, reusable sections:"
    )
    pdf.table(
        ["Section", "Purpose"],
        [
            ["frameworks",    "Build-system definitions (Yocto, Isar)"],
            ["distro",        "Distribution definitions (Poky, fsl-imx-xwayland, Isar v1.0, …)"],
            ["vendors",       "Cross-release board-vendor KAS fragments"],
            ["devices",       "Hardware board definitions (slug, vendor, SoC, KAS includes)"],
            ["releases",      "Yocto / Isar release definitions with vendor overrides"],
            ["features",      "Optional add-ons (OTA, secure-boot, ROS 2, hailo, …)"],
            ["bsp",           "Named presets = device + release(s) + features"],
            ["environments",  "Container + variable bundles per build class"],
            ["containers",    "Docker image definitions"],
            ["deploy",        "Cloud artifact upload configuration (Azure / AWS)"],
            ["include",       "Split large registries across multiple files"],
        ],
        col_widths=[38, 137],
    )

    pdf.section("Device Definition Example", level=2)
    pdf.code_block("""\
registry:
  devices:
    - slug: rsb3720
      description: "Advantech RSB-3720 (i.MX8M Plus, 6GB)"
      vendor: advantech-europe
      soc_vendor: nxp
      architecture: arm64
      includes:
        - vendors/advantech-europe/nxp/machine/imx8/rsb3720.yml""")

    pdf.section("Named Preset Example", level=2)
    pdf.code_block("""\
registry:
  bsp:
    - name: modular-bsp-rsb3720
      description: "Advantech RSB-3720 (i.MX8)"
      device: rsb3720
      releases: [scarthgap, styhead, walnascar, whinlatter]
      features: [systemd, security, virtualization, ipv6, usrmerge]
      build:
        path: build/modular-bsp-rsb3720""")

    pdf.section("Vendor Override Example", level=2)
    pdf.body(
        "Vendor overrides allow a single release slug (e.g. scarthgap) to apply "
        "different KAS fragments and distro settings per hardware vendor / SoC combination, "
        "with optional kernel sub-release pinning:"
    )
    pdf.code_block("""\
releases:
  - slug: scarthgap
    distro: poky
    includes: [compilers/clang/clang.yml, yocto/releases/scarthgap.yml]
    vendor_overrides:
      - vendor: advantech-europe
        soc_vendors:
          - vendor: nxp
            distro: fsl-imx-xwayland
            includes: [vendors/advantech-europe/nxp/modular-bsp-nxp.yml]
            releases:
              - slug: imx-6.6.52-2.2.2
                includes: [vendors/advantech-europe/nxp/imx-6.6.52-2.2.2-scarthgap.yml]""")

    # ── Features ──────────────────────────────────────────────────────────
    pdf.section("7. Feature System")
    pdf.body(
        "Features are optional, composable add-ons that can be mixed into any BSP preset. "
        "They support vendor_overrides and release_overrides so the correct KAS fragments "
        "are injected automatically based on the target hardware and Yocto release."
    )
    pdf.table(
        ["Slug", "Description"],
        [
            ["systemd",      "Enable systemd as the init system"],
            ["yocto-ssh",    "Include SSH server in the image"],
            ["debug-tweaks", "Enable debug build tweaks (empty root password, …)"],
            ["root-login",   "Enable root login"],
            ["security",     "Security hardening meta-layer"],
            ["secure-boot",  "NXP HAB / AHAB cryptographic image signing"],
            ["virtualization","KVM / container virtualisation support"],
            ["wayland",      "Wayland display server support"],
            ["x11",          "X11 display server support"],
            ["ipv6",         "IPv6 networking"],
            ["udev",         "udev device manager"],
            ["usrmerge",     "/usr merge (FHS compliance)"],
            ["rauc",         "RAUC Over-the-Air update framework"],
            ["swupdate",     "SWUpdate OTA framework"],
            ["ostree",       "OSTree atomic filesystem upgrades"],
            ["hailo",        "Hailo-8 AI accelerator support"],
            ["ros2",         "ROS 2 (Robot Operating System 2) via ros2-humble-scarthgap release"],
        ],
        col_widths=[35, 140],
    )

    # ── OTA ───────────────────────────────────────────────────────────────
    pdf.section("8. OTA Update Support")
    pdf.body(
        "Three OTA technologies are supported as first-class composable features. "
        "All major Advantech Europe NXP boards support all three OTA methods."
    )
    pdf.table(
        ["Technology", "Slug", "Supported Boards", "Releases"],
        [
            ["RAUC",    "rauc",     "RSB-3720, ROM-2620, ROM-2820, ROM-5720/21/22", "scarthgap → whinlatter"],
            ["SWUpdate","swupdate", "RSB-3720, ROM-2620, ROM-2820, ROM-5720/21/22", "scarthgap → whinlatter"],
            ["OSTree",  "ostree",   "RSB-3720, ROM-2620, ROM-2820, ROM-5720/21/22", "scarthgap → whinlatter"],
        ],
        col_widths=[25, 25, 80, 45],
    )
    pdf.code_block("""\
# Build RSB-3720 with RAUC OTA (Walnascar release)
bsp build modular-bsp-rauc-rsb3720 --release walnascar

# Build RSB-3720 with SWUpdate
bsp build modular-bsp-swupdate-rsb3720 --release scarthgap

# Build RSB-3720 with OSTree
bsp build modular-bsp-ostree-rsb3720 --release walnascar""")

    # ── Secure Boot ───────────────────────────────────────────────────────
    pdf.section("9. Secure Boot")
    pdf.body(
        "NXP-based Advantech boards support cryptographic boot-image signing via HAB "
        "(i.MX8 family) and AHAB (i.MX9 / i.MX95 family). The secure-boot feature uses the "
        "ubuntu-22.04-csb container which automatically mounts the NXP CST signing tool and "
        "keys from the host into the container."
    )
    pdf.table(
        ["SoC Family", "Technology", "Affected Boards"],
        [
            ["i.MX8",  "HAB (High Assurance Boot)",           "RSB-3720, ROM-2620, ROM-5720, ROM-5721, ROM-5722"],
            ["i.MX93", "AHAB (Advanced High Assurance Boot)", "ROM-2820"],
            ["i.MX95", "AHAB",                                "AOM-5521"],
        ],
        col_widths=[25, 65, 85],
    )
    pdf.code_block("""\
# Export signing key environment variables
export CST_TOOL_PATH=/opt/nxp/cst/
export KEYS_PATH=/path/to/srk-keys/

# Build ROM-5721 2G with Secure Boot (uses ubuntu-22.04-csb container)
bsp build modular-bsp-rom5721-2g-db5901-secureboot --release walnascar""")

    # ── Containers ────────────────────────────────────────────────────────
    pdf.section("10. Containers & Environments")
    pdf.body(
        "Container definitions bundle a Dockerfile, a Docker image name, build args, "
        "and optional volume mounts. Named environments combine a container with "
        "build-system environment variables."
    )
    pdf.table(
        ["Container", "Base OS", "KAS Ver.", "Use Case"],
        [
            ["ubuntu-20.04",     "Ubuntu 20.04", "4.7", "Kirkstone / Mickledore legacy builds"],
            ["ubuntu-22.04",     "Ubuntu 22.04", "5.2", "Default Yocto builds"],
            ["ubuntu-22.04-csb", "Ubuntu 22.04", "5.2", "Secure Boot builds (key mounts)"],
            ["ubuntu-24.04",     "Ubuntu 24.04", "5.2", "Latest Yocto builds"],
            ["debian-12",        "Debian 12",    "5.2", "Hardened / alternative builds"],
            ["debian-13",        "Debian 13",    "5.2", "Debian-based Yocto builds"],
            ["isar-debian-13",   "Debian 13",    "5.2", "Isar privileged builds"],
        ],
        col_widths=[42, 30, 22, 81],
    )

    # ── CLI Reference ─────────────────────────────────────────────────────
    pdf.section("11. CLI Reference")
    pdf.table(
        ["Command", "Description"],
        [
            ["bsp list",                         "List all named BSP presets"],
            ["bsp list devices",                  "List all device slugs"],
            ["bsp list releases",                 "List all release slugs"],
            ["bsp list features",                 "List all feature slugs"],
            ["bsp containers",                    "List container definitions"],
            ["bsp build <preset>",                "Build a named preset"],
            ["bsp build <preset> --release <r>",  "Build preset with a specific release"],
            ["bsp build <preset> --checkout",     "Validate config without building (CI gate)"],
            ["bsp build <preset> --deploy",       "Build and deploy artifacts to cloud storage"],
            ["bsp build <preset> --clean",        "Clean build dir before building"],
            ["bsp shell <preset>",                "Open interactive shell in build container"],
            ["bsp export <preset>",               "Export resolved KAS config to stdout or file"],
            ["bsp deploy <preset>",               "Upload build artifacts to cloud storage"],
            ["bsp gather <preset>",               "Download artifacts from cloud storage"],
            ["bsp registry init",                 "Scaffold a new registry YAML file"],
            ["bsp registry validate",             "Validate registry schema and references"],
            ["bsp registry diff <f1> <f2>",       "Show unified diff between two registry files"],
            ["bsp registry add device …",         "Add a new device entry"],
            ["bsp registry add release …",        "Add a new release entry"],
            ["bsp registry add feature …",        "Add a new feature entry"],
            ["bsp remotes add <name> <url>",      "Save a named remote registry URL"],
            ["bsp remotes show",                  "List all saved remotes"],
            ["bsp remotes remove <name>",         "Remove a saved remote"],
            ["bsp server",                        "Start REST + GraphQL HTTP server"],
            ["bsp completions bash|zsh|fish",     "Print shell completion script"],
        ],
        col_widths=[80, 95],
    )

    # ── CLI Flags ─────────────────────────────────────────────────────────
    pdf.section("Global CLI Flags", level=2)
    pdf.table(
        ["Flag", "Description"],
        [
            ["--registry <path>", "Use an explicit local registry file"],
            ["--local",           "Use ./bsp-registry.yml, no network"],
            ["--remote <url>",    "Specify remote registry URL (or saved remote name)"],
            ["--branch <b>",      "Branch to checkout when using --remote"],
            ["--no-update",       "Skip git-pull of the remote registry"],
        ],
        col_widths=[45, 130],
    )

    # ── Zero-Config Quick Start ────────────────────────────────────────────
    pdf.section("Quick Start", level=2)
    pdf.code_block("""\
# First run: clones the Advantech development registry automatically
bsp list

# Build Advantech RSB-3720 with the default release
bsp build modular-bsp-rsb3720

# Build with a specific Yocto release
bsp build modular-bsp-rsb3720 --release walnascar

# Validate config quickly (no full build)
bsp build modular-bsp-rsb3720 --checkout

# Use the Advantech development branch explicitly
bsp --remote https://github.com/Advantech-EECC/bsp-registry.git@development list""")

    # ── Python API ────────────────────────────────────────────────────────
    pdf.section("12. Python API")
    pdf.body(
        "All CLI operations are backed by a public Python API. The key entry point is "
        "BspManager, which coordinates registry loading, preset resolution, builds, "
        "deployment and artifact gathering."
    )
    pdf.code_block("""\
from bsp import BspManager, RegistryFetcher, V2Resolver

# 1. Fetch / update the remote registry
fetcher = RegistryFetcher()
registry_path = fetcher.fetch_registry(
    repo_url="https://github.com/Advantech-EECC/bsp-registry.git",
    branch="development",
    update=True,
)

# 2. Load registry and inspect contents
manager = BspManager(str(registry_path))
manager.initialize()
for dev in manager.model.registry.devices:
    print(dev.slug, dev.description)

# 3. Resolve a preset into a full build configuration
resolver = V2Resolver(manager.model)
config = resolver.resolve(
    device_slug="rsb3720",
    release_slug="walnascar",
    feature_slugs=["systemd", "security", "rauc"],
)
for f in config.kas_files:
    print(f)""")

    pdf.section("Multi-Registry Mode", level=2)
    pdf.code_block("""\
from bsp import BspManager

manager = BspManager(
    config_paths=[
        ("advantech",    "/path/to/advantech-registry.yaml"),
        ("my-additions", "/path/to/my-registry.yaml"),
    ]
)
manager.initialize()
# Disambiguate with registry:preset syntax
config = manager.resolve("advantech:modular-bsp-rsb3720")""")

    # ── HTTP Server ───────────────────────────────────────────────────────
    pdf.section("13. HTTP Server (REST + GraphQL)")
    pdf.code_block('pip install "bsp-registry-tools[server]"')
    pdf.code_block("bsp server --port 8080")
    pdf.section("REST API Endpoints", level=2)
    pdf.table(
        ["Method", "Path", "Description"],
        [
            ["GET",  "/api/v1/devices",           "List all devices"],
            ["GET",  "/api/v1/releases",          "List all releases"],
            ["GET",  "/api/v1/features",          "List all features"],
            ["GET",  "/api/v1/bsp",               "List all named presets"],
            ["POST", "/api/v1/bsp/{name}/build",  "Trigger a build"],
            ["POST", "/graphql",                  "GraphQL endpoint"],
        ],
        col_widths=[18, 70, 87],
    )
    pdf.code_block("""\
# GraphQL query example
curl -X POST http://localhost:8080/graphql \\
  -H 'Content-Type: application/json' \\
  -d '{"query": "{ releases { slug description yoctoVersion } }"}'""")

    # ── CI/CD ─────────────────────────────────────────────────────────────
    pdf.section("14. CI/CD Integration")
    pdf.code_block("""\
# .github/workflows/build.yml
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install bsp-registry-tools
        run: pip install "bsp-registry-tools[azure]"

      - name: Validate registry
        run: bsp --local registry validate

      - name: Fast checkout gate (no full build)
        run: bsp --no-update build modular-bsp-rsb3720 --checkout

      - name: Build and deploy artifacts
        env:
          AZURE_STORAGE_ACCOUNT_URL: ${{ secrets.AZURE_STORAGE_ACCOUNT_URL }}
        run: bsp --no-update build modular-bsp-rsb3720 --release walnascar --deploy""")

    # ── Extending BSPs ────────────────────────────────────────────────────
    pdf.section("15. Extending BSPs with Custom Yocto Layers")
    pdf.body(
        "A BSP registry KAS configuration can be included in your own project, "
        "allowing you to add custom Yocto layers on top of an existing BSP without "
        "modifying the registry itself."
    )
    pdf.code_block("""\
# my-custom-kas.yaml
header:
  version: 19
  includes:
    - repo: bsp-registry
      file: modular-bsp-rsb3720-walnascar.yaml   # from `bsp export`

repos:
  bsp-registry:
    url: "https://github.com/Advantech-EECC/bsp-registry"
    branch: "development"
    layers: { .: "disabled" }

  meta-custom:
    layers:
      meta-custom:    # your custom Yocto meta-layer""")

    pdf.code_block("""\
# meta-custom/meta-custom/recipes-core/imx-image-%.bbappend
CORE_IMAGE_EXTRA_INSTALL += "mpv"
LICENSE_FLAGS_ACCEPTED += "commercial"

# Build with your extension
kas build my-custom-kas.yaml""")

    # ── Summary ───────────────────────────────────────────────────────────
    pdf.section("16. Summary")
    pdf.table(
        ["Feature", "Benefit"],
        [
            ["YAML registry v2",         "Single source of truth for 30+ boards x 8+ releases"],
            ["Auto remote fetch",         "Zero-config first run; teams share the Advantech registry"],
            ["Two build systems",         "Yocto/BitBake AND Isar/Debian in one registry"],
            ["KAS integration",           "Reproducible builds for every device x release combo"],
            ["Named environments",        "Correct container per build class (secure-boot, Isar, …)"],
            ["Feature system",            "Composable add-ons: OTA, secure-boot, ROS 2, Hailo"],
            ["OTA support",               "RAUC, SWUpdate, OSTree -- all first-class features"],
            ["Secure Boot",               "NXP HAB / AHAB signing built into the registry"],
            ["Cloud deploy / gather",     "Full artifact lifecycle: build → upload → download"],
            ["HTTP server",               "REST + GraphQL for dashboards and automation"],
            ["Tab completions",           "Fast CLI use with context-aware completions"],
            ["Python API",                "Integrate into custom tools and CI scripts"],
        ],
        col_widths=[55, 120],
    )

    pdf.ln(6)
    pdf.set_font("Sans", "I", 10)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(
        0, 7,
        "Registry: github.com/Advantech-EECC/bsp-registry (development branch)",
        align="C",
    )

    # ── Insert TOC page (rebuild _toc before output) ──────────────────────
    pdf.toc_page()

    # ── Output ────────────────────────────────────────────────────────────
    pdf.output(str(output))
    print(f"  Generated {output} ({output.stat().st_size:,} bytes, {pdf.page_no()} pages)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    arch_img = HERE / "images" / "architecture.png"
    output   = HERE / "bsp_registry_tools.pdf"

    print("[1/2] Generating architecture diagram …")
    generate_architecture_diagram(arch_img)

    print("[2/2] Building PDF document …")
    build_pdf(output, arch_img)

    print("Done.")


if __name__ == "__main__":
    main()
