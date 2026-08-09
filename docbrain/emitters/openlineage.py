"""OpenLineage emitter — projects ledger entries into RunEvents.

The hash-chained JSONL stays the system of record; this is a replayable,
idempotent projection (runId is derived from entry_hash, so re-emitting never
duplicates). One wire format reaches Marquez, DataHub, OpenMetadata, Purview,
and Dataplex.

Mapping:
  ledger kind          -> job  docbrain.<kind>   (namespace docbrain://<project>)
  inputs/outputs       -> datasets; sha256 rides the datasetVersion facet
                          (the accepted slot — OL has no standard checksum facet)
  code_sha             -> sourceCode job facet
  chain fields         -> custom `docbrain_provenance` run facet (_schemaURL per spec)
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

PRODUCER = "https://github.com/transcality/docbrain"
SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json"
FACET_SCHEMA_URL = f"{PRODUCER}/blob/main/docs/facets/docbrain_provenance.json"
RUN_NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # stable namespace


def _dataset(project: str, item: dict) -> dict:
    facets = {}
    if item.get("sha256"):
        facets["version"] = {
            "_producer": PRODUCER,
            "_schemaURL": "https://openlineage.io/spec/facets/1-0-1/DatasetVersionDatasetFacet.json",
            "datasetVersion": item["sha256"],
        }
    return {"namespace": f"docbrain://{project}", "name": item.get("name", "?"),
            "facets": facets}


def to_runevent(entry: dict) -> dict | None:
    project = entry.get("project") or "unknown"
    kind = entry.get("kind", "run")
    run_id = str(uuid.uuid5(RUN_NS, entry["entry_hash"]))

    job_facets: dict = {}
    if entry.get("code_sha"):
        job_facets["sourceCode"] = {
            "_producer": PRODUCER,
            "_schemaURL": "https://openlineage.io/spec/facets/1-0-1/SourceCodeJobFacet.json",
            "language": "python",
            "sourceCode": f"sha256:{entry['code_sha']}",
        }

    run_facets = {
        "docbrain_provenance": {
            "_producer": PRODUCER,
            "_schemaURL": FACET_SCHEMA_URL,
            "entryHash": entry["entry_hash"],
            "prevHash": entry.get("prev_hash"),
            "codeSha256": entry.get("code_sha"),
            "inputs": entry.get("inputs", []),
            "detail": entry.get("detail", {}),
        }
    }

    return {
        "eventType": "COMPLETE" if entry.get("ok", True) else "FAIL",
        "eventTime": entry["ts"],
        "producer": PRODUCER,
        "schemaURL": SCHEMA_URL,
        "run": {"runId": run_id, "facets": run_facets},
        "job": {"namespace": f"docbrain://{project}", "name": f"docbrain.{kind}",
                "facets": job_facets},
        "inputs": [_dataset(project, i) for i in entry.get("inputs", []) or []],
        "outputs": [_dataset(project, o) for o in entry.get("outputs", []) or []],
    }


def emit(ledger_path: Path, out_dir: Path, since: str | None = None,
         url: str | None = None) -> dict:
    """Replay the ledger into RunEvents. Writes JSONL always; POSTs to an
    OpenLineage endpoint when url is given (requires the [lineage] extra)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "openlineage-runevents.jsonl"
    events = []
    with open(ledger_path) as f:
        for line in f:
            if not line.strip():
                continue
            entry = json.loads(line)
            if since and entry.get("ts", "") < since:
                continue
            ev = to_runevent(entry)
            if ev:
                events.append(ev)
    with open(out_file, "w") as f:
        for ev in events:
            f.write(json.dumps(ev, default=str) + "\n")

    posted = 0
    if url:
        try:
            from openlineage.client import OpenLineageClient  # noqa: F401
            import requests
        except ImportError:
            return {"events": len(events), "file": str(out_file), "posted": 0,
                    "error": "HTTP transport needs `docbrain[lineage]`"}
        for ev in events:
            r = requests.post(url.rstrip("/") + "/api/v1/lineage", json=ev, timeout=10)
            if r.status_code < 300:
                posted += 1
    return {"events": len(events), "file": str(out_file), "posted": posted}
