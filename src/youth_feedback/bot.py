"""Telegram bot for reviewing answers the AI could not read reliably.

Flow for a reviewer (on a phone):
  /start -> "Start reviewing"
  bot sends the sheet photo, then one flagged answer at a time with the AI's reading.
  Reviewer either taps "AI is correct", taps Blank/Unreadable, or simply types the right text
  (Amharic, or Amharic in Latin letters - it is converted later). Next answer appears automatically,
  and the next sheet after the last answer.

Several reviewers can work at once: each sheet is claimed by one reviewer, and the claim expires
after CLAIM_MINUTES of inactivity so abandoned sheets return to the queue.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import sqlite3

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import db
from .config import question_map, questions, settings
from .images import to_jpeg
from .pipeline import sheet_path
from .triage import REASONS, script_of

log = logging.getLogger(__name__)

UNREADABLE = "[unreadable]"


def _conn(ctx: ContextTypes.DEFAULT_TYPE) -> sqlite3.Connection:
    return ctx.application.bot_data["conn"]


async def _authorized(update: Update) -> bool:
    uid = update.effective_user.id
    if uid in settings.admin_ids:
        return True
    text = (f"Sorry, you are not a reviewer yet.\nYour Telegram ID is <code>{uid}</code> - "
            "send it to the organizer so they can add you.")
    if update.callback_query:
        await update.callback_query.answer("Not authorized", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)
    return False


def _remaining(conn: sqlite3.Connection) -> int:
    return db.counts(conn).get("needs_review", 0)


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------- screens ----------

async def show_next(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    conn, ud, uid = _conn(ctx), ctx.user_data, update.effective_user.id
    chat = update.effective_chat
    sheet = db.claim_next(conn, uid, ud.setdefault("skipped", set()))
    if sheet is None:
        ud.pop("sheet", None)
        ud.pop("current_q", None)
        msg = "🎉 Nothing is waiting for review right now. Thank you!"
        if ud["skipped"]:
            msg += f"\n\nYou skipped {len(ud['skipped'])} sheet(s). /reset_skips to see them again."
        await chat.send_message(msg)
        return

    if ud.get("sheet") != sheet["id"]:
        ud["sheet"], ud["history"] = sheet["id"], []
        jpeg = await asyncio.to_thread(to_jpeg, sheet_path(sheet), 2048, 85, sheet["rotation"])
        caption = f"📄 <b>Sheet #{sheet['id']}</b> · {_remaining(conn)} sheet(s) waiting"
        if sheet["unmapped_text"]:
            caption += ("\n\n<b>Text the AI could not place:</b>\n"
                        f"<code>{html.escape(_clip(sheet['unmapped_text'], 600))}</code>")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔍 Full resolution", callback_data=f"full:{sheet['id']}"),
            InlineKeyboardButton("⏭ Skip sheet", callback_data=f"skip:{sheet['id']}"),
        ]])
        await chat.send_photo(jpeg, caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)

    await ask_current(update, ctx)


async def ask_current(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    conn, ud, uid = _conn(ctx), ctx.user_data, update.effective_user.id
    sheet_id = ud["sheet"]
    pending = db.pending_answers(conn, sheet_id)
    if not pending and ud.get("return_to") == sheet_id:
        ud.pop("return_to")
        ud.pop("sheet", None)
        ud.pop("current_q", None)
        await show_overview(update, ctx, sheet_id)
        return
    if not pending:
        if db.finish_sheet_if_done(conn, sheet_id, uid):
            await update.effective_chat.send_message(f"✅ Sheet #{sheet_id} saved.")
        ud.pop("sheet", None)
        ud.pop("current_q", None)
        await show_next(update, ctx)
        return

    a = pending[0]
    ud["current_q"] = a["question_id"]
    qs = question_map()
    q = qs.get(a["question_id"])
    total = len(db.answers_for(conn, sheet_id))
    done = total - len(pending)
    reasons = ", ".join(REASONS.get(f, f) for f in json.loads(a["flags"]))

    lines = [
        f"<b>Q{q.number if q else '?'}.</b> {html.escape(q.label if q else a['question_id'])}",
        f"<i>Answer {done + 1} of {total} on this sheet</i>",
        "",
        "🤖 <b>AI read:</b>",
        f"<code>{html.escape(_clip(a['ai_text'], 1500))}</code>" if a["ai_text"] else "<i>(nothing)</i>",
    ]
    saved = a["final_text"]
    if saved is not None and saved != a["ai_text"]:
        lines += ["💾 <b>Saved answer:</b>", f"<code>{html.escape(_clip(saved, 1200))}</code>" if saved else "<i>(blank)</i>"]
    alt = a["verify_text"]
    show_alt = bool(alt) and alt.strip() != a["ai_text"].strip()
    if show_alt:
        lines += ["🤖 <b>2nd reader:</b>", f"<code>{html.escape(_clip(alt, 1200))}</code>"]
    if reasons:
        lines += ["", f"⚠️ {html.escape(reasons)}"]
    lines += ["", "✍️ <b>Type the correct answer</b> (Amharic or Latin letters), or tap a button.",
              "<i>Tip: tap the grey text to copy it, then fix it.</i>"]

    key = f"{sheet_id}:{a['question_id']}"
    rows = []
    if saved is not None and saved != a["ai_text"]:
        rows.append([InlineKeyboardButton("✅ Saved answer is correct", callback_data=f"saved:{key}")])
    if a["ai_text"]:
        rows.append([InlineKeyboardButton("✅ AI is correct", callback_data=f"keep:{key}")])
    if show_alt:
        rows.append([InlineKeyboardButton("✅ 2nd reader is correct", callback_data=f"alt:{key}")])
    rows.append([
        InlineKeyboardButton("⬜ Left blank", callback_data=f"blank:{key}"),
        InlineKeyboardButton("❓ Can't read", callback_data=f"unread:{key}"),
    ])
    if ud.get("return_to") == sheet_id:
        nav = [InlineKeyboardButton("↩️ Cancel", callback_data=f"cancel:{key}")]
    else:
        nav = [InlineKeyboardButton("⏭ Skip sheet", callback_data=f"skip:{sheet_id}")]
    if ud.get("history"):
        nav.insert(0, InlineKeyboardButton("⬅️ Back", callback_data=f"back:{sheet_id}"))
    rows.append(nav)

    await update.effective_chat.send_message(
        "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows)
    )


async def show_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Next automatically accepted sheet for a human to double-check."""
    conn, ud, uid = _conn(ctx), ctx.user_data, update.effective_user.id
    sheet = db.claim_next_auto(conn, uid, ud.setdefault("auto_skipped", set()))
    if sheet is None:
        await update.effective_chat.send_message(
            "🎉 Every automatically accepted sheet has been checked." if not ud["auto_skipped"] else
            f"No more unchecked sheets (you passed on {len(ud['auto_skipped'])}; /reset_skips to see them again)."
        )
        return
    await show_overview(update, ctx, sheet["id"], send_photo=True)


