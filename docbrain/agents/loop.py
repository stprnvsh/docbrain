"""Sandboxed iterative reasoning loop — the part worth copying wholesale from
SheetBrain/SheetAgent: understand (structural sketch) -> act (write + execute
code in the sandbox) -> validate -> re-execute on failure.

refine_table() is format-agnostic: it receives a TableCandidate + the source
file and may replace the candidate with better extractions. vision_extract_page()
is the scanned-PDF path (render -> look -> structured tables)."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ..ir import TableCandidate, dedupe_columns, infer_types, normalize_col, sample_markdown
from ..llm import LLM, LLMError
from ..sandbox import Sandbox, describe_outputs
from . import prompts
from .render import render_grid_png

MAX_ITERS = 3


@dataclass
class RefineOutcome:
    tables: list[TableCandidate]
    accepted_original: bool = False
    gave_up: bool = False
    iterations: int = 0
    log: list[str] = field(default_factory=list)
    # Set when the agent authored a parser that validated (curation inputs):
    winning_code: str | None = None
    format_id: str | None = None
    format_description: str = ""
    # Non-table text the exploration preserved (preambles, footnotes):
    text_notes: list[str] = field(default_factory=list)


def _state_prompt(candidate: TableCandidate, filename: str, attempts: list[dict],
                  vision_available: bool, image_attached: bool) -> str:
    state = {
        "filename": filename,
        "method": candidate.method,
        "source_ref": candidate.source_ref,
        "flags": candidate.flags,
        "notes": candidate.notes,
        "sketch": {k: v for k, v in candidate.sketch.items() if k != "grid_preview"},
        "grid_preview": candidate.sketch.get("grid_preview"),
        "current_result": {
            "shape": list(candidate.df.shape),
            "columns": [{"name": str(c), "dtype": str(candidate.df[c].dtype)}
                        for c in candidate.df.columns],
            "sample": sample_markdown(candidate.df),
        },
        "previous_attempts": attempts,
        "vision_available": vision_available and not image_attached,
    }
    return json.dumps(state, default=str, indent=1)


def refine_table(llm: LLM, sandbox: Sandbox, file_path: Path,
                 candidate: TableCandidate) -> RefineOutcome:
    outcome = RefineOutcome(tables=[candidate])
    attempts: list[dict] = []
    images: list[Path] = []
    tmp = Path(tempfile.mkdtemp(prefix="docbrain-agent-"))

    for it in range(1, MAX_ITERS + 1):
        outcome.iterations = it
        try:
            action = llm.complete_json(
                _state_prompt(candidate, file_path.name, attempts,
                              llm.supports_vision, bool(images)),
                system=prompts.REFINE_SYSTEM, images=images or None)
        except (LLMError, ValueError) as e:
            outcome.log.append(f"iter{it}: llm error: {e}")
            outcome.accepted_original = True
            return outcome

        kind = (action or {}).get("action")
        if kind == "accept":
            outcome.accepted_original = True
            outcome.log.append(f"iter{it}: accepted heuristic result")
            return outcome

        if kind == "give_up":
            outcome.gave_up = True
            outcome.tables = []
            outcome.log.append(f"iter{it}: not a table ({action.get('reason', '')})")
            return outcome

        if kind == "vision":
            grid = candidate.sketch.get("grid_preview")
            if grid:
                png = tmp / "region.png"
                render_grid_png(grid, png, title=candidate.source_ref)
                images = [png]
                outcome.log.append(f"iter{it}: rendered region for vision")
                continue
            outcome.log.append(f"iter{it}: vision requested but nothing to render")
            attempts.append({"error": "no renderable region available"})
            continue

        if kind == "code":
            code = action.get("code", "")
            res = sandbox.run_python(code, {file_path.name: file_path})
            desc = describe_outputs(res.outputs)
            attempts.append({
                "code": code[:2000],
                "ok": res.ok,
                "stdout": res.stdout[-1500:],
                "stderr": res.stderr[-1500:],
                "outputs": desc,
            })
            outcome.log.append(f"iter{it}: code run ok={res.ok} outputs={len(desc)}")
            if res.ok and desc and all("error" not in d for d in desc):
                tables = []
                for d in desc:
                    df = pd.read_parquet(res.workdir / "out" / d["path"])
                    df.columns = dedupe_columns([normalize_col(c) for c in df.columns])
                    df = infer_types(df)
                    tables.append(TableCandidate(
                        df=df,
                        name=f"{candidate.name}" if len(desc) == 1
                             else f"{candidate.name}_{Path(d['path']).stem}",
                        source_ref=candidate.source_ref,
                        method="agent",
                        flags=[],
                        notes=[f"agent re-extraction (iter {it})",
                               action.get("reason", "")],
                        sketch=candidate.sketch,
                    ))
                outcome.tables = tables
                return outcome
            continue

        outcome.log.append(f"iter{it}: unrecognized action {kind!r}")
        attempts.append({"error": f"unrecognized action {kind!r}"})

    outcome.accepted_original = True
    outcome.log.append("max iterations reached; keeping heuristic result")
    return outcome


def parse_unknown_text(llm: LLM, sandbox: Sandbox, file_path: Path,
                       head_text: str, doc_stem: str,
                       reference_script: str | None = None) -> RefineOutcome:
    """Unknown text format (controller logs, proprietary exports): the agent
    authors a reusable parser script, run in the sandbox against the
    standardized manifest contract. The winning script is exposed on the
    outcome (winning_code/format_id) so the registry can curate it."""
    from ..sandbox import load_manifest
    from ..scripts_registry import manifest_to_candidates

    outcome = RefineOutcome(tables=[])
    attempts: list[dict] = []
    size = file_path.stat().st_size
    for it in range(1, MAX_ITERS + 1):
        outcome.iterations = it
        state = {
            "filename": file_path.name,
            "file_size_bytes": size,
            "head": head_text[:3000],
            "reference_script": (reference_script or "")[:3000] or None,
            "previous_attempts": attempts,
        }
        try:
            action = llm.complete_json(json.dumps(state, default=str, indent=1),
                                       system=prompts.UNKNOWN_TEXT_SYSTEM,
                                       max_tokens=8000)
        except (LLMError, ValueError) as e:
            outcome.log.append(f"iter{it}: llm error: {e}")
            return outcome
        kind = (action or {}).get("action")
        if kind == "skip":
            outcome.gave_up = True
            outcome.log.append(f"iter{it}: skip ({action.get('reason', '')})")
            return outcome
        if kind != "code":
            attempts.append({"error": f"unrecognized action {kind!r}"})
            continue
        code = action.get("code", "")
        res = sandbox.run_python(code, {file_path.name: file_path})
        manifest = load_manifest(res)
        desc = describe_outputs(res.outputs)
        attempts.append({"code": code[:2500], "ok": res.ok,
                         "stdout": res.stdout[-1500:], "stderr": res.stderr[-1500:],
                         "manifest_valid": bool(manifest and manifest["tables"]),
                         "manifest_problems": (manifest or {}).get("_invalid", []),
                         "outputs": desc})
        outcome.log.append(f"iter{it}: parser run ok={res.ok} "
                           f"manifest={'ok' if manifest and manifest['tables'] else 'MISSING/EMPTY'} "
                           f"({action.get('reason', '')[:80]})")
        if res.ok and manifest and manifest["tables"]:
            outcome.tables = manifest_to_candidates(
                manifest, doc_stem, file_path.name, method="agent")
            outcome.winning_code = code
            outcome.format_id = manifest.get("format_id") or action.get("format_id") \
                or f"{doc_stem}-records"
            outcome.format_description = action.get("reason", "")[:200]
            return outcome
    outcome.log.append("max iterations reached; no usable parse")
    return outcome


MAX_EXPLORE_ITERS = 6


def explore_file(llm: LLM, sandbox: Sandbox, file_path: Path,
                 heuristic_summary: list[dict], doc_stem: str) -> RefineOutcome:
    """Whole-file exploration: the agent probes the file in the sandbox as many
    times as it needs (blank-line maps, widths, encodings, sub-table hunting),
    THEN writes one reusable extraction script under the manifest contract.
    Every probe is a sandbox run — ledger-recorded automatically."""
    from ..sandbox import load_manifest
    from ..scripts_registry import manifest_to_candidates

    outcome = RefineOutcome(tables=[])
    attempts: list[dict] = []
    for it in range(1, MAX_EXPLORE_ITERS + 1):
        outcome.iterations = it
        state = {
            "filename": file_path.name,
            "file_size_bytes": file_path.stat().st_size,
            "heuristic_draft": heuristic_summary,
            "exploration_so_far": attempts,
        }
        try:
            action = llm.complete_json(json.dumps(state, default=str, indent=1),
                                       system=prompts.EXPLORE_SYSTEM,
                                       max_tokens=8000)
        except (LLMError, ValueError) as e:
            outcome.log.append(f"iter{it}: llm error: {e}")
            outcome.accepted_original = True
            return outcome
        kind = (action or {}).get("action")
        if kind == "accept":
            outcome.accepted_original = True
            outcome.log.append(f"iter{it}: heuristic draft accepted "
                               f"({action.get('reason', '')[:80]})")
            return outcome
        if kind == "probe":
            res = sandbox.run_python(action.get("code", ""),
                                     {file_path.name: file_path})
            attempts.append({"probe": action.get("reason", "")[:100],
                             "ok": res.ok, "stdout": res.stdout[-2500:],
                             "stderr": res.stderr[-800:]})
            outcome.log.append(f"iter{it}: probe ({action.get('reason', '')[:60]})")
            continue
        if kind == "extract":
            code = action.get("code", "")
            res = sandbox.run_python(code, {file_path.name: file_path})
            manifest = load_manifest(res)
            attempts.append({"extract_attempt": True, "ok": res.ok,
                             "stdout": res.stdout[-1500:], "stderr": res.stderr[-1200:],
                             "manifest_valid": bool(manifest and manifest["tables"]),
                             "manifest_problems": (manifest or {}).get("_invalid", [])})
            outcome.log.append(
                f"iter{it}: extract ok={res.ok} "
                f"manifest={'ok' if manifest and manifest['tables'] else 'MISSING/EMPTY'}")
            if res.ok and manifest and manifest["tables"]:
                outcome.tables = manifest_to_candidates(
                    manifest, doc_stem, file_path.name, method="agent")
                outcome.winning_code = code
                outcome.format_id = manifest.get("format_id") or action.get("format_id") \
                    or f"{doc_stem}-explored"
                outcome.format_description = action.get("reason", "")[:200]
                outcome.text_notes = [str(t) for t in
                                      (manifest.get("text_notes") or [])][:10]
                return outcome
            continue
        attempts.append({"error": f"unrecognized action {kind!r}"})
    outcome.accepted_original = True
    outcome.log.append("exploration budget exhausted; keeping heuristic draft")
    return outcome


@dataclass
class ValidationOutcome:
    verdict: str            # pass | fail | skip | error
    checks: list[dict] = field(default_factory=list)
    discrepancies: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


def validate_extraction(llm: LLM, sandbox: Sandbox, file_path: Path,
                        candidate: TableCandidate, parquet_path: Path,
                        max_iters: int = 2) -> ValidationOutcome:
    """Verify-by-re-execution: the agent gets the ORIGINAL file and the
    extracted parquet mounted together and writes independent cross-checking
    code. The harness reads OUT/verdict.json — the agent never grades itself
    by assertion, only by executed checks."""
    outcome = ValidationOutcome(verdict="error")
    attempts: list[dict] = []
    old_phase = sandbox.context.get("phase")
    sandbox.context["phase"] = "validate"
    try:
        for it in range(1, max_iters + 1):
            state = {
                "filename": file_path.name,
                "source_ref": candidate.source_ref,
                "method": candidate.method,
                "extracted_shape": list(candidate.df.shape),
                "extracted_columns": [{"name": str(c), "dtype": str(candidate.df[c].dtype)}
                                      for c in candidate.df.columns],
                "extraction_notes": candidate.notes[:4],
                "sketch": {k: v for k, v in candidate.sketch.items()
                           if k in ("sheet", "range", "header_rows", "page",
                                    "delimiter", "encoding", "engine")},
                "previous_attempts": attempts,
            }
            try:
                action = llm.complete_json(json.dumps(state, default=str, indent=1),
                                           system=prompts.VALIDATE_SYSTEM,
                                           max_tokens=6000)
            except (LLMError, ValueError) as e:
                outcome.log.append(f"iter{it}: llm error: {e}")
                return outcome
            kind = (action or {}).get("action")
            if kind == "skip":
                outcome.verdict = "skip"
                outcome.log.append(f"iter{it}: skip ({action.get('reason', '')})")
                return outcome
            if kind != "code":
                attempts.append({"error": f"unrecognized action {kind!r}"})
                continue
            res = sandbox.run_python(action.get("code", ""),
                                     {file_path.name: file_path,
                                      "extracted.parquet": parquet_path})
            verdict_file = (res.workdir / "out" / "verdict.json") if res.workdir else None
            if res.ok and verdict_file and verdict_file.exists():
                try:
                    v = json.loads(verdict_file.read_text())
                except json.JSONDecodeError:
                    v = None
                if isinstance(v, dict) and v.get("verdict") in ("pass", "fail"):
                    outcome.verdict = v["verdict"]
                    outcome.checks = [c for c in v.get("checks", []) if isinstance(c, dict)]
                    outcome.discrepancies = [str(d) for d in v.get("discrepancies", [])][:8]
                    n_ok = sum(1 for c in outcome.checks if c.get("ok"))
                    outcome.log.append(
                        f"iter{it}: verdict={outcome.verdict} "
                        f"({n_ok}/{len(outcome.checks)} checks ok)")
                    return outcome
            attempts.append({"code": action.get("code", "")[:2000], "ok": res.ok,
                             "stdout": res.stdout[-1200:], "stderr": res.stderr[-1200:],
                             "verdict_json": "missing or malformed"})
            outcome.log.append(f"iter{it}: verification run ok={res.ok}, no usable verdict")
        outcome.log.append("max iterations reached without a verdict")
        return outcome
    finally:
        sandbox.context["phase"] = old_phase


def vision_extract_page(llm: LLM, png_path: Path, page_no: int,
                        doc_stem: str) -> tuple[list[TableCandidate], str]:
    """Scanned-page path: image -> structured tables + text."""
    data = llm.complete_json(
        f"This is page {page_no} of document '{doc_stem}'. Extract per the system instructions.",
        system=prompts.VISION_TABLE_SYSTEM, images=[png_path], max_tokens=8000)
    tables: list[TableCandidate] = []
    for ti, t in enumerate((data or {}).get("tables", [])):
        cols = dedupe_columns([normalize_col(c) for c in t.get("columns", [])])
        rows = t.get("rows", [])
        if not cols or not rows:
            continue
        width = len(cols)
        rows = [list(r)[:width] + [None] * max(0, width - len(r)) for r in rows]
        df = infer_types(pd.DataFrame(rows, columns=cols))
        tables.append(TableCandidate(
            df=df,
            name=f"{doc_stem}_p{page_no}_{normalize_col(t.get('name') or f't{ti + 1}')}",
            source_ref=f"page {page_no} (scanned)",
            method="vision",
            flags=["vision_extraction"],
            notes=["extracted from page image"],
        ))
    return tables, (data or {}).get("text", "")
