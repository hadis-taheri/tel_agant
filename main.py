"""Orchestrator: scrape -> transcribe -> summarize/translate -> post to Telegram.

Two coordinated workflows:
    Phase 1 - new episodes: each run checks sources for episodes published
        since the last check and processes all of them (capped at
        MAX_EPISODES_PER_RUN), marking them 'posted'.
    Phase 2 - historical backlog: each run also queues any not-yet-seen
        archive episodes as 'pending', then processes exactly one
        oldest-pending episode, marking it 'processed'. This works through a
        source's entire history gradually (e.g. one per day) instead of
        transcribing hundreds of old episodes at once.

Usage:
    python main.py                  # run phase 1 + one phase-2 backlog step, then exit
    python main.py --loop           # keep running both, every CHECK_INTERVAL_MINUTES
    python main.py --process-backlog  # run only the phase-2 backlog step
    python main.py --seed-only      # mark current archive as already-seen, skipping backlog
"""
import argparse
import logging
import sys
import time

from config import load_settings, Settings
import database
from database import EpisodeStore, Episode
import scraper
import transcriber
import summarizer
import image_generator
import telegram_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("agent.log", encoding="utf-8")],
)
logger = logging.getLogger("main")


def _run_pipeline(
    settings: Settings,
    store: EpisodeStore,
    episode_id: int,
    raw_ep: scraper.RawEpisode,
    final_status: str,
) -> None:
    """Transcribe -> summarize -> post an already-recorded episode row.

    Shared by both phase 1 (new episodes, final_status='posted') and phase 2
    (backlog episodes, final_status='processed').
    """
    try:
        logger.info("[%s] Transcribing: %s", episode_id, raw_ep.title)
        transcript = transcriber.transcribe_episode(
            audio_url=raw_ep.audio_url,
            tmp_dir=settings.temp_dir,
            groq_api_key=settings.groq_api_key,
            model=settings.groq_stt_model,
        )
        if not transcript.strip():
            raise ValueError("Empty transcript returned by STT")
        store.mark_status(episode_id, database.STATUS_TRANSCRIBED, transcript=transcript)

        logger.info("[%s] Summarizing/translating to Persian", episode_id)
        summary_html = summarizer.summarize_to_persian_html(
            transcript=transcript,
            episode_title=raw_ep.title,
            groq_api_key=settings.groq_api_key,
            model=settings.groq_llm_model,
        )
        store.mark_status(episode_id, database.STATUS_SUMMARIZED, summary_html=summary_html)

        photo_bytes = None
        try:
            photo_bytes = image_generator.get_episode_image(
                summary_html=summary_html,
                episode_title=raw_ep.title,
                groq_api_key=settings.groq_api_key,
                model=settings.groq_llm_model,
            )
        except Exception:
            logger.exception(
                "[%s] Topic image generation failed; falling back to a generic banner", episode_id
            )

        logger.info("[%s] Posting to Telegram", episode_id)
        message_id = telegram_bot.send_summary(
            bot_token=settings.telegram_bot_token,
            channel_id=settings.telegram_channel_id,
            summary_html=summary_html,
            photo_bytes=photo_bytes,
        )
        store.mark_status(episode_id, final_status, telegram_message_id=message_id)
        logger.info("[%s] Done: %s as Telegram message %s", episode_id, final_status, message_id)

    except Exception as exc:  # noqa: BLE001 - top-level per-episode guard
        logger.exception("[%s] Failed to process episode %r", episode_id, raw_ep.title)
        store.mark_failed(episode_id, exc)


def process_episode(settings: Settings, store: EpisodeStore, raw_ep: scraper.RawEpisode) -> None:
    """Phase 1: record and process a freshly-discovered episode, marking it 'posted'."""
    episode = Episode(
        source=raw_ep.source,
        external_id=raw_ep.external_id,
        title=raw_ep.title,
        episode_url=raw_ep.episode_url,
        audio_url=raw_ep.audio_url,
        published_at=raw_ep.published_at,
    )
    episode_id = store.insert_pending(episode)
    _run_pipeline(settings, store, episode_id, raw_ep, final_status=database.STATUS_POSTED)


def seed_existing_episodes(settings: Settings) -> int:
    """Mark every currently-found episode as already-seen, without processing it.

    Run this once right after setup so the first real run only picks up
    episodes published after that point, instead of backfilling the entire
    history of a source (e.g. sv101's RSS feed has 250+ past episodes).
    """
    store = EpisodeStore(settings.supabase_url, settings.supabase_key)
    raw_episodes = scraper.fetch_all_episodes(settings.crossingpodcast_api, settings.sv101_rss_url)

    count = 0
    for raw_ep in raw_episodes:
        if store.is_known(raw_ep.source, raw_ep.external_id):
            continue
        episode = Episode(
            source=raw_ep.source,
            external_id=raw_ep.external_id,
            title=raw_ep.title,
            episode_url=raw_ep.episode_url,
            audio_url=raw_ep.audio_url,
            published_at=raw_ep.published_at,
        )
        store.insert_seeded(episode)
        count += 1

    logger.info("Seeded %d existing episode(s) as already-seen.", count)
    return count


