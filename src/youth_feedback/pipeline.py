"""Ingest photos and run them through OCR + triage. Safe to re-run: finished sheets are skipped."""

from __future__ import annotations

import asyncio
import random
import sqlite3
from pathlib import Path

from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn

from . import db
from .config import ROOT, settings
from .images import find_images, rotations, sha256, to_jpeg
from .ocr import SheetRead, detect_rotation, read_sheet
from .triage import judge, script_of


def ingest(conn: sqlite3.Connection, folder: Path) -> tuple[int, int]:
    """Register every image in `folder`. Duplicate photos (same bytes) are stored once."""
    added = seen = 0
    for path in find_images(folder):
        seen += 1
        rel = str(path.resolve().relative_to(ROOT.resolve())) if path.resolve().is_relative_to(ROOT.resolve()) else str(path.resolve())
        added += db.add_sheet(conn, rel, sha256(path))
    return added, seen


def sheet_path(row: sqlite3.Row) -> Path:
    p = Path(row["path"])
    return p if p.is_absolute() else ROOT / p


def _store(conn: sqlite3.Connection, sheet_id: int, primary: SheetRead, raw_p: str,
           verify: SheetRead | None, raw_v: str | None) -> str:
    if not primary.is_response_sheet:
        with db.tx(conn):
            conn.execute(
                """UPDATE sheets SET status='other_format', legibility=?, unmapped_text=?, raw_primary=?,
                                     raw_verify=?, updated_at=? WHERE id=?""",
                (primary.legibility, primary.unmapped_text, raw_p, raw_v, db.now(), sheet_id),
            )
        return "other_format"

    verdict = judge(primary, verify)
    spot = not verdict.needs_review and random.random() < settings.spot_check_rate
    status = "needs_review" if verdict.needs_review or spot else "auto_ok"

    with db.tx(conn):
        conn.execute("DELETE FROM answers WHERE sheet_id=?", (sheet_id,))
        for v in verdict.answers:
            a = v.read
            flags = v.flags + (["spot_check"] if spot else [])
            text = a.text if a and not a.is_blank else ""
            conn.execute(
                """INSERT INTO answers (sheet_id, question_id, ai_text, ai_blank, ai_confidence, ai_makes_sense,
                                        verify_text, agreement, flags, status, final_text, final_script)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sheet_id, v.question_id, text, int(bool(a and a.is_blank)),
                    a.confidence if a else None, int(a.makes_sense) if a else None,
                    v.verify_text, v.agreement, db.dumps(flags),
                    "needs_review" if flags else "auto_ok",
                    None if flags else text, None if flags else script_of(text),
                ),
            )
        conn.execute(
            """UPDATE sheets SET status=?, error=NULL, spot_check=?, legibility=?, sheet_flags=?, unmapped_text=?,
                                 raw_primary=?, raw_verify=?, updated_at=? WHERE id=?""",
            (status, int(spot), primary.legibility, db.dumps(verdict.sheet_flags), primary.unmapped_text,
             raw_p, raw_v, db.now(), sheet_id),
        )
    return status


async def _process(conn: sqlite3.Connection, row: sqlite3.Row, sem: asyncio.Semaphore) -> str:
    async with sem:
        try:
            rotation = await detect_rotation(await asyncio.to_thread(rotations, sheet_path(row)))
            conn.execute("UPDATE sheets SET rotation=? WHERE id=?", (rotation, row["id"]))
            jpeg = await asyncio.to_thread(to_jpeg, sheet_path(row), 3072, 90, rotation)
            jobs = [read_sheet(jpeg, settings.model, settings.thinking)]
            if settings.verify_model:
                jobs.append(read_sheet(jpeg, settings.verify_model, settings.verify_thinking))
            results = await asyncio.gather(*jobs)
            primary, raw_p = results[0]
            verify, raw_v = results[1] if len(results) > 1 else (None, None)
            return _store(conn, row["id"], primary, raw_p, verify, raw_v)
        except Exception as e:  # keep going; failed sheets can be retried
            conn.execute(
                "UPDATE sheets SET status='failed', error=?, updated_at=? WHERE id=?",
                (f"{type(e).__name__}: {e}"[:2000], db.now(), row["id"]),
            )
            return "failed"


async def extract(conn: sqlite3.Connection, limit: int | None = None, retry_failed: bool = False) -> dict[str, int]:
    statuses = ("pending", "failed") if retry_failed else ("pending",)
    rows = conn.execute(
        f"SELECT * FROM sheets WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY id"
        + (" LIMIT ?" if limit else ""),
        (*statuses, *([limit] if limit else [])),
    ).fetchall()

    sem = asyncio.Semaphore(settings.concurrency)
    tally: dict[str, int] = {}
    with Progress(TextColumn("[bold]Reading sheets"), BarColumn(), MofNCompleteColumn(),
                  TextColumn("{task.description}"), TimeRemainingColumn()) as prog:
        task = prog.add_task("", total=len(rows))
        for fut in asyncio.as_completed([_process(conn, r, sem) for r in rows]):
            status = await fut
            tally[status] = tally.get(status, 0) + 1
            prog.update(task, advance=1, description=" ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    return tally
