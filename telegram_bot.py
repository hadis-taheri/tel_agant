"""Publishes the generated Persian summary to a Telegram channel."""
import asyncio
import logging
from typing import List, Optional

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, TelegramError

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4096
# Leave headroom for the "لینک اپیزود" footer appended to the last chunk.
SAFE_CHUNK_LEN = 3800


def _split_message(html: str, footer: str) -> List[str]:
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

    if not chunks:
        chunks = [html]

    chunks[-1] = f"{chunks[-1]}\n\n{footer}"
    if len(chunks[-1]) > TELEGRAM_MAX_LEN:
        chunks.append(footer)
    return chunks


async def _send_with_retry(bot: Bot, channel_id: str, text: str, max_attempts: int = 4) -> int:
    last_id = None
    for attempt in range(1, max_attempts + 1):
        try:
            message = await bot.send_message(
                chat_id=channel_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
            last_id = message.message_id
            return last_id
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
    episode_url: str,
    source_label: str,
) -> Optional[int]:
    bot = Bot(token=bot_token)
    footer = f'📎 <a href="{episode_url}">شنیدن نسخه اصلی اپیزود ({source_label})</a>'
    chunks = _split_message(summary_html, footer)

    first_message_id = None
    for chunk in chunks:
        message_id = await _send_with_retry(bot, channel_id, chunk)
        if first_message_id is None:
            first_message_id = message_id
    return first_message_id


def send_summary(bot_token: str, channel_id: str, summary_html: str, episode_url: str, source_label: str) -> Optional[int]:
    """Synchronous wrapper around the async Telegram send call."""
    return asyncio.run(send_summary_async(bot_token, channel_id, summary_html, episode_url, source_label))
