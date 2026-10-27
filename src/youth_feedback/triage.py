"""Decide which extracted answers can be trusted and which need a human.

The model's self-reported confidence alone is not reliable, so several independent signals are combined:
  * self-reported confidence and "makes sense" judgement
  * explicit uncertainty marks the model left in the text ([?word?], [...])
  * agreement with an independent second read by a different model
  * script sanity (unexpected characters usually mean a garbled read)
Any single failing signal sends the answer to review.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .config import questions, settings
from .ocr import AnswerRead, SheetRead

UNCERTAIN = re.compile(r"\[\?.*?\?\]|\[\.\.\.\]|\[…\]")
ETHIOPIC = re.compile(r"[ሀ-᎟ⶀ-⷟꬀-꬯]")
LATIN = re.compile(r"[A-Za-z]")

# Human-readable reasons, shown to reviewers in Telegram.
REASONS = {
    "low_confidence": "AI is not confident",
    "uncertain_words": "some words unclear",
    "not_sensible": "text may not make sense",
    "disagreement": "two AI readers disagree",
    "missing": "AI did not return this answer",
    "odd_characters": "unexpected characters",
    "sheet_unclear": "whole sheet is hard to read",
    "unmapped_text": "some text could not be placed",
    "spot_check": "random quality check",
}


def script_of(text: str) -> str:
    ge, la = len(ETHIOPIC.findall(text)), len(LATIN.findall(text))
    if not ge and not la:
        return "none"
    if ge and la:
        return "mixed" if min(ge, la) / max(ge, la) > 0.2 else ("geez" if ge > la else "latin")
    return "geez" if ge else "latin"


# Fidel series that are pronounced the same and used interchangeably in everyday writing
# (ሐ/ኀ -> ሀ, ሠ -> ሰ, ዐ -> አ, ፀ -> ጸ). Used only when comparing readings, never on stored text.
_HOMOPHONES = {}
for src, dst in ((0x1210, 0x1200), (0x1280, 0x1200), (0x1220, 0x1230), (0x12D0, 0x12A0), (0x1340, 0x1338)):
    for order in range(7):
        _HOMOPHONES[src + order] = dst + order


def normalize(text: str) -> str:
    text = UNCERTAIN.sub(" ", unicodedata.normalize("NFC", text)).translate(_HOMOPHONES)
    text = re.sub(r"[\s፡።፣፤፥፦፧፨.,;:!?\"'()\-]+", " ", text)
    return text.strip().lower()


def similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na and not nb:
        return 1.0
    return fuzz.ratio(na, nb) / 100


def _odd_characters(text: str) -> bool:
    for ch in UNCERTAIN.sub("", text):
        if ETHIOPIC.match(ch) or LATIN.match(ch) or ch.isdigit() or ch.isspace():
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("P") or cat in ("Sm", "Sc"):
            continue
        return True
    return False


@dataclass
class AnswerVerdict:
    question_id: str
    read: AnswerRead | None
    verify_text: str | None = None
    agreement: float | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return bool(self.flags)


@dataclass
class SheetVerdict:
    sheet_flags: list[str]
    answers: list[AnswerVerdict]

    @property
    def needs_review(self) -> bool:
        return bool(self.sheet_flags) or any(a.needs_review for a in self.answers)


def judge(primary: SheetRead, verify: SheetRead | None) -> SheetVerdict:
    by_q = {a.question_id: a for a in primary.answers}
    ver_q = {a.question_id: a for a in verify.answers} if verify else {}

    sheet_flags = []
    if primary.legibility < settings.min_legibility:
        sheet_flags.append("sheet_unclear")
    if primary.unmapped_text.strip():
        sheet_flags.append("unmapped_text")

    verdicts = []
    for q in questions():
        a = by_q.get(q.id)
        v = AnswerVerdict(q.id, a)
        if a is None:
            v.flags.append("missing")
            verdicts.append(v)
            continue

        if not a.is_blank:
            if a.confidence < settings.min_confidence:
                v.flags.append("low_confidence")
            if UNCERTAIN.search(a.text):
                v.flags.append("uncertain_words")
            if not a.makes_sense:
                v.flags.append("not_sensible")
            if _odd_characters(a.text):
                v.flags.append("odd_characters")
            if not a.text.strip():
                v.flags.append("missing")

        if verify is not None:
            b = ver_q.get(q.id)
            if b is None:
                v.flags.append("disagreement")
            else:
                v.verify_text = b.text
                v.agreement = 1.0 if a.is_blank and b.is_blank else similarity(a.text, b.text)
                if a.is_blank != b.is_blank or v.agreement < settings.min_agreement:
                    v.flags.append("disagreement")

        # A sheet-level problem means no answer on it can be trusted.
        v.flags.extend(f for f in sheet_flags if f not in v.flags)
        verdicts.append(v)

    return SheetVerdict(sheet_flags, verdicts)
