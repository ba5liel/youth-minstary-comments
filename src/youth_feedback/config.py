"""Settings loaded from environment / .env, plus the questionnaire."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(os.getenv("PROJECT_ROOT", Path.cwd()))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _ids(name: str) -> set[int]:
    raw = os.getenv(name, "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


@dataclass(frozen=True)
class Settings:
    db_path: Path = ROOT / os.getenv("DB_PATH", "data/feedback.db")
    images_dir: Path = ROOT / os.getenv("IMAGES_DIR", "images")
    questions_path: Path = ROOT / os.getenv("QUESTIONS_PATH", "questions.yaml")

    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    # Primary reader: use the strongest vision model you have access to.
    model: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
    # Independent second reader; disagreement between the two sends an answer to review.
    # Set to "" to disable the second read (cheaper, less accurate triage).
    verify_model: str = os.getenv("VERIFY_MODEL", "gemini-3.1-flash-lite")
    concurrency: int = int(os.getenv("CONCURRENCY", "4"))

    # Triage thresholds. Anything below goes to manual review.
    min_confidence: float = _float("MIN_CONFIDENCE", 0.85)
    min_agreement: float = _float("MIN_AGREEMENT", 0.85)
    min_legibility: float = _float("MIN_LEGIBILITY", 0.6)
    # Fraction of fully-automatic sheets randomly sent to review anyway,
    # so you can measure how accurate the automatic results really are.
    spot_check_rate: float = _float("SPOT_CHECK_RATE", 0.05)

    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    admin_ids: set[int] = field(default_factory=lambda: _ids("ADMIN_IDS"))
    claim_minutes: int = int(os.getenv("CLAIM_MINUTES", "30"))


settings = Settings()


@dataclass(frozen=True)
class Question:
    id: str
    number: int
    text_am: str
    text_en: str = ""
    description: str = ""

    @property
    def label(self) -> str:
        return self.text_am or self.text_en


@cache
def questions() -> list[Question]:
    data = yaml.safe_load(settings.questions_path.read_text(encoding="utf-8"))
    qs = [Question(**{k: (v or "") if k != "number" else v for k, v in q.items()}) for q in data["questions"]]
    ids = [q.id for q in qs]
    if len(set(ids)) != len(ids):
        raise ValueError(f"Duplicate question ids in {settings.questions_path}")
    return qs


def question_map() -> dict[str, Question]:
    return {q.id: q for q in questions()}
