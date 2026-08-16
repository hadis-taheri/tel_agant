"""Supabase-backed storage for yt_channels / yt_queue / yt_admin_state.

Table schema: see yt_relay_schema.sql and yt_relay_admin_schema.sql. Mirrors
the shape of database.py's EpisodeStore (same "thin wrapper around a
Supabase table" pattern) but is a separate class against separate tables --
this package never imports database.py.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from supabase import Client, create_client

logger = logging.getLogger(__name__)

CHANNELS_TABLE = "yt_channels"
QUEUE_TABLE = "yt_queue"
ADMIN_STATE_TABLE = "yt_admin_state"

STATUS_SEEDED = "seeded"
STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_POSTED = "posted"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"


@dataclass
class QueueItem:
    video_id: str
    channel_id: str
    title: str
    published_at: str
    raw_description: Optional[str] = None
    thumbnail_url: Optional[str] = None
    views: Optional[int] = None
    status: str = STATUS_PENDING


class YtStore:
    """Thin wrapper around the Supabase yt_channels / yt_queue tables."""

    def __init__(self, url: str, key: str):
        self.client: Client = create_client(url, key)

    # --- channels ---------------------------------------------------

    def get_channel_by_id(self, channel_id: str) -> Optional[dict]:
        resp = (
            self.client.table(CHANNELS_TABLE)
            .select("*")
            .eq("channel_id", channel_id)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    def insert_channel(self, channel_id: str, handle: Optional[str], title: Optional[str]) -> dict:
        row = {"channel_id": channel_id, "handle": handle, "title": title}
        resp = self.client.table(CHANNELS_TABLE).insert(row).execute()
        return resp.data[0]

    def mark_channel_seeded(self, channel_id: str) -> None:
        self.client.table(CHANNELS_TABLE).update(
            {"seeded_at": datetime.now(timezone.utc).isoformat()}
        ).eq("channel_id", channel_id).execute()

    def mark_channel_checked(self, channel_id: str, error: Optional[str] = None) -> None:
        fields = {"last_checked_at": datetime.now(timezone.utc).isoformat(), "last_error": error}
        self.client.table(CHANNELS_TABLE).update(fields).eq("channel_id", channel_id).execute()

    def active_channels(self) -> list[dict]:
        resp = self.client.table(CHANNELS_TABLE).select("*").eq("is_active", True).execute()
        return resp.data

    def all_channels(self) -> list[dict]:
        resp = self.client.table(CHANNELS_TABLE).select("*").order("created_at").execute()
        return resp.data

    def set_channel_active(self, channel_id: str, active: bool) -> None:
        self.client.table(CHANNELS_TABLE).update({"is_active": active}).eq(
            "channel_id", channel_id
        ).execute()

    # --- queue: dedupe / insert --------------------------------------

    def is_known(self, video_id: str) -> bool:
        resp = (
            self.client.table(QUEUE_TABLE)
            .select("id")
            .eq("video_id", video_id)
            .limit(1)
            .execute()
        )
        return len(resp.data) > 0

    def insert_queue_row(self, item: QueueItem) -> dict:
        row = {
            "video_id": item.video_id,
            "channel_id": item.channel_id,
            "title": item.title,
            "raw_description": item.raw_description,
            "published_at": item.published_at,
            "thumbnail_url": item.thumbnail_url,
            "views": item.views,
            "status": item.status,
        }
        resp = self.client.table(QUEUE_TABLE).insert(row).execute()
        return resp.data[0]

    # --- queue: discover / caption ------------------------------------

    def pending_rows(self, limit: int) -> list[dict]:
        resp = (
            self.client.table(QUEUE_TABLE)
            .select("*")
            .eq("status", STATUS_PENDING)
            .order("published_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data

    def count_ready(self) -> int:
        resp = (
            self.client.table(QUEUE_TABLE)
            .select("id", count="exact")
            .eq("status", STATUS_READY)
            .execute()
        )
        return resp.count or 0

    # --- queue: publish ------------------------------------------------

    def next_ready(self, exclude_channel: Optional[str] = None) -> Optional[dict]:
        """Newest-published-first 'ready' row, preferring a channel other than
        `exclude_channel` so consecutive posts rotate between channels (see
        CLAUDE.md section 5.5 -- mirrors get_newest_pending's exclude_source
        pattern in the podcast pipeline's database.py)."""
        if exclude_channel:
            resp = (
                self.client.table(QUEUE_TABLE)
                .select("*")
                .eq("status", STATUS_READY)
                .neq("channel_id", exclude_channel)
                .order("published_at", desc=True)
                .limit(1)
                .execute()
            )
            if resp.data:
                return resp.data[0]

        resp = (
            self.client.table(QUEUE_TABLE)
            .select("*")
            .eq("status", STATUS_READY)
            .order("published_at", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    def stale_ready(self, max_age_hours: int) -> list[dict]:
        """'ready' rows whose published_at is older than max_age_hours -- these
        get skipped in publish.py before a row is ever chosen, since a video
        that's been queued too long shouldn't get posted as if it were fresh
        (see CLAUDE.md section 5.5, guard (a))."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        resp = (
            self.client.table(QUEUE_TABLE)
            .select("*")
            .eq("status", STATUS_READY)
            .lt("published_at", cutoff)
            .execute()
        )
        return resp.data

    def last_posted(self) -> Optional[dict]:
        resp = (
            self.client.table(QUEUE_TABLE)
            .select("channel_id, posted_at")
            .eq("status", STATUS_POSTED)
            .order("posted_at", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    def count_posted_today(self) -> int:
        since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        resp = (
            self.client.table(QUEUE_TABLE)
            .select("id", count="exact")
            .eq("status", STATUS_POSTED)
            .gte("posted_at", since)
            .execute()
        )
        return resp.count or 0

    # --- generic updates -------------------------------------------------

    def update(self, row_id: str, **fields) -> None:
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.client.table(QUEUE_TABLE).update(fields).eq("id", row_id).execute()

    def mark_status(self, row_id: str, status: str, **extra_fields) -> None:
        self.update(row_id, status=status, **extra_fields)

    def mark_failed(self, row_id: str, error: str, attempts: int) -> None:
        self.update(row_id, status=STATUS_FAILED, last_error=str(error)[:2000], attempts=attempts)

    # --- status dashboard ------------------------------------------------

    def counts_by_status(self) -> dict:
        resp = self.client.table(QUEUE_TABLE).select("status").execute()
        counts: dict[str, int] = {}
        for row in resp.data:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return counts

    # --- admin bot state (singleton row, id=1) --------------------------

    def get_admin_state(self) -> dict:
        """Row seeded once by yt_relay_admin_schema.sql; the in-code default
        here is only a safety net if that seed row is ever missing."""
        resp = self.client.table(ADMIN_STATE_TABLE).select("*").eq("id", 1).limit(1).execute()
        return resp.data[0] if resp.data else {"id": 1, "last_update_id": 0, "pending_action": None}

    def set_admin_state(self, **fields) -> None:
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.client.table(ADMIN_STATE_TABLE).update(fields).eq("id", 1).execute()

    def recent_errors(self, limit: int = 10) -> list[dict]:
        resp = (
            self.client.table(QUEUE_TABLE)
            .select("video_id, channel_id, status, last_error, updated_at")
            .eq("status", STATUS_FAILED)
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data
