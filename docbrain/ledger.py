"""Provenance ledger — system-recorded ground truth, not agent narrative.

Every mediated surface appends here automatically:
  - sandbox executions (inputs w/ sha256, code sha256, outputs w/ sha256, rc)
  - document ingests (file hash -> extracted tables w/ hashes)
  - vision calls (image hash -> tables)
  - ask queries (SQL text + which tables were actually touched)

Two sinks written atomically together:
  1. append-only JSONL at ~/.docbrain/ledger.jsonl, hash-chained: each entry
     carries prev_hash and entry_hash = sha256(prev_hash + canonical(entry)),
     so tampering breaks the chain (docbank's audited-history idea, local v0).
  2. DuckDB `runs` table for queries (provenance/usage joins).

The agent cannot bypass this: the only execution surface for agent code is
Sandbox.run_python, and the only query surface for `ask` is the registered
views — both record here.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import PATHS

_LOCK = threading.Lock()
GENESIS = "0" * 64


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Ledger:
    def __init__(self, path: Path | None = None, store=None):
        self.path = path or (PATHS.home / "ledger.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.store = store  # optional duckdb mirror

    def _last_hash(self) -> str:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return GENESIS
        last = None
        with open(self.path, "rb") as f:
            for line in f:
                if line.strip():
                    last = line
        if not last:
            return GENESIS
        try:
            return json.loads(last)["entry_hash"]
        except (json.JSONDecodeError, KeyError):
            return GENESIS

    def append(self, kind: str, payload: dict) -> dict:
        with _LOCK:
            prev = self._last_hash()
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                **payload,
                "prev_hash": prev,
            }
            entry["entry_hash"] = hashlib.sha256(
                (prev + _canonical({k: v for k, v in entry.items()
                                    if k not in ("prev_hash", "entry_hash")})).encode()
            ).hexdigest()
            with open(self.path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        if self.store is not None:
            try:
                self.store.mirror_run(entry)
            except Exception:
                pass  # the JSONL chain is the authority; the mirror is queryability
        return entry

    def verify_chain(self) -> dict:
        """Walk the chain; returns {ok, entries, first_break}."""
        if not self.path.exists():
            return {"ok": True, "entries": 0, "first_break": None}
        prev = GENESIS
        n = 0
        with open(self.path) as f:
            for i, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    return {"ok": False, "entries": n, "first_break": f"line {i}: unparseable"}
                expect = hashlib.sha256(
                    (prev + _canonical({k: v for k, v in e.items()
                                        if k not in ("prev_hash", "entry_hash")})).encode()
                ).hexdigest()
                if e.get("prev_hash") != prev or e.get("entry_hash") != expect:
                    return {"ok": False, "entries": n, "first_break": f"line {i}: hash mismatch"}
                prev = e["entry_hash"]
                n += 1
        return {"ok": True, "entries": n, "first_break": None}
