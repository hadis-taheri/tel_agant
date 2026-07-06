"""Orchestrator: scrape -> transcribe -> summarize/translate -> post to Telegram.

Usage:
    python main.py            # run a single pass and exit
    python main.py --loop     # keep running, checking sources every
                               # CHECK_INTERVAL_MINUTES (see .env)
"""
import argparse
import logging
import sys
import time

from config import load_settings, Settings
from database import EpisodeStore, Episode
import scraper
import transcriber
import summarizer
import telegram_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("agent.log", encoding="utf-8")],
)
logger = logging.getLogger("main")


def process_episode(settings: Settings, store: EpisodeStore, raw_ep: scraper.RawEpisode) -> None:
    episode = Episode(
        source=raw_ep.source,
        external_id=raw_ep.external_id,
        title=raw_ep.title,
        episode_url=raw_ep.episode_url,
        audio_url=raw_ep.audio_url,
        published_at=raw_ep.published_at,
    )
    episode_id = store.insert_pending(episode)

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
        store.mark_status(episode_id, "transcribed", transcript=transcript)

        logger.info("[%s] Summarizing/translating to Persian", episode_id)
        summary_html = summarizer.summarize_to_persian_html(
            transcript=transcript,
            episode_title=raw_ep.title,
            groq_api_key=settings.groq_api_key,
            model=settings.groq_llm_model,
        )
        store.mark_status(episode_id, "summarized", summary_html=summary_html)

        logger.info("[%s] Posting to Telegram", episode_id)
        source_label = "sv101" if raw_ep.source == scraper.SOURCE_SV101 else "Crossing Podcast"
        message_id = telegram_bot.send_summary(
            bot_token=settings.telegram_bot_token,
            channel_id=settings.telegram_channel_id,
            summary_html=summary_html,
            episode_url=raw_ep.episode_url,
            source_label=source_label,
        )
        store.mark_status(episode_id, "posted", telegram_message_id=message_id)
        logger.info("[%s] Done: posted as Telegram message %s", episode_id, message_id)

    except Exception as exc:  # noqa: BLE001 - top-level per-episode guard
        logger.exception("[%s] Failed to process episode %r", episode_id, raw_ep.title)
        store.mark_failed(episode_id, exc)


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Podcast scraping/transcription/summarization Telegram agent")
    parser.add_argument("--loop", action="store_true", help="Keep running and re-check sources periodically")
    parser.add_argument(
        "--seed-only",
        action="store_true",
        help="Mark all currently-found episodes as already-seen without processing them, then exit "
        "(run this once after setup to skip backfilling a source's entire history)",
    )
    args = parser.parse_args()

    settings = load_settings()

    if args.seed_only:
        seed_existing_episodes(settings)
        return

    if not args.loop:
        run_once(settings)
        return

    logger.info("Starting loop mode: checking every %d minute(s).", settings.check_interval_minutes)
    while True:
        try:
            run_once(settings)
        except Exception:
            logger.exception("Unexpected error during run_once; will retry next cycle")
        time.sleep(settings.check_interval_minutes * 60)


if __name__ == "__main__":
    main()
