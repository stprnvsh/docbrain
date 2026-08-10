# docbrain

**Turn a folder of messy customer files into clean, queryable, provenance-tracked data — and a knowledge base you can ask questions.**

You point it at a directory (Excel workbooks with merged headers, CSVs in weird encodings, PDFs, proprietary machine logs, zips full of exports). It figures out what each file is, extracts every table into typed Parquet, remembers the formats it has seen so the next delivery is cheaper, normalizes data into *your* canonical schemas, records cryptographic proof of everything it did — and builds a per-project context pack you can query in natural language.

Everything runs locally. Your files never leave your machine except for the LLM calls you configure.

```
docbrain ingest myproject ./customer-delivery/
docbrain ask myproject "which counting stations appear in both the 2022 and 2023 campaigns?"
```

---

## The problem

Enterprises receive hundreds of heterogeneous files per project — every vendor exports differently, every year the format drifts, half the payload hides inside zips and PDFs. Getting that into internal formats is manual, repetitive work. And when an AI agent does it for you, you get a new problem: *"it says it used all the files — how would I know?"*

docbrain answers both:

1. **Extraction that learns.** Agents don't just parse — they *author reusable parser scripts*, remember schemas, and remember approved mappings. The second file of a format family costs zero LLM calls.
2. **Proof, not trust.** Every action is recorded by the harness (not self-reported by the agent) in a tamper-evident ledger: which file bytes went in, which exact code ran, which bytes came out.

## How it works

```
file ──► router ──► specialist track ──► agent refinement ──► agent validation ──► catalog
          what        xlsx | csv | pdf     (sandboxed, only     (verify-by-re-      DuckDB +
          is it?      txt | office | zip    when flagged)        execution) ──►      Parquet
                                                                 confidence gate
                                                                 ──► review queue
                └────────── every step recorded in the provenance ledger ──────────┘

project ──► schema memory ──► cross-file links ──► canonical mapping ──► context.md ──► ask
```

**Extractions are verified, not assumed.** After every extraction, the agent
gets the *original file* and the *extracted table* mounted together in the
sandbox and writes independent cross-checking code — re-reads the claimed
region, compares row counts, sample values, and column sums — emitting a
machine-checked verdict. A failed verdict triggers one re-extraction with the
discrepancies fed back; still failing → review queue with the evidence
attached. Passing tables carry "agent-verified (7/7 checks)" in their notes.

**Specialist tracks** (deterministic first, models only where they earn it):

