"""Curated extraction scripts — the agent authors them, the registry keeps them.

Nothing format-specific is hardcoded in the pipeline: when a records-type file
arrives, we (1) try remembered scripts whose format signature matches, no LLM
involved; (2) only if none validates, the agent writes/adapts one in the
sandbox (seeded with the best near-miss script); (3) a validated new script is
saved back to the registry. Scripts are global across projects — a parser is
format knowledge. Every script obeys the standardized output contract
(OUT/manifest.json + parquet; see sandbox.load_manifest)."""

from __future__ import annotations

import re
from pathlib import Path

from .config import PATHS
from .ir import TableCandidate, dedupe_columns, infer_types, normalize_col
from .sandbox import Sandbox, load_manifest
from .store import Store, short_id

MATCH_THRESHOLD = 0.45


def format_signature(head_text: str, filename: str) -> dict:
    tokens = set()
    for line in head_text.splitlines()[:40]:
        for tok in re.split(r"[\s;,|#=:]+", line.strip()):
            tok = re.sub(r"\d+", "#", tok.lower())[:24]
            if tok and len(tok) >= 2:
                tokens.add(tok)
    pattern = re.sub(r"\d+", "#", filename.lower())
    return {"head_tokens": sorted(tokens)[:80], "filename_pattern": pattern}


def _similarity(a: dict, b: dict) -> float:
    ta, tb = set(a.get("head_tokens", [])), set(b.get("head_tokens", []))
    tok = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    fn = 0.2 if a.get("filename_pattern") == b.get("filename_pattern") else 0.0
    return min(1.0, tok + fn)


def find_matching_scripts(store: Store, signature: dict, k: int = 2) -> list[dict]:
    scored = []
    for s in store.scripts():
        sim = _similarity(signature, s["signature"])
        if sim >= MATCH_THRESHOLD:
            scored.append({**s, "similarity": round(sim, 2)})
    return sorted(scored, key=lambda x: -x["similarity"])[:k]


def manifest_to_candidates(manifest: dict, doc_stem: str, source_name: str,
                           method: str) -> list[TableCandidate]:
    import pandas as pd
    out = []
    for t in manifest.get("tables", []):
        df = pd.read_parquet(t["_abs_path"])
        df.columns = dedupe_columns([normalize_col(c) for c in df.columns])
        out.append(TableCandidate(
            df=infer_types(df),
            name=f"{doc_stem}_{normalize_col(t.get('name') or Path(t['path']).stem)}",
            source_ref=f"parsed from {source_name}",
            method=method,
            flags=[] if method == "script" else ["agent_written_parser"],
            notes=[f"format: {manifest.get('format_id', 'unknown')}",
                   *( [t["description"]] if t.get("description") else [] )],
            sketch={"manifest_table": {k: v for k, v in t.items()
                                       if not k.startswith("_")}},
        ))
    return out


def try_script(sandbox: Sandbox, script: dict, path: Path,
               doc_stem: str) -> list[TableCandidate]:
    code = Path(script["script_path"]).read_text()
    res = sandbox.run_python(code, {path.name: path})
    if not res.ok:
        return []
    manifest = load_manifest(res)
    if not manifest or not manifest["tables"]:
        return []
    return manifest_to_candidates(manifest, doc_stem, path.name, method="script")


def register_script(store: Store, code: str, format_id: str, description: str,
                    signature: dict) -> str:
    script_id = short_id("script", format_id, str(sorted(signature["head_tokens"])))
    spath = PATHS.scripts_dir / f"{normalize_col(format_id)}__{script_id}.py"
    spath.write_text(code)
    store.save_script(script_id, format_id, description, signature, spath)
    return script_id
