"""Discovers new podcast episodes from the configured sources.

Source 1 - crossingpodcast.com
    The site is a client-rendered SPA with no static RSS feed. It is backed by a
    tRPC API which we call directly: GET /api/trpc/episodes.list?input={"json":{}}
    Returns the most recent episodes (newest first), each already carrying an
    English title/summary plus a direct audio URL (the underlying show is hosted
    on xiaoyuzhoufm.com, so audio is in Chinese). The same endpoint paginates via
    a `page` param (20 items/page); used to walk the full historical archive.

Source 2 - sv101.fireside.fm
    Fireside-hosted podcasts always expose a standard RSS feed
    (https://feeds.fireside.fm/<show>/rss) with an <enclosure> audio URL per item.
    We use feedparser instead of scraping the HTML episode list.

Source 3 - lennysnewsletter.com (Lenny's Podcast)
    A Substack-hosted podcast. Substack's public www.lennysnewsletter.com/feed
    RSS only returns the ~20 most recent items and mixes real podcast episodes
    together with plain newsletter posts (essays, weekly "How I AI" link
    roundups, "Community Wisdom" digests) that have no episode audio at all.

    Instead we use Substack's *dedicated* podcast-only RSS feed --
    api.substack.com/feed/podcast/<show-id>.rss, the same feed Apple Podcasts/
    Spotify/etc. actually subscribe to (found via the show's public Apple
    Podcasts page/iTunes lookup). Unlike the newsletter feed, this one: (a)
    only ever contains real episodes with a genuine audio/mpeg enclosure --
    text-only roundup/digest posts are never in it, so no separate filtering
    is needed -- and (b) returns the *entire* history in one response (352
    episodes back to 2022 as of writing), exactly like sv101's feed. So, like
    sv101, fetch_lenny_episodes doubles as both the "latest" (phase 1) and
    "archive" (phase 2) fetch for this source; there's no pagination or
    per-post page-scraping involved.

    Lenny's audio is native English, so this source skips summarizer.py's
    Chinese/English -> Persian bridge step entirely (see already_english= there)
    and transcriber.py is told language="en" explicitly (see main.py).
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
SOURCE_LENNY = "lenny"

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


def _parse_crossingpodcast_items(items: list) -> List[RawEpisode]:
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
    return episodes


@_retry_network()
def _fetch_crossingpodcast_page(api_base: str, page: int) -> tuple[List[RawEpisode], int]:
    """Fetch one page (20 items) of the crossingpodcast tRPC episode list.

    Returns (episodes_on_this_page, total_episode_count_reported_by_the_api).
    """
    query = quote(f'{{"json":{{"page":{page}}}}}')
    url = f"{api_base}?input={query}"
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()

    try:
        data = payload["result"]["data"]["json"]
        items = data["items"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Unexpected crossingpodcast API response shape: {payload}") from exc

    return _parse_crossingpodcast_items(items), data.get("total", 0)


def fetch_crossingpodcast_episodes(api_base: str) -> List[RawEpisode]:
    """Fetch only the most recent page of episodes (cheap check for new episodes)."""
    episodes, _total = _fetch_crossingpodcast_page(api_base, page=1)
    logger.info("crossingpodcast: found %d episodes (latest page)", len(episodes))
    return episodes


def fetch_crossingpodcast_archive(api_base: str) -> List[RawEpisode]:
    """Walk every page of the crossingpodcast tRPC API to collect the full historical archive."""
    episodes: List[RawEpisode] = []
    page = 1
    while True:
        page_episodes, total = _fetch_crossingpodcast_page(api_base, page=page)
        if not page_episodes:
            break
        episodes.extend(page_episodes)
        if len(episodes) >= total:
            break
        page += 1
    logger.info("crossingpodcast: found %d episodes in full archive", len(episodes))
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


def _lenny_slug_from_url(url: str) -> str:
    return url.rstrip("/").rsplit("/p/", 1)[-1]


@_retry_network()
def fetch_lenny_episodes(feed_url: str) -> List[RawEpisode]:
    """Fetch Lenny's Podcast episodes from Substack's dedicated podcast RSS
    feed. Every item in this feed is a real episode with a genuine audio
    enclosure (see module docstring), so unlike the other two sources there's
    no separate filtering step. Returns the full history in one response, so
    (like fetch_sv101_episodes) this doubles as both the "latest" (phase 1)
    and "archive" (phase 2) fetch for this source."""
    resp = requests.get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"Failed to parse lenny podcast RSS feed: {feed.bozo_exception}")

    episodes = []
    for entry in feed.entries:
        audio_url = None
        for enclosure in entry.get("links", []):
            if "audio" in enclosure.get("type", ""):
                audio_url = enclosure.get("href")
                break
        if not audio_url:
            logger.warning("Skipping lenny entry with no audio enclosure: %s", entry.get("title"))
            continue

        link = entry.get("link", "")
        episodes.append(
            RawEpisode(
                source=SOURCE_LENNY,
                external_id=_lenny_slug_from_url(link) if link else entry.get("id", ""),
                title=entry.get("title", "Untitled episode"),
                episode_url=link or feed_url,
                audio_url=audio_url,
                published_at=entry.get("published"),
            )
        )
    logger.info("lenny: found %d episodes", len(episodes))
    return episodes


def fetch_all_episodes(crossingpodcast_api: str, sv101_rss_url: str, lenny_feed_url: str) -> List[RawEpisode]:
    """Fetch the latest episodes from every configured source (cheap, for new-episode checks)."""
    episodes: List[RawEpisode] = []
    for name, fetch_fn, arg in (
        ("crossingpodcast", fetch_crossingpodcast_episodes, crossingpodcast_api),
        ("sv101", fetch_sv101_episodes, sv101_rss_url),
        ("lenny", fetch_lenny_episodes, lenny_feed_url),
    ):
        try:
            episodes.extend(fetch_fn(arg))
        except Exception:
            logger.exception("Failed to fetch episodes from source=%s; continuing with other sources", name)
    return episodes


def fetch_full_archive(crossingpodcast_api: str, sv101_rss_url: str, lenny_feed_url: str) -> List[RawEpisode]:
    """Fetch the entire historical archive from every configured source.

    sv101's and lenny's RSS feeds already list every episode in one response,
    so fetch_sv101_episodes/fetch_lenny_episodes double as their archive
    fetch; crossingpodcast needs explicit pagination via
    fetch_crossingpodcast_archive.
    """
    episodes: List[RawEpisode] = []
    for name, fetch_fn, arg in (
        ("crossingpodcast", fetch_crossingpodcast_archive, crossingpodcast_api),
        ("sv101", fetch_sv101_episodes, sv101_rss_url),
        ("lenny", fetch_lenny_episodes, lenny_feed_url),
    ):
        try:
            episodes.extend(fetch_fn(arg))
        except Exception:
            logger.exception("Failed to fetch archive from source=%s; continuing with other sources", name)
    return episodes
