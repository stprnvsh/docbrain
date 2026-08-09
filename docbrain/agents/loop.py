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
