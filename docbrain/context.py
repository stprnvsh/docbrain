"""The context brain: build one context pack per project — a single artifact
(context.md + context.json) that carries what the project's documents ARE,
what data they hold, how they relate, and how to query them.

`ask` runs a small evidence loop over the pack: the model may issue read-only
SQL against the extracted tables (registered as DuckDB views) or search text
chunks, then answers with citations."""

from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb
import pandas as pd

from .agents import prompts
from .config import PATHS
from .llm import LLM, LLMError
from .store import Store


# ---------------------------------------------------------------- summaries
def _doc_summary(llm: LLM | None, store: Store, doc: dict, tables: list[dict],
                 chunks: list[dict]) -> dict:
    cached = doc["meta"].get("summary_cache")
    if cached:
        return cached
    if not (llm and llm.available) or (not tables and not chunks):
        note = {"unsupported": " (format not parsed yet — recorded for completeness)",
                "failed": " (extraction failed)"}.get(doc["status"], "")
        return {"summary": f"{doc['filetype']} document with {len(tables)} extracted table(s){note}.",
                "topics": [], "entities": []}
    evidence = {
        "filename": doc["filename"],
        "filetype": doc["filetype"],
        "tables": [{
            "name": t["name"], "source": t["source_ref"],
            "shape": [t["n_rows"], t["n_cols"]],
            "columns": [c["name"] for c in t["schema"]][:20],
            "sample": _table_sample(t, n=4),
        } for t in tables[:8]],
        "text_excerpts": [c["text"][:600] for c in chunks[:3]],
    }
    try:
        data = llm.complete_json(json.dumps(evidence, default=str, indent=1),
                                 system=prompts.DOC_SUMMARY_SYSTEM, max_tokens=1200)
        if isinstance(data, dict) and data.get("summary"):
            meta = doc["meta"] | {"summary_cache": data}
            store.set_document_meta(doc["doc_id"], meta)
            return data
    except (LLMError, ValueError):
        pass
    return {"summary": f"{doc['filetype']} document with {len(tables)} extracted table(s).",
            "topics": [], "entities": []}


def _table_sample(t: dict, n: int = 4):
    try:
        df = pd.read_parquet(t["parquet_path"])
        return json.loads(df.head(n).to_json(orient="records", date_format="iso"))
    except Exception:
        return []


# ---------------------------------------------------------------- build
def build_context(store: Store, project: str, llm: LLM | None = None) -> Path:
    docs = store.documents(project)
    tables = store.tables(project)
    links = store.links(project)
    families = store.schema_families(project)
    by_doc: dict[str, list[dict]] = {}
    for t in tables:
        by_doc.setdefault(t["doc_id"], []).append(t)

    chunks_by_doc: dict[str, list[dict]] = {}
    for d in docs:
        rows = store.conn.execute(
            "SELECT loc, text FROM chunks WHERE doc_id=?", [d["doc_id"]]).fetchall()
        chunks_by_doc[d["doc_id"]] = [{"loc": r[0], "text": r[1]} for r in rows]

    doc_summaries = {d["doc_id"]: _doc_summary(llm, store, d, by_doc.get(d["doc_id"], []),
                                               chunks_by_doc.get(d["doc_id"], []))
                     for d in docs}

    review = [t for t in tables if t["needs_review"]]

    md = [f"# Project context: {project}", ""]
    md.append(f"{len(docs)} document(s), {len(tables)} extracted table(s), "
              f"{len(links)} cross-file link(s), {len(review)} table(s) awaiting review.")
    md.append("")

    md.append("## Documents")
    for d in docs:
        s = doc_summaries[d["doc_id"]]
        md.append(f"### {d['filename']}  `{d['filetype']}`  — status: {d['status']}")
        md.append(s["summary"])
        if s.get("entities"):
            md.append(f"*Entities:* {', '.join(s['entities'][:12])}")
        for t in by_doc.get(d["doc_id"], []):
            flag = " ⚠ needs review" if t["needs_review"] else ""
            cols = ", ".join(c["name"] for c in t["schema"][:12])
            md.append(f"- **{t['name']}** ({t['source_ref']}, {t['n_rows']}×{t['n_cols']}, "
                      f"method={t['method']}, confidence={t['confidence']:.2f}{flag})")
            md.append(f"  - columns: {cols}")
        md.append("")

    if families:
        md.append("## Schema families (same remembered schema across tables)")
        for f in families:
            md.append(f"- schema `{f['schema_id']}` (seen {f['seen_count']}×): "
                      f"{', '.join(f['tables'])}")
        md.append("")

    if links:
        md.append("## Cross-file links")
        name_of = {t["table_id"]: t["name"] for t in tables}
        for l in links:
            md.append(f"- `{name_of.get(l['src_table'], '?')}.{l['src_col']}` "
                      f"**{l['kind']}** `{name_of.get(l['dst_table'], '?')}.{l['dst_col']}` "
                      f"(confidence {l['confidence']:.2f}, {l['method']}; {l['evidence']})")
        md.append("")
        joins = [l for l in links if l["kind"] == "JOINABLE"]
        if joins:
            md.append("### Suggested joins")
            for l in joins[:8]:
                a, b = name_of.get(l["src_table"], "?"), name_of.get(l["dst_table"], "?")
                md.append(f"```sql\nSELECT * FROM {a} JOIN {b} ON "
                          f"{a}.{l['src_col']} = {b}.{l['dst_col']};\n```")
            md.append("")

    if review:
        md.append("## Review queue")
        for t in review:
            md.append(f"- {t['name']} ({t['source_ref']}): confidence {t['confidence']:.2f} "
                      f"— {'; '.join(t['issues'][:3])}")
        md.append("")

    md.append("## How to query")
    md.append("Every table is a Parquet file; query ad hoc with DuckDB:")
    md.append("```sql")
    for t in tables[:3]:
        md.append(f"SELECT * FROM read_parquet('{t['parquet_path']}') LIMIT 5;")
    md.append("```")
    md.append(f"\nOr `docbrain ask {project} \"your question\"`.")

    PATHS.ensure(project)
    out_md = PATHS.context_path(project)
    out_md.write_text("\n".join(md))
    out_json = out_md.with_suffix(".json")
    out_json.write_text(json.dumps({
        "project": project,
        "documents": [{**{k: d[k] for k in ("doc_id", "filename", "filetype", "status")},
                       "summary": doc_summaries[d["doc_id"]]} for d in docs],
        "tables": [{k: t[k] for k in ("table_id", "name", "source_ref", "parquet_path",
                                      "n_rows", "n_cols", "method", "confidence",
                                      "needs_review")} | {"columns": t["schema"]}
                   for t in tables],
        "links": links,
        "schema_families": families,
    }, indent=1, default=str))
    return out_md


