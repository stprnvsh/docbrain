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
    from .linker import link_project

    store = Store()
    llm = _llm(no_llm)
    sandbox = Sandbox()
    console.print(f"[dim]llm backend: {llm.backend if llm else 'none'} | "
                  f"sandbox: {sandbox.mode}[/dim]")

    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(q for q in p.rglob("*") if q.is_file() and not q.name.startswith(".")))
        else:
            files.append(p)

    for f in files:
        rep = ingest_file(store, project, f, llm=llm, sandbox=sandbox, force=force)
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
    store = Store()
    llm = LLM()
    console.print(f"[dim]llm backend: {llm.backend}[/dim]")
    answer = _ask(store, project, question, llm)
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
def home():
    """Print the data directory."""
    console.print(str(PATHS.home))


if __name__ == "__main__":
    app()