| Format | How it's handled |
|---|---|
| **xlsx** | Multi-table "island" detection per sheet, merged multi-row headers combined |
| **csv** | Encoding sniff (cp1252-aware), dialect detection, ragged-row repair, stacked-table splitting |
| **pdf** | Per-page classify (text/scanned/vector) → native extraction for text, vision for scans/drawings; optional [Docling](https://github.com/docling-project/docling) escalation for hard layouts |
| **txt** | Triage: delimited → csv machinery; **proprietary records → an agent writes a parser**; prose → searchable chunks |
| **docx/pptx/odt/…** | [firecrawl-anydoc](https://github.com/firecrawl/anydoc): deterministic, no ML, milliseconds |
| **zip** | A container, not a format: entries extracted and routed recursively, provenance keeps `archive::entry` paths |
| **unknown** | Sandbox exploration: the agent probes the file with code and either produces tables or explains what it is |

**The three memory systems** — what makes run *N+1* cheaper and better than run *N*:

- **Script registry** — when the agent writes a parser for an unknown format (say, a VR-Netlog signal-controller log), the script is saved, keyed by a format signature. The next similar file replays it with **zero LLM calls**. Win/fail counts tracked; failures trigger adaptation, not re-derivation.
- **Schema memory** — every table's schema is fingerprinted. Recurring schemas are recognized instantly, and learned per-column contracts (types, ranges, null rates) raise **drift flags** when a vendor's numbers move outside remembered bounds.
- **Mapping memory** — declare canonical target schemas (plain YAML or [ODCS v3.1](https://bitol-io.github.io/open-data-contract-standard/v3.1.0/home/) data contracts). The LLM proposes column mappings, **a human approves once**, and every future file with that fingerprint lands in your canonical format automatically. Approvals can run through git PRs (`docbrain mappings --export/--sync`).

**The provenance ledger** — a hash-chained, append-only log written by the harness at the chokepoints agents cannot bypass (the sandbox, ingest, mapping, SQL):

- every input file by sha256, every executed script by sha256, every output by sha256
- `docbrain verify` re-hashes everything and walks the chain — tampering breaks it loudly
- answers from `ask` carry a ledger-verified evidence line: which tables the SQL *actually* touched
- exports to standards: `docbrain emit openlineage` (DataHub / Marquez / Purview / Dataplex) and `docbrain attest` (DSSE-signed [in-toto/SLSA](https://slsa.dev) attestations — verify our work with standard tooling, don't trust us)

## Quick start

```bash
git clone https://github.com/stprnvsh/docbrain && cd docbrain
uv venv && uv pip install -e .

# optional extras (each pulls heavier deps):
#   .[docling]   hard-layout PDF tables    .[matching]  Valentine/Magneto schema matching
#   .[attest]    signed attestations       .[contracts] ODCS contract linting
#   .[lineage]   OpenLineage HTTP push

# demo on a generated messy corpus
.venv/bin/python scripts/make_samples.py
docbrain ingest demo samples/
docbrain tables demo
docbrain context demo
docbrain ask demo "total revenue by region across all files?"
```

**LLM backend** is auto-detected: `ANTHROPIC_API_KEY` if set, else your local `claude` CLI (Claude Code subscription auth — zero setup), else heuristics-only mode where every ambiguity routes to the review queue instead of a model.

## The commands

| Command | What it does |
|---|---|
| `docbrain ingest <project> <paths…>` | The pipeline. `--force` re-does, `--no-llm` heuristics only |
| `docbrain status / tables <project>` | What's in the catalog |
| `docbrain context <project>` | Build the context pack (`context.md` + `.json`) |
| `docbrain ask <project> "…"` | Natural-language Q&A with SQL evidence loop + verified citations |
| `docbrain review <project>` | The human-review queue (everything below the confidence gate) |
| `docbrain provenance <project> [name]` | Full lineage of any table or file: source hash → code hash → output hash |
| `docbrain verify [project]` | Re-hash everything, verify the ledger chain |
| `docbrain scripts` | The curated parser registry (wins/fails per format) |
| `docbrain targets / mappings / approve / canonical` | Canonical-schema workflow |
| `docbrain emit openlineage / attest` | Standards-based exports of the ledger |
| `docbrain eval [--freeze]` | Golden-corpus regression gate |

## Where the data lives

Everything under `~/.docbrain/` (override with `DOCBRAIN_HOME`): `catalog.duckdb` (all metadata — plain DuckDB, query it with anything), `projects/<name>/tables/*.parquet` (extracted tables), `projects/<name>/canonical/` (target-schema outputs), `projects/<name>/context.md` (the knowledge pack), `ledger.jsonl` (the provenance chain), `scripts/` (curated parsers), `targets/` (your schema contracts), `exports/` (OpenLineage + attestations).

## Configuration

| Env var | Default | Options |
|---|---|---|
| `DOCBRAIN_HOME` | `~/.docbrain` | data directory |
| `DOCBRAIN_LLM` | `auto` | `anthropic` \| `claude-cli` \| `none` |
| `DOCBRAIN_MODEL` | backend default | any Claude model id |
| `DOCBRAIN_AGENT` | `auto` | `auto` (only flagged tables) \| `always` \| `never` |
| `DOCBRAIN_VALIDATE` | `always` | `always` (verify every table) \| `auto` (per schema family) \| `never` |
| `DOCBRAIN_PDF_ENGINE` | `pymupdf` | `pymupdf` \| `docling` \| `auto` (escalate on triggers) |
| `DOCBRAIN_PDF_CLASSIFIER` | `heuristic` | `heuristic` \| `inspector` (pdf-inspector) |
| `DOCBRAIN_REVIEW_THRESHOLD` | `0.80` | confidence below this → review queue |

## Security posture

Agent-written code only ever runs in a sandbox (macOS `sandbox-exec`: network **denied**, writes confined to the job dir, inputs mounted read-only, CPU/file-size limits — both properties are probed at startup, not assumed). Documents are treated as adversarial: extracted text is never executed, and the sandbox has no credentials, no catalog access, no host paths. Every execution is ledger-recorded whether it succeeds or not.

## How this compares to other tools

Short version: the big open-source parsers (Docling, MinerU, Unstructured, RAGFlow…) turn documents into markdown for RAG — docbrain *consumes* the best of them as front-ends and owns the layer none of them ship: **typed tables + curated script reuse + schema/mapping memory + a tamper-evident ledger**. Full research with sources: [docs/landscape.md](docs/landscape.md). Standards-adoption plan and status: [docs/adoption-plan.md](docs/adoption-plan.md).

## Deliberate gaps / roadmap

Legacy `.xls`, shapefiles, and PTV Visum `.mtx` matrices are catalogued but not parsed yet. Sub-file read granularity (byte-range provenance via a FUSE/range-server chokepoint), embeddings-based search, Docker/Firecracker sandbox backends, a service/API deployment, and cross-project context are the next chapters — the seams for all of them are already in place.
