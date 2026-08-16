"""Stage A: fetch each active channel's feed, queue new videos, then caption
a bounded batch of pending rows.

Entry point: `python -m yt_relay discover`. Mirrors the two-part shape of
the podcast pipeline's phase 2 (scrape_backlog + process_backlog_once in
main.py): discovery/queueing always runs (it costs no LLM tokens), while the
LLM captioning step is gated separately.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from groq import Groq

from . import clean, feed
from .caption import CaptionValidationError, generate_caption
from .settings import YtSettings
from .store import QueueItem, STATUS_FAILED, STATUS_PENDING, STATUS_READY, STATUS_SEEDED, STATUS_SKIPPED, YtStore

logger = logging.getLogger(__name__)


def _is_stale(published_at: Optional[str], max_age_hours: int) -> bool:
    if not published_at:
        return False
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return published < datetime.now(timezone.utc) - timedelta(hours=max_age_hours)


def discover_channel(store: YtStore, channel: dict, max_age_hours: int) -> None:
    channel_id = channel["channel_id"]
    seeded_at = channel.get("seeded_at")

    try:
        videos = feed.fetch_entries(channel_id)
    except Exception as exc:  # noqa: BLE001 - one channel's failure must not stop the rest
        logger.exception("Failed to fetch feed for channel=%s", channel_id)
        store.mark_channel_checked(channel_id, error=str(exc)[:2000])
        return

    for video in videos:
        if store.is_known(video.video_id):
            continue

        if not seeded_at:
            status = STATUS_SEEDED
        elif _is_stale(video.published_at, max_age_hours):
            status = STATUS_SEEDED
        else:
            status = STATUS_PENDING

        store.insert_queue_row(
            QueueItem(
                video_id=video.video_id,
                channel_id=channel_id,
                title=video.title,
                published_at=video.published_at,
                raw_description=video.description,
                thumbnail_url=video.thumbnail_url,
                views=video.views,
                status=status,
            )
        )

    store.mark_channel_checked(channel_id, error=None)


def discover_all(store: YtStore, max_age_hours: int) -> None:
    channels = store.active_channels()
    logger.info("Discovering %d active channel(s)", len(channels))
    for channel in channels:
        discover_channel(store, channel, max_age_hours)


def caption_pending(store: YtStore, groq_client: Groq, model: str, max_captions: int, ready_queue_max: int) -> None:
    """Caption up to `max_captions` pending rows, gated by backpressure: if
    the 'ready' queue already has >= ready_queue_max rows, skip captioning
    entirely this run (rows stay 'pending') rather than spend LLM calls on
    videos that will likely go stale before they're ever posted -- see
    CLAUDE.md section 5.5, guard (d)."""
    ready_count = store.count_ready()
    if ready_count >= ready_queue_max:
        logger.info(
            "Ready queue has %d row(s) (>= %d), skipping caption step this run",
            ready_count, ready_queue_max,
        )
        return

    budget = min(max_captions, ready_queue_max - ready_count)
    rows = store.pending_rows(limit=budget)
    logger.info("Captioning %d pending row(s)", len(rows))

    for row in rows:
        cleaned = clean.clean_description(row.get("raw_description") or "")
        weak = clean.is_weak_source(cleaned)
        description_for_llm = "" if weak else cleaned

        try:
            caption = generate_caption(groq_client, model, row["title"], description_for_llm)
        except CaptionValidationError as exc:
            logger.warning("Caption validation failed for video_id=%s: %s", row["video_id"], exc)
            store.update(
                row["id"],
                status=STATUS_FAILED,
                cleaned_description=cleaned,
                last_error=str(exc)[:2000],
                attempts=row.get("attempts", 0) + 1,
            )
            continue
        except Exception as exc:  # noqa: BLE001 - one video's failure must not stop the batch
            logger.exception("Caption call failed for video_id=%s", row["video_id"])
            store.update(
                row["id"],
                status=STATUS_FAILED,
                cleaned_description=cleaned,
                last_error=str(exc)[:2000],
                attempts=row.get("attempts", 0) + 1,
            )
            continue

        if caption.insufficient:
            store.update(
                row["id"],
                status=STATUS_SKIPPED,
                cleaned_description=cleaned,
                skip_reason="insufficient_source",
            )
            continue

        store.update(
            row["id"],
            status=STATUS_READY,
            cleaned_description=cleaned,
            caption_json={
                "headline": caption.headline,
                "summary": caption.summary,
                "takeaways": caption.takeaways,
                "tags": caption.tags,
            },
        )


def run_discover(settings: YtSettings) -> None:
    store = YtStore(settings.supabase_url, settings.supabase_key)
    discover_all(store, settings.max_age_hours)

    groq_client = Groq(api_key=settings.groq_api_key)
    caption_pending(store, groq_client, settings.groq_llm_model, settings.max_captions_per_run, settings.ready_queue_max)
