"""TXT track — a first-class path for text files, separate from CSV.

Triage per file:
  delimited  -> genuinely tabular text (a real delimiter + consistent widths):
                delegate to the CSV machinery.
  records    -> structured but proprietary (controller logs, exports): the
                agent identifies the format and writes a parser that runs in
                the sandbox (LLM path). Header text is always kept as context.
  prose      -> narrative text: chunked for the context/search layer.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from pathlib import Path

from ..detectors.csv_dialect import decode_bytes, sniff_dialect
from ..ir import TableCandidate

ALLOWED_DELIMS = {",", ";", "\t", "|"}


def triage(text: str) -> tuple[str, dict]:
    """Returns (subtype, evidence). Subtype: delimited | records | prose."""
    lines = [l for l in text.splitlines() if l.strip()][:400]
    if not lines:
        return "prose", {"reason": "empty"}
    sample = "\n".join(lines)

    delim, quote, method = sniff_dialect(sample)
    if delim in ALLOWED_DELIMS:
        widths = Counter()
        for row in csv.reader(io.StringIO(sample), delimiter=delim, quotechar=quote):
            if any(c.strip() for c in row):
                widths[len(row)] += 1
        if widths:
            modal, count = widths.most_common(1)[0]
            consistency = count / sum(widths.values())
            if modal >= 2 and consistency >= 0.7:
                return "delimited", {"delimiter": delim, "modal_width": modal,
                                     "consistency": round(consistency, 2)}

    # Prose vs records: prose is alphabetic and space-worded; logs are dense in
    # digits/symbols and structurally repetitive.
    joined = "".join(lines)
    nonspace = [c for c in joined if not c.isspace()]
    alpha_ratio = sum(c.isalpha() for c in nonspace) / max(len(nonspace), 1)
    prefixes = Counter(l.strip()[:2] for l in lines)
    repetitive = prefixes.most_common(1)[0][1] / len(lines) if lines else 0.0
    ev = {"alpha_ratio": round(alpha_ratio, 2), "prefix_repetition": round(repetitive, 2)}
    if alpha_ratio < 0.7 or repetitive > 0.5:
        return "records", ev
    return "prose", ev


def chunk_text(text: str, target: int = 2500, max_chunks: int = 60) -> list[str]:
    paras = text.split("\n\n")
    out: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) > target and buf:
            out.append(buf.strip())
            buf = p
        else:
            buf += "\n\n" + p
        if len(out) >= max_chunks:
            break
    if buf.strip() and len(out) < max_chunks:
        out.append(buf.strip())
    return [c for c in out if c]


def records_extract(path: Path, head_text: str, llm, sandbox,
                    agent_enabled: bool, store=None) -> tuple[list[TableCandidate], list[str]]:
    """Proprietary record formats, curated-script flow:
    1. try remembered scripts matching this format signature (no LLM);
    2. else the agent authors/adapts a parser in the sandbox;
    3. a validated new script is saved back to the registry."""
    from ..scripts_registry import (find_matching_scripts, format_signature,
                                    register_script, try_script)
    notes: list[str] = []
    signature = format_signature(head_text, path.name)

    reference_code: str | None = None
    if store is not None:
        for script in find_matching_scripts(store, signature):
            tables = try_script(sandbox, script, path, path.stem)
            if tables:
                store.mark_script(script["script_id"], success=True)
                notes.append(f"[script-registry] reused '{script['format_id']}' "
                             f"(match {script['similarity']}, no LLM call)")
                return tables, notes
            store.mark_script(script["script_id"], success=False)
            notes.append(f"[script-registry] '{script['format_id']}' matched "
                         f"(sim {script['similarity']}) but failed validation here")
            reference_code = Path(script["script_path"]).read_text()

    if not (agent_enabled and llm is not None and llm.available):
        notes.append("records format, no LLM available — header captured, parsing deferred")
        return [], notes

    from ..agents.loop import parse_unknown_text
    outcome = parse_unknown_text(llm, sandbox, path, head_text, path.stem,
                                 reference_script=reference_code)
    notes.extend(f"[agent-parser] {line}" for line in outcome.log)
    if outcome.winning_code and store is not None:
        sid = register_script(store, outcome.winning_code, outcome.format_id,
                              outcome.format_description, signature)
        for c in outcome.tables:
            c.sketch["script_id"] = sid
        notes.append(f"[script-registry] saved new parser '{outcome.format_id}' ({sid})")
    return outcome.tables, notes


def extract(path: Path, llm=None, sandbox=None, agent_enabled: bool = True,
            store=None) -> tuple[list[TableCandidate], list[dict], dict]:
    """Returns (candidates, chunks, meta)."""
    raw = path.read_bytes()
    text, encoding = decode_bytes(raw)
    subtype, evidence = triage(text)
    meta: dict = {"subtype": subtype, "encoding": encoding, "evidence": evidence,
                  "notes": [f"txt triage: {subtype} ({evidence})"]}

    if subtype == "delimited":
        from . import csv_extract
        candidates, csv_meta = csv_extract.extract(path)
        if not csv_meta.get("unstructured"):
            meta |= {k: v for k, v in csv_meta.items() if k != "notes"}
            return candidates, [], meta
        subtype = "records"  # the CSV machinery disagreed — fall through
        meta["subtype"] = subtype

    head = "\n".join(text.splitlines()[:60])[:2500]

    if subtype == "prose":
        chunks = [{"loc": f"chunk {i + 1}", "text": c}
                  for i, c in enumerate(chunk_text(text))]
        meta["notes"].append(f"prose: {len(chunks)} chunk(s) for context")
        return [], chunks, meta

    # records
    chunks = [{"loc": "file head", "text": head}]
    from ..sandbox import Sandbox
    candidates, notes = records_extract(path, head, llm, sandbox or Sandbox(),
                                        agent_enabled, store=store)
    meta["notes"].extend(notes)
    return candidates, chunks, meta
