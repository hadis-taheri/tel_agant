"""Discovers videos from a YouTube channel's public Atom feed.

Spec section 2 (youtube-telegram-relay-spec.md): fetching
`?playlist_id=UULF{channel_id_without_UC}` instead of the plain channel feed
returns only long-form videos -- YouTube's own "uploads minus Shorts" virtual
playlist. Since the feed carries no duration field, this prefix swap is the
only reliable way to filter out Shorts without an API call.

Parsed with the standard library's xml.etree.ElementTree against explicit
namespaces, not `feedparser` (already a dependency of the core podcast
pipeline, see requirements.txt) -- feedparser doesn't reliably surface
media:group > media:description, and the caption is the one field this whole
feature depends on. ElementTree's findall() also sidesteps the spec's warned
single-entry-becomes-a-dict parser quirk entirely, since it always returns a
list.
"""
import logging
from dataclasses import dataclass
from typing import List, Optional
from xml.etree import ElementTree

import requests

logger = logging.getLogger(__name__)

FEED_URL_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?playlist_id=UULF{suffix}"
HTTP_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; YtRelay/1.0)"

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


@dataclass
class RawVideo:
    video_id: str
    channel_id: str
    title: str
    published_at: Optional[str]
    description: Optional[str]
    thumbnail_url: Optional[str]
    views: Optional[int]


def build_feed_url(channel_id: str) -> str:
    """channel_id must be the UC... form; the UULF playlist only exists for
    channels whose id starts with UC (true for every real YouTube channel)."""
    if not channel_id.startswith("UC"):
        raise ValueError(f"Expected a UC... channel id, got: {channel_id!r}")
    return FEED_URL_TEMPLATE.format(suffix=channel_id[2:])


def _text(el, path: str) -> Optional[str]:
    node = el.find(path, _NS)
    return node.text.strip() if node is not None and node.text else None


def _parse_entry(entry, channel_id: str) -> Optional[RawVideo]:
    video_id = _text(entry, "yt:videoId")
    title = _text(entry, "atom:title")
    if not video_id or not title:
        logger.warning("Skipping feed entry missing videoId/title for channel=%s", channel_id)
        return None

    published_at = _text(entry, "atom:published")

    group = entry.find("media:group", _NS)
    description = None
    thumbnail_url = None
    views = None
    if group is not None:
        description = _text(group, "media:description")
        thumb = group.find("media:thumbnail", _NS)
        if thumb is not None:
            thumbnail_url = thumb.get("url")
        stats = group.find("media:community/media:statistics", _NS)
        if stats is not None and stats.get("views"):
            try:
                views = int(stats.get("views"))
            except ValueError:
                views = None

    return RawVideo(
        video_id=video_id,
        channel_id=channel_id,
        title=title,
        published_at=published_at,
        description=description,
        thumbnail_url=thumbnail_url,
        views=views,
    )


def fetch_entries(channel_id: str) -> List[RawVideo]:
    """Fetch the current (max ~15 item) window of long-form videos for a
    channel. Returns an empty list for an empty feed or a deleted/unknown
    channel (404) -- callers should record the failure (see
    YtStore.mark_channel_checked) rather than let it propagate and stop
    discovery for every other channel."""
    url = build_feed_url(channel_id)
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    if resp.status_code == 404:
        logger.warning("Feed 404 for channel=%s (deleted or invalid channel id)", channel_id)
        return []
    resp.raise_for_status()

    if not resp.content.strip():
        return []

    try:
        root = ElementTree.fromstring(resp.content)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Failed to parse feed XML for channel={channel_id}: {exc}") from exc

    entries = root.findall("atom:entry", _NS)
    videos = [v for v in (_parse_entry(e, channel_id) for e in entries) if v is not None]
    logger.info("channel=%s: found %d video(s) in feed", channel_id, len(videos))
    return videos