async def show_overview(update: Update, ctx: ContextTypes.DEFAULT_TYPE, sheet_id: int,
                        send_photo: bool = False) -> None:
    """Photo + every answer on one screen, with buttons to confirm all or fix a single answer."""
    conn, chat = _conn(ctx), update.effective_chat
    sheet = conn.execute("SELECT * FROM sheets WHERE id=?", (sheet_id,)).fetchone()
    if sheet is None:
        await chat.send_message(f"There is no sheet #{sheet_id}.")
        return
    if sheet["status"] in ("pending", "failed"):
        await chat.send_message(f"Sheet #{sheet_id} has not been read by the AI yet ({sheet['status']}).")
        return
    ctx.user_data["auto_sheet"] = sheet_id

    if send_photo:
        jpeg = await asyncio.to_thread(to_jpeg, sheet_path(sheet), 2048, 85, sheet["rotation"])
        unchecked = db.counts(conn).get("auto_ok", 0)
        await chat.send_photo(jpeg, caption=f"📄 <b>Sheet #{sheet_id}</b> · {unchecked} accepted sheet(s) not yet checked",
                              parse_mode=ParseMode.HTML)

    if sheet["status"] == "other_format":
        await chat.send_message(
            f"📝 Sheet #{sheet_id} is not in the 1-5 answer format. Full text:\n\n"
            f"<code>{html.escape(_clip(sheet['unmapped_text'] or '', 3500))}</code>", parse_mode=ParseMode.HTML)
        return
    if sheet["status"] == "needs_review":
        await chat.send_message(f"Sheet #{sheet_id} is in the review queue - use /next to review it.")
        return

    qs = question_map()
    answers = db.answers_for(conn, sheet_id)
    status = {"auto_ok": "🤖 accepted automatically", "reviewed": "✔️ checked by a person"}.get(sheet["status"], sheet["status"])
    lines = [f"<b>Sheet #{sheet_id}</b> · {status}", ""]
    fix_buttons = []
    for a in answers:
        q = qs.get(a["question_id"])
        num = q.number if q else "?"
        text = a["final_text"] if a["final_text"] is not None else a["ai_text"]
        mark = "✔️" if a["status"] == "reviewed" else "🤖"
        lines.append(f"<b>Q{num}.</b> {mark} " + (f"<code>{html.escape(_clip(text, 600))}</code>" if text else "<i>(blank)</i>"))
        fix_buttons.append(InlineKeyboardButton(f"✏️ Q{num}", callback_data=f"efix:{sheet_id}:{a['question_id']}"))
    lines += ["", "Compare with the photo. Tap <b>All correct</b>, or the question you need to fix."]

    rows = [[InlineKeyboardButton("✅ All correct", callback_data=f"aok:{sheet_id}")], fix_buttons,
            [InlineKeyboardButton("🔍 Full resolution", callback_data=f"full:{sheet_id}"),
             InlineKeyboardButton("⏭ Next sheet", callback_data=f"anext:{sheet_id}")]]
    await chat.send_message("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def _record(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str, script: str,
                  raw: str | None = None) -> None:
    ud = ctx.user_data
    db.save_review(_conn(ctx), ud["sheet"], ud["current_q"], text, script, update.effective_user.id, raw)
    ud.setdefault("history", []).append(ud["current_q"])
    await ask_current(update, ctx)


