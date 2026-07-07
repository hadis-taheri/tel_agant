"""Publishes the generated Persian summary to a Telegram channel."""
import asyncio
import glob
import logging
import os
import random
from typing import List, Optional

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, TelegramError

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4096
TELEGRAM_CAPTION_MAX_LEN = 1024
SAFE_CHUNK_LEN = 3800

# Both podcast sources are Chinese-language; letting Telegram auto-preview the
# episode's own page pulled in the source's Chinese title/description/cover
# image. Rather than link to the source at all, every post opens with a photo
# instead: ideally one generated for this specific episode's topic (see
# image_generator.py), falling back to one of these generic local AI/tech
# banners if topic-image generation isn't available or fails for this episode.
_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def _pick_banner_image_bytes() -> Optional[bytes]:
    candidates = sorted(glob.glob(os.path.join(_ASSETS_DIR, "topic_banner_*.jpg")))
    if not candidates:
        return None
    with open(random.choice(candidates), "rb") as photo_file:
        return photo_file.read()


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


async def send_summary_async(
    bot_token: str,
    channel_id: str,
    summary_html: str,
    photo_bytes: Optional[bytes] = None,
) -> Optional[int]:
    """Post the summary. `photo_bytes`, if given, should be an episode-specific
    topic image (see image_generator.py); otherwise a generic local banner is
    used so there's always some image rather than a bare wall of text."""
    bot = Bot(token=bot_token)
    chunks = _split_message(summary_html)

    first_message_id = None
    photo_bytes = photo_bytes or _pick_banner_image_bytes()

    if photo_bytes:
        title_chunk, *rest = chunks
        caption = title_chunk if len(title_chunk) <= TELEGRAM_CAPTION_MAX_LEN else None

        first_message_id = await _send_with_retry(
            lambda: bot.send_photo(
                chat_id=channel_id,
                photo=photo_bytes,
                caption=caption,
                parse_mode=ParseMode.HTML if caption else None,
            )
        )
        # If the title alone didn't fit as a caption, send it as the first text chunk.
        remaining_chunks = rest if caption else chunks
    else:
        remaining_chunks = chunks

    for chunk in remaining_chunks:
        message_id = await _send_with_retry(
            lambda c=chunk: bot.send_message(chat_id=channel_id, text=c, parse_mode=ParseMode.HTML)
        )
        if first_message_id is None:
            first_message_id = message_id

    return first_message_id


def send_summary(
    bot_token: str,
    channel_id: str,
    summary_html: str,
    photo_bytes: Optional[bytes] = None,
) -> Optional[int]:
    """Synchronous wrapper around the async Telegram send call."""
    return asyncio.run(send_summary_async(bot_token, channel_id, summary_html, photo_bytes))
