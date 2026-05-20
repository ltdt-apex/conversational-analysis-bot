"""Topic taxonomy derivation and slot extraction.

Customer opening turns follow a closed set of 8 templates with a single
product/service slot, plus 1 no-slot login template — verified across all 408
distinct customer-opening prefixes in the dataset:

    "Hello, my {X} is not working as expected."
    "Hi, I need help with my {X}."
    "App crash ho rahi hai while using {X}."
    "Coupon isn't applying on checkout for the {X}."
    "Mujhe {X} ke baare mein help chahiye."
    "I was charged twice for my {X}."
    "UPI shows successful but I didn't get the service for {X}."
    "The {X} hasn't arrived yet. Can you check?"
    "I can't log in. It says account locked."     ← no slot → auth_login

This module:
  - extracts the slot value deterministically (no LLM needed for the slot)
  - asks Claude Sonnet to group the deduped slot values into ~15-20 canonical
    categories (one LLM call, ~$0.04)
  - exposes the resulting taxonomy as a stable YAML artifact
    (``data/topic_taxonomy.yaml``) that is committed to the repo and treated
    as a frozen constant for reproducibility.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from anthropic import Anthropic

# -- Deterministic slot extraction ------------------------------------------

# (template_id, regex). Each regex captures the topic slot as group 1.
# Apostrophes in the templates may be ASCII (') or curly (’) — we accept both.
#
# NOTE (future improvement, tracked in CLAUDE.md "Future improvements"):
# These regexes are fitted to the synthetic Syncora.ai templates we observed
# in `cs_conversations.csv` (8 templates × 51 slot values + 1 no-slot
# template, covering 100% of the 408 distinct customer openings). On real
# data with free-form openings this would not generalise. The intended
# replacement is LLM-based slot extraction (Claude identifies the topic per
# prefix) or embedding-cluster-then-label, both of which can be plugged in
# behind ``extract_slot`` without touching the rest of the pipeline.
SLOT_TEMPLATES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("en_not_working",    re.compile(r"^Hello, my (.+) is not working as expected\.$")),
    ("en_need_help",      re.compile(r"^Hi, I need help with my (.+)\.$")),
    ("hi_app_crash",      re.compile(r"^App crash ho rahi hai while using (.+)\.$")),
    ("en_coupon",         re.compile(r"^Coupon isn.?t applying on checkout for the (.+)\.$")),
    ("hi_mujhe_help",     re.compile(r"^Mujhe (.+) ke baare mein help chahiye\.$")),
    ("en_charged_twice",  re.compile(r"^I was charged twice for my (.+)\.$")),
    ("en_upi_service",    re.compile(r"^UPI shows successful but I didn.?t get the service for (.+)\.$")),
    ("en_not_arrived",    re.compile(r"^The (.+) hasn.?t arrived yet\. Can you check\?$")),
)

# No-slot templates and the category they map to directly.
NO_SLOT_TEMPLATES: dict[str, str] = {
    "I can’t log in. It says account locked.": "auth_login",
    "I can't log in. It says account locked.": "auth_login",
}


def extract_slot(prefix: str) -> tuple[str | None, str | None]:
    """Return ``(slot_value, template_id)`` for an opening-turn prefix.

    For no-slot templates the returned slot is ``None`` and the template_id is
    ``"<no_slot>"``. Returns ``(None, None)`` for unrecognised prefixes.
    """
    if prefix in NO_SLOT_TEMPLATES:
        return None, "<no_slot>"
    for tpl_id, pat in SLOT_TEMPLATES:
        m = pat.match(prefix)
        if m:
            return m.group(1).strip(), tpl_id
    return None, None


# -- LLM-driven category grouping -------------------------------------------


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    description: str


@dataclass(frozen=True)
class Taxonomy:
    categories: tuple[Category, ...]
    slot_to_category: dict[str, str]
    no_slot_template_categories: dict[str, str] = field(default_factory=dict)

    def topic_for(self, prefix: str) -> str | None:
        """Resolve a customer opening prefix to a category id."""
        slot, tpl_id = extract_slot(prefix)
        if tpl_id == "<no_slot>":
            return NO_SLOT_TEMPLATES.get(prefix)
        if slot is None:
            return None
        return self.slot_to_category.get(slot)

    def to_yaml(self) -> str:
        data = {
            "categories": [
                {"id": c.id, "label": c.label, "description": c.description}
                for c in self.categories
            ],
            "slot_to_category": dict(sorted(self.slot_to_category.items())),
            "no_slot_template_categories": dict(
                sorted(self.no_slot_template_categories.items())
            ),
        }
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, text: str) -> "Taxonomy":
        data = yaml.safe_load(text)
        cats = tuple(
            Category(id=c["id"], label=c["label"], description=c["description"])
            for c in data["categories"]
        )
        return cls(
            categories=cats,
            slot_to_category=dict(data["slot_to_category"]),
            no_slot_template_categories=dict(
                data.get("no_slot_template_categories", {})
            ),
        )


_SYSTEM_PROMPT = """You are a taxonomist for customer-support analytics. \
Your job is to group a list of product/service names into a small set of \
canonical topic categories that contact-centre leadership would use to \
review recurring customer concerns.

