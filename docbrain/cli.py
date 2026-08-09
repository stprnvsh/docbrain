"""docbrain CLI."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table as RichTable

from .config import PATHS, REVIEW_THRESHOLD
from .llm import LLM
from .sandbox import Sandbox
from .store import Store

app = typer.Typer(help="Local-first multi-format document understanding + project context brain.",
                  no_args_is_help=True, pretty_exceptions_show_locals=False)
console = Console()


def _llm(no_llm: bool) -> LLM | None:
    if no_llm:
        return None
    llm = LLM()
    return llm if llm.available else None


@app.command()
def ingest(project: str, paths: list[Path], force: bool = typer.Option(False, "--force"),
           no_llm: bool = typer.Option(False, "--no-llm", help="heuristics only"),
           link: bool = typer.Option(True, help="run cross-file linking after ingest")):
    """Ingest files into a project: classify -> extract -> refine -> validate -> store."""
    from .ingest import ingest_file
    from .ledger import Ledger
    from .linker import link_project

    store = Store()
    llm = _llm(no_llm)
    ledger = Ledger(store=store)
    sandbox = Sandbox(ledger=ledger)
    console.print(f"[dim]llm backend: {llm.backend if llm else 'none'} | "
                  f"sandbox: {sandbox.mode} | ledger: {ledger.path.name}[/dim]")

    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(q for q in p.rglob("*") if q.is_file() and not q.name.startswith(".")))
        else:
            files.append(p)

    for f in files:
        rep = ingest_file(store, project, f, llm=llm, sandbox=sandbox, force=force,
                          ledger=ledger)
        tag = {"ingested": "green", "partial": "yellow", "failed": "red",
               "unsupported": "red"}.get(rep.status, "white")
        skipmark = " [dim](skipped, already ingested)[/dim]" if rep.skipped else ""
        console.print(f"[bold]{f.name}[/bold] [{tag}]{rep.status}[/{tag}] "
                      f"({rep.filetype}){skipmark}")
        for t in rep.tables:
            mark = "[yellow]⚠ review[/yellow]" if t["needs_review"] else "[green]✓[/green]"
            reuse = " [cyan]schema-memory hit[/cyan]" if t.get("schema_reused") else ""
            console.print(f"   {mark} {t['name']}  {t['shape'][0]}×{t['shape'][1]} "
                          f"({t['source']}, {t['method']}, conf {t['confidence']}){reuse}")
        for n in rep.notes:
            console.print(f"   [dim]· {n}[/dim]")

    if link:
        links = link_project(store, project, llm)
        console.print(f"[dim]cross-file linking: {len(links)} link(s)[/dim]")
    store.close()


@app.command()
def status(project: str = typer.Argument(None)):
    """Show catalog contents."""
    store = Store()
    projects = [project] if project else [r[0] for r in store.conn.execute(
        "SELECT DISTINCT project FROM documents ORDER BY 1").fetchall()]
    for proj in projects:
        docs = store.documents(proj)
        tables = store.tables(proj)
        review = sum(t["needs_review"] for t in tables)
        console.print(f"\n[bold]{proj}[/bold]: {len(docs)} docs, {len(tables)} tables, "
                      f"{review} awaiting review")
        rt = RichTable(show_header=True, header_style="dim")
        for col in ("file", "type", "status", "tables"):
            rt.add_column(col)
        for d in docs:
            n = sum(1 for t in tables if t["doc_id"] == d["doc_id"])
            rt.add_row(d["filename"], d["filetype"], d["status"], str(n))
        console.print(rt)
    store.close()


@app.command()
def tables(project: str):
    """List extracted tables with confidence."""
    store = Store()
    rt = RichTable(show_header=True, header_style="dim")
    for col in ("table", "source", "shape", "method", "conf", "review", "columns"):
        rt.add_column(col)
    for t in store.tables(project):
        rt.add_row(t["name"], t["source_ref"], f"{t['n_rows']}×{t['n_cols']}",
                   t["method"], f"{t['confidence']:.2f}",
                   "⚠" if t["needs_review"] else "",
                   ", ".join(c["name"] for c in t["schema"][:6]))
    console.print(rt)
    store.close()


@app.command()
def context(project: str, rebuild: bool = typer.Option(False, "--rebuild"),
            no_llm: bool = typer.Option(False, "--no-llm")):
    """Build (or rebuild) the project context pack: context.md + context.json."""
    from .context import build_context
    store = Store()
    if rebuild:
        for d in store.documents(project):
            meta = d["meta"]
            meta.pop("summary_cache", None)
            store.set_document_meta(d["doc_id"], meta)
    out = build_context(store, project, _llm(no_llm))
    console.print(f"context pack written to [bold]{out}[/bold] (+ .json)")
    console.print(out.read_text())
    store.close()


@app.command()
def ask(project: str, question: str):
    """Ask a question across all documents in a project (SQL + text evidence loop)."""
    from .context import ask as _ask
    from .ledger import Ledger
    store = Store()
    llm = LLM()
    console.print(f"[dim]llm backend: {llm.backend}[/dim]")
    answer = _ask(store, project, question, llm, ledger=Ledger(store=store))
    console.print(answer)
    store.close()


@app.command()
def review(project: str):
    """List tables below the confidence threshold (the human-review queue)."""
    store = Store()
    queue = [t for t in store.tables(project) if t["needs_review"]]
    if not queue:
        console.print(f"[green]review queue empty[/green] (threshold {REVIEW_THRESHOLD})")
    for t in queue:
        console.print(f"[yellow]⚠[/yellow] [bold]{t['name']}[/bold] ({t['source_ref']}) "
                      f"conf {t['confidence']:.2f}")
        for i in t["issues"]:
            console.print(f"   · {i}")
        console.print(f"   parquet: {t['parquet_path']}")
    store.close()


@app.command()
def targets(export_odcs: str = typer.Option(None, "--export-odcs",
                                            help="convert a native target to ODCS v3.1 YAML"),
            lint: bool = typer.Option(False, "--lint",
                                      help="lint ODCS targets with datacontract-cli if installed")):
    """List declared target schemas (~/.docbrain/targets/, native or ODCS v3.1)."""
    import shutil as _shutil
    import subprocess as _sp
    import yaml as _yaml
    from .targets import load_targets, targets_dir, to_odcs
    ts = load_targets()
    if export_odcs:
        t = ts.get(export_odcs)
        if not t:
            console.print(f"[red]no target named {export_odcs!r}[/red]")
            raise typer.Exit(1)
        out = targets_dir() / f"{export_odcs}.odcs.yaml"
        out.write_text(_yaml.safe_dump(to_odcs(t), sort_keys=False, allow_unicode=True))
        console.print(f"ODCS contract written to [bold]{out}[/bold]")
        console.print("[dim]note: remove the native yaml if you switch over — "
                      "both files would declare the same target name[/dim]")
        return
    if not ts:
        console.print(f"no target schemas yet — drop YAML files into {targets_dir()}")
        return
    for t in ts.values():
        req = [c["name"] for c in t["columns"] if c.get("required")]
        opt = [c["name"] for c in t["columns"] if not c.get("required")]
        console.print(f"[bold]{t['name']}[/bold] v{t.get('version', 1)} "
                      f"[dim]({t['format']}, on_drift={t.get('on_drift')})[/dim] — {t.get('description', '')}")
        console.print(f"  required: {', '.join(req) or '-'}   optional: {', '.join(opt) or '-'}")
    if lint:
        dc = _shutil.which("datacontract")
        if not dc:
            console.print("[yellow]datacontract-cli not installed[/yellow] — "
                          "`uv pip install 'docbrain[contracts]'`")
            raise typer.Exit(1)
        for t in ts.values():
            if t["format"] != "odcs":
                console.print(f"[dim]{t['name']}: native format, skipping lint[/dim]")
                continue
            r = _sp.run([dc, "lint", t["path"]], capture_output=True, text=True)
            tag = "[green]lint ok[/green]" if r.returncode == 0 else "[red]lint FAILED[/red]"
            console.print(f"{t['name']}: {tag}")
            if r.returncode != 0:
                console.print(r.stdout[-800:] + r.stderr[-400:])


@app.command()
def mappings(status: str = typer.Option(None, help="proposed|approved|rejected|superseded|needs_transform"),
             export: Path = typer.Option(None, "--export", help="write proposals as reviewable YAML files (PR-review flow)"),
             sync: Path = typer.Option(None, "--sync", help="import reviewer decisions from an export dir")):
    """List target-schema mappings — or export/sync them for git-PR approval."""
    import yaml as _yaml
    store = Store()
    if export:
        export.mkdir(parents=True, exist_ok=True)
        n = 0
        for m in store.mappings("proposed"):
            f = export / f"{m['mapping_id']}.yaml"
            f.write_text(_yaml.safe_dump({
                "mapping_id": m["mapping_id"],
                "source_schema_id": m["source_schema_id"],
                "target_schema": m["target_schema"],
                "version": m["version"],
                "confidence": m["confidence"],
                "rationale": m["rationale"],
                # reviewer edits this to approved / rejected (and may edit mapping)
                "status": "proposed",
                "mapping": m["mapping"],
            }, sort_keys=False, allow_unicode=True))
            n += 1
        console.print(f"{n} proposal(s) exported to {export} — review in a PR, "
                      f"set status: approved|rejected, then `docbrain mappings --sync {export}`")
        store.close()
        return
    if sync:
        for f in sorted(sync.glob("*.yaml")):
            doc = _yaml.safe_load(f.read_text())
            mid, decision = doc.get("mapping_id"), doc.get("status")
            if not mid or decision not in ("approved", "rejected"):
                console.print(f"[dim]{f.name}: no decision yet[/dim]")
                continue
            if decision == "approved":
                current = next((m for m in store.mappings() if m["mapping_id"] == mid), None)
                if current and doc.get("mapping") and doc["mapping"] != current["mapping"]:
                    # reviewer edited the mapping: new version, then approve it
                    from .store import short_id
                    new_id = short_id("map", current["source_schema_id"],
                                      current["target_schema"], str(current["version"] + 1))
                    store.save_mapping(new_id, current["source_schema_id"],
                                       current["target_schema"], doc["mapping"],
                                       current["confidence"], "reviewer-edited", "proposed")
                    store.approve_mapping(new_id)
                    store.set_mapping_status(mid, "superseded")
                    console.print(f"[green]✓[/green] {f.name}: reviewer edit → new version approved ({new_id})")
                else:
                    store.approve_mapping(mid)
                    console.print(f"[green]✓[/green] {f.name}: approved")
            else:
                store.set_mapping_status(mid, "rejected")
                console.print(f"[red]✗[/red] {f.name}: rejected")
        store.close()
        return
    rows = store.mappings(status)
    rt = RichTable(show_header=True, header_style="dim")
    for col in ("mapping_id", "source schema", "→ target", "v", "conf", "status", "mapping"):
        rt.add_column(col)
    for m in rows:
        spec = ", ".join(f"{k}←{v.get('source', v.get('const'))}"
                         for k, v in list(m["mapping"].items())[:5])
        status_disp = m["status"] + (f" → {m['superseded_by']}" if m.get("superseded_by") else "")
        rt.add_row(m["mapping_id"], m["source_schema_id"], m["target_schema"],
                   str(m.get("version", 1)), f"{m['confidence']:.2f}", status_disp, spec[:50])
    console.print(rt)
    store.close()


@app.command()
def approve(mapping_id: str, reject: bool = typer.Option(False, "--reject")):
    """Approve (or --reject) a proposed mapping. Approving supersedes any prior
    approved version for the same fingerprint+target (never deletes)."""
    store = Store()
    if reject:
        store.set_mapping_status(mapping_id, "rejected")
        console.print(f"mapping {mapping_id} → [bold]rejected[/bold]")
    else:
        res = store.approve_mapping(mapping_id)
        if res is None:
            console.print(f"[red]no mapping {mapping_id}[/red]")
        else:
            console.print(f"mapping {mapping_id} → [bold]approved[/bold] "
                          f"(fingerprint {res['source_schema_id']} → {res['target_schema']}; "
                          f"prior approved versions superseded)")
    store.close()


@app.command("propose-mappings")
def propose_mappings(project: str, like: str = typer.Option(None,
                     help="only tables whose name contains this")):
    """Ask the LLM to propose target-schema mappings for existing tables whose
    schema fingerprint has no mapping yet (one proposal per fingerprint)."""
    from .targets import maybe_propose
    import pandas as pd
    store = Store()
    llm = LLM()
    seen: set[str] = set()
    for t in store.tables(project):
        if like and like.lower() not in t["name"].lower():
            continue
        srow = store.conn.execute(
            "SELECT schema_id FROM table_schema_map WHERE table_id=?",
            [t["table_id"]]).fetchone()
        if not srow or srow[0] in seen:
            continue
        seen.add(srow[0])
        try:
            df = pd.read_parquet(t["parquet_path"])
        except Exception:
            continue
        prop = maybe_propose(store, llm, schema_id=srow[0],
                             source_schema=[{"name": c["name"], "dtype": c["dtype"]}
                                            for c in t["schema"]],
                             df=df, source_name=t["name"])
        if prop is None:
            console.print(f"[dim]{t['name'][:60]}: no target match (or already decided)[/dim]")
        else:
            console.print(f"[bold]{t['name'][:60]}[/bold] → [{prop['target']}] "
                          f"conf {prop['confidence']:.2f} status={prop['status']} "
                          f"id={prop['mapping_id']}")
            console.print(f"   {prop.get('rationale', '')[:100]}")
    store.close()


@app.command("apply-mappings")
def apply_mappings(project: str):
    """Retroactively apply approved mappings to already-ingested tables."""
    from .ledger import Ledger, sha256_file
    from .targets import map_table_if_remembered
    import json as _json
    store = Store()
    ledger = Ledger(store=store)
    import pandas as pd
    done = 0
    already = {c["source_table_id"] for c in store.canonicals(project)}
    for t in store.tables(project):
        if t["table_id"] in already:
            continue
        srow = store.conn.execute(
            "SELECT schema_id FROM table_schema_map WHERE table_id=?",
            [t["table_id"]]).fetchone()
        if not srow:
            continue
        try:
            df = pd.read_parquet(t["parquet_path"])
        except Exception:
            continue
        res = map_table_if_remembered(store, project=project, table_id=t["table_id"],
                                      table_name=t["name"], df=df, schema_id=srow[0],
                                      parquet_sha=sha256_file(Path(t["parquet_path"])),
                                      ledger=ledger)
        if res:
            done += 1
            console.print(f"[green]✓[/green] {t['name']} → [{res['target']}] {res['rows']} rows")
    console.print(f"{done} canonical table(s) produced")
    store.close()


@app.command()
def canonical(project: str):
    """List canonical (target-schema) tables for a project."""
    store = Store()
    rows = store.canonicals(project)
    name_of = {t["table_id"]: t["name"] for t in store.tables(project)}
    rt = RichTable(show_header=True, header_style="dim")
    for col in ("target", "rows", "source table", "parquet"):
        rt.add_column(col)
    for c in rows:
        rt.add_row(c["target_schema"], str(c["n_rows"]),
                   name_of.get(c["source_table_id"], "?")[:45],
                   c["parquet_path"][-60:])
    console.print(rt)
    store.close()


@app.command()
def provenance(project: str, name: str = typer.Argument(None,
               help="table name or filename; omit for project overview")):
    """Where data came from — lineage, runs, and usage from the ledger."""
    from .ledger import Ledger
    store = Store()
    ledger = Ledger(store=store)

    if name is None:
        chain = ledger.verify_chain()
        state = "[green]intact[/green]" if chain["ok"] else f"[red]BROKEN at {chain['first_break']}[/red]"
        console.print(f"ledger: {chain['entries']} entries, chain {state}")
        rows = store.conn.execute("""
            SELECT u.entity_kind, coalesce(t.name, d.filename, s.format_id, u.entity_id),
                   u.uses
            FROM usage u
            LEFT JOIN extracted_tables t ON t.table_id = u.entity_id
            LEFT JOIN documents d ON d.doc_id = u.entity_id
            LEFT JOIN script_registry s ON s.script_id = u.entity_id
            WHERE coalesce(t.project, d.project, ?) = ?
            ORDER BY u.uses DESC LIMIT 15
        """, [project, project]).fetchall()
        rt = RichTable(show_header=True, header_style="dim")
        for col in ("kind", "entity", "uses"):
            rt.add_column(col)
        for r in rows:
            rt.add_row(r[0], str(r[1])[:60], str(r[2]))
        console.print(rt)
        store.close()
        return

    tables = [t for t in store.tables(project) if name.lower() in t["name"].lower()]
    docs = [d for d in store.documents(project) if name.lower() in d["filename"].lower()]

    for t in tables[:3]:
        console.print(f"\n[bold]{t['name']}[/bold]  ({t['n_rows']}×{t['n_cols']}, "
                      f"method={t['method']}, conf {t['confidence']:.2f})")
        for l in store.lineage_of(t["table_id"]):
            if l["output_id"] != t["table_id"]:
                continue
            if l["relation"] == "extracted_from":
                fname = next((d["filename"] for d in store.documents(project)
                              if d["doc_id"] == l["input_id"]), l["input_id"])
                det = l["detail"]
                console.print(f"  extracted_from  [cyan]{fname}[/cyan] "
                              f"({det.get('source_ref')})")
                console.print(f"    file sha256    {det.get('file_sha', '?')[:16]}…")
                console.print(f"    parquet sha256 {det.get('parquet_sha', '?')[:16]}…")
                console.print(f"    ledger run     {l['run_hash'][:16]}…" if l["run_hash"] else "")
            elif l["relation"] == "produced_by_code":
                s = next((s for s in store.scripts() if s["script_id"] == l["input_id"]), None)
                console.print(f"  produced_by     script [magenta]{s['format_id'] if s else l['input_id']}[/magenta] "
                              f"(wins {s['success_count']}, fails {s['fail_count']})" if s else "")
        origin_cols = [c for c in t["schema"] if c.get("origin")]
        if origin_cols:
            console.print("  column origins (derived):")
            for c in origin_cols[:12]:
                console.print(f"    {c['name']} ← {c['origin']}")
        uses = store.usage_of("table", t["table_id"])
        console.print(f"  queried by ask: {uses}×")

    for d in docs[:3]:
        console.print(f"\n[bold]{d['filename']}[/bold]  ({d['filetype']}, {d['status']})")
        derived = [t for t in store.tables(project) if t["doc_id"] == d["doc_id"]]
        console.print(f"  ingested {store.usage_of('document', d['doc_id'])}×, "
                      f"{len(derived)} table(s) derived")
        runs = store.runs_for(d["doc_id"], limit=8)
        for r in runs:
            n_in = len(r["inputs"] or [])
            n_out = len(r["outputs"] or [])
            code = f" code={r['code_sha'][:12]}…" if r.get("code_sha") else ""
            console.print(f"  [dim]{str(r['ts'])[:19]}[/dim] {r['kind']:12s} "
                          f"in={n_in} out={n_out} ok={r['ok']}{code}")
    if not tables and not docs:
        console.print(f"[yellow]nothing named like {name!r} in {project}[/yellow]")
    store.close()


@app.command()
def verify(project: str = typer.Argument(None)):
    """Re-verify everything: ledger chain, source-file hashes, output hashes."""
    from .ledger import Ledger, sha256_file
    store = Store()
    ledger = Ledger(store=store)

    chain = ledger.verify_chain()
    ok = "[green]OK[/green]" if chain["ok"] else f"[red]BROKEN: {chain['first_break']}[/red]"
    console.print(f"ledger chain   : {chain['entries']} entries {ok}")

    projects = [project] if project else [r[0] for r in store.conn.execute(
        "SELECT DISTINCT project FROM documents").fetchall()]
    src_ok = src_changed = src_missing = 0
    out_ok = out_bad = 0
    for proj in projects:
        for d in store.documents(proj):
            p = Path(d["path"])
            row = store.conn.execute("SELECT content_hash FROM documents WHERE doc_id=?",
                                     [d["doc_id"]]).fetchone()
            if not p.exists():
                src_missing += 1
                console.print(f"  [yellow]source missing[/yellow] {d['filename']}")
            elif sha256_file(p) != row[0]:
                src_changed += 1
                console.print(f"  [red]source CHANGED since ingest[/red] {d['filename']}")
            else:
                src_ok += 1
        rows = store.conn.execute("""
            SELECT t.name, t.parquet_path, l.detail FROM extracted_tables t
            JOIN lineage l ON l.output_id = t.table_id AND l.relation='extracted_from'
            WHERE t.project=?""", [proj]).fetchall()
        import json as _json
        for tname, pq, detail in rows:
            want = _json.loads(detail or "{}").get("parquet_sha")
            p = Path(pq)
            if want and p.exists() and sha256_file(p) == want:
                out_ok += 1
            else:
                out_bad += 1
                console.print(f"  [red]output hash mismatch/missing[/red] {tname}")
    console.print(f"source files   : {src_ok} verified, {src_changed} changed, {src_missing} missing")
    console.print(f"output tables  : {out_ok} verified, {out_bad} mismatched "
                  f"[dim](tables from runs before the ledger existed have no recorded hash)[/dim]")
    store.close()


@app.command()
def scripts():
    """List curated agent-authored extraction scripts (global registry)."""
    store = Store()
    rows = store.scripts()
    if not rows:
        console.print("no curated scripts yet")
    rt = RichTable(show_header=True, header_style="dim")
    for col in ("format_id", "description", "wins", "fails", "script"):
        rt.add_column(col)
    for s in rows:
        rt.add_row(s["format_id"], (s["description"] or "")[:60],
                   str(s["success_count"]), str(s["fail_count"]),
                   s["script_path"])
    console.print(rt)
    store.close()


@app.command()
def home():
    """Print the data directory."""
    console.print(str(PATHS.home))


if __name__ == "__main__":
    app()
