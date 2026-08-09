"""CSV track, stage 2: blocks -> TableCandidates (with malformed-row handling)."""

from __future__ import annotations

from pathlib import Path

from collections import Counter

from ..detectors.csv_dialect import read_block, sniff_file
from ..ir import TableCandidate, frame_from_grid


def _normalize_widths(rows: list[list]) -> tuple[list[list], int]:
    """Tasheeh-style light repair: standardize every row to the modal width —
    extra trailing fields are folded into the last column, short rows padded."""
    width = Counter(len(r) for r in rows).most_common(1)[0][0]
    fixed = []
    for r in rows:
        if len(r) > width:
            r = r[:width - 1] + [" | ".join(str(x) for x in r[width - 1:])]
        elif len(r) < width:
            r = list(r) + [None] * (width - len(r))
        fixed.append(r)
    return fixed, width


def extract(path: Path) -> tuple[list[TableCandidate], dict]:
    sniff = sniff_file(path)
    candidates: list[TableCandidate] = []

    # Unstructured-text guard: files routed here by extension (.txt logs etc.)
    # that aren't actually delimited tables. Signal: the modal row is 1 field
    # wide. Return no tables; ship the head as a text chunk instead.
    all_rows = [read_block(b, sniff.delimiter, sniff.quotechar)[0] for b in sniff.blocks]
    widths = Counter(len(r) for rows in all_rows for r in rows)
    if widths and widths.most_common(1)[0][0] <= 1:
        head_text = "\n".join(
            line for b in sniff.blocks[:4] for line in b.text.splitlines()[:40])
        return [], {"encoding": sniff.encoding, "unstructured": True,
                    "head_text": head_text[:2500], "notes": sniff.notes
                    + ["not a delimited table — captured header text only"]}

    for block in sniff.blocks:
        rows, bad = read_block(block, sniff.delimiter, sniff.quotechar)
        if not rows:
            continue
        flags, notes = [], list(sniff.notes)
        if bad:
            flags.append("malformed_rows")
            notes.append(f"{len(bad)} ragged row(s) repaired to modal width "
                         f"(block-relative index {bad[:10]})")
        rows, _width = _normalize_widths(rows)
        # Header heuristic: first row is header if it's mostly non-numeric.
        first = rows[0]
        numericish = sum(_is_number(v) for v in first)
        header_rows = 0 if numericish > len(first) / 2 else 1
        if header_rows == 0:
            flags.append("no_obvious_header")
        origins: dict = {}
        df = frame_from_grid(rows, header_rows, origins_out=origins)
        col_origins = {c: f"field {i + 1}" for c, i in origins.items() if c in df.columns}
        name_suffix = f"_block{block.index + 1}" if len(sniff.blocks) > 1 else ""
        candidates.append(TableCandidate(
            df=df,
            name=f"{path.stem}{name_suffix}",
            source_ref=f"csv block {block.index + 1} (lines {block.start_line + 1}-{block.start_line + block.n_lines})",
            method="csv",
            flags=flags,
            notes=notes,
            sketch={"delimiter": sniff.delimiter, "encoding": sniff.encoding,
                    "ragged_rows": bad[:20], "n_lines": block.n_lines,
                    "column_origins": col_origins, "origin_trust": "derived"},
        ))
    meta = {"encoding": sniff.encoding, "delimiter": sniff.delimiter,
            "n_blocks": len(sniff.blocks), "notes": sniff.notes}
    return candidates, meta


def _is_number(v: str) -> bool:
    try:
        float(str(v).replace(",", "").replace("'", "").strip() or "x")
        return True
    except ValueError:
        return False
