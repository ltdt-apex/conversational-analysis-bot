"""Offline preparation pipeline.

Runs in stages; each stage is idempotent and skips work that's already done so
the script is safe to re-run.

Stages:
  1. ingest_raw   — read CSV, clean prefixes, populate `turns` table
  2. taxonomy     — derive ~20-label closed-set topic taxonomy (LLM, one-shot)
  3. classify     — per-turn classification (Haiku, batched, resumable)
  4. rollups      — build `conversations` and `agents` tables from `turns`
  5. embed        — seed ChromaDB with one doc per conversation

Usage:
    uv run python scripts/prepare_data.py            # run all stages
    uv run python scripts/prepare_data.py --stage ingest_raw
    uv run python scripts/prepare_data.py --stage classify --limit 200
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

# Allow running as a script: `python scripts/prepare_data.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import Config  # noqa: E402
from backend.db import connect, init_schema, transaction  # noqa: E402
from backend.text import clean_prefix  # noqa: E402

STAGES = ["ingest_raw", "taxonomy", "classify", "rollups", "embed"]


# ---------------------------------------------------------------------------
# Stage 1 — ingest_raw
# ---------------------------------------------------------------------------
def stage_ingest_raw(cfg: Config, *, force: bool = False, **_: object) -> None:
    """Read the raw CSV, clean prefixes, populate the `turns` table.

    Idempotent: if `turns` already has the expected row count and `force` is
    False, we skip the load. Run with `--force` to truncate and re-ingest.
    """
    if not cfg.raw_csv_path.exists():
        raise FileNotFoundError(
            f"Raw dataset not found at {cfg.raw_csv_path}. "
            "Download cs_conversations.csv and place it there. See README."
        )

    with connect(cfg.sqlite_path) as conn:
        init_schema(conn)
        existing = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        if existing > 0 and not force:
            print(f"[ingest_raw] skipping — {existing:,} rows already in turns table")
            return

        if force:
            print("[ingest_raw] --force: truncating turns table")
            conn.execute("DELETE FROM turns")

        print(f"[ingest_raw] reading {cfg.raw_csv_path}")
        with cfg.raw_csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            batch: list[tuple] = []
            total = 0
            empty_prefix = 0
            for row in reader:
                clean = clean_prefix(row["text"])
                if not clean:
                    empty_prefix += 1
                batch.append((
                    row["conv_id"],
                    int(row["turn_index"]),
                    row["timestamp"],
                    row["role"],
                    row["text"],
                    clean,
                    row["customer_name"] or None,
                    row["agent_name"] or None,
                ))
                if len(batch) >= 5000:
                    _flush_turns(conn, batch)
                    total += len(batch)
                    batch.clear()
                    print(f"  ingested {total:,} rows…", end="\r")
            if batch:
                _flush_turns(conn, batch)
                total += len(batch)

        print(f"[ingest_raw] done — {total:,} rows ingested, {empty_prefix:,} had empty cleaned prefix")


def _flush_turns(conn, batch: list[tuple]) -> None:
    with transaction(conn):
        conn.executemany(
            """INSERT OR REPLACE INTO turns
               (conv_id, turn_index, timestamp, role, text_raw, text_clean,
                customer_name, agent_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            batch,
        )


# ---------------------------------------------------------------------------
# Later stages (stubs — filled in by follow-up tasks)
# ---------------------------------------------------------------------------
def stage_taxonomy(cfg: Config, **_: object) -> None:
    print("[taxonomy] not yet implemented — task #3")


def stage_classify(cfg: Config, *, limit: int | None = None, **_: object) -> None:
    print("[classify] not yet implemented — task #4")


def stage_rollups(cfg: Config, **_: object) -> None:
    print("[rollups] not yet implemented — task #5")


def stage_embed(cfg: Config, **_: object) -> None:
    print("[embed] not yet implemented — task #6")


STAGE_FNS = {
    "ingest_raw": stage_ingest_raw,
    "taxonomy": stage_taxonomy,
    "classify": stage_classify,
    "rollups": stage_rollups,
    "embed": stage_embed,
}


def main() -> None:
    ap = argparse.ArgumentParser(description="Offline preparation pipeline")
    ap.add_argument(
        "--stage",
        choices=STAGES + ["all"],
        default="all",
        help="Which stage to run (default: all stages in order)",
    )
    ap.add_argument("--force", action="store_true", help="Re-run even if outputs exist")
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="(classify) limit number of turns processed — for smoke-testing",
    )
    args = ap.parse_args()

    cfg = Config.load()
    stages = STAGES if args.stage == "all" else [args.stage]
    for stage in stages:
        STAGE_FNS[stage](cfg, force=args.force, limit=args.limit)


if __name__ == "__main__":
    main()
