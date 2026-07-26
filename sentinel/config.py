from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency installed in normal setup
    def load_dotenv(*args, **kwargs):
        return False


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Set SENTINEL_LOG_LEVEL=DEBUG/INFO/WARNING/ERROR to control verbosity.
# Defaults to WARNING so production deployments stay quiet unless something
# breaks, while developers can opt in to verbose output.
logging.basicConfig(
    level=os.getenv("SENTINEL_LOG_LEVEL", "WARNING").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DATA_DIR = BASE_DIR / "data"
SYNTHETIC_CASES_DIR = DATA_DIR / "synthetic_cases"
DEMO_SAMPLES_DIR = DATA_DIR / "demo_samples"
UPLOADS_DIR = DATA_DIR / "uploads"
QUARANTINE_DIR = DATA_DIR / "quarantine"
DB_DIR = BASE_DIR / "db"
DEFAULT_DB_PATH = DB_DIR / "audit.sqlite"
EVAL_RUNS_DIR = BASE_DIR / "eval_runs"
POLICY_DIR = BASE_DIR / "policy"
POLICY_CORPUS_PATH = POLICY_DIR / "corpus.md"

# ---------------------------------------------------------------------------
# Tunable runtime constants (override via environment variables)
# ---------------------------------------------------------------------------
MAX_TEXT_CHARS: int = int(os.getenv("SENTINEL_MAX_TEXT_CHARS", "12000"))
MAX_AGENT_TURNS: int = int(os.getenv("SENTINEL_MAX_AGENT_TURNS", "10"))
MAX_VIDEO_FRAMES: int = int(os.getenv("SENTINEL_MAX_VIDEO_FRAMES", "4"))
AGENT_TEMPERATURE: float = float(os.getenv("SENTINEL_AGENT_TEMPERATURE", "0.1"))
JIRA_REQUEST_TIMEOUT: int = int(os.getenv("SENTINEL_JIRA_TIMEOUT", "10"))

# Bounds an individual OpenAI call so a stalled request cannot pin a FastAPI
# threadpool worker indefinitely. MAX_AGENT_TURNS caps runaway tool loops; this
# caps network stalls, which is a different failure.
LLM_TIMEOUT_SECONDS: float = float(os.getenv("SENTINEL_LLM_TIMEOUT", "60"))
LLM_MAX_RETRIES: int = int(os.getenv("SENTINEL_LLM_MAX_RETRIES", "2"))

# Ceiling on a decoded upload before it is written to disk. Without this, a
# caller can exhaust disk while paying for only a small (truncated) LLM call.
MAX_UPLOAD_BYTES: int = int(os.getenv("SENTINEL_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))

# Seconds SQLite waits on a locked database before raising.
SQLITE_BUSY_TIMEOUT: float = float(os.getenv("SENTINEL_SQLITE_BUSY_TIMEOUT", "30"))


@dataclass(frozen=True)
class Settings:
    openai_api_key_present: bool
    specialist_model: str
    senior_model: str
    production_model: str
    transcribe_model: str
    embed_model: str
    db_path: Path


# Searched in order; the first file to define a variable wins, since
# python-dotenv does not overwrite an already-set key. The repo root is the
# documented location, but `.env.example` ships inside `sentinel/`, so copying
# it in place — the obvious move — used to produce a file that was silently
# never read. Both locations are honoured rather than making that a support
# question.
ENV_FILE_CANDIDATES = (
    PROJECT_ROOT / ".env.local",
    PROJECT_ROOT / ".env",
    BASE_DIR / ".env.local",
    BASE_DIR / ".env",
)


def load_settings() -> Settings:
    for env_file in ENV_FILE_CANDIDATES:
        load_dotenv(env_file)
    return Settings(
        openai_api_key_present=bool(os.getenv("OPENAI_API_KEY")),
        specialist_model=os.getenv("SENTINEL_SPECIALIST_MODEL", "gpt-4o-mini"),
        senior_model=os.getenv("SENTINEL_SENIOR_MODEL", "gpt-4o"),
        production_model=os.getenv("SENTINEL_PRODUCTION_MODEL", "gpt-4o-mini"),
        transcribe_model=os.getenv("SENTINEL_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"),
        embed_model=os.getenv("SENTINEL_EMBED_MODEL", "text-embedding-3-small"),
        db_path=Path(os.getenv("SENTINEL_DB_PATH", DEFAULT_DB_PATH)),
    )
