"""Export results for analysis. CSVs are written with a BOM so Excel shows Amharic correctly."""

from __future__ import annotations

import asyncio
import csv
import json
import sqlite3
from pathlib import Path

from . import db
from .config import questions
from .ocr import transliterate


def _final(a: sqlite3.Row) -> str:
    # final_text is set for auto_ok and reviewed answers; fall back to the AI draft otherwise.
    return a["final_text"] if a["final_text"] is not None else a["ai_text"]


def export(conn: sqlite3.Connection, out_dir: Path, include_unreviewed: bool = False) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    statuses = ["auto_ok", "reviewed"] + (["needs_review"] if include_unreviewed else [])
    sheets = conn.execute(
        f"SELECT * FROM sheets WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY id", statuses
    ).fetchall()
    qs = questions()

    wide, long_, jsonl = out_dir / "responses_wide.csv", out_dir / "answers_long.csv", out_dir / "responses.jsonl"
    with wide.open("w", newline="", encoding="utf-8-sig") as fw, \
         long_.open("w", newline="", encoding="utf-8-sig") as fl, \
         jsonl.open("w", encoding="utf-8") as fj:
        ww, wl = csv.writer(fw), csv.writer(fl)
        ww.writerow(["sheet_id", "image", "status"] + [q.id for q in qs])
        wl.writerow(["sheet_id", "question_id", "question", "answer", "source", "script",
                     "ai_confidence", "flags"])
        for s in sheets:
            answers = {a["question_id"]: a for a in db.answers_for(conn, s["id"])}
            ww.writerow([s["id"], s["path"], s["status"]] + [_final(answers[q.id]) if q.id in answers else "" for q in qs])
            record = {"sheet_id": s["id"], "image": s["path"], "status": s["status"], "answers": {}}
            for q in qs:
                a = answers.get(q.id)
                if a is None:
                    continue
                source = "human" if a["status"] == "reviewed" else ("ai" if a["status"] == "auto_ok" else "ai_unreviewed")
                wl.writerow([s["id"], q.id, q.label, _final(a), source, a["final_script"] or "",
                             a["ai_confidence"], a["flags"]])
                record["answers"][q.id] = {"text": _final(a), "source": source}
            fj.write(json.dumps(record, ensure_ascii=False) + "\n")

    other = out_dir / "other_format.csv"
    with other.open("w", newline="", encoding="utf-8-sig") as fo:
        wo = csv.writer(fo)
        wo.writerow(["sheet_id", "image", "transcription", "notes"])
        for s in conn.execute("SELECT * FROM sheets WHERE status='other_format' ORDER BY id"):
            notes = json.loads(s["raw_primary"] or "{}").get("notes", "")
            wo.writerow([s["id"], s["path"], s["unmapped_text"] or "", notes])
    return {"wide": wide, "long": long_, "jsonl": jsonl, "other_format": other}


async def transliterate_reviewed(conn: sqlite3.Connection, concurrency: int = 4) -> int:
    """Convert reviewer answers typed in Latin letters to Ge'ez. Original typing stays in reviewer_raw."""
    rows = conn.execute(
        "SELECT sheet_id, question_id, final_text FROM answers WHERE status='reviewed' AND final_script IN ('latin','mixed')"
    ).fetchall()
    sem = asyncio.Semaphore(concurrency)

    async def one(r: sqlite3.Row) -> None:
        async with sem:
            text = await transliterate(r["final_text"])
        conn.execute(
            "UPDATE answers SET final_text=?, final_script='geez' WHERE sheet_id=? AND question_id=?",
            (text, r["sheet_id"], r["question_id"]),
        )

    await asyncio.gather(*(one(r) for r in rows))
    return len(rows)


def spot_check_accuracy(conn: sqlite3.Connection) -> tuple[int, int]:
    """(answers checked, answers the reviewer left unchanged) on randomly spot-checked auto sheets."""
    rows = conn.execute(
        """SELECT a.ai_text, a.reviewer_raw FROM answers a JOIN sheets s ON s.id=a.sheet_id
           WHERE s.spot_check=1 AND a.status='reviewed'"""
    ).fetchall()
    same = sum(1 for r in rows if (r["reviewer_raw"] or "").strip() == (r["ai_text"] or "").strip())
    return len(rows), same
