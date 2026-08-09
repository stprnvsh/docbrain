"""in-toto Statements with the SLSA Provenance v1 predicate, DSSE-signed.

Turns "trust our ledger" into "verify with standard tooling": every ledger
entry that produced artifacts becomes an attestation whose subjects are the
output digests and whose resolvedDependencies are the input files + the exact
code (by sha256) that ran. Envelopes are DSSE (ed25519). Archivista can store
these as-is; GUAC can graph them; Sigstore/Rekor registration is a follow-on.

Requires the [attest] extra (cryptography).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from ..config import PATHS

STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
PAYLOAD_TYPE = "application/vnd.in-toto+json"
BUILDER_ID = "urn:docbrain:v0.1"


def keys_dir() -> Path:
    d = PATHS.home / "keys"
    d.mkdir(parents=True, exist_ok=True)
    return d


def init_keys(force: bool = False) -> Path:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv_path = keys_dir() / "docbrain-ed25519.pem"
    pub_path = keys_dir() / "docbrain-ed25519.pub"
    if priv_path.exists() and not force:
        return priv_path
    key = Ed25519PrivateKey.generate()
    priv_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    priv_path.chmod(0o600)
    pub_path.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return priv_path


def _load_private():
    from cryptography.hazmat.primitives import serialization
    p = keys_dir() / "docbrain-ed25519.pem"
    if not p.exists():
        raise FileNotFoundError("no signing key — run `docbrain keys-init` first")
    return serialization.load_pem_private_key(p.read_bytes(), password=None)


def _load_public():
    from cryptography.hazmat.primitives import serialization
    p = keys_dir() / "docbrain-ed25519.pub"
    return serialization.load_pem_public_key(p.read_bytes())


def _pae(payload_type: str, payload: bytes) -> bytes:
    pt = payload_type.encode()
    return b"DSSEv1 %d %s %d %s" % (len(pt), pt, len(payload), payload)


def statement_from_entry(entry: dict) -> dict | None:
    outputs = entry.get("outputs") or []
    subjects = [{"name": o["name"], "digest": {"sha256": o["sha256"]}}
                for o in outputs if o.get("sha256")]
    if not subjects:
        return None
    deps = [{"name": i.get("name", "?"), "digest": {"sha256": i["sha256"]}}
            for i in (entry.get("inputs") or []) if i.get("sha256")]
    if entry.get("code_sha"):
        deps.append({"name": "docbrain:extraction-code",
                     "digest": {"sha256": entry["code_sha"]}})
    return {
        "_type": STATEMENT_TYPE,
        "subject": subjects,
        "predicateType": PREDICATE_TYPE,
        "predicate": {
            "buildDefinition": {
                "buildType": f"urn:docbrain:buildtype:{entry.get('kind', 'run')}@v1",
                "externalParameters": {
                    "project": entry.get("project"),
                    "docId": entry.get("doc_id"),
                    "detail": entry.get("detail", {}),
                },
                "resolvedDependencies": deps,
            },
            "runDetails": {
                "builder": {"id": BUILDER_ID},
                "metadata": {
                    "invocationId": entry["entry_hash"],
                    "startedOn": entry["ts"],
                },
                "byproducts": [{"name": "ledger:prevHash",
                                "digest": {"sha256": entry.get("prev_hash", "")}}],
            },
        },
    }


def sign_statement(statement: dict) -> dict:
    key = _load_private()
    payload = json.dumps(statement, sort_keys=True, separators=(",", ":"),
                         default=str).encode()
    sig = key.sign(_pae(PAYLOAD_TYPE, payload))
    return {
        "payload": base64.standard_b64encode(payload).decode(),
        "payloadType": PAYLOAD_TYPE,
        "signatures": [{"keyid": "docbrain-ed25519", "sig": base64.standard_b64encode(sig).decode()}],
    }


def attest_ledger(ledger_path: Path, out_dir: Path, project: str | None = None,
                  name_filter: str | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = out_dir / "docbrain-attestations.intoto.jsonl"
    n = 0
    with open(ledger_path) as f, open(bundle, "w") as out:
        for line in f:
            if not line.strip():
                continue
            entry = json.loads(line)
            if project and entry.get("project") != project:
                continue
            stmt = statement_from_entry(entry)
            if stmt is None:
                continue
            if name_filter and not any(name_filter.lower() in s["name"].lower()
                                       for s in stmt["subject"]):
                continue
            out.write(json.dumps(sign_statement(stmt)) + "\n")
            n += 1
    return {"attestations": n, "bundle": str(bundle)}


def verify_bundle(bundle: Path, search_dirs: list[Path]) -> dict:
    """Verify DSSE signatures and re-hash any subject artifacts found on disk."""
    from cryptography.exceptions import InvalidSignature
    from ..ledger import sha256_file
    pub = _load_public()
    index: dict[str, Path] = {}
    all_paths: list[Path] = []
    for d in search_dirs:
        for p in Path(d).rglob("*.parquet"):
            index[p.name] = p
            all_paths.append(p)

    def find(name: str) -> Path | None:
        # exact filename, else catalog naming convention <table_name>__<id>.parquet
        if name in index:
            return index[name]
        hits = [p for p in all_paths if p.name.startswith(f"{name}__")]
        return hits[0] if len(hits) == 1 else None
    sig_ok = sig_bad = art_ok = art_bad = art_missing = 0
    problems: list[str] = []
    with open(bundle) as f:
        for line in f:
            if not line.strip():
                continue
            env = json.loads(line)
            payload = base64.standard_b64decode(env["payload"])
            try:
                pub.verify(base64.standard_b64decode(env["signatures"][0]["sig"]),
                           _pae(env["payloadType"], payload))
                sig_ok += 1
            except InvalidSignature:
                sig_bad += 1
                problems.append("signature invalid")
                continue
            stmt = json.loads(payload)
            for s in stmt.get("subject", []):
                p = find(s["name"])
                if p is None:
                    art_missing += 1
                elif sha256_file(p) == s["digest"]["sha256"]:
                    art_ok += 1
                else:
                    art_bad += 1
                    problems.append(f"digest mismatch: {s['name']}")
    return {"signatures_ok": sig_ok, "signatures_bad": sig_bad,
            "artifacts_verified": art_ok, "artifacts_mismatched": art_bad,
            "artifacts_not_found": art_missing, "problems": problems[:10]}
