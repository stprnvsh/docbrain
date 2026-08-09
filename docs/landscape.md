# OSS landscape vs docbrain (researched 2026-08-09)

Three independent research passes: document-ETL stacks, provenance/lineage
systems, schema-contract/mapping tooling. Full agent reports live in the
session; this is the load-bearing synthesis. Verdicts verified against live
repos/docs at research time.

## 1. Document-ETL: where docbrain sits

The star-mass tools — [Unstructured](https://github.com/Unstructured-IO/unstructured) (~15k★),
[Docling](https://github.com/docling-project/docling) (~64k★),
[MinerU](https://github.com/opendatalab/MinerU) (~77k★),
[RAGFlow/deepdoc](https://github.com/infiniflow/ragflow) (~87k★),
[Marker](https://github.com/datalab-to/marker) (~39k★), NVIDIA nv-ingest —
are all **docs → markdown/JSON for RAG**, not typed tables to a warehouse
schema. Tables come out as HTML-in-metadata at best.

Target-schema extraction got commoditized in 2025-26 (Docling beta extractor,
Marker ExtractionConverter, Google [LangExtract](https://github.com/google/langextract),
DocStrange, ExtractThinker, ContextGem, [Unstract](https://github.com/Zipstack/unstract) —
note AGPL) — **but every one runs LLM inference per document.** Amortizing
inference into reusable artifacts is the uncommoditized part.

**Prior art for "LLM writes the parser" — know these by name:**
- [Evaporate](https://github.com/HazyResearch/evaporate) (Stanford, VLDB'23) —
  the original LLM-synthesizes-extraction-functions; code stale.
- [Palimpzest](https://github.com/mitdbg/palimpzest) (MIT) — compiles semantic
  operators into deterministic functions, one LLM call then zero per row.
- [DocETL](https://github.com/ucbepic/docetl) (Berkeley, MIT license) — agentic
  plan optimizer replaces LLM subtasks with generated code; DocWrangler IDE.
- [TWIX](https://github.com/ucbepic/TWIX) — closest single overlap: infers a
  template per PDF family once (~$0.001), deterministic extraction after,
  claimed 734× faster / 5836× cheaper than VLMs. PDF-template-specific.

**None persist a curated, human-approved script registry keyed by format
signature with zero-LLM replay across runs and datasets.** All amortize within
a workload. docbrain's defensible claim is the **registry lifecycle**
(curation, signature keying, win/fail record, drift-triggered re-authoring) —
not "LLM writes parsers."

Also uncontested in OSS core: **schema-fingerprint memory + drift detection**
(nothing surveyed does it) and a **confidence-gated review queue** (annotation
tools exist; a review queue integrated into extraction does not).

**[anydoc + pdf-inspector](https://github.com/firecrawl/anydoc)** (Firecrawl,
MIT, Rust, ~12–13k★ each, active) deserve their own line: the *no-ML* extreme
of the parser tier — 14 formats → markdown at <5ms median, page
classification (text/scanned/mixed) in ~10–50ms without rendering. Zero
overlap with docbrain's layer (markdown tables, no schemas/provenance/reuse),
but the best-fitting consume candidate of all: pdf-inspector is the
battle-tested original of our page classifier; anydoc closes the docx/pptx/
html gap as fast text coverage without a torch install. Complementary to
Docling, not competing: anydoc = speed floor for born-digital, Docling =
accuracy ceiling for layout-hard tables.

Positioning: don't compete on parsing quality — consume anydoc/pdf-inspector
for the born-digital fast path and Docling/MinerU when native find_tables hits
its ceiling; own the typed-tables / registry / ledger / canonical-mapping
layer. Escalation ladder: pdf-inspector classify → anydoc born-digital →
PyMuPDF geometry → Docling hard-layout → vision/agent.

## 2. Provenance: claims plane vs facts plane

The landscape splits cleanly:
- **Claims plane** (catalogs): [OpenLineage](https://openlineage.io)+Marquez,
  [DataHub](https://github.com/datahub-project/datahub),
  [OpenMetadata](https://github.com/open-metadata/OpenMetadata), Egeria —
  JSON metadata, job/dataset/column granularity, huge enterprise reach,
  **no hashes, no tamper evidence**.
- **Facts plane**: [in-toto attestations](https://github.com/in-toto/attestation)
  + [SLSA Provenance v1](https://slsa.dev) (CNCF-graduated; mandatory sha256
  digests, DSSE signatures), IETF [SCITT](https://datatracker.ietf.org/doc/draft-ietf-scitt-architecture/)
  (standardizing append-only transparency ledgers — exactly our chain),
  [Kamu/ODF](https://docs.kamu.dev/odf/) (hash-chained dataset ledgers,
  BUSL-licensed), [AuditWeave](https://arxiv.org/abs/2607.09682) (arXiv 2026 —
  the only direct doc-pipeline analog, paper-stage).
- Content-addressed storage cousins: lakeFS (Merkle, unsigned), DVC (code→data
  binding via git), Pachyderm, Dolt, Quilt (per-object sha256 manifests).

**Nothing bridges both planes. Recommendation adopted as roadmap:** emit TWO
serializations from the same ledger entries —
1. **OpenLineage RunEvents** (job = script w/ SourceCodeLocationJobFacet;
   output version = parquet sha256; custom `docbrain_provenance` facet for the
   chain) → free ingestion into DataHub/OpenMetadata/Purview/Dataplex.
2. **in-toto Statements w/ SLSA Provenance v1 predicate, DSSE-signed**
   (outputs → subject digests; input/code sha256s → resolvedDependencies) →
   standard signing, Rekor/SCITT transparency, [Archivista](https://github.com/in-toto/archivista)
   as attestation store, [GUAC](https://github.com/guacsec/guac) as a free graph.

Keep the hash-chained JSONL as the local system of record (ordering +
completeness that per-attestation signatures don't give).

## 3. Target schemas / mapping: adopt, don't invent

- **[ODCS v3.1](https://bitol-io.github.io/open-data-contract-standard/v3.1.0/home/)**
  (Bitol/LF, Apache-2.0) won the format war — the competing Data Contract
  Specification is deprecated in its favor (EOL 2026). Migrate our target YAML
  to ODCS: `logicalType`, `required/primaryKey`, `quality` rules,
  `authoritativeDefinitions` (semantic anchors the matcher prompts on),
  `transformSourceObjects/transformLogic` (per-column lineage hooks). Units
  have NO native field — use `customProperties` with UCUM codes.
- **[datacontract-cli](https://github.com/datacontract/datacontract-cli)**
  (MIT): lint/test contracts + export to dbt/JSON Schema/SQL/HTML.
- **[bdi-kit](https://github.com/VIDA-NYU/bdi-kit)** (Apache-2.0): one dep
  wraps Valentine matchers + Magneto (VLDB'25) + LLM matchers, with
  `rank_schema_matches` (top-k for human review) → `materialize_mapping` —
  literally our propose→approve→apply loop, as a library. Adopt as the matcher
  layer under `propose-mappings` (heuristics + SLM before the LLM call).
- **[Pandera](https://pandera.readthedocs.io/)** (MIT): generate runtime
  enforcement schemas from the ODCS contracts.
- **Patterns to copy**: dlt contract modes (`evolve/freeze/discard_row`) as
  drift *policy*; dbt model-versions semantics (`version`, `deprecation_date`)
  for mapping versioning; Airbyte's raw→typed hop with row-level error
  quarantine (`_airbyte_meta`); [whyqd](https://whyqd.readthedocs.io/)
  crosswalks (auditable replayable transforms — closest prior art to our
  mapping artifacts); dbt_utils.unpivot for the `needs_transform` melt case.
- **Approval loop options**: Argilla (suggestions→accept/edit), Airflow ≥3.1
  HITL operators / Prefect `wait_for_input` when orchestrated, or
  mappings-as-code with PR review + datacontract-cli CI.
- **Skip**: Soda Core v4 (ELv2 relicense + churn), Great Expectations (heavy;
  GX Cloud sold to FICO), ReMatch/KcMF (no code), Schemora code (no license),
  Frictionless (subset of ODCS).
- **Necessarily ours**: unit-conversion rules; format-fingerprint →
  mapping-memory lookup. No surveyed tool ships either.

## Priority adopt list

1. ODCS v3.1 target-schema format + datacontract-cli (small migration of
   targets/*.yaml).
2. OpenLineage emitter from the ledger (catalog interop).
3. in-toto/SLSA attestation export + DSSE signing (the audit product).
4. bdi-kit under propose-mappings and the cross-file linker.
5. Pandera enforcement generated from contracts.
6. dlt-style drift policy + dbt-style mapping versioning in the registry.
7. Docling as optional PDF layout/TableFormer front-end for the hard tail.
8. Argilla (or PR-review flow) for approval UX.