# ---------- handlers ----------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    c = db.counts(_conn(ctx))
    total = sum(c.values())
    text = (
        "👋 <b>Youth feedback review</b>\n\n"
        "The AI read the handwritten sheets. Some answers were hard to read - "
        "please check them against the photo.\n\n"
        f"Sheets total: {total}\n"
        f"✅ Read automatically: {c.get('auto_ok', 0)}\n"
        f"👀 Waiting for review: {c.get('needs_review', 0)}\n"
        f"✔️ Reviewed: {c.get('reviewed', 0)}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Review unclear answers", callback_data="next")],
        [InlineKeyboardButton(f"🔎 Check accepted sheets ({c.get('auto_ok', 0)})", callback_data="check")],
    ])
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_next(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await _authorized(update):
        await show_next(update, ctx)


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if await _authorized(update):
        await show_auto(update, ctx)


async def cmd_sheet(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    if not ctx.args or not ctx.args[0].lstrip("#").isdigit():
        await update.effective_message.reply_text("Usage: /sheet 12")
        return
    await show_overview(update, ctx, int(ctx.args[0].lstrip("#")), send_photo=True)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    conn = _conn(ctx)
    c = db.counts(conn)
    mine = conn.execute(
        "SELECT COUNT(*) FROM sheets WHERE reviewed_by=?", (update.effective_user.id,)
    ).fetchone()[0]
    body = "\n".join(f"{k}: {v}" for k, v in sorted(c.items())) or "no sheets yet"
    await update.effective_message.reply_text(f"📊 Sheets by status\n{body}\n\nYou reviewed: {mine}")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    if sid := ctx.user_data.pop("sheet", None):
        db.release(_conn(ctx), sid, update.effective_user.id)
    ctx.user_data.pop("current_q", None)
    await update.effective_message.reply_text("⏸ Paused. Your progress is saved. Send /next to continue.")


async def cmd_reset_skips(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    ctx.user_data["skipped"] = set()
    ctx.user_data["auto_skipped"] = set()
    await update.effective_message.reply_text("Skipped sheets are back in your queue. /next")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    if not ctx.user_data.get("current_q"):
        await update.effective_message.reply_text("Send /next to get a sheet to review.")
        return
    text = update.effective_message.text.strip()
    await _record(update, ctx, text, script_of(text))


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    query = update.callback_query
    conn, ud, uid = _conn(ctx), ctx.user_data, update.effective_user.id
    action, _, rest = query.data.partition(":")

    if action in ("next", "full", "check"):
        await query.answer()
    if action == "next":
        await show_next(update, ctx)
        return
    if action == "check":
        await show_auto(update, ctx)
        return

    if action in ("aok", "anext", "efix"):
        sheet_id = int(rest.split(":")[0])
        if sheet_id != ud.get("auto_sheet") or ud.get("sheet"):
            await query.answer("Finish the current answer first, or that button is from an earlier sheet.")
            return
        await query.answer()
        if action == "aok":
            db.confirm_sheet(conn, sheet_id, uid)
            await update.effective_chat.send_message(f"✅ Sheet #{sheet_id} confirmed.")
            await show_auto(update, ctx)
        elif action == "anext":
            ud.setdefault("auto_skipped", set()).add(sheet_id)
            db.release(conn, sheet_id, uid)
            await show_auto(update, ctx)
        else:
            qid = rest.split(":", 1)[1]
            db.open_answer(conn, sheet_id, qid)
            ud.update(sheet=sheet_id, history=[], return_to=sheet_id)
            await ask_current(update, ctx)
        return

    if action == "full":
        row = conn.execute("SELECT * FROM sheets WHERE id=?", (int(rest),)).fetchone()
        if row:
            jpeg = await asyncio.to_thread(to_jpeg, sheet_path(row), 4096, 92, row["rotation"])
            await update.effective_chat.send_document(
                InputFile(jpeg, filename=f"sheet-{row['id']}.jpg"), caption="Open and zoom in 🔍"
            )
        return

    # Everything below acts on the sheet the reviewer is currently on; ignore taps on old messages.
    sheet_id = int(rest.split(":")[0])
    if sheet_id != ud.get("sheet"):
        await query.answer("That button is from an earlier sheet.")
        return

    if action in ("skip", "back"):
        await query.answer()
    if action == "cancel":
        # Leave the answer as it was before the reviewer tapped ✏️ (its status comes back with the text).
        await query.answer()
        qid = rest.split(":", 1)[1]
        ans = conn.execute("SELECT * FROM answers WHERE sheet_id=? AND question_id=?", (sheet_id, qid)).fetchone()
        status = "reviewed" if ans["reviewed_by"] else "auto_ok"
        conn.execute("UPDATE answers SET status=? WHERE sheet_id=? AND question_id=?", (status, sheet_id, qid))
        ud.pop("return_to", None)
        ud.pop("sheet", None)
        ud.pop("current_q", None)
        await show_overview(update, ctx, sheet_id)
        return
    if action == "skip":
        ud.setdefault("skipped", set()).add(sheet_id)
        db.release(conn, sheet_id, uid)
        ud.pop("sheet", None)
        ud.pop("current_q", None)
        await update.effective_chat.send_message(f"⏭ Skipped sheet #{sheet_id}.")
        await show_next(update, ctx)
        return

    if action == "back":
        if ud.get("history"):
            db.reopen_answer(conn, sheet_id, ud["history"].pop())
        await ask_current(update, ctx)
        return

    qid = rest.split(":", 1)[1]
    if qid != ud.get("current_q"):
        await query.answer("That button is from an earlier question.")
        return
    await query.answer()
    ans = conn.execute("SELECT * FROM answers WHERE sheet_id=? AND question_id=?", (sheet_id, qid)).fetchone()
    if action == "keep":
        await _record(update, ctx, ans["ai_text"], script_of(ans["ai_text"]))
    elif action == "saved":
        await _record(update, ctx, ans["final_text"], script_of(ans["final_text"]), ans["reviewer_raw"])
    elif action == "alt":
        await _record(update, ctx, ans["verify_text"], script_of(ans["verify_text"]))
    elif action == "blank":
        await _record(update, ctx, "", "none")
    elif action == "unread":
        await _record(update, ctx, UNREADABLE, "none")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Bot error", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_chat:
        await update.effective_chat.send_message("Something went wrong. Send /next to continue.")


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("next", "Review unclear answers"),
        BotCommand("check", "Double-check sheets the AI accepted"),
        BotCommand("sheet", "Open a sheet by number, e.g. /sheet 12"),
        BotCommand("stats", "Progress"),
        BotCommand("stop", "Pause reviewing"),
        BotCommand("reset_skips", "Show skipped sheets again"),
    ])


def run() -> None:
    if not settings.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see .env.example)")
    if not settings.admin_ids:
        log.warning("ADMIN_IDS is empty: nobody can review. Message the bot to learn your Telegram ID.")
    questions()  # fail fast on a broken questions.yaml

    app = Application.builder().token(settings.telegram_token).post_init(_post_init).build()
    app.bot_data["conn"] = db.connect()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("sheet", cmd_sheet))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("reset_skips", cmd_reset_skips))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
