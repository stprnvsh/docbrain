"""PDF track, stage 1: per-page classification (the pdf-inspector pattern).

Classify each page text | scanned | mixed BEFORE choosing an extraction path,
so native pages never pay for OCR/VLM. Cheap: text length + image coverage."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf

from ..config import PDF_IMAGE_COVERAGE_SCANNED, PDF_TEXT_MIN_CHARS


@dataclass
class PageClass:
    page: int          # 0-based
    kind: str          # text | scanned | mixed | empty
    text_chars: int
    image_coverage: float


def classify_pages(path: Path) -> list[PageClass]:
    doc = pymupdf.open(path)
    out: list[PageClass] = []
    for i, page in enumerate(doc):
        text = page.get_text("text").strip()
        page_area = max(page.rect.get_area(), 1.0)
        img_area = 0.0
        for info in page.get_image_info():
            r = pymupdf.Rect(info["bbox"])
            img_area += r.get_area()
        coverage = min(img_area / page_area, 1.0)
        if len(text) >= PDF_TEXT_MIN_CHARS:
            kind = "mixed" if coverage >= PDF_IMAGE_COVERAGE_SCANNED else "text"
        elif coverage >= PDF_IMAGE_COVERAGE_SCANNED:
            kind = "scanned"
        elif len(text) > 0:
            kind = "text"
        else:
            # No text, no raster images — vector-only drawings (plans, diagrams)
            # are visually meaningful and go to the vision path like scans.
            kind = "vector" if len(page.get_drawings()) > 20 else "empty"
        out.append(PageClass(i, kind, len(text), round(coverage, 3)))
    doc.close()
    return out


def render_page_png(path: Path, page_index: int, out_path: Path, dpi: int = 150) -> Path:
    doc = pymupdf.open(path)
    pix = doc[page_index].get_pixmap(dpi=dpi)
    pix.save(out_path)
    doc.close()
    return out_path
