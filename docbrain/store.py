"""DuckDB catalog — the local-first stand-in for the Iceberg/Parquet lakehouse +
metadata catalog. Extracted tables live as Parquet files on disk; DuckDB holds
documents, table metadata, schema registry (schema memory), cross-file links,
and text chunks. Swap point for cloud: replace Parquet paths with s3:// URIs and
this catalog with Iceberg/Glue — the interfaces below stay the same."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from .config import PATHS

DDL = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    project TEXT,
    path TEXT,
    filename TEXT,
    content_hash TEXT,
    filetype TEXT,
    status TEXT,               -- ingested | partial | failed | unsupported
    n_pages INTEGER,
    meta JSON,
    ingested_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS extracted_tables (
    table_id TEXT PRIMARY KEY,
    doc_id TEXT,
    project TEXT,
    name TEXT,
    source_ref TEXT,           -- e.g. "Sheet1!B4:F12", "page 3", "csv block 2"
    parquet_path TEXT,
    n_rows INTEGER,
    n_cols INTEGER,
    schema_json JSON,
    method TEXT,               -- csv | xlsx-island | pdf-table | vision | agent
    confidence DOUBLE,
    needs_review BOOLEAN,
    issues JSON,
    created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS schema_registry (
    schema_id TEXT PRIMARY KEY,
    fingerprint TEXT UNIQUE,
    columns_json JSON,         -- [{name, dtype}]
    contracts_json JSON,       -- per-column expectations learned on first sight
    seen_count INTEGER,
    first_seen TIMESTAMP,
    last_seen TIMESTAMP
);
CREATE TABLE IF NOT EXISTS table_schema_map (
    table_id TEXT,
    schema_id TEXT,
    reused_memory BOOLEAN,     -- true when fingerprint was already known
    drift_json JSON            -- contract violations vs remembered schema
);
CREATE TABLE IF NOT EXISTS links (
    link_id TEXT PRIMARY KEY,
    project TEXT,
    src_table TEXT, src_col TEXT,
    dst_table TEXT, dst_col TEXT,
    kind TEXT,                 -- SAME_AS | JOINABLE
    confidence DOUBLE,
    method TEXT,               -- heuristic | llm
    evidence TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    doc_id TEXT,
    project TEXT,
    loc TEXT,                  -- e.g. "page 2"
    text TEXT
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def short_id(*parts: str, n: int = 12) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:n]


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Store:
    def __init__(self, db_path: Path | None = None):
        db = db_path or PATHS.catalog
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(db))
        for stmt in DDL.strip().split(";"):
            if stmt.strip():
                self.conn.execute(stmt)

    # -- documents ------------------------------------------------------
    def get_document_by_hash(self, project: str, content_hash: str):
        return self.conn.execute(
            "SELECT doc_id, status FROM documents WHERE project=? AND content_hash=?",
            [project, content_hash],
        ).fetchone()

    def upsert_document(self, doc_id: str, project: str, path: Path, content_hash: str,
                        filetype: str, status: str, n_pages: int = 0, meta: dict | None = None):
        self.conn.execute("DELETE FROM documents WHERE doc_id=?", [doc_id])
        self.conn.execute(
            "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)",
            [doc_id, project, str(path), path.name, content_hash, filetype,
             status, n_pages, json.dumps(meta or {}), _now()],
        )

    def set_document_meta(self, doc_id: str, meta: dict):
        self.conn.execute("UPDATE documents SET meta=? WHERE doc_id=?", [json.dumps(meta), doc_id])

    def documents(self, project: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT doc_id, filename, filetype, status, n_pages, meta, path FROM documents "
            "WHERE project=? ORDER BY filename", [project]).fetchall()
        return [dict(zip(["doc_id", "filename", "filetype", "status", "n_pages", "meta", "path"], r,
                         strict=True)) | {"meta": json.loads(r[5] or "{}")} for r in rows]

    # -- extracted tables -----------------------------------------------
    def delete_tables_for_doc(self, doc_id: str):
        ids = [r[0] for r in self.conn.execute(
            "SELECT table_id FROM extracted_tables WHERE doc_id=?", [doc_id]).fetchall()]
        self.conn.execute("DELETE FROM extracted_tables WHERE doc_id=?", [doc_id])
        self.conn.execute("DELETE FROM chunks WHERE doc_id=?", [doc_id])
        for tid in ids:
            self.conn.execute("DELETE FROM table_schema_map WHERE table_id=?", [tid])
            self.conn.execute("DELETE FROM links WHERE src_table=? OR dst_table=?", [tid, tid])

    def add_table(self, *, table_id: str, doc_id: str, project: str, name: str,
                  source_ref: str, parquet_path: Path, df: pd.DataFrame, method: str,
                  confidence: float, needs_review: bool, issues: list[str]):
        schema = [{"name": str(c), "dtype": str(df[c].dtype)} for c in df.columns]
        self.conn.execute(
            "INSERT INTO extracted_tables VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [table_id, doc_id, project, name, source_ref, str(parquet_path),
             len(df), len(df.columns), json.dumps(schema), method,
             confidence, needs_review, json.dumps(issues), _now()],
        )

    def tables(self, project: str) -> list[dict]:
        cols = ["table_id", "doc_id", "name", "source_ref", "parquet_path", "n_rows",
                "n_cols", "schema_json", "method", "confidence", "needs_review", "issues"]
        rows = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM extracted_tables WHERE project=? ORDER BY name",
            [project]).fetchall()
        out = []
        for r in rows:
            d = dict(zip(cols, r, strict=True))
            d["schema"] = json.loads(d.pop("schema_json"))
            d["issues"] = json.loads(d["issues"] or "[]")
            out.append(d)
        return out

    # -- schema registry (schema memory) --------------------------------
    def lookup_schema(self, fingerprint: str):
        return self.conn.execute(
            "SELECT schema_id, contracts_json, seen_count FROM schema_registry WHERE fingerprint=?",
            [fingerprint]).fetchone()

    def register_schema(self, schema_id: str, fingerprint: str, columns: list[dict], contracts: dict):
        self.conn.execute(
            "INSERT INTO schema_registry VALUES (?,?,?,?,?,?,?)",
            [schema_id, fingerprint, json.dumps(columns), json.dumps(contracts), 1, _now(), _now()])

    def touch_schema(self, schema_id: str):
        self.conn.execute(
            "UPDATE schema_registry SET seen_count = seen_count + 1, last_seen=? WHERE schema_id=?",
            [_now(), schema_id])

    def map_table_schema(self, table_id: str, schema_id: str, reused: bool, drift: list[str]):
        self.conn.execute("INSERT INTO table_schema_map VALUES (?,?,?,?)",
                          [table_id, schema_id, reused, json.dumps(drift)])

    def schemas(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT schema_id, fingerprint, columns_json, seen_count FROM schema_registry").fetchall()
        return [{"schema_id": r[0], "fingerprint": r[1], "columns": json.loads(r[2]),
                 "seen_count": r[3]} for r in rows]

    def schema_families(self, project: str) -> list[dict]:
        """Groups of tables in this project sharing a remembered schema."""
        rows = self.conn.execute("""
            SELECT m.schema_id, list(t.name), any_value(s.columns_json), any_value(s.seen_count)
            FROM table_schema_map m
            JOIN extracted_tables t ON t.table_id = m.table_id
            JOIN schema_registry s ON s.schema_id = m.schema_id
            WHERE t.project = ?
            GROUP BY m.schema_id HAVING count(*) > 1
        """, [project]).fetchall()
        return [{"schema_id": r[0], "tables": r[1], "columns": json.loads(r[2]),
                 "seen_count": r[3]} for r in rows]

    # -- links -----------------------------------------------------------
    def clear_links(self, project: str):
        self.conn.execute("DELETE FROM links WHERE project=?", [project])

    def add_link(self, project: str, src_table: str, src_col: str, dst_table: str,
                 dst_col: str, kind: str, confidence: float, method: str, evidence: str):
        lid = short_id(project, src_table, src_col, dst_table, dst_col, kind)
        self.conn.execute("INSERT OR REPLACE INTO links VALUES (?,?,?,?,?,?,?,?,?,?)",
                          [lid, project, src_table, src_col, dst_table, dst_col,
                           kind, confidence, method, evidence])

    def links(self, project: str) -> list[dict]:
        cols = ["src_table", "src_col", "dst_table", "dst_col", "kind", "confidence", "method", "evidence"]
        rows = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM links WHERE project=? ORDER BY confidence DESC",
            [project]).fetchall()
        return [dict(zip(cols, r, strict=True)) for r in rows]

    # -- chunks ------------------------------------------------------------
    def add_chunk(self, doc_id: str, project: str, loc: str, text: str):
        cid = short_id(doc_id, loc, text[:64])
        self.conn.execute("INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?)",
                          [cid, doc_id, project, loc, text])

    def search_chunks(self, project: str, query: str, k: int = 5) -> list[dict]:
        """Tiny token-overlap scorer — good enough locally; swap for FTS/embeddings later."""
        terms = [t.lower() for t in query.split() if len(t) > 2]
        rows = self.conn.execute(
            "SELECT c.chunk_id, c.loc, c.text, d.filename FROM chunks c "
            "JOIN documents d ON d.doc_id=c.doc_id WHERE c.project=?", [project]).fetchall()
        scored = []
        for cid, loc, text, fname in rows:
            low = text.lower()
            score = sum(low.count(t) for t in terms)
            if score:
                scored.append({"chunk_id": cid, "loc": loc, "text": text,
                               "filename": fname, "score": score})
        return sorted(scored, key=lambda x: -x["score"])[:k]

    def close(self):
        self.conn.close()
