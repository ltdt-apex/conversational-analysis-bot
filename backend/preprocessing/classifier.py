"""Per-prefix turn classifier (Claude Haiku, batched, retried).

Each distinct cleaned prefix in ``turns`` is classified exactly once; results
are written to ``prefix_classifications`` and then JOIN-back-copied onto the
``turns`` table by :func:`scripts.prepare_data.stage_classify`.

Schema returned by the LLM (per item):
    sentiment_label  : 'pos' | 'neu' | 'neg'
    sentiment_score  : float in [-1.0, 1.0]
    intent           : one of INTENT_LABELS
    empathy_signal   : 'empathetic' | 'neutral' | 'dismissive' | 'na'
                       (assessed for role=agent only; 'na' for customers)
    is_escalation    : 0 | 1
    contains_pii     : 0 | 1 (REAL PII only — placeholders like "registered
                              email" do NOT count)
    language         : 'en' | 'hi' | 'mixed'
    text_en          : faithful English translation (= text_clean for English)

The dataset is bilingual (English + romanized Hindi/Hinglish). Haiku handles
both natively — see CLAUDE.md "Bilingual data handling" for the rationale on
why we classify the original text rather than pre-translating.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone

from anthropic import Anthropic, RateLimitError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from tqdm import tqdm


# Closed sets — kept narrow so downstream SQL filters/aggregations stay sane.
INTENT_LABELS: tuple[str, ...] = (
    "complaint",                 # customer reports a problem
    "query",                     # customer asks a question
    "acknowledgement",           # "okay", "got it", "noted"
    "urgency_request",           # "please expedite", "it's urgent"
    "escalation_request",        # "can you escalate", "speak to manager"
    "resolution_confirmation",   # "it works now", "issue fixed"
    "thanks",                    # gratitude
    "apology",                   # agent expresses regret
    "solution_offer",            # agent provides a resolution
    "status_update",             # agent reports progress / "investigating"
    "info_request",              # agent asks customer for info (email, phone …)
    "other",
)

EMPATHY_LABELS: tuple[str, ...] = ("empathetic", "neutral", "dismissive", "na")
SENTIMENT_LABELS: tuple[str, ...] = ("pos", "neu", "neg")
LANGUAGE_LABELS: tuple[str, ...] = ("en", "hi", "mixed")


@dataclass(frozen=True)
class PrefixInput:
    text_clean: str
    role: str  # 'customer' | 'agent'


@dataclass(frozen=True)
class PrefixClassification:
    text_clean: str
    role: str
    sentiment_label: str
    sentiment_score: float
    intent: str
    empathy_signal: str
    is_escalation: int
    contains_pii: int
    language: str
    text_clean_en: str
    classified_at: str
    model: str


class ClassifierResponseError(ValueError):
    """Raised when the LLM response doesn't conform to the expected schema."""


_USER_PROMPT_TEMPLATE = """Classify each customer-support conversation turn below.

The data is bilingual: turns may be in English or romanized Hindi / Hinglish \
(e.g. "Mujhe Refund ke baare mein help chahiye.", "App crash ho rahi hai while \
using Wallet.", "Thoda jaldi please, flight in 2 hours."). Classify the \
ORIGINAL text without translating first — pre-translation flattens tone.

Items ({n} total):
{items}

Return a JSON array of EXACTLY {n} objects, one per item, in input order. \
Schema for each object:
{{
  "n": <1-indexed item number>,
  "sentiment_label": "pos" | "neu" | "neg",
  "sentiment_score": <float in [-1.0, 1.0]>,
  "intent": one of [{intents}],
  "empathy_signal": "empathetic" | "neutral" | "dismissive" | "na",
  "is_escalation": 0 | 1,
  "contains_pii": 0 | 1,
  "language": "en" | "hi" | "mixed",
  "text_en": "<faithful English translation; equals input text if already English>"
}}

Classification rules:
- sentiment_score: -1 very negative, 0 neutral, +1 very positive. Anchor on \
  customer experience tone; for agent turns use the emotional content the \
  customer would receive.
- intent: pick the SINGLE best-fitting label from the closed set.
- empathy_signal: assess ONLY for role=agent (use "na" for role=customer). \
  "empathetic" = explicit acknowledgement of feelings or concern; "neutral" = \
  matter-of-fact / transactional; "dismissive" = cold or perfunctory.
- is_escalation: 1 only if the turn explicitly requests escalation OR clearly \
  asks for managerial intervention.
- contains_pii: 1 only if the text contains a REAL email/phone/account number/ \
  street address/name. Placeholders like "your registered email" do NOT count. \
  Customer/agent display names in the data ARE NOT in the turn text — ignore them.
- language: assess the original `text` field.
- text_en: concise, faithful translation; preserve politeness/urgency register.

Return ONLY the JSON array — no commentary, no markdown fences."""


