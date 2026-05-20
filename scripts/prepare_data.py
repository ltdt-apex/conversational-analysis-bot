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
from backend import taxonomy as tax  # noqa: E402

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
def stage_taxonomy(cfg: Config, *, force: bool = False, **_: object) -> None:
    """Derive the closed-set topic taxonomy and persist as YAML.

    Idempotent: if ``data/topic_taxonomy.yaml`` exists and ``--force`` is not
    set, this is a no-op. Re-running classification therefore uses a stable
    label set unless the file is deliberately regenerated.
    """
    out_path = cfg.data_dir / "topic_taxonomy.yaml"
    if out_path.exists() and not force:
        existing = tax.load(out_path)
        print(
            f"[taxonomy] skipping — {out_path} already exists with "
            f"{len(existing.categories)} categories and "
            f"{len(existing.slot_to_category)} slot mappings (use --force to regen)"
        )
        return

    # Pull deterministic slot values from the ingested turns.
    with connect(cfg.sqlite_path) as conn:
        prefixes = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT text_clean FROM turns "
                "WHERE role='customer' AND turn_index=0"
            )
        ]
    if not prefixes:
        raise RuntimeError("[taxonomy] no customer-opening turns in DB — run ingest_raw first")

    slots: set[str] = set()
    unmatched: list[str] = []
    for p in prefixes:
        slot, tpl = tax.extract_slot(p)
        if slot is not None:
            slots.add(slot)
        elif tpl != "<no_slot>":
            unmatched.append(p)
    if unmatched:
        # Don't silently lose prefixes; surface them so the regex bank can be updated.
        print(
            f"[taxonomy] WARNING: {len(unmatched)} opening prefixes did not match any "
            f"template — first few: {unmatched[:3]}"
        )

    print(
        f"[taxonomy] {len(prefixes)} distinct openers → {len(slots)} distinct slot "
        f"values. Asking {cfg.planner_model} to group them…"
    )

    # Lazy import so the script still imports cleanly when anthropic is unused.
    from anthropic import Anthropic

    client = Anthropic(api_key=cfg.require_api_key())
    taxonomy = tax.derive_taxonomy_with_llm(
        sorted(slots),
        client,
        model=cfg.planner_model,
    )
    tax.save(taxonomy, out_path)
    print(
        f"[taxonomy] wrote {out_path} — {len(taxonomy.categories)} categories, "
        f"{len(taxonomy.slot_to_category)} slot mappings"
    )
    print("[taxonomy] categories:")
    for c in taxonomy.categories:
        n_slots = sum(1 for v in taxonomy.slot_to_category.values() if v == c.id)
        print(f"  - {c.id:30s} ({n_slots:2d} slots)  {c.label}")


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