def run_once(settings: Settings) -> int:
    store = EpisodeStore(settings.supabase_url, settings.supabase_key)

    try:
        raw_episodes = scraper.fetch_all_episodes(settings.crossingpodcast_api, settings.sv101_rss_url)
    except Exception:
        logger.exception("Failed to fetch episode lists; skipping this run")
        return 0

    new_episodes = [ep for ep in raw_episodes if not store.is_known(ep.source, ep.external_id)]
    # Oldest first, so the Telegram channel receives them in chronological order.
    new_episodes.sort(key=lambda ep: ep.published_at or "")
    new_episodes = new_episodes[: settings.max_episodes_per_run]

    if not new_episodes:
        logger.info("No new episodes found.")
        return 0

    logger.info("Found %d new episode(s) to process (capped at %d per run).", len(new_episodes), settings.max_episodes_per_run)
    for raw_ep in new_episodes:
        process_episode(settings, store, raw_ep)

    return len(new_episodes)


def scrape_backlog(settings: Settings) -> int:
    """Queue any not-yet-seen archive episode from any source as 'pending'.

    Safe to call repeatedly: already-known episodes (whether seeded, pending,
    or already processed) are skipped.
    """
    store = EpisodeStore(settings.supabase_url, settings.supabase_key)
    try:
        raw_episodes = scraper.fetch_full_archive(settings.crossingpodcast_api, settings.sv101_rss_url)
    except Exception:
        logger.exception("Failed to fetch archive; skipping backlog scrape")
        return 0

    queued = 0
    for raw_ep in raw_episodes:
        if store.is_known(raw_ep.source, raw_ep.external_id):
            continue
        episode = Episode(
            source=raw_ep.source,
            external_id=raw_ep.external_id,
            title=raw_ep.title,
            episode_url=raw_ep.episode_url,
            audio_url=raw_ep.audio_url,
            published_at=raw_ep.published_at,
        )
        store.insert_pending(episode)
        queued += 1

    if queued:
        logger.info("Queued %d new archive episode(s) into the backlog.", queued)
    return queued


def process_backlog_once(settings: Settings) -> bool:
    """Phase 2: ensure the backlog is populated, then process exactly one
    oldest-pending episode, marking it 'processed'. Returns True if an
    episode was processed, False if the backlog is empty."""
    store = EpisodeStore(settings.supabase_url, settings.supabase_key)
    scrape_backlog(settings)

    row = store.get_oldest_pending()
    if not row:
        logger.info("Backlog is empty: no pending archive episodes to process.")
        return False

    logger.info("[%s] Processing oldest backlog episode: %r", row["id"], row["title"])
    raw_ep = scraper.RawEpisode(
        source=row["source"],
        external_id=row["external_id"],
        title=row["title"],
        episode_url=row["episode_url"],
        audio_url=row["audio_url"],
        published_at=row["published_at"],
    )
    _run_pipeline(settings, store, row["id"], raw_ep, final_status=database.STATUS_PROCESSED)
    return True


def daily_cycle(settings: Settings) -> None:
    """Phase 1 (new episodes) followed by phase 2 (one backlog episode)."""
    run_once(settings)
    process_backlog_once(settings)


def main() -> None:
    parser = argparse.ArgumentParser(description="Podcast scraping/transcription/summarization Telegram agent")
    parser.add_argument("--loop", action="store_true", help="Keep running and re-check sources periodically")
    parser.add_argument(
        "--seed-only",
        action="store_true",
        help="Mark all currently-found (latest-page) episodes as already-seen without processing them, "
        "then exit (skips backfilling a source's recent history; does not affect backlog processing)",
    )
    parser.add_argument(
        "--process-backlog",
        action="store_true",
        help="Only run the phase-2 backlog step: queue any new historical episodes as pending, "
        "then process exactly one oldest-pending episode, then exit",
    )
    args = parser.parse_args()

    settings = load_settings()

    if args.seed_only:
        seed_existing_episodes(settings)
        return

    if args.process_backlog:
        process_backlog_once(settings)
        return

    if not args.loop:
        daily_cycle(settings)
        return

    logger.info("Starting loop mode: checking every %d minute(s).", settings.check_interval_minutes)
    while True:
        try:
            daily_cycle(settings)
        except Exception:
            logger.exception("Unexpected error during daily_cycle; will retry next cycle")
        time.sleep(settings.check_interval_minutes * 60)


if __name__ == "__main__":
    main()
