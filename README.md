# docbrain

Local-first, sandboxed, multi-format document understanding pipeline **plus** a
cross-document context brain: define a project, throw files at it (xlsx, csv,
pdf — messy ones), and get back validated tables, a schema memory that
recognizes recurring file shapes, cross-file links, and a single per-project
context pack you can query in natural language.

```
file in → router (format + quality classify)
        → specialist track (xlsx islands | csv dialect/encoding |
                            pdf page-class | txt triage)
        → structural sketch
        → sandboxed agentic refinement (code exec + vision fallback, only when flagged)
        → validator → confidence score → parquet + catalog
                        ↳ below threshold → human review queue
project → schema memory + cross-file linker → context.md / context.json → ask
```

The **txt track** triages every text file three ways: `delimited` (a real
delimiter + consistent row widths → CSV machinery), `records` (structured but
proprietary — controller logs, exports), `prose` (→ chunks for the context
layer). Proven on VR-Netlog traffic-signal logs: 6,355 per-second records
parsed into a typed table by an agent-written parser, first iteration.

**Nothing format-specific is hardcoded for `records` files.** The agent authors
reusable parser *scripts*; a **curated script registry** (`~/.docbrain/scripts`
+ catalog table, inspect with `docbrain scripts`) keeps them keyed by a format
signature. Similar file arrives → remembered scripts run first (zero LLM
calls); only on validation failure does the agent write/adapt one (seeded with
the best near-miss script). Every script — remembered or fresh — must satisfy
one **standardized output contract**: `OUT/manifest.json` (format_id, tables,
columns, descriptions) + typed parquet, validated by the harness before
anything enters the catalog. Freedom in *how*, fixed contract for *what*.

## Quick start

```bash
uv venv && uv pip install -e .

# demo corpus (messy xlsx w/ merged headers, cp1252 csv w/ ragged rows,
# pdf with a native page + a scanned page)
.venv/bin/python scripts/make_samples.py
docbrain ingest demo samples/
docbrain tables demo
docbrain context demo
docbrain ask demo "total revenue by region across all files?"
docbrain review demo        # what the confidence gate quarantined
```

Data lands in `~/.docbrain/` (override with `DOCBRAIN_HOME`): one DuckDB
catalog + per-project Parquet tables + sketches + context packs.

## LLM backends (auto-detected)

| backend      | when                            | notes                              |
|--------------|--------------------------------|------------------------------------|
| `anthropic`  | `ANTHROPIC_API_KEY` set         | vision via Messages API            |
| `claude-cli` | `claude` binary on PATH         | uses your Claude Code subscription auth; vision via Read tool |
| `none`       | neither                         | heuristics only; ambiguity → review queue |

Override with `DOCBRAIN_LLM=anthropic|claude-cli|none`, model with
`DOCBRAIN_MODEL`. Agent refinement: `DOCBRAIN_AGENT=auto|always|never`
(`auto` = only tables flagged ambiguous by the heuristics).

## What implements what (mapping to the research)

| pipeline piece | pattern source | file |
|---|---|---|
| per-page PDF classify before OCR/vision | Firecrawl pdf-inspector | `detectors/pdf_pages.py` |
| xlsx multi-table island detection + merged headers | eparse / TableSense problem class | `detectors/xlsx_tables.py` |
| csv encoding + dialect sniff + block split | CleverCSV + brief §4 | `detectors/csv_dialect.py` |
| ragged-row repair to modal width | Tasheeh (light version) | `extractors/csv_extract.py` |
| txt triage + agent-written parsers for unknown record formats | SheetAgent execution loop, generalized | `extractors/txt_extract.py`, `agents/loop.py` |
| understand → execute (sandboxed code) → validate loop | SheetBrain / SheetAgent | `agents/loop.py`, `sandbox.py`, `validate.py` |
| render-region vision fallback, one primitive two callers | SpreadsheetAgent / agentic-PDF | `agents/render.py`, `agents/loop.py` |
| confidence gate + review queue | agentic-extraction literature (~95% field conf) | `validate.py`, `cli.py review` |
| schema fingerprint registry + contracts + drift | brief §"schema memory" | `schema_memory.py`, `store.py` |
| cross-file SAME_AS / JOINABLE links (heuristic + LLM confirm) | Valentine-style schema matching, v0 | `linker.py` |
| per-project context pack + evidence-loop Q&A | the actual product | `context.py` |

## Sandbox

Agent-written code runs via `sandbox.py`:
- macOS: `sandbox-exec` — network **denied** (verified at startup), writes
  confined to the job dir, inputs mounted read-only, CPU/file-size rlimits.
- elsewhere/fallback: subprocess with rlimits + minimal env (no network denial
  — the run records which mode executed).

Treat documents as adversarial: the model only ever sees file *content* via
its own sandboxed code or rendered images; extracted text is never executed.

## Cloud swap points (deliberate seams)

| local v0 | AWS/GCP later |
|---|---|
| `~/.docbrain` Parquet + DuckDB catalog | S3/GCS Parquet + Iceberg/Glue/BigLake |
| `sandbox.py` subprocess/sandbox-exec | Firecracker/Docker executor, same `run_python` contract |
| `llm.py` claude-cli/anthropic | Bedrock / Vertex / first-party API behind the same class |
| CLI `ingest` loop | SQS fan-out + Step Functions/Temporal, same `ingest_file()` |
| token-overlap chunk search | embeddings/FTS/LightRAG |

## Standards & interop (adoption plan Waves 0–2, implemented)

- **Target schemas, two formats**: native minimal YAML *and* ODCS v3.1
  DataContracts in `~/.docbrain/targets/` (`docbrain targets`,
  `--export-odcs`, `--lint` via `[contracts]` extra). Units via
  `customProperties` UCUM codes.
- **Declared-contract enforcement**: Pandera schemas generated from targets,
  enforced at the canonical hop; failing rows quarantined with
  `_docbrain_meta` (Airbyte raw→typed pattern). Drift policy per target:
  `on_drift: evolve | freeze`.
- **Mapping memory with versions** (dbt model-versions semantics): approve
  supersedes, never deletes; PR-review approval flow via
  `docbrain mappings --export/--sync`.
- **Ledger projections** (the JSONL chain stays the system of record):
  `docbrain emit openlineage` → RunEvents (Marquez/DataHub/OpenMetadata/
  Purview/Dataplex-ingestible); `docbrain attest` → DSSE-signed in-toto
  Statements w/ SLSA Provenance v1 predicate (`docbrain keys-init` once,
  `docbrain attest --verify` re-checks signatures + re-hashes artifacts).
- **Golden eval gate**: `docbrain eval [--freeze]` — deterministic corpus
  re-ingest diffed against `tests/golden/`; catalog snapshots via
  `--snapshot-project`. Gate for Wave 3 (bdi-kit matching, Docling front-end).

See [docs/landscape.md](docs/landscape.md) and
[docs/adoption-plan.md](docs/adoption-plan.md) for the reasoning.

## Known v0 gaps (deliberate)

- docx/pptx, legacy .xls, shapefiles, Visum .mtx, zip archives: routed +
  recorded as unsupported, not parsed.
- Linker is pairwise heuristic + one LLM confirm pass — no entity resolution yet.
- Chunk search is token overlap, not embeddings.
- Sandbox is process-level isolation, not a microVM.
- No cross-project context yet (the catalog schema already carries `project`,
  so it's an additive step).
