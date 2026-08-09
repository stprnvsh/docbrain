"""Validation + confidence gate (SheetBrain's Validation module, generalized).

Deterministic checks produce a confidence score per table; anything under the
threshold is queued for human review instead of being trusted blind."""

from __future__ import annotations

import pandas as pd

from .config import REVIEW_THRESHOLD
from .ir import TableCandidate

# Prior confidence by extraction method — native parses are trusted more than
# layout-recovered or vision-recovered tables.
METHOD_PRIOR = {
    "csv": 0.95,
    "xlsx-island": 0.90,
    "script": 0.90,       # curated parser that already proved itself on this format
    "office": 0.85,       # anydoc deterministic conversion
    "pdf-docling": 0.85,  # TableFormer layout recovery
    "pdf-table": 0.80,
    "agent": 0.85,
    "vision": 0.65,
}


def score(candidate: TableCandidate) -> tuple[float, list[str], bool]:
    """Returns (confidence, issues, needs_review)."""
    df = candidate.df
    issues: list[str] = []
    conf = METHOD_PRIOR.get(candidate.method, 0.7)

    if df.empty:
        return 0.0, ["empty table"], True

    # Weak/positional column names suggest a missed header.
    weak = sum(1 for c in df.columns if str(c).startswith("col_") or str(c).startswith("unnamed"))
    if weak:
        ratio = weak / len(df.columns)
        conf -= 0.15 * ratio
        issues.append(f"{weak}/{len(df.columns)} positional column names")

    # First data row repeating the header (double-header artifact).
    first = df.iloc[0]
    matches = sum(1 for c in df.columns
                  if isinstance(first[c], str) and first[c].strip().lower() == str(c).strip().lower())
    if matches >= max(2, len(df.columns) // 2):
        conf -= 0.15
        issues.append("first data row repeats header")

    # Sparse columns.
    null_heavy = [c for c in df.columns if df[c].isna().mean() > 0.6]
    if null_heavy:
        conf -= min(0.15, 0.04 * len(null_heavy))
        issues.append(f"{len(null_heavy)} column(s) >60% null: {null_heavy[:4]}")

    # Mixed python types inside object columns (str+number mixes).
    mixed = []
    for c in df.columns:
        if df[c].dtype == object and not pd.api.types.is_string_dtype(df[c]):
            kinds = {type(v).__name__ for v in df[c].dropna().head(50)}
            if len(kinds - {"NoneType"}) > 1:
                mixed.append(str(c))
    if mixed:
        conf -= min(0.1, 0.03 * len(mixed))
        issues.append(f"mixed-type column(s): {mixed[:4]}")

    # Flags raised upstream count against confidence.
    for flag in candidate.flags:
        penalty = {"malformed_rows": 0.10, "merged_header_uncertain": 0.08,
                   "multi_row_header": 0.03, "no_obvious_header": 0.10,
                   "weak_header_names": 0.05, "single_row_island": 0.15}.get(flag, 0.05)
        conf -= penalty
        issues.append(f"flag: {flag}")

    conf = max(0.0, min(1.0, conf))
    return conf, issues, conf < REVIEW_THRESHOLD


def contract_stats(df: pd.DataFrame) -> dict:
    """Learn light-weight expectations from a dataframe — remembered in the schema
    registry and checked when a similar file arrives later (drift detection)."""
    out = {}
    for c in df.columns:
        s = df[c]
        entry: dict = {"dtype": str(s.dtype), "null_ratio_max": round(min(1.0, s.isna().mean() + 0.15), 3)}
        if pd.api.types.is_numeric_dtype(s) and s.notna().any():
            lo, hi = float(s.min()), float(s.max())
            span = (hi - lo) or max(abs(hi), 1.0)
            entry["min"] = lo - 0.5 * span
            entry["max"] = hi + 0.5 * span
        out[str(c)] = entry
    return out


def check_contracts(df: pd.DataFrame, contracts: dict) -> list[str]:
    drift: list[str] = []
    for c, rules in contracts.items():
        if c not in df.columns:
            drift.append(f"missing remembered column '{c}'")
            continue
        s = df[c]
        from .ir import coarse_dtype
        if coarse_dtype(str(s.dtype)) != coarse_dtype(rules.get("dtype", "")):
            drift.append(f"'{c}' dtype {s.dtype} vs remembered {rules.get('dtype')}")
        if s.isna().mean() > rules.get("null_ratio_max", 1.0):
            drift.append(f"'{c}' null ratio {s.isna().mean():.2f} exceeds remembered bound")
        if "min" in rules and pd.api.types.is_numeric_dtype(s) and s.notna().any():
            if float(s.min()) < rules["min"] or float(s.max()) > rules["max"]:
                drift.append(f"'{c}' values outside remembered range")
    new_cols = [str(c) for c in df.columns if str(c) not in contracts]
    if new_cols:
        drift.append(f"new column(s) not in remembered schema: {new_cols[:5]}")
    return drift