Rules:
- Produce between 12 and 18 categories. Fewer is better as long as each \
  category remains analytically useful.
- Each category id must be lower_snake_case and stable (will be used as a \
  database key).
- Every input slot value must map to exactly one category.
- Group by the customer's problem domain (e.g. "Telecom & Connectivity", \
  "Payments & Wallet", "E-commerce Orders") — not by sentiment or severity.
- Treat ambiguous slots (e.g. "API", "Report") by their most likely \
  customer-support context (these look like B2B/SaaS tickets, so group them \
  under a developer/platform category).
- Return ONLY valid JSON matching the schema given below — no commentary."""


_USER_PROMPT_TEMPLATE = """Group these {n_slots} product/service names into canonical topic categories.

Slot values:
{slot_list}

Additionally, there is one no-slot template that you should also include in the \
taxonomy mapping:
- "{no_slot_prefix}" → must map to a category about login / authentication issues.

Return JSON in this exact schema:
{{
  "categories": [
    {{"id": "<snake_case_id>", "label": "<2-4 word human label>", "description": "<one sentence>"}}
  ],
  "slot_to_category": {{
    "<slot_value>": "<category_id>"
  }},
  "no_slot_template_categories": {{
    "{no_slot_prefix}": "<category_id>"
  }}
}}"""


def derive_taxonomy_with_llm(
    slot_values: list[str],
    client: Anthropic,
    *,
    model: str,
    no_slot_prefix: str = "I can’t log in. It says account locked.",
) -> Taxonomy:
    """Call Claude to group ``slot_values`` into canonical categories."""
    slot_list = "\n".join(f"- {s}" for s in sorted(slot_values))
    user_prompt = _USER_PROMPT_TEMPLATE.format(
        n_slots=len(slot_values),
        slot_list=slot_list,
        no_slot_prefix=no_slot_prefix,
    )
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    # Single text block expected
    text = "".join(b.text for b in resp.content if b.type == "text").strip()

    # Strip optional ```json fences if the model added them despite instructions.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()

    data = json.loads(text)

    # Strict validation: every input slot must appear in the mapping.
    missing = set(slot_values) - set(data["slot_to_category"].keys())
    if missing:
        raise ValueError(
            f"LLM omitted {len(missing)} slot values from slot_to_category: "
            f"{sorted(missing)[:5]}…"
        )
    category_ids = {c["id"] for c in data["categories"]}
    bad = {v for v in data["slot_to_category"].values() if v not in category_ids}
    if bad:
        raise ValueError(
            f"LLM assigned slots to non-existent category ids: {sorted(bad)}"
        )
    return Taxonomy(
        categories=tuple(
            Category(id=c["id"], label=c["label"], description=c["description"])
            for c in data["categories"]
        ),
        slot_to_category=dict(data["slot_to_category"]),
        no_slot_template_categories=dict(data.get("no_slot_template_categories", {})),
    )


# -- File I/O ----------------------------------------------------------------


def save(taxonomy: Taxonomy, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(taxonomy.to_yaml(), encoding="utf-8")


def load(path: Path) -> Taxonomy:
    return Taxonomy.from_yaml(path.read_text(encoding="utf-8"))
