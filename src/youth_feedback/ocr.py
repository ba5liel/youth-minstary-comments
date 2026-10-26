"""Gemini transcription of one handwritten response sheet."""

from __future__ import annotations

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Question, questions, settings


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
    is_response_sheet: bool = Field(description="False if the image is not a filled-in questionnaire page")
    legibility: float = Field(description="0.0-1.0 overall legibility of the handwriting and photo")
    answers: list[AnswerRead]
    unmapped_text: str = Field(
        default="", description="Any handwritten text you could not confidently attribute to a specific question"
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
You are digitising anonymous feedback that church youth wrote by hand on a paper questionnaire.
Accuracy matters more than completeness: a wrong guess is worse than an honest "uncertain" mark.

Rules:
1. Transcribe VERBATIM. Do not correct spelling, grammar or punctuation. Do not translate, summarise or paraphrase.
2. Keep the writer's script: Ge'ez stays Ge'ez, English stays English, mixed stays mixed.
   Pay close attention to Fidel vowel orders (e.g. ለ ሉ ሊ ላ ሌ ል ሎ) and look-alike characters
   (ሀ/ሐ/ኀ, ሰ/ሠ, አ/ዐ, ጸ/ፀ, ቸ/ቼ, ዉ/ው); transcribe the form actually written.
3. Wrap any word you are not sure about as [?word?]. Use [...] for spans you cannot read at all.
4. Omit text that is clearly crossed out.
5. Map each answer to its question using the printed question text/number on the sheet and the layout.
   If an answer continues onto the margin or another area, join it. If you cannot tell which question
   some text belongs to, put it in `unmapped_text` instead of guessing.
6. Return exactly one entry per question id listed below, even if blank (is_blank=true, text="").
7. `confidence` must be calibrated: 0.95+ only when every character is clearly legible;
   lower it for every uncertain word. `makes_sense` = false if the text reads as gibberish,
   looks like a mis-read, or does not plausibly answer that question.
"""


def build_prompt() -> str:
    return f"The questionnaire has these questions:\n{_question_block(questions())}\n\nTranscribe the sheet in the image."


_client: genai.Client | None = None


def client() -> genai.Client:
    global _client
    if _client is None:
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not set (see .env.example)")
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=60), reraise=True)
async def read_sheet(jpeg: bytes, model: str) -> tuple[SheetRead, str]:
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
        ),
    )
    raw = resp.text or ""
    return SheetRead.model_validate_json(raw), raw


TRANSLIT_SYSTEM = """Convert Romanized (Latin-letter) Amharic into Amharic Ge'ez script (ፊደል).
Keep English words, names and numbers that are clearly English as-is. Keep punctuation.
Do not translate, correct or rephrase. Output only the converted text."""


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=2, min=2, max=60), reraise=True)
async def transliterate(text: str) -> str:
    resp = await client().aio.models.generate_content(
        model=settings.model,
        contents=text,
        config=types.GenerateContentConfig(system_instruction=TRANSLIT_SYSTEM, temperature=0.0),
    )
    return (resp.text or "").strip()
