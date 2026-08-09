"""Shared intermediate representation + dataframe utilities.

TableCandidate is the unit that flows through the pipeline:
extractor -> (optional agent refinement) -> validation -> schema memory -> store.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class TableCandidate:
    df: pd.DataFrame
    name: str
    source_ref: str          # "Sheet1!B4:F12" | "page 3" | "csv block 2"
    method: str              # csv | xlsx-island | pdf-table | vision | agent
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sketch: dict = field(default_factory=dict)   # extra evidence for agent/context


def normalize_col(name) -> str:
    s = str(name).strip().lower()
    s = re.sub(r"[^\w]+", "_", s)
    return re.sub(r"_+", "_", s).strip("_") or "col"


def dedupe_columns(cols: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for c in cols:
        if c in seen:
            seen[c] += 1
            out.append(f"{c}_{seen[c]}")
        else:
            seen[c] = 0
            out.append(c)
    return out


def coarse_dtype(dtype: str) -> str:
    d = dtype.lower()
    if "int" in d:
        return "int"
    if "float" in d:
        return "float"
    if "bool" in d:
        return "bool"
    if "datetime" in d or "date" in d:
        return "date"
    return "str"


def infer_types(df: pd.DataFrame) -> pd.DataFrame:
    """Best-effort type refinement for object columns: numbers (incl. thousands
    separators / currency), then dates. Leaves anything ambiguous as string."""
    df = df.copy()
    for col in df.columns:
        s = df[col]
        # pandas>=3 stores text as the dedicated 'str' dtype, older as object.
        if not (s.dtype == object or pd.api.types.is_string_dtype(s)):
            continue
        stripped = s.astype(str).str.strip()
        non_null = stripped[(stripped != "") & (stripped.str.lower() != "none") & (stripped.str.lower() != "nan")]
        if non_null.empty:
            continue
        cleaned = non_null.str.replace(r"[\s,']", "", regex=True).str.replace(
            r"^[€$£]|[€$£]$", "", regex=True)
        as_num = pd.to_numeric(cleaned, errors="coerce")
        if as_num.notna().mean() >= 0.9:
            full = stripped.str.replace(r"[\s,']", "", regex=True).str.replace(
                r"^[€$£]|[€$£]$", "", regex=True)
            df[col] = pd.to_numeric(full, errors="coerce")
            continue
        try:
            as_date = pd.to_datetime(non_null, errors="coerce", format="mixed", dayfirst=False)
        except (ValueError, TypeError):
            as_date = pd.Series(pd.NaT, index=non_null.index)
        looks_datey = non_null.str.contains(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}", regex=True).mean() > 0.8
        if as_date.notna().mean() >= 0.9 and looks_datey:
            df[col] = pd.to_datetime(stripped, errors="coerce", format="mixed", dayfirst=False)
    return df


def frame_from_grid(grid: list[list], header_rows: int,
                    origins_out: dict | None = None) -> pd.DataFrame:
    """Build a DataFrame from a raw value grid. Multi-row headers are combined
    with ' / '; zero header rows get positional names. When origins_out is
    given, it is filled with {output_column_name: source_column_index} for
    column-level provenance (indices are grid positions, pre-drop)."""
    body = grid[header_rows:]
    width = max((len(r) for r in grid), default=0)

    def pad(row):
        return list(row) + [None] * (width - len(row))

    if header_rows == 0:
        cols = [f"col_{i}" for i in range(width)]
    else:
        headers = [pad(r) for r in grid[:header_rows]]
        cols = []
        for i in range(width):
            parts = []
            for h in headers:
                v = h[i]
                if v is not None and str(v).strip() and (not parts or str(v).strip() != parts[-1]):
                    parts.append(str(v).strip())
            cols.append(" / ".join(parts) if parts else f"col_{i}")
    cols = dedupe_columns([normalize_col(c) for c in cols])
    if origins_out is not None:
        origins_out.update({c: i for i, c in enumerate(cols)})
    df = pd.DataFrame([pad(r) for r in body], columns=cols)
    df = df.dropna(axis=0, how="all").dropna(axis=1, how="all")
    df.columns = [str(c) for c in df.columns]
    return infer_types(df)


def sample_markdown(df: pd.DataFrame, n: int = 8) -> str:
    with pd.option_context("display.max_columns", 20, "display.width", 200):
        return df.head(n).to_string(index=False, max_colwidth=30)


def grid_preview(grid: list[list], max_rows: int = 14, max_cols: int = 12) -> list[list[str]]:
    out = []
    for row in grid[:max_rows]:
        out.append([("" if v is None else str(v))[:40] for v in row[:max_cols]])
    return out
