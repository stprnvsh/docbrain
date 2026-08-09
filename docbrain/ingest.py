"""Ingestion orchestrator: one file in -> classified -> specialist track ->
(optional) sandboxed agent refinement -> validation/confidence gate ->
schema memory -> parquet + catalog. This is the router + glue the research
stack doesn't ship."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .agents.loop import refine_table, vision_extract_page
from .config import AGENT_MODE, PATHS
from .extractors import csv_extract, pdf_extract, txt_extract, xlsx_extract
from .ir import TableCandidate
from .llm import LLM, LLMError
from .router import detect_type
from .sandbox import Sandbox
from .schema_memory import register as register_schema
from .store import Store, file_hash, short_id
from .validate import score

AGENT_TRIGGER_FLAGS = {
    "malformed_rows", "merged_header_uncertain", "multi_row_header",
    "no_obvious_header", "weak_header_names",
}


@dataclass
class IngestReport:
    path: Path
    doc_id: str = ""
    filetype: str = ""
    status: str = ""
    tables: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skipped: bool = False


def _should_refine(candidate: TableCandidate, llm: LLM | None) -> bool:
    if AGENT_MODE == "never" or llm is None or not llm.available:
        return False
    if AGENT_MODE == "always":
        return True
    return bool(AGENT_TRIGGER_FLAGS & set(candidate.flags))


def ingest_file(store: Store, project: str, path: Path, llm: LLM | None = None,
                sandbox: Sandbox | None = None, force: bool = False,
                ledger=None) -> IngestReport:
    path = path.resolve()
    report = IngestReport(path=path)
    content_hash = file_hash(path)
    existing = store.get_document_by_hash(project, content_hash)
    if existing and not force:
        report.doc_id, report.status = existing[0], existing[1]
        report.skipped = True
        report.notes.append("already ingested (same content hash) — use --force to redo")
        return report

    doc_id = short_id(project, content_hash)
    report.doc_id = doc_id
    ftype = detect_type(path)
    report.filetype = ftype
    PATHS.ensure(project)
    scratch = Path(tempfile.mkdtemp(prefix="docbrain-ingest-"))
    if sandbox is not None:
        sandbox.context = {"project": project, "doc_id": doc_id}

    if existing and force:
        store.delete_tables_for_doc(existing[0])

    candidates: list[TableCandidate] = []
    chunks: list[dict] = []
    meta: dict = {}
    n_pages = 0

    try:
        if ftype == "csv":
            candidates, meta = csv_extract.extract(path)
            if meta.get("unstructured"):
                # A .csv that isn't actually delimited: same records path as txt.
                chunks.append({"loc": "file head", "text": meta["head_text"]})
                report.notes.append("not a delimited table — routed to records path")
                agent_tables, notes = txt_extract.records_extract(
                    path, meta["head_text"], llm, sandbox or Sandbox(),
                    AGENT_MODE != "never", store=store)
                report.notes.extend(notes)
                candidates.extend(agent_tables)
        elif ftype == "txt":
            candidates, chunks, meta = txt_extract.extract(
                path, llm=llm, sandbox=sandbox or Sandbox(),
                agent_enabled=AGENT_MODE != "never", store=store)
            report.notes.extend(meta.get("notes", []))
        elif ftype == "xlsx":
            candidates, meta = xlsx_extract.extract(path)
        elif ftype == "pdf":
            candidates, chunks, meta = pdf_extract.extract(path, scratch)
            n_pages = meta.get("n_pages", 0)
            # Vision fallback for scanned pages.
            for sp in meta.get("scanned_pages", []):
                if llm and llm.supports_vision:
                    try:
                        vtables, vtext = vision_extract_page(
                            llm, Path(sp["png"]), sp["page"], path.stem)
                        if ledger is not None:
                            from .ledger import sha256_file as _shaf
                            ledger.append("vision", {
                                "project": project, "doc_id": doc_id,
                                "inputs": [{"name": Path(sp["png"]).name,
                                            "sha256": _shaf(Path(sp["png"]))}],
                                "outputs": [{"name": t.name, "rows": len(t.df)}
                                            for t in vtables],
                                "ok": True,
                                "detail": {"page": sp["page"]},
                            })
                        candidates.extend(vtables)
                        if vtext:
                            chunks.append({"loc": f"page {sp['page']} (vision)", "text": vtext})
                        report.notes.append(f"page {sp['page']}: vision-extracted "
                                            f"{len(vtables)} table(s)")
                    except (LLMError, ValueError) as e:
                        report.notes.append(f"page {sp['page']}: vision failed ({e}) — queued for review")
                        chunks.append({"loc": f"page {sp['page']}",
                                       "text": f"[scanned page, unprocessed: {sp['png']}]"})
                else:
                    report.notes.append(f"page {sp['page']}: scanned, no vision backend — queued for review")
                    chunks.append({"loc": f"page {sp['page']}",
                                   "text": f"[scanned page, unprocessed: {sp['png']}]"})
        else:
            store.upsert_document(doc_id, project, path, content_hash, ftype,
                                  "unsupported", 0, {"reason": f"type {ftype} not supported yet"})
            report.status = "unsupported"
            return report
    except Exception as e:
        store.upsert_document(doc_id, project, path, content_hash, ftype,
                              "failed", 0, {"error": str(e)})
        report.status = "failed"
        report.notes.append(f"extraction failed: {e}")
        return report

    # Agent refinement pass (sandboxed reasoning loop) for flagged candidates.
    final: list[TableCandidate] = []
    sandbox = sandbox or Sandbox()
    for cand in candidates:
        if _should_refine(cand, llm):
            outcome = refine_table(llm, sandbox, path, cand)
            report.notes.extend(f"[agent:{cand.name}] {line}" for line in outcome.log)
            final.extend(outcome.tables)
        else:
            final.append(cand)

    # Validate -> confidence gate -> schema memory -> persist (+ provenance).
    from .ledger import sha256_file as _sha_file
    tables_dir = PATHS.tables_dir(project)
    persisted: list[dict] = []
    for cand in final:
        conf, issues, needs_review = score(cand)
        table_id = short_id(doc_id, cand.name, cand.source_ref)
        pq = tables_dir / f"{cand.name}__{table_id}.parquet"
        try:
            cand.df.to_parquet(pq)
        except Exception:
            fallback = cand.df.astype(str)
            fallback.to_parquet(pq)
            issues.append("stored with string-coerced dtypes (mixed types)")
        store.add_table(table_id=table_id, doc_id=doc_id, project=project,
                        name=cand.name, source_ref=cand.source_ref, parquet_path=pq,
                        df=cand.df, method=cand.method, confidence=conf,
                        needs_review=needs_review, issues=issues,
                        origins=cand.sketch.get("column_origins"),
                        origin_trust=cand.sketch.get("origin_trust"))
        persisted.append({"table_id": table_id, "name": cand.name,
                          "parquet": pq, "parquet_sha": _sha_file(pq),
                          "rows": len(cand.df), "method": cand.method,
                          "source_ref": cand.source_ref,
                          "script_id": cand.sketch.get("script_id")})
        mem = register_schema(store, table_id, cand.df)
        report.tables.append({
            "name": cand.name, "source": cand.source_ref, "method": cand.method,
            "shape": list(cand.df.shape), "confidence": round(conf, 2),
            "needs_review": needs_review, "issues": issues,
            "schema_reused": mem["reused"], "drift": mem["drift"],
        })
        if mem["reused"]:
            report.notes.append(f"{cand.name}: schema recognized from memory"
                                + (f" — drift: {mem['drift'][:2]}" if mem["drift"] else ""))
        elif mem["similar"]:
            report.notes.append(f"{cand.name}: new schema, similar to {mem['similar']}")

    for ch in chunks:
        store.add_chunk(doc_id, project, ch["loc"], ch["text"])

    # Sketch (evidence trail) for debugging/review.
    sketch_path = PATHS.sketches_dir(project) / f"{path.stem}__{doc_id}.json"
    sketch_path.write_text(json.dumps({"file": str(path), "type": ftype, "meta": meta,
                                       "tables": report.tables, "notes": report.notes},
                                      indent=1, default=str))

    status = "ingested" if report.tables or chunks else "partial"
    store.upsert_document(doc_id, project, path, content_hash, ftype, status,
                          n_pages, {"extract_meta": meta})
    report.status = status

    # Provenance: one ingest entry in the ledger, then lineage edges + usage.
    if ledger is not None:
        entry = ledger.append("ingest", {
            "project": project, "doc_id": doc_id,
            "inputs": [{"name": path.name, "sha256": content_hash,
                        "bytes": path.stat().st_size}],
            "outputs": [{"name": p["name"], "sha256": p["parquet_sha"],
                         "rows": p["rows"]} for p in persisted],
            "ok": status == "ingested",
            "detail": {"filetype": ftype, "status": status,
                       "n_chunks": len(chunks)},
        })
        store.bump_usage("document", doc_id)
        for p in persisted:
            store.add_lineage("table", p["table_id"], "extracted_from",
                              "document", doc_id, entry["entry_hash"],
                              {"source_ref": p["source_ref"], "method": p["method"],
                               "parquet_sha": p["parquet_sha"],
                               "file_sha": content_hash})
            if p["script_id"]:
                store.add_lineage("table", p["table_id"], "produced_by_code",
                                  "script", p["script_id"], entry["entry_hash"], {})
                store.bump_usage("script", p["script_id"])
    return report
