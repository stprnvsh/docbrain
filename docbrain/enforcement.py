"""Declared-contract enforcement (Pandera), generated from target schemas.

Two contract systems coexist on purpose:
- schema_memory's learned contracts watch SOURCE-shaped tables ("did the
  vendor's file change?") — drift detection;
- this module enforces DECLARED target contracts on CANONICAL tables ("does
  our output meet the contract we published?").

Failures are handled Airbyte-style: bad rows are quarantined with a
`_docbrain_meta` error column, good rows proceed — one bad row degrades the
row, never the file.
"""

from __future__ import annotations

import json

import pandas as pd

try:  # pandera >=0.24 namespaced backend; older flat import as fallback
    import pandera.pandas as pa
except ImportError:  # pragma: no cover
    import pandera as pa

PANDERA_DTYPE = {"str": "object", "int": "Int64", "float": "float64",
                 "date": "datetime64[ns]", "bool": "boolean"}


def schema_from_target(target: dict) -> pa.DataFrameSchema:
    cols = {}
    for c in target["columns"]:
        checks = []
        if c.get("allowed"):
            checks.append(pa.Check.isin(c["allowed"]))
        cols[c["name"]] = pa.Column(
            PANDERA_DTYPE.get(c.get("dtype", "str"), "object"),
            nullable=bool(c.get("nullable", not c.get("required"))),
            required=True,          # mapping always materializes every column
            coerce=True,
            checks=checks,
        )
    return pa.DataFrameSchema(cols, strict=False, coerce=True)


def enforce(df: pd.DataFrame, target: dict) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Returns (passing_rows, quarantined_rows_with__docbrain_meta, notes)."""
    schema = schema_from_target(target)
    try:
        validated = schema.validate(df, lazy=True)
        return validated, df.iloc[0:0].copy(), []
    except pa.errors.SchemaErrors as err:
        failures = err.failure_cases
        # Row-level failures carry an index; schema-level (column/dtype) don't.
        row_fail = failures[failures["index"].notna()]
        schema_fail = failures[failures["index"].isna()]
        notes = []
        if not schema_fail.empty:
            notes.extend(f"schema: {r.column}: {r.check}"
                         for r in schema_fail.head(5).itertuples())
        bad_idx = sorted(set(int(i) for i in row_fail["index"].dropna()))
        errors_by_row: dict[int, list[str]] = {}
        for r in row_fail.itertuples():
            errors_by_row.setdefault(int(r.index), []).append(
                f"{r.column}: {r.check} (value={r.failure_case!r})")
        bad = df.loc[df.index.intersection(bad_idx)].copy()
        bad["_docbrain_meta"] = [json.dumps({"errors": errors_by_row.get(int(i), [])})
                                 for i in bad.index]
        good = df.drop(index=bad_idx, errors="ignore")
        # Re-validate the survivors so coercion still applies to them.
        try:
            good = schema.validate(good, lazy=True)
        except pa.errors.SchemaErrors:
            notes.append("column-level contract violation persists after row quarantine")
        return good, bad, notes
