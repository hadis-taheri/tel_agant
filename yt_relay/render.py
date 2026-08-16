"""Renders a queued video's caption JSON into the final Telegram HTML text.

Format (CLAUDE.md section 5 -- "title + expandable blockquote" style,
chosen over the spec's plain-bullet template): headline, then the summary
and takeaways collapsed into a <blockquote expandable> so the channel stays
scannable, then a channel/link line. No promotional footer.

Produces plain HTML text only -- it has no idea whether the caller will send
it via a link-preview message or a photo caption (see CLAUDE.md section
5.1); that choice lives entirely in telegram_bot.py / publish.py, driven by
YT_POST_STYLE. Kept under TEXT_LENGTH_BUDGET so either delivery path stays
available (Telegram's photo-caption cap is 1024 chars).
"""
import html
import re

TEXT_LENGTH_BUDGET = 900

# Written as an explicit escape, not a literal invisible character -- this
# repo has already been burned once by a copy-pasted invisible bidi
# character silently corrupting a GitHub secret (see CLAUDE.md). Keep this
# one explicit and greppable.
_RLM = "‏"


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _force_rtl_lines(text: str) -> str:
    """Telegram picks a line's base direction from its first strong-directional
    character. A bullet ('•') or a Latin proper noun (common here -- "Claude
    ...", "OpenAI ...") at the start of a line gets misdetected as LTR even
    though the rest of the line is Persian. Prefixing every line with U+200F
    forces RTL regardless of what follows, with no visible change."""
    lines = text.split("\n")
    return "\n".join(line if not line or line.startswith(_RLM) else _RLM + line for line in lines)


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


_TAG_INVALID_RE = re.compile(r"[^\w؀-ۿ]+", re.UNICODE)


def _sanitize_tag(tag: str) -> str:
    """A Telegram hashtag ends at the first character that isn't a word
    character, so a multi-word LLM tag like "Alexandr Wang" would render as
    the hashtag "#Alexandr" followed by plain, unlinked text " Wang". Collapse
    internal whitespace/punctuation into underscores so the whole tag stays
    one clickable hashtag (e.g. "Scale AI" -> "Scale_AI")."""
    return _TAG_INVALID_RE.sub("_", tag.strip()).strip("_")


def render_post(
    headline: str,
    summary: str,
    takeaways: list[str],
    tags: list[str],
    channel_title: str,
    video_id: str,
) -> str:
    bullet_lines = "\n".join(f"• {_esc(t)}" for t in takeaways)
    sanitized_tags = [_sanitize_tag(t) for t in tags]
    tag_line = " ".join(f"#{_esc(t)}" for t in sanitized_tags if t)
    url = video_url(video_id)

    quote_body = _force_rtl_lines(f"{_esc(summary)}\n\n{bullet_lines}")

    parts = [
        f"🎥 <b>{_RLM}{_esc(headline)}</b>",
        "",
        f"<blockquote expandable>{quote_body}</blockquote>",
        "",
        f"{_RLM}📺 {_esc(channel_title)} · 🔗 <a href=\"{url}\">تماشای ویدیو</a>",
    ]
    if tag_line:
        parts += ["", _RLM + tag_line]

    text = "\n".join(parts)

    if len(text) > TEXT_LENGTH_BUDGET:
        text = _fit_to_budget(text, TEXT_LENGTH_BUDGET)

    return text


def _fit_to_budget(text: str, limit: int) -> str:
    """Trim on a paragraph boundary if the model ignored the prompt's length
    limits, so the post still fits the photo-caption budget."""
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut_at = window.rfind("\n\n")
    if cut_at == -1 or cut_at < limit * 0.4:
        cut_at = limit
    trimmed = window[:cut_at].rstrip()
    return _close_open_blockquote(trimmed)


def _close_open_blockquote(text: str) -> str:
    opens = len(re.findall(r"<blockquote[^>]*>", text))
    closes = text.count("</blockquote>")
    if opens > closes:
        text += "</blockquote>"
    return text
