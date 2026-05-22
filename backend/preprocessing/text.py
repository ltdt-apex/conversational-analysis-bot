"""Text-cleaning utilities for the synthetic Syncora.ai dataset.

The raw turn text is shaped as:
    <coherent leading sentence(s)> <long run of random gibberish tokens>

Cleaning runs in two passes:

1. ``clean_prefix``           — truncate at the **last** sentence terminator
                                (``.`` / ``!`` / ``?``). Empirically the
                                synthetic gibberish never contains a
                                terminator, so this strips ~100% of the tail
                                noise.

2. ``strip_gibberish_tokens`` — defence in depth. Tokenise the candidate
                                prefix and drop any token that scores below a
                                Zipf-frequency threshold in **both** English
                                and Hindi (the dataset is bilingual /
                                Hinglish) AND is not on a domain allowlist of
                                known proper-noun terms (product names like
                                ``FASTag``, ``Teleconsult``, ``eSIM`` …).

The second pass mostly no-ops on the current dataset (verified scan: zero
unaccounted gibberish tokens in 426 distinct prefixes). It exists so the
pipeline stays robust if a future data load introduces in-prefix noise.
"""
from __future__ import annotations

import re
from functools import lru_cache

from wordfreq import zipf_frequency

# Sentence-terminator regex.
_TERMINATOR_RE = re.compile(r"[.!?]")

# Word token: letters + apostrophes (incl. curly), slashes, hyphens — preserves
# things like ``rahi/raha``, ``I'm``, ``isn't``. Numbers (``4G``, ``5G``) are
# captured by the broader token regex below.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’/-]*")
_NUMERIC_TOKEN_RE = re.compile(r"^[0-9]+[A-Za-z]*$")  # 2, 3, 4G, 5G …

# Domain-specific allowlist: real terms missing from wordfreq's word lists.
# Add to this list when scans surface new false positives.
DOMAIN_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Product / service names
        "fastag",
        "teleconsult",
        "esim",
        "upi",
        "sso",
        "webhooks",
        "broadband",
        "roaming",
        "porting",
        "wallet",
        "voucher",
        "coupon",
        # Hindi/Hinglish words sometimes missing from the wordfreq `hi` corpus
        "baare",
        "mein",
        "mujhe",
        "thoda",
        "jaldi",
        "abhi",
        "rahi",
        "raha",
        "hoon",
        "chahiye",
        "kindly",  # already common in en, but cheap to allowlist
    }
)

ZIPF_THRESHOLD: float = 1.0  # ``zipf_frequency < 1.0`` ≈ "appears < 1 / 10⁸ words"


@lru_cache(maxsize=10_000)
def _token_zipf(token: str) -> float:
    """Max Zipf frequency across English and Hindi for ``token``."""
    t = token.lower()
    return max(zipf_frequency(t, "en"), zipf_frequency(t, "hi"))


def looks_gibberish(token: str, threshold: float = ZIPF_THRESHOLD) -> bool:
    """Return True iff the token is not a known English/Hindi word and not on the allowlist."""
    t = token.lower()
    if t in DOMAIN_ALLOWLIST:
        return False
    if _NUMERIC_TOKEN_RE.match(token):  # 4G, 5G, plain numbers
        return False
    return _token_zipf(token) < threshold


def gibberish_token_ratio(text: str) -> float:
    """Fraction of word-tokens in ``text`` that look gibberish.

    Returns 0.0 for empty / non-word text.
    """
    tokens = _WORD_RE.findall(text)
    if not tokens:
        return 0.0
    bad = sum(1 for t in tokens if looks_gibberish(t))
    return bad / len(tokens)


def strip_gibberish_tokens(text: str) -> str:
    """Remove gibberish word-tokens while preserving surrounding punctuation/whitespace.

    Only word-tokens are affected; punctuation, numbers, and allowlisted words
    are left untouched. Multiple resulting spaces are collapsed.
    """

    def _replace(match: re.Match[str]) -> str:
        tok = match.group(0)
        return "" if looks_gibberish(tok) else tok

    out = _WORD_RE.sub(_replace, text)
    # Collapse runs of whitespace and stray spaces before punctuation.
    out = re.sub(r"\s+", " ", out).strip()
    out = re.sub(r"\s+([.,!?;:])", r"\1", out)
    return out


def clean_prefix(raw: str) -> str:
    """Return the signal-bearing prefix of a turn's text.

    Pass 1: truncate at the last sentence terminator (kills the synthetic tail).
    Pass 2: strip any residual gibberish tokens via wordfreq + allowlist.

    Returns an empty string if no terminator is present (caller decides what
    to do with empty prefixes).
    """
    if not raw:
        return ""
    matches = list(_TERMINATOR_RE.finditer(raw))
    if not matches:
        return ""
    end = matches[-1].end()
    prefix = raw[:end].strip()
    return strip_gibberish_tokens(prefix)
