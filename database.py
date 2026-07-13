"""Supabase-backed storage for tracking podcast episodes and their processing state.

Table schema (create once in the Supabase SQL editor — see supabase_schema.sql):

    episodes (
        id            bigserial primary key,
        source        text not null,
        external_id   text not null,
        title         text,
        episode_url   text,
        audio_url     text,
        published_at  timestamptz,
        status        text not null default 'pending',
        -- status values: pending, downloading, transcribed, summarized,
        -- posted (phase 1 live episode), processed (phase 2 backlog episode),
        -- failed, seeded (pre-existing episode explicitly skipped)
        transcript    text,
        summary_html  text,
        telegram_message_id bigint,
        error_message text,
        created_at    timestamptz not null default now(),
        updated_at    timestamptz not null default now(),
        unique (source, external_id)
    )
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from supabase import create_client, Client

logger = logging.getLogger(__name__)

TABLE = "episodes"

STATUS_PENDING = "pending"          # queued, not processed yet (newly-discovered or backlog)
STATUS_DOWNLOADING = "downloading"
STATUS_TRANSCRIBED = "transcribed"
STATUS_SUMMARIZED = "summarized"
STATUS_POSTED = "posted"            # phase 1: freshly-discovered episode, processed and posted
STATUS_PROCESSED = "processed"      # phase 2: backlog episode, processed and posted
STATUS_FAILED = "failed"
STATUS_SEEDED = "seeded"  # pre-existing episode marked as seen without processing


@dataclass
class Episode:
    source: str
    external_id: str
    title: str
    episode_url: str
    audio_url: str
    published_at: Optional[str] = None
    id: Optional[int] = None
    status: str = STATUS_PENDING
    transcript: Optional[str] = None
    summary_html: Optional[str] = None


class EpisodeStore:
    """Thin wrapper around the Supabase `episodes` table."""

    def __init__(self, url: str, key: str):
        self.client: Client = create_client(url, key)

    def is_known(self, source: str, external_id: str) -> bool:
        resp = (
            self.client.table(TABLE)
            .select("id")
            .eq("source", source)
            .eq("external_id", external_id)
            .limit(1)
            .execute()
        )
        return len(resp.data) > 0

    def insert_pending(self, episode: Episode) -> int:
        row = {
            "source": episode.source,
            "external_id": episode.external_id,
            "title": episode.title,
            "episode_url": episode.episode_url,
            "audio_url": episode.audio_url,
            "published_at": episode.published_at,
            "status": STATUS_PENDING,
        }
        resp = self.client.table(TABLE).insert(row).execute()
        new_id = resp.data[0]["id"]
        logger.info("Recorded new episode id=%s source=%s title=%r", new_id, episode.source, episode.title)
        return new_id

    def insert_seeded(self, episode: Episode) -> int:
        """Record a pre-existing episode as already-seen, without processing it.

        Used for one-time backfill: on first setup, a source may already have
        hundreds of old episodes; we don't want to transcribe/post all of them.
        """
        row = {
            "source": episode.source,
            "external_id": episode.external_id,
            "title": episode.title,
            "episode_url": episode.episode_url,
            "audio_url": episode.audio_url,
            "published_at": episode.published_at,
            "status": STATUS_SEEDED,
        }
        resp = self.client.table(TABLE).insert(row).execute()
        return resp.data[0]["id"]

    def get_last_finalized(self) -> Optional[dict]:
        """Return {"source", "updated_at"} of the most recently posted/processed
        episode, or None if none exist yet. Used to throttle how often phase 2
        does real work (see MIN_BACKLOG_INTERVAL_MINUTES in main.py)."""
        resp = (
            self.client.table(TABLE)
            .select("source, updated_at")
            .in_("status", [STATUS_POSTED, STATUS_PROCESSED])
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    def get_recent_finalized_sources(self, limit: int = 2) -> list[str]:
        """Return the sources of the `limit` most-recently posted/processed
        episodes, most recent first. With 3+ sources sharing one backlog
        rotation, excluding just the single last source (as with 2 sources)
        isn't enough to keep rotation fair -- excluding the last `limit`
        keeps any one source from being picked twice in a row across a wider
        window. See get_oldest_pending's exclude_sources."""
        resp = (
            self.client.table(TABLE)
            .select("source")
            .in_("status", [STATUS_POSTED, STATUS_PROCESSED])
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [row["source"] for row in resp.data]

    def get_oldest_pending(self, exclude_sources: Optional[list[str]] = None) -> Optional[dict]:
        """Return the oldest (by published date) episode still queued as 'pending', or None.

        Used by backlog processing (phase 2) to work through the historical
        archive one episode at a time, oldest first. If `exclude_sources` is
        given, prefer an episode from a source *not* in that list first (so
        backlog posts rotate fairly between sources run to run instead of one
        source dominating); falls back to the oldest pending episode from any
        source if every candidate in the oldest window is excluded, so the
        backlog never stalls just because some sources ran dry.

        Picks from a small oldest-first window (20) rather than issuing a
        `not in (...)` query, since which sources should be excluded is a
        short, dynamic, Python-side list (see get_recent_finalized_sources).
        """
        exclude_sources = exclude_sources or []
        resp = (
            self.client.table(TABLE)
            .select("*")
            .eq("status", STATUS_PENDING)
            .order("published_at", desc=False, nullsfirst=False)
            .limit(20)
            .execute()
        )
        rows = resp.data
        if not rows:
            return None
        for row in rows:
            if row["source"] not in exclude_sources:
                return row
        return rows[0]

    def update(self, episode_id: int, **fields) -> None:
        fields["updated_at"] = datetime.utcnow().isoformat()
        self.client.table(TABLE).update(fields).eq("id", episode_id).execute()

    def mark_failed(self, episode_id: int, error: str) -> None:
        self.update(episode_id, status=STATUS_FAILED, error_message=str(error)[:2000])

    def mark_status(self, episode_id: int, status: str, **extra_fields) -> None:
        self.update(episode_id, status=status, **extra_fields)
