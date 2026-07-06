"""Discovers new podcast episodes from the configured sources.

Source 1 - crossingpodcast.com
    The site is a client-rendered SPA with no static RSS feed. It is backed by a
    tRPC API which we call directly: GET /api/trpc/episodes.list?input={"json":{}}
    Returns the most recent episodes (newest first), each already carrying an
    English title/summary plus a direct audio URL (the underlying show is hosted
    on xiaoyuzhoufm.com, so audio is in Chinese).

Source 2 - sv101.fireside.fm
    Fireside-hosted podcasts always expose a standard RSS feed
    (https://feeds.fireside.fm/<show>/rss) with an <enclosure> audio URL per item.
    We use feedparser instead of scraping the HTML episode list.
"""
import logging
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import quote

import feedparser
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

SOURCE_CROSSINGPODCAST = "crossingpodcast"
SOURCE_SV101 = "sv101"

HTTP_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; PodcastAgent/1.0)"


@dataclass
class RawEpisode:
    source: str
    external_id: str
    title: str
    episode_url: str
    audio_url: str
    published_at: Optional[str]


def _retry_network():
    return retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((requests.exceptions.RequestException,)),
    )


@_retry_network()
def fetch_crossingpodcast_episodes(api_base: str) -> List[RawEpisode]:
    """Fetch the latest episode list from crossingpodcast.com's tRPC API."""
    query = quote('{"json":{}}')
    url = f"{api_base}?input={query}"
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()

    try:
        items = payload["result"]["data"]["json"]["items"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Unexpected crossingpodcast API response shape: {payload}") from exc

    episodes = []
    for item in items:
        slug = item.get("slug")
        audio_url = item.get("audioUrl")
        if not slug or not audio_url:
            logger.warning("Skipping crossingpodcast item missing slug/audioUrl: %s", item.get("id"))
            continue
        title = item.get("englishTitle") or item.get("chineseTitle") or slug
        episodes.append(
            RawEpisode(
                source=SOURCE_CROSSINGPODCAST,
                external_id=slug,
                title=title,
                episode_url=f"https://crossingpodcast.com/episodes/{slug}",
                audio_url=audio_url,
                published_at=item.get("publishDate"),
            )
        )
    logger.info("crossingpodcast: found %d episodes", len(episodes))
    return episodes


@_retry_network()
def fetch_sv101_episodes(rss_url: str) -> List[RawEpisode]:
    """Fetch episodes from the sv101 (fireside.fm) RSS feed."""
    resp = requests.get(rss_url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"Failed to parse sv101 RSS feed: {feed.bozo_exception}")

    episodes = []
    for entry in feed.entries:
        audio_url = None
        for enclosure in entry.get("links", []):
            if enclosure.get("rel") == "enclosure" or "audio" in enclosure.get("type", ""):
                audio_url = enclosure.get("href")
                break
        if not audio_url:
            logger.warning("Skipping sv101 entry with no audio enclosure: %s", entry.get("title"))
            continue

        guid = entry.get("id") or entry.get("guid") or entry.get("link")
        episodes.append(
            RawEpisode(
                source=SOURCE_SV101,
                external_id=guid,
                title=entry.get("title", "Untitled episode"),
                episode_url=entry.get("link", rss_url),
                audio_url=audio_url,
                published_at=entry.get("published"),
            )
        )
    logger.info("sv101: found %d episodes", len(episodes))
    return episodes


def fetch_all_episodes(crossingpodcast_api: str, sv101_rss_url: str) -> List[RawEpisode]:
    """Fetch episodes from every configured source, tolerating per-source failures."""
    episodes: List[RawEpisode] = []
    for name, fetch_fn, arg in (
        ("crossingpodcast", fetch_crossingpodcast_episodes, crossingpodcast_api),
        ("sv101", fetch_sv101_episodes, sv101_rss_url),
    ):
        try:
            episodes.extend(fetch_fn(arg))
        except Exception:
            logger.exception("Failed to fetch episodes from source=%s; continuing with other sources", name)
    return episodes
