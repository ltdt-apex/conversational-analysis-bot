"""Offline preparation pipeline.

These modules run once via ``preprocess/prepare_data.py`` to populate the
SQLite + ChromaDB stores that the online serving path (``backend.api``,
``backend.agent``, ``backend.tools``) reads from.

  text.py        — 2-pass prefix cleaner (terminator truncation + wordfreq filter)
  taxonomy.py    — slot extraction templates + LLM grouping into 15 categories
  classifier.py  — per-distinct-prefix Haiku classifier (11 fields per turn)
  rollups.py     — conversation + agent aggregation tables
  embed.py       — ChromaDB seeding with multilingual cosine embeddings

Shared infrastructure (``backend.config``, ``backend.db``,
``backend.schemas``) stays at the top of the ``backend`` package because
both the offline pipeline and the online serving path use it.
"""
