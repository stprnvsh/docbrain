"""PDF track, stage 2.

Text pages  -> PyMuPDF native text (markdown chunks) + find_tables() for tables.
Scanned pages -> rendered to PNG for the vision fallback (handled by the agent
layer); if no vision backend is available they are recorded for human review.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pymupdf4llm

from ..detectors.pdf_pages import classify_pages
from ..ir import TableCandidate, dedupe_columns, infer_types, normalize_col


def extract(path: Path, scratch: Path) -> tuple[list[TableCandidate], list[dict], dict]:
    """Returns (table candidates, text chunks, meta). Scanned pages are flagged in
    meta["scanned_pages"] with pre-rendered PNGs for the vision step."""
    pages = classify_pages(path)
    text_pages = [p.page for p in pages if p.kind in ("text", "mixed")]
    scanned_pages = [p.page for p in pages if p.kind in ("scanned", "vector")]

    candidates: list[TableCandidate] = []
    chunks: list[dict] = []

    doc = pymupdf.open(path)
    for pno in text_pages:
        page = doc[pno]
        finder = page.find_tables()
        for ti, tab in enumerate(finder.tables):
            try:
                df = tab.to_pandas()
            except Exception:
                continue
            if df.empty or len(df.columns) < 2:
                continue
            df.columns = dedupe_columns([normalize_col(c) for c in df.columns])
            df = infer_types(df)
            flags = []
            if any(str(c).startswith("col") and str(c[-1:]).isdigit() for c in df.columns):
                flags.append("weak_header_names")
            candidates.append(TableCandidate(
                df=df,
                name=f"{path.stem}_p{pno + 1}_t{ti + 1}",
                source_ref=f"page {pno + 1}",
                method="pdf-table",
                flags=flags,
                sketch={"page": pno + 1, "bbox": list(tab.bbox)},
            ))

    # Markdown chunks for the text/context layer (native path — cheap).
    if text_pages:
        try:
            md = pymupdf4llm.to_markdown(doc, pages=text_pages, show_progress=False)
            for i, piece in enumerate(_split_markdown(md)):
                chunks.append({"loc": f"md chunk {i + 1}", "text": piece})
        except Exception:
            for pno in text_pages:
                txt = doc[pno].get_text("text").strip()
                if txt:
                    chunks.append({"loc": f"page {pno + 1}", "text": txt[:4000]})

    scanned = []
    for pno in scanned_pages:
        png = scratch / f"{path.stem}_p{pno + 1}.png"
        rect = doc[pno].rect
        # Cap the long edge ~2300px (vision-model friendly), whatever the page size.
        dpi = max(60, min(150, int(2300 / max(rect.width, rect.height) * 72)))
        pix = doc[pno].get_pixmap(dpi=dpi)
        pix.save(png)
        scanned.append({"page": pno + 1, "png": str(png)})

    doc.close()
    meta = {
        "pages": [{"page": p.page + 1, "kind": p.kind, "text_chars": p.text_chars,
                   "image_coverage": p.image_coverage} for p in pages],
        "scanned_pages": scanned,
        "n_pages": len(pages),
    }
    return candidates, chunks, meta


def extract_docling(path: Path, max_pages: int | None = None) -> tuple[list[TableCandidate], list[dict], dict]:
    """Docling front-end for the hard-layout tail: TableFormer table structure
    + OCR. Heavy ([docling] extra); called directly (engine=docling) or via
    auto-escalation from ingest."""
    from ..config import DOCLING_MAX_PAGES
    from docling.document_converter import DocumentConverter

    conv = DocumentConverter()
    res = conv.convert(str(path), max_num_pages=max_pages or DOCLING_MAX_PAGES)
    doc = res.document
    candidates: list[TableCandidate] = []
    for ti, table in enumerate(doc.tables):
        try:
            df = table.export_to_dataframe()
        except Exception:
            continue
        if df.empty or len(df.columns) < 2:
            continue
        df.columns = dedupe_columns([normalize_col(c) for c in df.columns])
        df = infer_types(df)
        page_no = None
        try:
            page_no = table.prov[0].page_no
        except (AttributeError, IndexError):
            pass
        candidates.append(TableCandidate(
            df=df,
            name=f"{path.stem}_p{page_no or '?'}_dt{ti + 1}",
            source_ref=f"page {page_no}" if page_no else "docling",
            method="pdf-docling",
            sketch={"page": page_no, "engine": "docling"},
        ))
    chunks = [{"loc": f"docling chunk {i + 1}", "text": piece}
              for i, piece in enumerate(_split_markdown(doc.export_to_markdown()))]
    meta = {"engine": "docling", "n_tables": len(candidates),
            "n_pages": getattr(res.document, "num_pages", lambda: 0)()
                       if callable(getattr(res.document, "num_pages", None))
                       else len(getattr(doc, "pages", []) or [])}
    return candidates, chunks, meta


def should_escalate_to_docling(candidates: list[TableCandidate], meta: dict) -> str | None:
    """Auto-engine triggers: scanned/vector pages present, or table-dense pages
    that produced no native tables."""
    kinds = {p["kind"] for p in meta.get("pages", [])}
    if "scanned" in kinds or "vector" in kinds:
        return "scanned/vector pages present"
    if not candidates and meta.get("n_pages", 0) > 0:
        return "no native tables found"
    return None


def _split_markdown(md: str, target: int = 2500) -> list[str]:
    """Split on headings, then greedily pack to ~target chars."""
    import re
    parts = re.split(r"(?m)^(?=#{1,4} )", md)
    out: list[str] = []
    buf = ""
    for p in parts:
        if not p.strip():
            continue
        if len(buf) + len(p) > target and buf:
            out.append(buf.strip())
            buf = p
        else:
            buf += p
    if buf.strip():
        out.append(buf.strip())
    return out
