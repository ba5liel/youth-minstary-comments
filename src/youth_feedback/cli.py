from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import db
from .config import questions, settings

app = typer.Typer(help="Digitise handwritten youth feedback sheets.", no_args_is_help=True)
console = Console()


@app.command()
def ingest(folder: Optional[Path] = typer.Argument(None, help="Folder of photos (default: IMAGES_DIR)")):
    """Register photos in the database (duplicates are ignored)."""
    from .pipeline import ingest as _ingest

    folder = folder or settings.images_dir
    if not folder.is_dir():
        raise typer.BadParameter(f"{folder} is not a folder")
    added, seen = _ingest(db.connect(), folder)
    console.print(f"Found {seen} image(s), {added} new.")


@app.command()
def extract(
    limit: Optional[int] = typer.Option(None, help="Only process this many sheets (try 10-20 first)"),
    retry_failed: bool = typer.Option(False, help="Also retry sheets that errored before"),
):
    """Read pending sheets with Gemini and triage them into auto_ok / needs_review."""
    from .pipeline import extract as _extract

    questions()
    tally = asyncio.run(_extract(db.connect(), limit, retry_failed))
    console.print(tally or "Nothing pending.")
    status()


@app.command()
def run(folder: Optional[Path] = typer.Argument(None), limit: Optional[int] = typer.Option(None)):
    """ingest + extract in one go."""
    ingest(folder)
    extract(limit=limit, retry_failed=False)


@app.command()
def status():
    """Show progress and review stats."""
    from .export import spot_check_accuracy

    conn = db.connect()
    c = db.counts(conn)
    t = Table("status", "sheets")
    for k in ("pending", "auto_ok", "needs_review", "reviewed", "failed", "not_a_sheet"):
        t.add_row(k, str(c.get(k, 0)))
    t.add_row("[bold]total", f"[bold]{sum(c.values())}")
    console.print(t)

    a = conn.execute(
        "SELECT status, COUNT(*) n FROM answers GROUP BY status"
    ).fetchall()
    if a:
        console.print("Answers: " + ", ".join(f"{r['status']}={r['n']}" for r in a))
    checked, same = spot_check_accuracy(conn)
    if checked:
        console.print(f"Spot-check: reviewers left {same}/{checked} automatic answers unchanged "
                      f"({same / checked:.0%} estimated auto accuracy).")
    if c.get("failed"):
        err = conn.execute("SELECT id, error FROM sheets WHERE status='failed' LIMIT 3").fetchall()
        for r in err:
            console.print(f"[red]failed #{r['id']}: {r['error'][:200]}")


@app.command()
def requeue(sheet_ids: list[int] = typer.Argument(..., help="Sheet ids to send back to manual review")):
    """Send sheets (e.g. an auto_ok one you found an error in) back to the review queue."""
    conn = db.connect()
    with db.tx(conn):
        for sid in sheet_ids:
            conn.execute(
                """UPDATE answers SET status='needs_review', final_text=NULL, final_script=NULL,
                   reviewer_raw=NULL, reviewed_by=NULL, reviewed_at=NULL WHERE sheet_id=?""", (sid,))
            conn.execute(
                "UPDATE sheets SET status='needs_review', claimed_by=NULL, claimed_at=NULL, updated_at=? WHERE id=?",
                (db.now(), sid))
    console.print(f"Requeued {len(sheet_ids)} sheet(s).")


@app.command()
def transliterate():
    """Convert reviewer answers typed in Latin letters into Ge'ez script."""
    from .export import transliterate_reviewed

    n = asyncio.run(transliterate_reviewed(db.connect(), settings.concurrency))
    console.print(f"Transliterated {n} answer(s).")


@app.command()
def export(
    out: Path = typer.Option(Path("exports"), help="Output folder"),
    include_unreviewed: bool = typer.Option(False, help="Also include AI drafts still waiting for review"),
):
    """Write CSV (wide + long) and JSONL files for analysis."""
    from .export import export as _export

    for kind, path in _export(db.connect(), out, include_unreviewed).items():
        console.print(f"{kind}: {path}")


@app.command()
def bot():
    """Run the Telegram review bot."""
    from .bot import run as _run

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _run()
