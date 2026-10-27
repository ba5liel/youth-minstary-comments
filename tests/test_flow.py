import asyncio
import json

import pytest
from PIL import Image

from youth_feedback import db, pipeline
from youth_feedback.config import questions
from youth_feedback.export import export, spot_check_accuracy
from youth_feedback.ocr import AnswerRead, SheetRead
from youth_feedback.triage import judge, script_of, similarity

QIDS = [q.id for q in questions()]


def sheet(**over):
    answers = [AnswerRead(question_id=q, text="ፕሮግራሙ ጥሩ ነው", is_blank=False, confidence=0.97, makes_sense=True)
               for q in QIDS]
    for q, kw in over.items():
        i = QIDS.index(q)
        answers[i] = answers[i].model_copy(update=kw)
    return SheetRead(is_response_sheet=True, legibility=0.9, answers=answers)


def test_clean_sheet_is_auto():
    assert not judge(sheet(), sheet()).needs_review


@pytest.mark.parametrize("over,flag", [
    ({QIDS[1]: {"confidence": 0.5}}, "low_confidence"),
    ({QIDS[1]: {"text": "ፕሮግራሙ [?ጥሩ?] ነው"}}, "uncertain_words"),
    ({QIDS[1]: {"makes_sense": False}}, "not_sensible"),
    ({QIDS[1]: {"text": "ፕሮግራሙ ጥሩ ነው ☃"}}, "odd_characters"),
])
def test_flags(over, flag):
    v = judge(sheet(**over), sheet())
    q2 = next(a for a in v.answers if a.question_id == QIDS[1])
    assert flag in q2.flags
    assert all(not a.flags for a in v.answers if a.question_id != QIDS[1])


def test_disagreement_and_missing():
    v = judge(sheet(), sheet(**{QIDS[2]: {"text": "መዝሙሩ ይበዛል በጣም"}}))
    assert "disagreement" in next(a for a in v.answers if a.question_id == QIDS[2]).flags
    p = sheet()
    p.answers.pop()
    assert "missing" in judge(p, None).answers[-1].flags


def test_unclear_sheet_flags_everything():
    s = sheet()
    s.legibility = 0.3
    assert all("sheet_unclear" in a.flags for a in judge(s, None).answers)


def test_helpers():
    assert script_of("ሰላም") == "geez" and script_of("selam") == "latin" and script_of("") == "none"
    assert similarity("ሰላም።", "ሰላም") == 1.0


def test_pipeline_bot_flow_export(tmp_path, monkeypatch):
    imgs = tmp_path / "imgs"
    imgs.mkdir()
    for i in range(3):
        Image.new("RGB", (40 + i, 40), "white").save(imgs / f"s{i}.jpg")
    reads = {40: sheet(), 41: sheet(**{QIDS[1]: {"confidence": 0.4}}), 42: sheet(**{QIDS[4]: {"text": "[...] ነው"}})}

    async def fake_read(jpeg, model):
        import io
        w = Image.open(io.BytesIO(jpeg)).width
        s = reads[w]
        return s, s.model_dump_json()

    monkeypatch.setattr(pipeline, "read_sheet", fake_read)
    monkeypatch.setattr(pipeline.settings.__class__, "spot_check_rate", 0.0, raising=False)
    conn = db.connect(tmp_path / "t.db")
    assert pipeline.ingest(conn, imgs) == (3, 3)
    assert pipeline.ingest(conn, imgs) == (0, 3)  # idempotent
    tally = asyncio.run(pipeline.extract(conn))
    assert tally == {"auto_ok": 1, "needs_review": 2}

    # two reviewers get different sheets
    a = db.claim_next(conn, 111)
    b = db.claim_next(conn, 222)
    assert a["id"] != b["id"]
    assert db.claim_next(conn, 111)["id"] == a["id"]  # resumes own claim
    assert db.claim_next(conn, 333) is None

    pend = db.pending_answers(conn, a["id"])
    assert len(pend) == 1
    db.save_review(conn, a["id"], pend[0]["question_id"], "selam new", script_of("selam new"), 111)
    assert db.finish_sheet_if_done(conn, a["id"], 111)
    assert db.counts(conn)["reviewed"] == 1

    out = export(conn, tmp_path / "out")
    rows = [json.loads(l) for l in out["jsonl"].read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2  # auto_ok + reviewed; the unreviewed one is excluded
    texts = [ans["text"] for r in rows for ans in r["answers"].values()]
    assert "selam new" in texts
    assert spot_check_accuracy(conn) == (0, 0)


def test_one_letter_difference_needs_review():
    v = judge(sheet(**{QIDS[4]: {"text": "Mentorship program አጀምራለሁ"}}),
              sheet(**{QIDS[4]: {"text": "Mentorship Program እጀምራለሁ።"}}))
    assert "disagreement" in v.answers[4].flags


def test_homophones_and_punctuation_agree():
    assert similarity("ሐሳብ ጸሎት ::", "ሀሳብ ፀሎት።") == 1.0
