"""Publishes the generated Persian summary to a Telegram channel as plain text."""
import asyncio
import logging
from typing import List, Optional

from telegram import Bot, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, TelegramError

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4096
# summarizer.py already targets and enforces a single-message summary (see
# TELEGRAM_TEXT_MAX_LEN - _TEXT_SAFETY_MARGIN there, currently 3996) before
# this module ever sees it -- this split is only meant to catch that
# guarantee somehow failing, not to re-chunk something that already fits.
# Must stay >= summarizer's own ceiling, or a summary summarizer considers
# "fits in one message" gets needlessly split into two here anyway (this
# was a real bug: 3800 here vs. 3996 there silently split a 3976-char
# single-message summary into two Telegram messages).
SAFE_CHUNK_LEN = 4000


def _split_message(html: str) -> List[str]:
    """Split a long HTML body into Telegram-sized chunks on paragraph breaks."""
    paragraphs = html.split("\n\n")
    chunks: List[str] = []
    current = ""

    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > SAFE_CHUNK_LEN:
            if current:
                chunks.append(current)
            current = para
        else:
            current = candidate
    if current:
        chunks.append(current)

    return chunks or [html]


async def _send_with_retry(send_coro_factory, max_attempts: int = 4) -> int:
    """Call `send_coro_factory()` (a zero-arg callable returning a fresh awaitable
    each time, since a single coroutine object can't be awaited twice) with
    retry-after/timeout handling. Returns the sent message's id."""
    for attempt in range(1, max_attempts + 1):
        try:
            message = await send_coro_factory()
            return message.message_id
        except RetryAfter as exc:
            logger.warning("Telegram rate limit hit, sleeping %.1fs", exc.retry_after)
            await asyncio.sleep(exc.retry_after + 1)
        except TimedOut:
            logger.warning("Telegram timed out, retrying (%d/%d)", attempt, max_attempts)
            await asyncio.sleep(3 * attempt)
    raise TelegramError(f"Failed to send Telegram message after {max_attempts} attempts")


async def send_summary_async(bot_token: str, channel_id: str, summary_html: str) -> Optional[int]:
    """Post the summary as one or more plain-text Telegram messages.

    summarizer.py already keeps summaries within TELEGRAM_MAX_LEN, but this
    still splits on a paragraph boundary as a fallback if one ever doesn't.
    """
    bot = Bot(token=bot_token)
    chunks = _split_message(summary_html)

    first_message_id = None
    for chunk in chunks:
        message_id = await _send_with_retry(
            lambda c=chunk: bot.send_message(chat_id=channel_id, text=c, parse_mode=ParseMode.HTML)
        )
        if first_message_id is None:
            first_message_id = message_id

    return first_message_id


def send_summary(bot_token: str, channel_id: str, summary_html: str) -> Optional[int]:
    """Synchronous wrapper around the async Telegram send call."""
    return asyncio.run(send_summary_async(bot_token, channel_id, summary_html))


# --- yt_relay add-on: link-preview / photo-caption posts -------------------
#
# Used only by yt_relay/ (see CLAUDE.md), never by the podcast pipeline
# above. Both reuse _send_with_retry so a 429/timeout is handled the same
# way as every other send in this module.

async def send_link_post_async(bot_token: str, channel_id: str, text: str, preview_url: str) -> Optional[int]:
    """Post `text` as a single message whose link preview card is pinned to
    `preview_url` and rendered large, above the text -- gives a clickable
    video thumbnail without downloading/uploading the video itself (see
    CLAUDE.md section 5.1)."""
    bot = Bot(token=bot_token)
    link_preview = LinkPreviewOptions(url=preview_url, prefer_large_media=True, show_above_text=True)
    message_id = await _send_with_retry(
        lambda: bot.send_message(
            chat_id=channel_id, text=text, parse_mode=ParseMode.HTML, link_preview_options=link_preview
        )
    )
    return message_id


def send_link_post(bot_token: str, channel_id: str, text: str, preview_url: str) -> Optional[int]:
    return asyncio.run(send_link_post_async(bot_token, channel_id, text, preview_url))


async def send_photo_post_async(bot_token: str, channel_id: str, photo_url: str, caption: str) -> Optional[int]:
    """Post `photo_url` (e.g. a YouTube thumbnail) as a photo with `caption`
    as its HTML caption. The photo itself isn't clickable through to the
    video -- the caption's own link is (see CLAUDE.md section 5.1)."""
    bot = Bot(token=bot_token)
    message_id = await _send_with_retry(
        lambda: bot.send_photo(chat_id=channel_id, photo=photo_url, caption=caption, parse_mode=ParseMode.HTML)
    )
    return message_id


def send_photo_post(bot_token: str, channel_id: str, photo_url: str, caption: str) -> Optional[int]:
    return asyncio.run(send_photo_post_async(bot_token, channel_id, photo_url, caption))
