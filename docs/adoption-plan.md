# Adoption plan — standards & OSS integration (from docs/landscape.md)

Eight items, sequenced into four waves by dependency and dependency-weight.
Core principle throughout: **heavy integrations are optional extras**
(`pip install docbrain[lineage|attest|matching|docling|contracts]`) — the core
pipeline stays light and offline-capable. Ledger JSONL remains the system of
record; everything interop is an *emitter* over it.

Effort key: S ≤ 1 day, M = 1–2 days. Total ≈ 8–12 dev-days.

---

## Wave 0 (gate, recommended before Waves 2–3): golden eval set — S/M

Freeze the Winterthur corpus + the synthetic demo corpus as regression
fixtures: for each file, expected table count/shapes/columns and (for a
sample) cell values; `docbrain eval` compares a fresh ingest against
fixtures. Without this, items 4 and 7 ("matcher/parser is *better*") are
unverifiable claims.

- New: `tests/golden/` fixtures + `docbrain/eval.py` + CLI `docbrain eval`.
- Acceptance: current pipeline scores 100% on its own frozen baseline; any
  regression in later waves is visible as a diff.

---

## Wave 1 — contracts & mapping registry (items 1, 5, 6, 8) 

These four touch the same code (targets.py, mappings, canonical hop) — do
them together, in this order.

### 1a. ODCS v3.1 target format — dual-format loader (item 1) — S
- `targets.py` accepts **both** formats, detected by shape:
  - **native** (current minimal YAML) — stays supported, zero migration
    pressure;
  - **ODCS v3.1** (`kind: DataContract`, `apiVersion: v3.1.0`) — normalized
    into the same internal dict.
- ODCS→internal mapping: `schema[0].properties[]` → columns;
  `logicalType` → dtype (`string→str, integer→int, number→float, date→date,
  boolean→bool`); `required`, `primaryKey`; units from
  `customProperties[key=unit]` (UCUM codes — ODCS has no native unit field);
  `quality[rule=validValues]` → allowed values; `authoritativeDefinitions` +
  `businessName` carried into the matcher/LLM prompt as semantic anchors.
- Converter both ways: `docbrain targets --export-odcs <name>` and
  `--import-odcs <file>` so teams migrate gradually.
- **datacontract-cli** as optional extra `[contracts]`:
  `docbrain targets --lint` shells out to `datacontract lint` when present;
  document the CI action for PR-time linting.
- Acceptance: `counting_stations` re-expressed as ODCS lints clean, loads
  identically, existing approved mapping keeps working (mappings reference
  target *name*, unaffected).

### 1b. Pandera enforcement from contracts (item 5) — S/M
- New `docbrain/enforcement.py`: build `pandera.DataFrameSchema` from the
  internal target dict (dtype coercion, required→column presence,
  `nullable` as a new per-column field, validValues→`isin`, min/max from
  `logicalTypeOptions`).
