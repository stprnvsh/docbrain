"""Schema matching via bdi-kit (Valentine + Magneto), optional [matching] extra.

Used under propose-mappings: cheap matcher candidates are computed BEFORE the
LLM call and passed in as evidence (top-k alternatives per target column).
When the matcher covers every required target column with high confidence and
no LLM is available, a matcher-only proposal is stored (lower confidence,
method noted). Degrades to None cleanly when bdi-kit isn't installed."""

from __future__ import annotations

import os

import pandas as pd

DEFAULT_METHOD = os.environ.get("DOCBRAIN_MATCHER", "magneto_zs_bp")


def available() -> bool:
    try:
        import bdikit  # noqa: F401
        return True
    except ImportError:
        return False


def rank_matches(source_df: pd.DataFrame, target: dict, top_k: int = 3,
                 method: str | None = None) -> dict[str, list[dict]] | None:
    """Returns {target_col: [{source, score}, ...]} or None on any failure."""
    if not available():
        return None
    try:
        import bdikit as bdi
        # Magneto profiles target column values — an empty frame crashes it, so
        # synthesize three representative rows per column from the contract.
        dummy = {"str": ["example_a", "example_b", "example_c"],
                 "int": [1, 2, 3], "float": [1.0, 2.0, 3.0],
                 "bool": [True, False, True],
                 "date": pd.to_datetime(["2024-01-01", "2024-06-01", "2025-01-01"])}
        data = {}
        for c in target["columns"]:
            vals = (c.get("allowed") or [])[:3]
            data[c["name"]] = list(vals) + list(dummy.get(c.get("dtype", "str"),
                                                          dummy["str"]))[:3 - len(vals)] \
                if vals else dummy.get(c.get("dtype", "str"), dummy["str"])
        target_df = pd.DataFrame(data)
        ranked = bdi.rank_schema_matches(source_df.head(100), target=target_df,
                                         method=method or DEFAULT_METHOD,
                                         top_k=top_k)
        out: dict[str, list[dict]] = {}
        cols = {c.lower(): c for c in ranked.columns}
        src_c = cols.get("source_attribute") or cols.get("source") or cols.get("source_column")
        tgt_c = cols.get("target_attribute") or cols.get("target") or cols.get("target_column")
        sim_c = cols.get("similarity") or cols.get("score")
        if not (src_c and tgt_c):
            return None
        for _, row in ranked.iterrows():
            tcol = str(row[tgt_c])
            entry = {"source": str(row[src_c]),
                     "score": round(float(row[sim_c]), 3) if sim_c else None}
            out.setdefault(tcol, []).append(entry)
        for tcol in out:
            out[tcol] = sorted(out[tcol], key=lambda e: -(e["score"] or 0))[:top_k]
        return out or None
    except Exception:
        return None
