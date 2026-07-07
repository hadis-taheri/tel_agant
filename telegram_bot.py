"""Publishes the generated Persian summary to a Telegram channel as plain text."""
import asyncio
import logging
from typing import List, Optional

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, TelegramError

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4096
SAFE_CHUNK_LEN = 3800


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
