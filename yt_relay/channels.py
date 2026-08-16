"""Channel resolution (handle -> channel id), registration, and seeding.

Spec section 2: a feed can't be built directly from a handle (`@name`); the
channel id (`UC...`) must be scraped once from the channel's page and stored
-- a handle can change, the id never does.
"""
import logging
import re
from dataclasses import dataclass
from typing import List, Optional

import requests

from . import feed
from .store import QueueItem, STATUS_SEEDED, YtStore

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 20
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Without a consent cookie, YouTube redirects a cookie-less request to
# consent.youtube.com (an EU/GDPR cookie-consent gate) instead of serving the
# actual channel page -- confirmed directly: the same request without these
# returned a ~580KB consent-gate page (base href="https://consent.youtube.com/")
# with none of the channel id markers below present at all. SOCS/CONSENT are
# the same pre-accepted values yt-dlp and other scrapers use; hl=en&gl=US
# additionally pins the response to English/US markup regardless of the
# requesting IP's apparent geolocation.
_CONSENT_COOKIES = {
    "SOCS": "CAISHAgCEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiA_LyaBg",
    "CONSENT": "YES+cb",
}
_LOCALE_PARAMS = {"hl": "en", "gl": "US"}

_CHANNEL_ID_RE = re.compile(r"UC[a-zA-Z0-9_-]{22}")
_META_CHANNEL_ID_RE = re.compile(r'itemprop="channelId"\s+content="(UC[a-zA-Z0-9_-]{22})"')
_CANONICAL_RE = re.compile(r'rel="canonical"\s+href="https://www\.youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})"')
# Last-resort only, and confirmed unreliable as anything but that: on a real
# fetch of youtube.com/@lexfridman this matched a channel id belonging to a
# completely different, unrelated channel (most likely pulled from a
# recommended-channels module elsewhere on the page), while the canonical
# link above correctly resolved to Lex Fridman's own channel. Never let this
# pattern be tried before _CANONICAL_RE.
_JSON_CHANNEL_ID_RE = re.compile(r'"channelId":"(UC[a-zA-Z0-9_-]{22})"')


class ChannelResolutionError(RuntimeError):
    pass


def _normalize_to_handle_or_id(raw: str) -> str:
    """Accepts a bare handle (@name), a full channel/handle URL, or a raw
    UC... id, and returns the piece we actually need to look up."""
    raw = raw.strip()
    match = _CHANNEL_ID_RE.fullmatch(raw)
    if match:
        return raw

    # Strip a full URL down to its @handle or its bare UC... id (the
    # "channel/" prefix is discarded, not captured, so a /channel/UC... URL
    # resolves to the same fast path as a bare id -- no network call needed).
    url_match = re.search(r"youtube\.com/(?:channel/)?(@[\w.-]+|UC[a-zA-Z0-9_-]{22})", raw)
    if url_match:
        return url_match.group(1)

    if raw.startswith("@"):
        return raw

    return raw if raw.startswith("@") else f"@{raw}"


def resolve_channel_id(raw: str) -> tuple[str, Optional[str]]:
    """Return (channel_id, handle_or_None) for a handle, URL, or raw channel id.

    A bare UC... id is accepted directly (no network call needed -- this is
    the guaranteed escape hatch if page scraping ever breaks). Everything
    else is resolved by fetching the channel page and trying, in order:
    the <meta itemprop="channelId"> tag, the canonical <link>, then a raw
    "channelId":"UC..." occurrence in the page's embedded JSON.
    """
    normalized = _normalize_to_handle_or_id(raw)

    if _CHANNEL_ID_RE.fullmatch(normalized):
        return normalized, None

    handle = normalized
    url = f"https://www.youtube.com/{handle}"
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        cookies=_CONSENT_COOKIES,
        params=_LOCALE_PARAMS,
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code == 404:
        raise ChannelResolutionError(f"Channel page not found: {url}")
    resp.raise_for_status()
    html = resp.text

    for pattern in (_META_CHANNEL_ID_RE, _CANONICAL_RE, _JSON_CHANNEL_ID_RE):
        match = pattern.search(html)
        if match:
            if pattern is _JSON_CHANNEL_ID_RE:
                logger.warning(
                    "Resolved channel id for %s via the unreliable JSON fallback pattern "
                    "(no <meta> or canonical link found) -- verify %s is actually correct",
                    handle, match.group(1),
                )
            return match.group(1), handle

    raise ChannelResolutionError(f"Could not extract a channel id from {url}")


@dataclass
class AddChannelResult:
    input: str
    channel_id: Optional[str] = None
    handle: Optional[str] = None
    seeded_count: int = 0
    already_existed: bool = False
    error: Optional[str] = None


def add_channel(store: YtStore, raw: str) -> AddChannelResult:
    """Resolve, register, and seed one channel. Idempotent: re-running on an
    already-registered channel is a no-op (does NOT re-seed), since re-seeding
    would silently mark videos published between the two runs as 'seeded'
    and they would never be posted."""
    result = AddChannelResult(input=raw)
    try:
        channel_id, handle = resolve_channel_id(raw)
    except (ChannelResolutionError, requests.exceptions.RequestException) as exc:
        result.error = str(exc)
        return result

    result.channel_id = channel_id
    result.handle = handle

    existing = store.get_channel_by_id(channel_id)
    if existing:
        result.already_existed = True
        return result

    store.insert_channel(channel_id, handle, title=None)

    try:
        videos = feed.fetch_entries(channel_id)
    except requests.exceptions.RequestException as exc:
        result.error = f"Registered but failed to seed: {exc}"
        return result

    for video in videos:
        if store.is_known(video.video_id):
            continue
        store.insert_queue_row(
            QueueItem(
                video_id=video.video_id,
                channel_id=channel_id,
                title=video.title,
                published_at=video.published_at,
                raw_description=video.description,
                thumbnail_url=video.thumbnail_url,
                views=video.views,
                status=STATUS_SEEDED,
            )
        )
        result.seeded_count += 1

    store.mark_channel_seeded(channel_id)
    return result


def parse_channel_list_file(path: str) -> List[str]:
    """Parse a plain-text channel list: one entry per line (handle, full URL,
    or raw UC... id); blank lines and lines starting with # are ignored.
    This file is only ever read once, at import time -- yt_channels is the
    source of truth afterward (see CLAUDE.md section 6)."""
    entries = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            entries.append(line)
    return entries
