"""Golden-corpus regression eval — the gate for any change to heuristics,
prompts, models, or parser integrations (Wave 0 of the adoption plan).

Two modes:
- corpus eval: deterministically re-ingest a sample corpus (--no-llm) into a
  throwaway project and diff against a frozen fixture;
- catalog snapshot: freeze/compare the current state of a real project's
  catalog (used for LLM-dependent corpora like winterthur, where re-ingest
  isn't deterministic).
"""

from __future__ import annotations

import json
from pathlib import Path

from .ir import coarse_dtype
from .store import Store

EVAL_PROJECT = "__eval__"


def snapshot_project(store: Store, project: str) -> dict:
    docs = store.documents(project)
    tables = store.tables(project)
    by_doc: dict[str, list] = {}
    for t in tables:
        by_doc.setdefault(t["doc_id"], []).append(t)
    out = {}
    for d in docs:
        out[d["filename"]] = {
            "filetype": d["filetype"],
            "status": d["status"],
            "tables": sorted(
                [{"name": t["name"], "rows": t["n_rows"], "cols": t["n_cols"],
                  "columns": [c["name"] for c in t["schema"]],
                  "dtypes": [coarse_dtype(c["dtype"]) for c in t["schema"]],
                  "method": t["method"]}
                 for t in by_doc.get(d["doc_id"], [])],
                key=lambda x: x["name"]),
        }
    return out


def delete_project(store: Store, project: str):
    for doc in store.documents(project):
        store.delete_tables_for_doc(doc["doc_id"])
    store.conn.execute("DELETE FROM documents WHERE project=?", [project])
    store.conn.execute("DELETE FROM canonical_tables WHERE project=?", [project])
    from .config import PATHS
    import shutil
    pdir = PATHS.project_dir(project)
    if pdir.exists():
        shutil.rmtree(pdir)


def ingest_corpus(store: Store, samples_dir: Path) -> dict:
    """Deterministic (no-LLM) ingest of a corpus into the throwaway project."""
    from .ingest import ingest_file
    from .sandbox import Sandbox
    delete_project(store, EVAL_PROJECT)
    sandbox = Sandbox()
    for f in sorted(samples_dir.iterdir()):
        if f.is_file() and not f.name.startswith("."):
            ingest_file(store, EVAL_PROJECT, f, llm=None, sandbox=sandbox, force=True)
    snap = snapshot_project(store, EVAL_PROJECT)
    delete_project(store, EVAL_PROJECT)
    return snap


def diff_snapshots(expected: dict, actual: dict) -> list[str]:
    problems: list[str] = []
    for fname, exp in expected.items():
        act = actual.get(fname)
        if act is None:
            problems.append(f"{fname}: missing from actual run")
            continue
        for key in ("filetype", "status"):
            if exp[key] != act[key]:
                problems.append(f"{fname}: {key} {act[key]!r} != expected {exp[key]!r}")
        exp_tables = {t["name"]: t for t in exp["tables"]}
        act_tables = {t["name"]: t for t in act["tables"]}
        for name in exp_tables.keys() - act_tables.keys():
            problems.append(f"{fname}: table {name} missing")
        for name in act_tables.keys() - exp_tables.keys():
            problems.append(f"{fname}: unexpected extra table {name}")
        for name in exp_tables.keys() & act_tables.keys():
            e, a = exp_tables[name], act_tables[name]
            for key in ("rows", "cols", "columns", "dtypes", "method"):
                if e[key] != a[key]:
                    problems.append(f"{fname}/{name}: {key} changed — "
                                    f"expected {e[key]!r}, got {a[key]!r}")
    for fname in actual.keys() - expected.keys():
        problems.append(f"{fname}: unexpected extra document")
    return problems