# ---------------------------------------------------------------- ask
SQL_FORBIDDEN = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|ATTACH|COPY|PRAGMA|INSTALL|LOAD)\b", re.I)
MAX_EVIDENCE_TURNS = 4


def ask(store: Store, project: str, question: str, llm: LLM,
        ledger=None) -> str:
    if not llm.available:
        return "No LLM backend available — set ANTHROPIC_API_KEY or install the claude CLI."
    ctx_path = PATHS.context_path(project)
    if not ctx_path.exists():
        build_context(store, project, llm)
    context_pack = ctx_path.read_text()

    tables = store.tables(project)
    view_to_table: dict[str, str] = {}
    conn = duckdb.connect()
    for t in tables:
        safe = re.sub(r"[^\w]", "_", t["name"])
        view_to_table[safe] = t["table_id"]
        try:
            conn.execute(f"CREATE OR REPLACE VIEW {safe} AS "
                         f"SELECT * FROM read_parquet('{t['parquet_path']}')")
        except Exception:
            continue

    queries: list[dict] = []      # provenance: every SQL + tables it touched
    touched: set[str] = set()

    def _record(q: str, ok: bool):
        hit = sorted({v for v in view_to_table
                      if re.search(rf"\b{re.escape(v)}\b", q)})
        for v in hit:
            touched.add(view_to_table[v])
        queries.append({"sql": q, "ok": ok, "tables": hit})

    transcript: list[dict] = []
    for _turn in range(MAX_EVIDENCE_TURNS + 1):
        prompt = (f"CONTEXT PACK:\n{context_pack[:14000]}\n\n"
                  f"QUESTION: {question}\n\n"
                  f"EVIDENCE SO FAR:\n{json.dumps(transcript, default=str)[:8000]}")
        try:
            step = llm.complete_json(prompt, system=prompts.ASK_SYSTEM, max_tokens=3000)
        except (LLMError, ValueError) as e:
            return f"(ask failed: {e})"
        action = (step or {}).get("action")
        if action == "answer":
            conf = step.get("confidence")
            suffix = f"\n\n_confidence: {conf}_" if conf is not None else ""
            answer = step.get("answer", "").strip()
            if ledger is not None:
                entry = ledger.append("ask", {
                    "project": project,
                    "detail": {"question": question, "queries": queries,
                               "tables_touched": sorted(touched),
                               "confidence": conf},
                    "ok": True,
                })
                for tid in touched:
                    store.bump_usage("table", tid)
                    store.add_lineage("answer", entry["entry_hash"], "queried",
                                      "table", tid, entry["entry_hash"], {})
                name_of = {t["table_id"]: t["name"] for t in tables}
                evidence = ", ".join(name_of.get(t, t) for t in sorted(touched)) or "text search only"
                suffix += f"\n_evidence (ledger-verified): {evidence}_"
            return answer + suffix
        if action == "sql":
            q = step.get("query", "")
            if SQL_FORBIDDEN.search(q):
                _record(q, ok=False)
                transcript.append({"sql": q, "error": "only SELECT queries allowed"})
                continue
            try:
                df = conn.execute(q).fetchdf().head(40)
                _record(q, ok=True)
                transcript.append({"sql": q, "rows": json.loads(
                    df.to_json(orient="records", date_format="iso"))})
            except Exception as e:
                _record(q, ok=False)
                transcript.append({"sql": q, "error": str(e)[:400]})
            continue
        if action == "search":
            hits = store.search_chunks(project, step.get("query", ""), k=4)
            transcript.append({"search": step.get("query", ""),
                               "hits": [{"file": h["filename"], "loc": h["loc"],
                                         "text": h["text"][:800]} for h in hits]})
            continue
        transcript.append({"error": f"unrecognized action {action!r}"})
    return "(no answer after evidence loop — try `docbrain context` output directly)"
