"""XLSX track, stage 2: islands -> TableCandidates."""

from __future__ import annotations

from pathlib import Path

from ..detectors.xlsx_tables import detect_islands
from ..ir import TableCandidate, frame_from_grid, grid_preview, normalize_col


def extract(path: Path) -> tuple[list[TableCandidate], dict]:
    islands = detect_islands(path)
    candidates: list[TableCandidate] = []
    per_sheet_counter: dict[str, int] = {}
    for isl in islands:
        per_sheet_counter[isl.sheet] = per_sheet_counter.get(isl.sheet, 0) + 1
        idx = per_sheet_counter[isl.sheet]
        origins: dict = {}
        df = frame_from_grid(isl.grid, isl.header_rows, origins_out=origins)
        if df.empty:
            continue
        from openpyxl.utils import get_column_letter
        col_origins = {c: f"{isl.sheet}!{get_column_letter(isl.c0 + i + 1)}"
                       for c, i in origins.items() if c in df.columns}
        name = f"{path.stem}_{normalize_col(isl.sheet)}_t{idx}"
        candidates.append(TableCandidate(
            df=df,
            name=name,
            source_ref=isl.ref,
            method="xlsx-island",
            flags=list(isl.flags),
            notes=[f"header_rows={isl.header_rows}",
                   *(["merged cells in header"] if isl.merged_in_header else [])],
            sketch={
                "sheet": isl.sheet,
                "range": isl.ref,
                "header_rows": isl.header_rows,
                "merged_in_header": isl.merged_in_header,
                "grid_preview": grid_preview(isl.grid),
                "shape": [isl.n_rows, isl.n_cols],
                "column_origins": col_origins,
                "origin_trust": "derived",
            },
        ))
    meta = {"n_islands": len(islands),
            "sheets": sorted({i.sheet for i in islands})}
    return candidates, meta
