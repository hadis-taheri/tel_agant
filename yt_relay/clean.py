"""Deterministic (no-LLM) cleanup of a raw YouTube video description.

Spec section 5: raw YouTube descriptions are usually full of ads, links,
discount codes, and timestamps -- none of that belongs in front of the LLM,
both to keep the caption call cheap and to keep promotional filler out of
the generated Persian text (the same lesson this repo's podcast summarizer
already learned the hard way -- see CLAUDE.md's note on skipped
promotional/administrative filler).
"""
import re

MAX_CLEANED_LENGTH = 800
WEAK_SOURCE_MIN_LENGTH = 80

_URL_RE = re.compile(r"https?://\S+")
_TIMESTAMP_LINE_RE = re.compile(r"^\s*\d{1,2}:\d{2}(:\d{2})?\s.*$", re.MULTILINE)
_PROMO_KEYWORDS = (
    "sponsor",
    "promo code",
    "use code",
    "discount",
    "affiliate",
    "patreon",
    "discord.gg",
    "subscribe",
    "newsletter",
    "merch",
)
_SOCIAL_PREFIX_RE = re.compile(
    r"^\s*(twitter|instagram|x|tiktok|facebook|linkedin|youtube|telegram)\s*:.*$",
    re.IGNORECASE | re.MULTILINE,
)
# A line made up entirely of hashtag tokens (#word) and/or emoji -- e.g.
# "#ai #tech" or "🚀🔥". The token itself is #\w+ (not a bare '#'), since a
# real hashtag always has text after the '#'.
_HASHTAG_OR_EMOJI_TOKEN = r"(?:#\w+|[\U0001F300-\U0001FAFF☀-➿])"
_HASHTAG_ONLY_RE = re.compile(
    rf"^\s*{_HASHTAG_OR_EMOJI_TOKEN}(?:\s+{_HASHTAG_OR_EMOJI_TOKEN})*\s*$", re.MULTILINE
)
_SENTENCE_END_RE = re.compile(r"[.!?؟۔]")


def _drop_promo_lines(text: str) -> str:
    lowered_keywords = _PROMO_KEYWORDS
    kept = []
    for line in text.split("\n"):
        lowered = line.lower()
        if any(kw in lowered for kw in lowered_keywords):
            continue
        kept.append(line)
    return "\n".join(kept)


def _collapse_whitespace(text: str) -> str:
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _truncate_at_sentence(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    window = text[:limit]
    matches = list(_SENTENCE_END_RE.finditer(window))
    if matches:
        cut = matches[-1].end()
        if cut >= limit * 0.4:
            return window[:cut].strip()
    return window.strip()


def clean_description(raw: str) -> str:
    """Apply the seven deterministic cleanup steps from spec section 5, in
    order: strip URLs, strip timestamp lines, strip promotional-keyword
    lines, strip hashtag/emoji-only lines, strip social-media link blocks,
    collapse whitespace, then truncate at a sentence boundary (max
    MAX_CLEANED_LENGTH chars)."""
    if not raw:
        return ""

    text = _URL_RE.sub("", raw)
    text = _TIMESTAMP_LINE_RE.sub("", text)
    text = _drop_promo_lines(text)
    text = _HASHTAG_ONLY_RE.sub("", text)
    text = _SOCIAL_PREFIX_RE.sub("", text)
    text = _collapse_whitespace(text)
    text = _truncate_at_sentence(text, MAX_CLEANED_LENGTH)
    return text


def is_weak_source(cleaned: str) -> bool:
    """First gate (spec section 5): if cleanup leaves too little to work
    with, the caption step should fall back to title-only input and flag
    the result as weak_source rather than let the LLM invent content."""
    return len(cleaned) < WEAK_SOURCE_MIN_LENGTH
