"""Schema memory: fingerprint every extracted table's schema, remember it, and
reuse what we learned when a similar file shows up again.

- Exact fingerprint hit  -> reuse remembered contracts, run drift checks.
- Fuzzy match (jaccard)  -> note the similarity (candidate for the linker).
This is the piece that turns a parser into something that *learns your data*.
"""

from __future__ import annotations

import hashlib

import pandas as pd

from .ir import coarse_dtype, normalize_col
from .store import Store, short_id
from .validate import check_contracts, contract_stats


def fingerprint(df: pd.DataFrame) -> tuple[str, list[dict]]:
    cols = sorted((normalize_col(c), coarse_dtype(str(df[c].dtype))) for c in df.columns)
    sig = "|".join(f"{n}:{t}" for n, t in cols)
    return hashlib.sha256(sig.encode()).hexdigest()[:16], [
        {"name": n, "dtype": t} for n, t in cols]


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def register(store: Store, table_id: str, df: pd.DataFrame) -> dict:
    """Register table schema; returns {schema_id, reused, drift, similar}."""
    fp, columns = fingerprint(df)
    hit = store.lookup_schema(fp)
    if hit:
        schema_id, contracts_json, _seen = hit
        import json
        drift = check_contracts(df, json.loads(contracts_json or "{}"))
        store.touch_schema(schema_id)
        store.map_table_schema(table_id, schema_id, reused=True, drift=drift)
        return {"schema_id": schema_id, "reused": True, "drift": drift, "similar": []}

    schema_id = short_id("schema", fp)
    store.register_schema(schema_id, fp, columns, contract_stats(df))
    # Fuzzy: is this close to something we already know?
    mine = {c["name"] for c in columns}
    similar = []
    for known in store.schemas():
        if known["schema_id"] == schema_id:
            continue
        sim = jaccard(mine, {c["name"] for c in known["columns"]})
        if sim >= 0.6:
            similar.append({"schema_id": known["schema_id"], "similarity": round(sim, 2)})
    store.map_table_schema(table_id, schema_id, reused=False, drift=[])
    return {"schema_id": schema_id, "reused": False, "drift": [], "similar": similar}
