"""Cross-document linking: which columns across files mean the same thing
(SAME_AS) and which can be joined on shared values (JOINABLE).

Candidate generation is heuristic (name similarity + value overlap on key-ish
columns); an optional LLM pass confirms/rejects candidates in one batch call.
This populates the project knowledge graph that context.py renders."""

from __future__ import annotations

import difflib
import itertools
import json

import pandas as pd

from .llm import LLM, LLMError
from .store import Store
from .agents import prompts

NAME_SIM_MIN = 0.78
VALUE_OVERLAP_MIN = 0.30
SAMPLE_VALUES = 300


def _col_profile(df: pd.DataFrame) -> dict[str, dict]:
    prof = {}
    for c in df.columns:
        s = df[c].dropna()
        uniq = s.nunique()
        entry = {
            "dtype": str(df[c].dtype),
            "uniq_ratio": (uniq / len(s)) if len(s) else 0.0,
            "values": None,
        }
        keyish = pd.api.types.is_string_dtype(df[c]) or df[c].dtype == object \
            or pd.api.types.is_integer_dtype(df[c])
        if keyish and entry["uniq_ratio"] >= 0.15:
            entry["values"] = set(s.astype(str).str.strip().str.lower().head(SAMPLE_VALUES))
        prof[str(c)] = entry
    return prof


def link_project(store: Store, project: str, llm: LLM | None = None) -> list[dict]:
    tables = store.tables(project)
    profiles: dict[str, dict] = {}
    doc_of: dict[str, str] = {}
    for t in tables:
        try:
            df = pd.read_parquet(t["parquet_path"])
        except Exception:
            continue
        profiles[t["table_id"]] = {"name": t["name"], "prof": _col_profile(df)}
        doc_of[t["table_id"]] = t["doc_id"]

    candidates: list[dict] = []
    for (ta, tb) in itertools.combinations(profiles, 2):
        if doc_of.get(ta) == doc_of.get(tb) and profiles[ta]["name"].rsplit("_t", 1)[0] == \
           profiles[tb]["name"].rsplit("_t", 1)[0]:
            pass  # same doc is fine too — stacked tables often share keys
        pa, pb = profiles[ta]["prof"], profiles[tb]["prof"]
        for ca, cb in itertools.product(pa, pb):
            name_sim = difflib.SequenceMatcher(None, ca, cb).ratio()
            ea, eb = pa[ca], pb[cb]
            overlap = 0.0
            if ea["values"] and eb["values"]:
                inter = len(ea["values"] & eb["values"])
                union = len(ea["values"] | eb["values"])
                overlap = inter / union if union else 0.0
            if name_sim >= NAME_SIM_MIN or overlap >= VALUE_OVERLAP_MIN:
                kind = "JOINABLE" if overlap >= VALUE_OVERLAP_MIN else "SAME_AS"
                conf = max(overlap, name_sim * 0.85)
                candidates.append({
                    "src_table": ta, "src_col": ca, "dst_table": tb, "dst_col": cb,
                    "kind": kind, "confidence": round(conf, 2),
                    "evidence": f"name_sim={name_sim:.2f}, value_overlap={overlap:.2f}",
                    "src_name": profiles[ta]["name"], "dst_name": profiles[tb]["name"],
                })

    # LLM confirmation pass (single batch call) — upgrades method + prunes noise.
    method = "heuristic"
    if llm and llm.available and candidates:
        try:
            payload = [{
                "index": i,
                "a": {"table": c["src_name"], "column": c["src_col"]},
                "b": {"table": c["dst_name"], "column": c["dst_col"]},
                "heuristic": {"kind": c["kind"], "evidence": c["evidence"]},
            } for i, c in enumerate(candidates)]
            verdicts = llm.complete_json(
                "Candidates:\n" + json.dumps(payload, indent=1),
                system=prompts.LINK_CONFIRM_SYSTEM, max_tokens=4000)
            if isinstance(verdicts, list):
                keep = []
                for v in verdicts:
                    i = v.get("index")
                    if i is None or not (0 <= i < len(candidates)):
                        continue
                    if v.get("verdict") in ("SAME_AS", "JOINABLE"):
                        c = dict(candidates[i])
                        c["kind"] = v["verdict"]
                        c["confidence"] = float(v.get("confidence", c["confidence"]))
                        keep.append(c)
                candidates = keep
                method = "llm"
        except (LLMError, ValueError):
            pass  # keep heuristic candidates

    store.clear_links(project)
    for c in candidates:
        store.add_link(project, c["src_table"], c["src_col"], c["dst_table"],
                       c["dst_col"], c["kind"], c["confidence"], method, c["evidence"])
    return candidates
