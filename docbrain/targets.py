"""Target schemas + mapping memory — normalize extracted tables into the
organization's canonical formats.

- Target schemas are declarative YAML files in ~/.docbrain/targets/ (global:
  canonical formats are org knowledge, like curated scripts).
- Mappings are proposed by the LLM per SOURCE SCHEMA FINGERPRINT (the same
  schema_id the registry tracks), stored as `proposed`, applied only after
  human approval — and then remembered: every future file with that schema
  fingerprint maps automatically, no LLM involved.
- Transforms are deterministic and declarative (rename / cast / scale / const)
  so canonical outputs carry trust="derived". Reshapes (wide→long) are out of
  v0 scope and surface as status `needs_transform` instead of a silent guess.
- Provenance: each apply appends a ledger entry and lineage hops
  (canonical ← mapped_from ← table ← extracted_from ← document).

Target YAML shape (kept minimal; field names chosen to translate directly to
data-contract standards like ODCS later):

    name: counting_stations
    version: 1
    description: Canonical counting-station registry used by PLAN.
    columns:
      - {name: station_id, dtype: str, required: true,  description: vendor station number}
      - {name: station_name, dtype: str, required: false, description: human name}
      - {name: x, dtype: float, required: true, unit: LV95 east,  description: coordinate}
      - {name: y, dtype: float, required: true, unit: LV95 north, description: coordinate}
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import yaml

from .agents import prompts
from .config import PATHS
from .llm import LLM, LLMError
from .store import Store, short_id

CASTS = {
    "str": lambda s: s.astype(str),
    "int": lambda s: pd.to_numeric(s, errors="coerce").astype("Int64"),
    "float": lambda s: pd.to_numeric(s, errors="coerce"),
    "date": lambda s: pd.to_datetime(s, errors="coerce", format="mixed"),
    "bool": lambda s: s.astype(bool),
}


def targets_dir() -> Path:
    d = PATHS.home / "targets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_targets() -> dict[str, dict]:
    out = {}
    for f in sorted(targets_dir().glob("*.yaml")):
        try:
            t = yaml.safe_load(f.read_text())
            if isinstance(t, dict) and t.get("name") and t.get("columns"):
                out[t["name"]] = t
        except yaml.YAMLError:
            continue
    return out


# ---------------------------------------------------------------- proposal
def propose_mapping(llm: LLM, targets: dict[str, dict], source_schema: list[dict],
                    sample_rows: list[dict], source_name: str) -> dict | None:
    """One LLM call: which target (if any) does this source table map to, and
    how? Returns {"target", "mapping", "confidence", "rationale"} or None."""
    if not targets or not (llm and llm.available):
        return None
    payload = {
        "source_table": source_name,
        "source_columns": source_schema,
        "sample_rows": sample_rows[:5],
        "target_schemas": [
            {"name": t["name"], "description": t.get("description", ""),
             "columns": t["columns"]} for t in targets.values()],
    }
    try:
        out = llm.complete_json(json.dumps(payload, default=str, indent=1),
                                system=prompts.MAP_SYSTEM, max_tokens=3000)
    except (LLMError, ValueError):
        return None
    if not isinstance(out, dict) or out.get("target") in (None, "none"):
        return None
    if out.get("needs_transform"):
        return {"target": out["target"], "mapping": {}, "confidence": 0.0,
                "rationale": out.get("rationale", ""), "needs_transform": True}
    mapping = out.get("mapping") or {}
    tgt = targets.get(out["target"])
    if not tgt:
        return None
    src_cols = {c["name"] for c in source_schema}
    clean = {}
    for tcol, spec in mapping.items():
        if tcol not in {c["name"] for c in tgt["columns"]}:
            continue
        if isinstance(spec, str):
            spec = {"source": spec}
        if spec.get("source") and spec["source"] not in src_cols and "const" not in spec:
            continue
        clean[tcol] = {k: spec[k] for k in ("source", "cast", "scale", "const")
                       if k in spec}
    required = {c["name"] for c in tgt["columns"] if c.get("required")}
    if not clean or not required.issubset(clean.keys()):
        return {"target": out["target"], "mapping": clean,
                "confidence": min(float(out.get("confidence", 0.3)), 0.4),
                "rationale": (out.get("rationale", "") +
                              " [required target columns unmapped]").strip()}
    return {"target": out["target"], "mapping": clean,
            "confidence": float(out.get("confidence", 0.6)),
            "rationale": out.get("rationale", "")}


# ---------------------------------------------------------------- apply
def apply_mapping(df: pd.DataFrame, target: dict, mapping: dict) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index.copy())
    for col in target["columns"]:
        name = col["name"]
        spec = mapping.get(name)
        if spec is None:
            out[name] = None
            continue
        if "const" in spec:
            s = pd.Series([spec["const"]] * len(df), index=df.index)
        else:
            s = df[spec["source"]]
        cast = spec.get("cast") or col.get("dtype")
        if cast in CASTS:
            try:
                s = CASTS[cast](s)
            except (ValueError, TypeError):
                pass
        if spec.get("scale") not in (None, 1):
            s = pd.to_numeric(s, errors="coerce") * float(spec["scale"])
        out[name] = s
    return out.reset_index(drop=True)


# ---------------------------------------------------------------- pipeline hook
def map_table_if_remembered(store: Store, *, project: str, table_id: str,
                            table_name: str, df: pd.DataFrame, schema_id: str,
                            parquet_sha: str, ledger=None) -> dict | None:
    """Called during ingest: if an APPROVED mapping exists for this source
    schema fingerprint, produce the canonical table automatically."""
    row = store.get_mapping_for(schema_id, status="approved")
    if not row:
        return None
    targets = load_targets()
    target = targets.get(row["target_schema"])
    if not target:
        return None
    canonical_df = apply_mapping(df, target, row["mapping"])
    cdir = PATHS.project_dir(project) / "canonical" / row["target_schema"]
    cdir.mkdir(parents=True, exist_ok=True)
    cid = short_id("canonical", table_id, row["mapping_id"])
    pq = cdir / f"{table_name}__{cid}.parquet"
    canonical_df.to_parquet(pq)
    from .ledger import sha256_file
    out_sha = sha256_file(pq)
    store.add_canonical(canonical_id=cid, project=project,
                        target_schema=row["target_schema"],
                        source_table_id=table_id, mapping_id=row["mapping_id"],
                        parquet_path=pq, n_rows=len(canonical_df))
    run_hash = None
    if ledger is not None:
        entry = ledger.append("map", {
            "project": project,
            "inputs": [{"name": table_name, "sha256": parquet_sha}],
            "outputs": [{"name": pq.name, "sha256": out_sha,
                         "rows": len(canonical_df)}],
            "ok": True,
            "detail": {"target_schema": row["target_schema"],
                       "mapping_id": row["mapping_id"],
                       "source_schema_id": schema_id},
        })
        run_hash = entry["entry_hash"]
    store.add_lineage("canonical", cid, "mapped_from", "table", table_id,
                      run_hash, {"mapping_id": row["mapping_id"],
                                 "parquet_sha": out_sha})
    store.add_lineage("canonical", cid, "conforms_to", "target",
                      row["target_schema"], run_hash, {})
    return {"canonical_id": cid, "target": row["target_schema"],
            "rows": len(canonical_df), "path": str(pq)}


def maybe_propose(store: Store, llm, *, schema_id: str, source_schema: list[dict],
                  df: pd.DataFrame, source_name: str) -> dict | None:
    """Propose a mapping once per (schema fingerprint): if any mapping row —
    proposed, approved, or rejected — already exists, do nothing."""
    if store.get_mapping_for(schema_id, status=None):
        return None
    targets = load_targets()
    samples = json.loads(df.head(5).to_json(orient="records", date_format="iso"))
    prop = propose_mapping(llm, targets, source_schema, samples, source_name)
    if prop is None:
        store.save_mapping(short_id("map", schema_id, "none"), schema_id, "-",
                           {}, 0.0, "no matching target", "no_match")
        return None
    status = "needs_transform" if prop.get("needs_transform") else "proposed"
    mid = short_id("map", schema_id, prop["target"])
    store.save_mapping(mid, schema_id, prop["target"], prop["mapping"],
                       prop["confidence"], prop["rationale"], status)
    return {"mapping_id": mid, "status": status, **prop}
