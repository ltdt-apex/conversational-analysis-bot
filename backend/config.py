"""Centralised config loaded from .env. Never hardcode secrets — always read here."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    planner_model: str
    classifier_model: str
    data_dir: Path
    sqlite_path: Path
    chroma_path: Path
    raw_csv_path: Path
    classify_batch_size: int
    classify_concurrency: int
    memory_ttl_hours: int
    embedding_model: str

    @classmethod
    def load(cls) -> "Config":
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        data_dir = Path(os.getenv("DATA_DIR", REPO_ROOT / "data")).resolve()
        return cls(
            anthropic_api_key=key,
            planner_model=os.getenv("ANTHROPIC_PLANNER_MODEL", "claude-sonnet-4-6"),
            classifier_model=os.getenv(
                "ANTHROPIC_CLASSIFIER_MODEL", "claude-haiku-4-5-20251001"
            ),
            data_dir=data_dir,
            sqlite_path=Path(os.getenv("SQLITE_PATH", data_dir / "processed.db")).resolve(),
            chroma_path=Path(os.getenv("CHROMA_PATH", data_dir / "chroma")).resolve(),
            raw_csv_path=Path(
                os.getenv("RAW_CSV_PATH", data_dir / "cs_conversations.csv")
            ).resolve(),
            # Conservative defaults so we play nice with low-tier Anthropic
            # rate limits (~10K output tok/min on free tier). Increase
            # via .env if you have higher-tier limits.
            classify_batch_size=int(os.getenv("CLASSIFY_BATCH_SIZE", "20")),
            classify_concurrency=int(os.getenv("CLASSIFY_CONCURRENCY", "1")),
            memory_ttl_hours=int(os.getenv("MEMORY_TTL_HOURS", "24")),
            # Multilingual by default — the dataset contains English + romanized
            # Hindi/Hinglish turns. See CLAUDE.md "Bilingual data handling".
            embedding_model=os.getenv(
                "EMBEDDING_MODEL",
                "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            ),
        )

    def require_api_key(self) -> str:
        if not self.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."
            )
        return self.anthropic_api_key