- Applied at the canonical hop (`map_table_if_remembered`) — this **replaces
  hand-rolled checks only for declared target contracts**. The learned
  contracts in `schema_memory` (drift on *source-shaped* tables) stay — they
  answer a different question (has the vendor's file changed?) than Pandera
  (does the canonical output meet our declared contract?).
- Pandera becomes a core dependency (moderate weight, acceptable).

### 1c. Policy + versioning + quarantine (item 6) — M
- **Drift policy (dlt contract modes)**: per-target `on_drift:` block —
  `{tables|columns|data_type: evolve | freeze | discard_row | discard_value}`.
  Wired where drift is already detected: `freeze` → no canonical produced,
  table to review queue; `evolve` → proceed + `schema-change` ledger entry;
  `discard_*` → row/value quarantine (below).
- **Mapping versioning (dbt model-versions semantics)**: `mappings` gains
  `version` (int, increments per fingerprint+target), `superseded_by`,
  `deprecation_date`; approving a new version marks the old `superseded`,
  never deletes; `canonical_tables` already records `mapping_id` → provenance
  says exactly which mapping version produced every canonical row.
  CLI: `docbrain mappings --history <fingerprint>`.
- **Row-level quarantine (Airbyte raw→typed pattern)**: `apply_mapping`
  returns (passing_df, quarantined_df + `_docbrain_meta` error column);
  quarantine parquet lands in `canonical/<target>/_quarantine/`, counts go
  into the `map` ledger entry, non-empty quarantine surfaces in the review
  queue. A bad row degrades the row, never fails the file.

### 1d. Approval UX v1 — mappings-as-code (item 8, option A) — S
- `docbrain mappings --export <dir>`: each proposal as a reviewable YAML file
  (mapping + rationale + samples + top-k alternatives); **PR review = the
  approval**; `docbrain mappings --sync <dir>` imports decisions back
  (file merged/edited → approved at its content; deleted → rejected).
- datacontract-cli GitHub Action lints `targets/` in the same PR.
- **Argilla (option B) explicitly deferred**, documented as the upgrade path
  when non-git reviewers (domain experts) need in: proposals pushed as
  records-with-suggestions, accept/edit in Argilla UI, poller syncs statuses.
  No bespoke UI in either option.

---

## Wave 2 — interop emitters (items 2, 3); independent of Wave 1, parallelizable

Shared skeleton first: `docbrain/emitters/` + CLI `docbrain emit
{openlineage|attest} [--since TS] [--out DIR]`. Emitters **replay the ledger
JSONL** (batch, idempotent — run id derived from `entry_hash`), never block
ingest; live-push is a config flag later.

### 2. OpenLineage RunEvents (item 2) — M
- `emitters/openlineage.py`: ledger entry → COMPLETE RunEvent.
  - job: `docbrain.<kind>` in namespace `docbrain://<project>`; script runs
    carry `sourceCode`-family facets (script path + code sha; git URL when
    the registry gains it).
  - inputs/outputs: datasets named by file/table; **`datasetVersion` facet =
    sha256** (the accepted slot — no standard checksum facet exists).
  - our chain rides a custom `docbrain_provenance` facet
    (`entry_hash`, `prev_hash`, input digests) with an immutable
    `_schemaURL` in the repo, per spec rules for custom facets.
- Sinks: JSONL export dir (always) + HTTP transport (`DOCBRAIN_OL_URL`,
  optional extra `[lineage]` = openlineage-python client) → Marquez/DataHub/
  OpenMetadata/Purview/Dataplex all ingest this one format.
- Acceptance: exported events validate against the published OL JSON schema;
  optional local demo: docker-compose Marquez showing the winterthur graph.

### 3. in-toto Statements + SLSA Provenance v1, DSSE-signed (item 3) — M
- `emitters/attestation.py`: per output artifact →
  `Statement{subject: [{name, digest:{sha256}}], predicateType: slsa v1}`;
  `resolvedDependencies` = input files + script (uri + code sha);
  `builder.id` = docbrain version; byproducts = stdout sha.
- DSSE signing with a local ed25519 key: `docbrain keys init`
  (`~/.docbrain/keys/`), PAE encoding via the `cryptography` lib
  (extra `[attest]`); Sigstore/Rekor and SCITT registration documented as
  follow-ons, not built.
- CLI: `docbrain attest <project> [--table NAME]` → `.intoto.jsonl` bundle;
  `docbrain attest --verify <bundle>` re-hashes artifacts on disk and checks
  signatures — the customer-facing audit deliverable ("verify our work with
  standard tooling"; Archivista store / GUAC graph consume these as-is).
- Acceptance: round-trip verify passes; single-byte tamper of any artifact or
  bundle fails loudly.

---

## Wave 3 — quality upgrades (items 4, 7); gated on Wave 0 eval

### 4. bdi-kit under propose-mappings + linker (item 4) — M
- Optional extra `[matching]` (bdi-kit pulls torch/transformers — never in
  core); lazy import, graceful fallback to current heuristics.
- `propose-mappings`: cheap matchers first (Valentine coma/simflood), Magneto
  when installed; **top-k candidates per target column** feed the LLM
  proposal as evidence — or skip the LLM entirely when matcher confidence is
  high and all required columns are covered. Proposals store per-column
  alternatives so the approval file shows choices, not one take-it-or-leave-it
  guess.
- Linker: valentine scores replace bare `difflib` name similarity (pair count
  capped, scores cached).
- Acceptance: on the winterthur eval, mapping proposals ≥ current quality
  with alternatives present; measurable LLM-call reduction.

### 7. Docling as optional PDF front-end (item 7) — M
- Optional extra `[docling]`; config `DOCBRAIN_PDF_ENGINE=auto|pymupdf|docling`.
- `auto` escalation triggers (per page/doc): find_tables()==0 on a table-dense
  vector page, pdf-table confidence below threshold, or scanned/mixed pages
  (Docling brings OCR + TableFormer for borderless/dense tables).
- DoclingDocument tables → TableCandidates (`method="pdf-docling"`,
  prior 0.85); provenance records engine per table.
- Acceptance: Kennzahlen annex + the review-queue p13 table re-ingested with
  docling ≥ baseline on the eval; no regression on native-text docs.

---

## Dependency graph

```
Wave 0 (eval) ──────────────┐
1a ODCS dual ─→ 1b Pandera ─→ 1c policy/versioning/quarantine ─→ 1d approvals
                                   (independent)
2 OpenLineage ──┐  shared emitters/ skeleton
3 in-toto/DSSE ─┘  (parallel with Wave 1)
Wave 0 + 1 ─→ 4 bdi-kit, 7 Docling (each independent)
```

## Decisions locked by this plan
- Dual target-format support (native + ODCS) — native never breaks.
- Ledger JSONL stays the system of record; OL + in-toto are projections.
- Heavy deps only as extras; core install stays lean.
- Approval = git PR first; Argilla later; no bespoke UI.
- Learned drift contracts and declared Pandera contracts coexist (different
  questions).
