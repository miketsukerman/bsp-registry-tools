#!/usr/bin/env python3
"""
BSP Registry Tools -- PowerPoint Generator
==========================================
Generates a .pptx presentation from BSP Registry Tools content.

Dependencies:
    pip install python-pptx pillow matplotlib

Usage:
    python3 generate_pptx.py
    # produces bsp_registry_tools.pptx in the same directory
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).parent
ARCH_IMG_LEFT_INCHES = 0.7
ARCH_IMG_TOP_INCHES = 1.4
ARCH_IMG_WIDTH_INCHES = 8.6


def build_pptx(output: Path, arch_img: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()

    # Layout aliases
    title_layout = prs.slide_layouts[0]      # Title Slide
    title_body_layout = prs.slide_layouts[1] # Title and Content

    def add_title_slide(title: str, subtitle: str) -> None:
        slide = prs.slides.add_slide(title_layout)
        slide.shapes.title.text = title
        slide.placeholders[1].text = subtitle

    def add_bullets_slide(title: str, bullets: list[str]) -> None:
        slide = prs.slides.add_slide(title_body_layout)
        slide.shapes.title.text = title
        tf = slide.placeholders[1].text_frame
        tf.clear()
        for idx, item in enumerate(bullets):
            p = tf.paragraphs[0] if idx == 0 else tf.add_paragraph()
            p.text = item
            p.level = 0
            p.font.size = Pt(22)

    add_title_slide(
        "BSP Registry Tools",
        "Streamlined Yocto and Isar BSP management\nfor embedded Linux teams",
    )

    add_bullets_slide(
        "What is BSP Registry Tools?",
        [
            "Unified CLI and API for BSP build workflows",
            "YAML registry as a single source of truth",
            "Supports Yocto and Isar release flows",
            "Designed for reproducibility and team collaboration",
        ],
    )

    add_bullets_slide(
        "Core Benefits",
        [
            "Faster onboarding for new engineers and projects",
            "Repeatable builds across devices and releases",
            "Reduced manual errors from ad-hoc build scripts",
            "Improved traceability from config to artifact",
        ],
    )

    add_bullets_slide(
        "Latest Improvements",
        [
            "Build provenance: build-manifest.json generated per build",
            "Repo-manifest export for downstream integration flows",
            "Runtime selection flags with shell completion support",
            "Expanded testing backends and improved direct-test flows",
            "Flexible scan and SBOM workflows for CI pipelines",
            "Multi-registry and named remote collaboration",
        ],
    )

    # Architecture slide (optional image)
    slide = prs.slides.add_slide(title_body_layout)
    slide.shapes.title.text = "System Architecture"
    if arch_img.exists():
        slide.shapes.add_picture(
            str(arch_img),
            Inches(ARCH_IMG_LEFT_INCHES),
            Inches(ARCH_IMG_TOP_INCHES),
            width=Inches(ARCH_IMG_WIDTH_INCHES),
        )
    else:
        slide.placeholders[1].text = "Architecture image not available"

    add_bullets_slide(
        "Typical Workflow",
        [
            "Select preset (or device + release + features)",
            "Build via KAS/kas-container orchestration",
            "Deploy, gather, scan, and test artifacts",
            "Integrate with REST/GraphQL APIs and CI automation",
        ],
    )

    add_bullets_slide(
        "Adoption Path",
        [
            "Start with one product line",
            "Define shared presets and release variants",
            "Roll into CI/CD build, test, and scan pipelines",
            "Expand to multi-team multi-registry operations",
        ],
    )

    add_bullets_slide(
        "Summary",
        [
            "Accelerates delivery while improving reliability",
            "Aligns engineering workflows across teams",
            "Scales from local developer use to enterprise CI/CD",
        ],
    )

    prs.save(str(output))
    print(f"  Generated {output} ({output.stat().st_size:,} bytes, {len(prs.slides)} slides)")


def main() -> None:
    # Reuse the same architecture diagram generation as PDF tooling.
    sys.path.insert(0, str(HERE))
    from generate_pdf import generate_architecture_diagram

    arch_img = HERE / "images" / "architecture.png"
    output = HERE / "bsp_registry_tools.pptx"

    print("[1/2] Generating architecture diagram …")
    generate_architecture_diagram(arch_img)

    print("[2/2] Building PPTX presentation …")
    build_pptx(output, arch_img)

    print("Done.")


if __name__ == "__main__":
    main()
