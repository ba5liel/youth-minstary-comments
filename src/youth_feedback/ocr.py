"""Gemini transcription of one handwritten response sheet."""

from __future__ import annotations

import logging

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from tenacity import before_sleep_log, retry, stop_after_attempt, wait_exponential

from .config import Question, form_description, questions, settings


class AnswerRead(BaseModel):
    question_id: str = Field(description="id of the question this answer belongs to")
    text: str = Field(
        description="Verbatim transcription in the script the writer used. Mark uncertain words as [?word?] "
        "and unreadable spans as [...]. Empty string if the answer is blank."
    )
    is_blank: bool = Field(description="True if nothing was written for this question")
    confidence: float = Field(description="0.0-1.0: probability that `text` is an exact, character-accurate transcription")
    makes_sense: bool = Field(
        description="True if the transcribed text reads as coherent language that plausibly answers the question"
    )
    notes: str = Field(default="", description="Short note on any problem (smudge, crossed out, cut off, etc.)")


class SheetRead(BaseModel):
    is_response_sheet: bool = Field(
        description="True if the page holds answers to this questionnaire (numbered 1-5 or under the printed "
        "questions). False for anything else, e.g. group discussion notes with a different list of points."
    )
    legibility: float = Field(description="0.0-1.0 overall legibility of the handwriting and photo")
    answers: list[AnswerRead]
    unmapped_text: str = Field(
        default="",
        description="Handwritten text you could not confidently attribute to a specific question. "
        "If is_response_sheet is false, the full verbatim transcription of the page goes here.",
    )
    notes: str = Field(default="")


def _question_block(qs: list[Question]) -> str:
    lines = []
    for q in qs:
        line = f"- id={q.id} | number {q.number} | {q.text_am}"
        if q.text_en:
            line += f" | EN: {q.text_en}"
        if q.description:
            line += f" | expected answer: {q.description}"
        lines.append(line)
    return "\n".join(lines)


SYSTEM = """You are an expert transcriber of handwritten Amharic (Ge'ez / Fidel script ፊደል).
You are digitising anonymous feedback that church youth wrote by hand in answer to a questionnaire.
Accuracy matters more than completeness: a wrong guess is worse than an honest "uncertain" mark.
Never invent a fluent, plausible sentence for handwriting you cannot actually read - mark it instead.

Rules:
1. Transcribe VERBATIM. Do not correct spelling, grammar or punctuation. Do not translate, summarise or paraphrase.
   Never swap a word for a more common spelling, synonym or "better" form (e.g. if ትናንሽ is written,
   do not write ትንንሽ; if ስለምገባ is written, do not write ስለገባኝ). Write exactly the letters on the page.
2. Keep the writer's script: Ge'ez stays Ge'ez, English stays English, mixed stays mixed.
   Pay close attention to Fidel vowel orders (e.g. ለ ሉ ሊ ላ ሌ ል ሎ) and look-alike characters
   (ሀ/ሐ/ኀ, ሰ/ሠ, አ/ዐ, ጸ/ፀ, ቸ/ቼ, ዉ/ው); transcribe the form actually written.
3. Wrap any word you are not sure about as [?word?]. Use [...] for spans you cannot read at all.
4. Omit text that is clearly crossed out.
5. Map each answer to its question using the number the writer put before it (1, 2, ... or ①),
   or the printed question text if present, and the layout. Do NOT include that leading number
   or its punctuation (e.g. "1," or "2.") in `text`.
   If an answer continues onto the margin or another area, join it. If you cannot tell which question
   some text belongs to, put it in `unmapped_text` instead of guessing.
6. Return exactly one entry per question id listed below, even if blank (is_blank=true, text="").
7. `confidence` must be calibrated: 0.95+ only when every character is clearly legible;
   lower it for every uncertain word. `makes_sense` = false if the text reads as gibberish,
   looks like a mis-read, or does not plausibly answer that question.
"""


def build_prompt() -> str:
    form = f"About the form: {form_description()}\n\n" if form_description() else ""
    return f"{form}The questionnaire has these questions:\n{_question_block(questions())}\n\nTranscribe the sheet in the image."


log = logging.getLogger(__name__)
RETRY = dict(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)

_client: genai.Client | None = None


def client() -> genai.Client:
    global _client
    if _client is None:
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not set (see .env.example)")
        _client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=settings.request_timeout * 1000),
        )
    return _client


@retry(**RETRY)
async def read_sheet(jpeg: bytes, model: str, thinking: str = "") -> tuple[SheetRead, str]:
    """Returns the parsed read and the raw JSON (kept for auditing)."""
    resp = await client().aio.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(
                data=jpeg,
                mime_type="image/jpeg",
                media_resolution=types.PartMediaResolutionLevel.MEDIA_RESOLUTION_HIGH,
            ),
            build_prompt(),
        ],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            temperature=0.0,
            response_mime_type="application/json",
            response_json_schema=SheetRead.model_json_schema(),
            # Some models can "think" for minutes on very messy handwriting; a level caps that.
            thinking_config=types.ThinkingConfig(thinking_level=thinking.upper()) if thinking else None,
        ),
    )
    raw = resp.text or ""
    return SheetRead.model_validate_json(raw), raw


class _Orientation(BaseModel):
    upright: str = Field(description="Letter of the upright image: A, B, C or D")


ORIENT_PROMPT = (
    "Above are four copies of the same photo of handwritten Amharic/English text, each turned a different way "
    "(labelled A, B, C, D). Which ONE shows the handwriting upright, so the lines run horizontally and "
    "read left-to-right with the letters right side up? Answer with the single letter."
)


@retry(**RETRY)
async def detect_rotation(variants: list[tuple[int, bytes]]) -> int:
    """Degrees clockwise needed to make the text upright. Reading sideways text is much less accurate.

    Asking the model to pick the upright copy out of four is far more reliable than asking it for an angle
    (which flipped between 90 and 270 on the same photo in testing).
    """
    contents: list = []
    for label, (_, data) in zip("ABCD", variants):
        contents += [f"Image {label}:", types.Part.from_bytes(data=data, mime_type="image/jpeg")]
    resp = await client().aio.models.generate_content(
        model=settings.orient_model,
        contents=contents + [ORIENT_PROMPT],
        config=types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
            response_json_schema=_Orientation.model_json_schema(),
        ),
    )
    pick = _Orientation.model_validate_json(resp.text or "").upright.strip().upper()[:1]
    return dict(zip("ABCD", (rot for rot, _ in variants))).get(pick, 0)


TRANSLIT_SYSTEM = """Convert Romanized (Latin-letter) Amharic into Amharic Ge'ez script (ፊደል).
Keep English words, names and numbers that are clearly English as-is. Keep punctuation.
Do not translate, correct or rephrase. Output only the converted text."""


@retry(**RETRY)
async def transliterate(text: str) -> str:
    resp = await client().aio.models.generate_content(
        model=settings.model,
        contents=text,
        config=types.GenerateContentConfig(system_instruction=TRANSLIT_SYSTEM, temperature=0.0),
    )
    return (resp.text or "").strip()
