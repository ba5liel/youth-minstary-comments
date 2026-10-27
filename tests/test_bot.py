"""Drive the bot handlers with a fake Telegram chat (no network)."""
import asyncio
from types import SimpleNamespace

from PIL import Image

from youth_feedback import bot, db, pipeline
from youth_feedback.config import questions, settings
from test_flow import QIDS, sheet

UID = 4242


class Chat:
    def __init__(self):
        self.sent = []  # (text, buttons)

    async def send_message(self, text, reply_markup=None, **kw):
        self.sent.append((text, _buttons(reply_markup)))

    async def send_photo(self, photo, caption=None, reply_markup=None, **kw):
        self.sent.append((f"[photo] {caption}", _buttons(reply_markup)))

    async def send_document(self, doc, caption=None, **kw):
        self.sent.append((f"[doc] {caption}", {}))


def _buttons(markup):
    if markup is None:
        return {}
    return {b.text: b.callback_data for row in markup.inline_keyboard for b in row}


class Harness:
    def __init__(self, conn):
        self.chat = Chat()
        self.ctx = SimpleNamespace(user_data={}, args=[], application=SimpleNamespace(bot_data={"conn": conn}))

    def _update(self, data=None, text=None):
        async def answer(*a, **k):
            pass

        async def reply_text(t, reply_markup=None, **k):
            self.chat.sent.append((t, _buttons(reply_markup)))

        msg = SimpleNamespace(text=text, reply_text=reply_text)
        cq = SimpleNamespace(data=data, answer=answer) if data else None
        return SimpleNamespace(effective_user=SimpleNamespace(id=UID), effective_chat=self.chat,
                               effective_message=msg, callback_query=cq)

    def press(self, label_prefix):
        """Tap the button starting with `label_prefix` on the most recent message that has it."""
        for _, buttons in reversed(self.chat.sent):
            for label, data in buttons.items():
                if label.startswith(label_prefix):
                    asyncio.run(bot.on_button(self._update(data=data), self.ctx))
                    return
        raise AssertionError(f"no button {label_prefix!r}; last: {self.chat.sent[-1]}")

    def type(self, text):
        asyncio.run(bot.on_text(self._update(text=text), self.ctx))

    def command(self, fn, *args):
        self.ctx.args = list(args)
        asyncio.run(fn(self._update(text="/cmd"), self.ctx))

    @property
    def last(self):
        return self.chat.sent[-1][0]


def setup(tmp_path, monkeypatch):
    imgs = tmp_path / "imgs"
    imgs.mkdir()
    for i in range(2):
        Image.new("RGB", (40 + i, 40), "white").save(imgs / f"s{i}.jpg")
    reads = {40: sheet(), 41: sheet(**{QIDS[1]: {"confidence": 0.4}})}

    async def fake_read(jpeg, model, thinking=""):
        import io
        s = reads[Image.open(io.BytesIO(jpeg)).width]
        return s, s.model_dump_json()

    async def fake_rotation(variants):
        return 0

    monkeypatch.setattr(pipeline, "read_sheet", fake_read)
    monkeypatch.setattr(pipeline, "detect_rotation", fake_rotation)
    object.__setattr__(pipeline.settings, "spot_check_rate", 0.0)
    object.__setattr__(settings, "admin_ids", {UID})
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    monkeypatch.setattr(bot, "sheet_path", lambda row: tmp_path / row["path"] if not row["path"].startswith("/") else __import__("pathlib").Path(row["path"]))
    conn = db.connect(tmp_path / "t.db")
    pipeline.ingest(conn, imgs)
    asyncio.run(pipeline.extract(conn))
    return conn


def test_unauthorized_user_gets_their_id(tmp_path, monkeypatch):
    conn = setup(tmp_path, monkeypatch)
    object.__setattr__(settings, "admin_ids", set())
    h = Harness(conn)
    h.command(bot.cmd_start)
    assert str(UID) in h.last


def test_check_accepted_sheet_fix_one_answer_then_confirm(tmp_path, monkeypatch):
    conn = setup(tmp_path, monkeypatch)
    h = Harness(conn)
    h.command(bot.cmd_start)
    h.press("🔎 Check accepted")
    assert "accepted automatically" in h.last
    auto_id = h.ctx.user_data["auto_sheet"]

    h.press("✏️ Q3")
    assert "AI read" in h.last and "Cancel" in str(h.chat.sent[-1][1])
    h.type("ተስተካክሏል")
    assert "ተስተካክሏል" in h.last and "✔️" in h.last  # back on the overview with the fix
    assert conn.execute("SELECT status FROM sheets WHERE id=?", (auto_id,)).fetchone()[0] == "auto_ok"

    h.press("✏️ Q4")
    h.press("↩️ Cancel")  # cancel restores the answer untouched
    row = conn.execute("SELECT status FROM answers WHERE sheet_id=? AND question_id=?", (auto_id, QIDS[3])).fetchone()
    assert row[0] == "auto_ok"

    h.press("✅ All correct")
    assert conn.execute("SELECT status FROM sheets WHERE id=?", (auto_id,)).fetchone()[0] == "reviewed"
    finals = dict(conn.execute("SELECT question_id, final_text FROM answers WHERE sheet_id=?", (auto_id,)).fetchall())
    assert finals[QIDS[2]] == "ተስተካክሏል" and finals[QIDS[0]] == "ፕሮግራሙ ጥሩ ነው"
    assert "Every automatically accepted sheet has been checked" in h.last

    # reopen later by number; the saved human answer is offered
    h.command(bot.cmd_sheet, str(auto_id))
    assert "checked by a person" in h.last
    h.press("✏️ Q3")
    assert "Saved answer" in h.last
    h.press("✅ Saved answer is correct")
    assert "ተስተካክሏል" in h.last


def test_review_queue(tmp_path, monkeypatch):
    conn = setup(tmp_path, monkeypatch)
    h = Harness(conn)
    h.command(bot.cmd_next)
    assert "[photo]" in h.chat.sent[-2][0] and "AI read" in h.last
    h.press("✅ AI is correct")
    assert any("saved" in t for t, _ in h.chat.sent)
    assert "Nothing is waiting" in h.last
    assert db.counts(conn).get("reviewed") == 1
