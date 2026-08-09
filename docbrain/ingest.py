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

MAX_ARCHIVE_DEPTH = 3  # zip-of-zips guard


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


def _ingest_archive(store: Store, project: str, path: Path, doc_id: str,
                    content_hash: str, report: IngestReport, llm: LLM | None,
                    sandbox: Sandbox | None, force: bool, ledger, scratch: Path,
                    depth: int) -> IngestReport:
    """Zip is a container, not a format: extract entries and route each one
    through ingest_file recursively (nested zips included, depth-guarded).
    The archive itself gets a document row summarizing its children — no
    tables of its own."""
    from .extractors import archive_extract
    if depth >= MAX_ARCHIVE_DEPTH:
        store.upsert_document(doc_id, project, path, content_hash, "archive",
                              "unsupported", 0, {"reason": "max archive nesting depth reached"})
        report.status = "unsupported"
        return report
    try:
        entry_paths = archive_extract.extract_entries(path, scratch / "entries")
    except Exception as e:
        store.upsert_document(doc_id, project, path, content_hash, "archive",
                              "failed", 0, {"error": str(e)})
        report.status = "failed"
        report.notes.append(f"archive extraction failed: {e}")
        return report

    children: list[tuple[str, IngestReport]] = []
    for entry_path in entry_paths:
        entry_type = detect_type(entry_path)
        if entry_type == "unknown":
            report.notes.append(f"{entry_path.name}: unrecognized format inside "
                                f"{path.name}, skipped")
            continue
        child = ingest_file(store, project, entry_path, llm=llm, sandbox=sandbox,
                            force=force, ledger=ledger, _depth=depth + 1)
        children.append((entry_path.name, child))
        for t in child.tables:
            report.tables.append({**t, "source": f"{path.name}::{entry_path.name} :: {t['source']}"})
        report.notes.extend(f"[{path.name}::{entry_path.name}] {n}" for n in child.notes)
        if child.doc_id:
            store.add_lineage("document", child.doc_id, "contained_in",
                              "document", doc_id, None, {"archive": path.name})

    n_ok = sum(1 for _, c in children if c.status == "ingested")
    status = "ingested" if n_ok else ("partial" if children else "unsupported")
    store.upsert_document(doc_id, project, path, content_hash, "archive", status, 0, {
        "entries": len(entry_paths), "children_ingested": n_ok,
        "children": [{"name": n, "status": c.status, "doc_id": c.doc_id} for n, c in children],
    })
    report.status = status
    if ledger is not None:
        ledger.append("ingest", {
            "project": project, "doc_id": doc_id,
            "inputs": [{"name": path.name, "sha256": content_hash,
                        "bytes": path.stat().st_size}],
            "outputs": [{"name": n, "doc_id": c.doc_id} for n, c in children],
            "ok": status == "ingested",
            "detail": {"filetype": "archive", "n_entries": len(entry_paths),
                       "n_ingested_children": n_ok},
        })
    return report