def _build_user_prompt(batch: list[PrefixInput]) -> str:
    items = "\n".join(
        f"{i+1}. role={p.role} text={json.dumps(p.text_clean, ensure_ascii=False)}"
        for i, p in enumerate(batch)
    )
    return _USER_PROMPT_TEMPLATE.format(
        n=len(batch),
        items=items,
        intents=", ".join(INTENT_LABELS),
    )


def _strip_fences(text: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()


@retry(
    retry=retry_if_exception_type(
        (ClassifierResponseError, json.JSONDecodeError, RateLimitError)
    ),
    stop=stop_after_attempt(8),
    wait=wait_exponential(min=2, max=60),
    reraise=True,
)
def classify_batch(
    batch: list[PrefixInput],
    client: Anthropic,
    *,
    model: str,
) -> list[PrefixClassification]:
    """Classify a single batch of (text, role) inputs.

    Retries on schema-validation failures up to 3 times with exponential
    backoff. Raises :class:`ClassifierResponseError` if validation still fails.
    """
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": _build_user_prompt(batch)}],
    )
    raw_text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        items = json.loads(_strip_fences(raw_text))
    except json.JSONDecodeError as e:
        raise ClassifierResponseError(f"non-JSON response: {raw_text[:200]}") from e

    if not isinstance(items, list) or len(items) != len(batch):
        raise ClassifierResponseError(
            f"expected list of {len(batch)} items, got {type(items).__name__} of "
            f"length {len(items) if hasattr(items, '__len__') else 'n/a'}"
        )

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results: list[PrefixClassification] = []
    for i, (inp, item) in enumerate(zip(batch, items)):
        if not isinstance(item, dict):
            raise ClassifierResponseError(f"item {i+1} is not an object")
        if item.get("n") != i + 1:
            raise ClassifierResponseError(
                f"item index mismatch at position {i+1}: got n={item.get('n')!r}"
            )
        try:
            results.append(_coerce_item(inp, item, ts=ts, model=model))
        except (KeyError, ValueError) as e:
            raise ClassifierResponseError(f"item {i+1} validation: {e}") from e
    return results


def _coerce_item(
    inp: PrefixInput, item: dict, *, ts: str, model: str
) -> PrefixClassification:
    sentiment_label = str(item["sentiment_label"])
    if sentiment_label not in SENTIMENT_LABELS:
        raise ValueError(f"bad sentiment_label={sentiment_label!r}")
    sentiment_score = float(item["sentiment_score"])
    if not -1.0 <= sentiment_score <= 1.0:
        raise ValueError(f"sentiment_score out of range: {sentiment_score}")
    intent = str(item["intent"])
    if intent not in INTENT_LABELS:
        raise ValueError(f"bad intent={intent!r}")
    empathy = str(item["empathy_signal"])
    if empathy not in EMPATHY_LABELS:
        raise ValueError(f"bad empathy_signal={empathy!r}")
    if inp.role == "customer" and empathy != "na":
        # Don't fail — coerce silently so a single misbehaving Haiku reply
        # doesn't tank the whole batch. Empathy is meaningless for customers.
        empathy = "na"
    is_escalation = int(item["is_escalation"])
    if is_escalation not in (0, 1):
        raise ValueError(f"bad is_escalation={is_escalation}")
    contains_pii = int(item["contains_pii"])
    if contains_pii not in (0, 1):
        raise ValueError(f"bad contains_pii={contains_pii}")
    language = str(item["language"])
    if language not in LANGUAGE_LABELS:
        raise ValueError(f"bad language={language!r}")
    text_en = str(item["text_en"]).strip()
    if not text_en:
        raise ValueError("text_en is empty")

    return PrefixClassification(
        text_clean=inp.text_clean,
        role=inp.role,
        sentiment_label=sentiment_label,
        sentiment_score=sentiment_score,
        intent=intent,
        empathy_signal=empathy,
        is_escalation=is_escalation,
        contains_pii=contains_pii,
        language=language,
        text_clean_en=text_en,
        classified_at=ts,
        model=model,
    )


def classify_all(
    inputs: list[PrefixInput],
    client: Anthropic,
    *,
    model: str,
    batch_size: int = 25,
    concurrency: int = 4,
) -> list[PrefixClassification]:
    """Classify all inputs with bounded concurrency and a progress bar."""
    batches = [inputs[i : i + batch_size] for i in range(0, len(inputs), batch_size)]
    if not batches:
        return []
    results: list[PrefixClassification] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(classify_batch, b, client, model=model): b for b in batches
        }
        with tqdm(total=len(inputs), desc="classify", unit="prefix") as bar:
            for fut in as_completed(futures):
                batch_results = fut.result()
                results.extend(batch_results)
                bar.update(len(batch_results))
    return results
