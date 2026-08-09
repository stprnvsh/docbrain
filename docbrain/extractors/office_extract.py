"""Office/e-book track via firecrawl-anydoc — the born-digital fast path.

Deterministic, no ML: anydoc converts doc/docx/odt/rtf/epub/ppt/pptx/ods/odp
to a structured Document in milliseconds. Tables (Block.kind == "table",
grid of CellSlots with merge origins) become TableCandidates; text blocks
become context chunks. Closes the docx/pptx gap with no model download."""

from __future__ import annotations

from pathlib import Path

from ..ir import TableCandidate, dedupe_columns, frame_from_grid, grid_preview

ANYDOC_FORMATS = {"doc", "docx", "odt", "rtf", "epub", "ppt", "pptx", "ods", "odp"}


def available() -> bool:
    try:
        import anydoc  # noqa: F401
        return True
    except ImportError:
        return False


def _inlines_text(content) -> str:
    parts = []
    for inline in content or []:
        t = getattr(inline, "text", None)
        if t:
            parts.append(str(t))
        sub = getattr(inline, "content", None)
        if sub:
            parts.append(_inlines_text(sub))
    return " ".join(p for p in parts if p).strip()


def _block_text(block) -> str:
    parts = [_inlines_text(getattr(block, "content", None))]
    for sub in getattr(block, "blocks", None) or []:
        parts.append(_block_text(sub))
    lst = getattr(block, "list", None)
    for item in getattr(lst, "items", None) or []:
        for sub in getattr(item, "blocks", None) or []:
            parts.append(_block_text(sub))
    return "\n".join(p for p in parts if p)


def _cell_text(slot) -> str:
    """grid cells are CellSlots; merged spans repeat the origin cell."""
    cell = getattr(slot, "cell", None)
    if cell is None:
        return ""
    blocks = getattr(cell, "blocks", None)
    if blocks:
        return " ".join(_block_text(b) for b in blocks).strip()
    return _inlines_text(getattr(cell, "content", None))


def extract(path: Path) -> tuple[list[TableCandidate], list[dict], dict]:
    import anydoc
    data = path.read_bytes()
    fmt = None
    try:
        fmt = anydoc.format_from_path(str(path))
    except Exception:
        pass
    doc = anydoc.to_document(data, format=fmt)

    candidates: list[TableCandidate] = []
    texts: list[str] = []
    n_tables = 0
    for block in doc.blocks:
        if block.kind == "table" and block.table is not None:
            n_tables += 1
            t = block.table
            grid = [[_cell_text(slot) for slot in row] for row in t.grid]
            grid = [r for r in grid if any(c.strip() for c in r)]
            if len(grid) < 2 or max(len(r) for r in grid) < 2:
                continue
            header_rows = max(1, int(getattr(t, "header_rows", 1) or 1))
            df = frame_from_grid(grid, header_rows=min(header_rows, len(grid) - 1))
            if df.empty:
                continue
            df.columns = dedupe_columns([str(c) for c in df.columns])
            candidates.append(TableCandidate(
                df=df,
                name=f"{path.stem}_t{n_tables}",
                source_ref=f"{path.suffix.lstrip('.')} table {n_tables}",
                method="office",
                sketch={"engine": "anydoc", "header_rows": header_rows,
                        "grid_preview": grid_preview(grid)},
            ))
        else:
            t = _block_text(block)
            if t:
                texts.append(t)

    if not texts:
        try:
            texts = [anydoc.to_markdown(str(path))]
        except Exception:
            pass
    chunks = []
    buf = ""
    for t in texts:
        if len(buf) + len(t) > 2500 and buf:
            chunks.append({"loc": f"chunk {len(chunks) + 1}", "text": buf.strip()})
            buf = t
        else:
            buf += "\n\n" + t
    if buf.strip():
        chunks.append({"loc": f"chunk {len(chunks) + 1}", "text": buf.strip()})

    meta = {"engine": "anydoc", "n_tables": len(candidates), "n_chunks": len(chunks)}
    return candidates, chunks, meta