def ingest_file(store: Store, project: str, path: Path, llm: LLM | None = None,
                sandbox: Sandbox | None = None, force: bool = False,
                ledger=None, _depth: int = 0) -> IngestReport:
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
            for i, pre in enumerate(meta.get("preambles") or []):
                chunks.append({"loc": f"non-table block {i + 1}", "text": pre[:2000]})
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
        elif ftype == "office":
            from .extractors import office_extract
            if office_extract.available():
                candidates, chunks, meta = office_extract.extract(path)
                report.notes.append(f"anydoc: {meta['n_tables']} table(s), "
                                    f"{meta['n_chunks']} chunk(s)")
            else:
                store.upsert_document(doc_id, project, path, content_hash, ftype,
                                      "unsupported", 0,
                                      {"reason": "office track needs firecrawl-anydoc"})
                report.status = "unsupported"
                return report
        elif ftype == "pdf":
            from .config import PDF_ENGINE
            if PDF_ENGINE == "docling":
                candidates, chunks, meta = pdf_extract.extract_docling(path)
                report.notes.append(f"docling engine: {meta['n_tables']} table(s)")
            else:
                candidates, chunks, meta = pdf_extract.extract(path, scratch)
                if PDF_ENGINE == "auto":
                    reason = pdf_extract.should_escalate_to_docling(candidates, meta)
                    if reason:
                        try:
                            d_cands, d_chunks, d_meta = pdf_extract.extract_docling(path)
                            if len(d_cands) > len(candidates):
                                report.notes.append(
                                    f"escalated to docling ({reason}): "
                                    f"{len(d_cands)} vs {len(candidates)} native table(s)")
                                candidates = d_cands
                                chunks = chunks + d_chunks
                                meta["scanned_pages"] = []  # docling covered them
                        except Exception as e:
                            report.notes.append(f"docling escalation failed: {str(e)[:120]}")
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
        elif ftype == "archive":
            return _ingest_archive(store, project, path, doc_id, content_hash,
                                   report, llm, sandbox, force, ledger, scratch, _depth)
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

        # Target schemas: remembered mapping -> canonical table automatically;
        # unknown schema -> propose once (human approves via `docbrain mappings`).
        from .targets import load_targets, map_table_if_remembered, maybe_propose
        canonical = None
        all_targets = load_targets()
        if all_targets:
            # Drift policy (dlt contract modes): a drifting source under an
            # approved mapping consults the target's on_drift before mapping.
            policy_blocked = False
            if mem["drift"]:
                approved = store.get_mapping_for(mem["schema_id"], status="approved")
                if approved:
                    policy = (all_targets.get(approved["target_schema"]) or {}).get(
                        "on_drift", "evolve")
                    if ledger is not None:
                        ledger.append("schema-change", {
                            "project": project, "doc_id": doc_id,
                            "ok": policy != "freeze",
                            "detail": {"table": cand.name, "drift": mem["drift"][:6],
                                       "policy": policy,
                                       "target": approved["target_schema"]},
                        })
                    if policy == "freeze":
                        policy_blocked = True
                        store.conn.execute(
                            "UPDATE extracted_tables SET needs_review=true WHERE table_id=?",
                            [table_id])
                        report.notes.append(
                            f"{cand.name}: drift under on_drift=freeze — canonical "
                            f"mapping withheld, table sent to review")
            if not policy_blocked:
                canonical = map_table_if_remembered(
                    store, project=project, table_id=table_id, table_name=cand.name,
                    df=cand.df, schema_id=mem["schema_id"],
                    parquet_sha=persisted[-1]["parquet_sha"], ledger=ledger)
            if canonical:
                q = (f", {canonical['quarantined']} row(s) quarantined"
                     if canonical.get("quarantined") else "")
                report.notes.append(
                    f"{cand.name}: mapped to canonical [{canonical['target']}] "
                    f"({canonical['rows']} rows{q}) via remembered mapping")
            elif not policy_blocked and llm and llm.available and not needs_review:
                prop = maybe_propose(store, llm, schema_id=mem["schema_id"],
                                     source_schema=[{"name": str(c),
                                                     "dtype": str(cand.df[c].dtype)}
                                                    for c in cand.df.columns],
                                     df=cand.df, source_name=cand.name)
                if prop and prop["status"] == "proposed":
                    report.notes.append(
                        f"{cand.name}: mapping PROPOSED → [{prop['target']}] "
                        f"(conf {prop['confidence']:.2f}) — approve with "
                        f"`docbrain approve {prop['mapping_id']}`")
                elif prop and prop["status"] == "needs_transform":
                    report.notes.append(
                        f"{cand.name}: matches [{prop['target']}] but needs a "
                        f"reshape — out of scope for auto-mapping (flagged)")

        report.tables.append({
            "name": cand.name, "source": cand.source_ref, "method": cand.method,
            "shape": list(cand.df.shape), "confidence": round(conf, 2),
            "needs_review": needs_review, "issues": issues,
            "schema_reused": mem["reused"], "drift": mem["drift"],
            "canonical": canonical,
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
