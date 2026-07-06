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

STATUS_PENDING = "pending"
STATUS_DOWNLOADING = "downloading"
STATUS_TRANSCRIBED = "transcribed"
STATUS_SUMMARIZED = "summarized"
STATUS_POSTED = "posted"
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

    def update(self, episode_id: int, **fields) -> None:
        fields["updated_at"] = datetime.utcnow().isoformat()
        self.client.table(TABLE).update(fields).eq("id", episode_id).execute()

    def mark_failed(self, episode_id: int, error: str) -> None:
        self.update(episode_id, status=STATUS_FAILED, error_message=str(error)[:2000])

    def mark_status(self, episode_id: int, status: str, **extra_fields) -> None:
        self.update(episode_id, status=status, **extra_fields)
