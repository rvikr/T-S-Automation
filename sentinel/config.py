from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency installed in normal setup
    def load_dotenv(*args, **kwargs):
        return False


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DATA_DIR = BASE_DIR / "data"
SYNTHETIC_CASES_DIR = DATA_DIR / "synthetic_cases"
UPLOADS_DIR = DATA_DIR / "uploads"
QUARANTINE_DIR = DATA_DIR / "quarantine"
DB_DIR = BASE_DIR / "db"
DEFAULT_DB_PATH = DB_DIR / "audit.sqlite"
POLICY_DIR = BASE_DIR / "policy"
POLICY_CORPUS_PATH = POLICY_DIR / "corpus.md"
SEVERITY_TIERS_PATH = POLICY_DIR / "severity_tiers.yaml"


@dataclass(frozen=True)
class Settings:
    openai_api_key_present: bool
    specialist_model: str
    senior_model: str
    production_model: str
    transcribe_model: str
    db_path: Path


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env.local")
    load_dotenv(PROJECT_ROOT / ".env")
    return Settings(
        openai_api_key_present=bool(os.getenv("OPENAI_API_KEY")),
        specialist_model=os.getenv("SENTINEL_SPECIALIST_MODEL", "configured-at-runtime"),
        senior_model=os.getenv("SENTINEL_SENIOR_MODEL", "configured-at-runtime"),
        production_model=os.getenv("SENTINEL_PRODUCTION_MODEL", "gpt-4o-mini"),
        transcribe_model=os.getenv("SENTINEL_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"),
        db_path=Path(os.getenv("SENTINEL_DB_PATH", DEFAULT_DB_PATH)),
    )
