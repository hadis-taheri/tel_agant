"""Stage B: publish exactly one 'ready' video per invocation, behind the
guards in spec section 8 / CLAUDE.md section 5.5.

Entry point: `python -m yt_relay publish`.
"""
import logging
from typing import Optional

import telegram_bot
from .render import render_post, video_url
from .settings import YtSettings
from .store import STATUS_POSTED, STATUS_SKIPPED, YtStore

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


def _channel_title(store: YtStore, channel_id: str) -> str:
    channel = store.get_channel_by_id(channel_id)
    if not channel:
        return channel_id
    return channel.get("title") or channel.get("handle") or channel_id


def _skip_stale_rows(store: YtStore, max_age_hours: int) -> None:
    for row in store.stale_ready(max_age_hours):
        logger.info("Skipping stale ready row video_id=%s (published_at=%s)", row["video_id"], row["published_at"])
        store.mark_status(row["id"], STATUS_SKIPPED, skip_reason="stale")


def _pick_row(store: YtStore) -> Optional[dict]:
    last = store.last_posted()
    exclude_channel = last["channel_id"] if last else None
    return store.next_ready(exclude_channel=exclude_channel)


def _send(settings: YtSettings, text: str, row: dict) -> int:
    url = video_url(row["video_id"])
    if settings.post_style == "photo" and row.get("thumbnail_url"):
        return telegram_bot.send_photo_post(
            settings.telegram_bot_token, settings.telegram_channel_id, row["thumbnail_url"], text
        )
    return telegram_bot.send_link_post(settings.telegram_bot_token, settings.telegram_channel_id, text, url)


def publish_one(settings: YtSettings, store: YtStore) -> bool:
    """Returns True if a row was published (or would have been, under
    DRY_RUN); False if nothing happened this run."""
    if not settings.enabled:
        logger.info("YT_RELAY_ENABLED is off; publish stage exiting")
        return False

    last = store.last_posted()
    if last and last.get("posted_at"):
        from datetime import datetime, timedelta, timezone

        posted_at = datetime.fromisoformat(last["posted_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - posted_at < timedelta(hours=settings.min_hours_between_posts):
            logger.info("Last post was too recent (< %dh); exiting", settings.min_hours_between_posts)
            return False

    if store.count_posted_today() >= settings.max_posts_per_day:
        logger.info("Daily post cap (%d) reached; exiting", settings.max_posts_per_day)
        return False

    _skip_stale_rows(store, settings.max_age_hours)

    row = _pick_row(store)
    if not row:
        logger.info("No 'ready' row to publish")
        return False

    channel_title = _channel_title(store, row["channel_id"])
    caption = row.get("caption_json") or {}
    text = render_post(
        headline=caption.get("headline", row["title"]),
        summary=caption.get("summary", ""),
        takeaways=caption.get("takeaways", []),
        tags=caption.get("tags", []),
        channel_title=channel_title,
        video_id=row["video_id"],
    )
    store.update(row["id"], rendered_text=text)

    if settings.dry_run:
        logger.info("DRY_RUN: would post video_id=%s\n%s", row["video_id"], text)
        return True

    attempts = row.get("attempts", 0)
    try:
        message_id = _send(settings, text, row)
    except Exception as exc:  # noqa: BLE001 - failure of one row must not crash the run
        attempts += 1
        logger.exception("Failed to publish video_id=%s (attempt %d/%d)", row["video_id"], attempts, MAX_ATTEMPTS)
        if attempts >= MAX_ATTEMPTS:
            store.mark_failed(row["id"], str(exc), attempts)
        else:
            store.update(row["id"], attempts=attempts, last_error=str(exc)[:2000])
        return False

    from datetime import datetime, timezone

    store.mark_status(
        row["id"],
        STATUS_POSTED,
        telegram_message_id=message_id,
        posted_at=datetime.now(timezone.utc).isoformat(),
    )
    logger.info("Published video_id=%s message_id=%s", row["video_id"], message_id)
    return True


def run_publish(settings: YtSettings) -> None:
    store = YtStore(settings.supabase_url, settings.supabase_key)
    publish_one(settings, store)
