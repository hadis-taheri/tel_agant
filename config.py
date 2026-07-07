"""Central configuration loaded from environment variables (.env)."""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_key: str

    groq_api_key: str
    groq_stt_model: str
    groq_llm_model: str

    telegram_bot_token: str
    telegram_channel_id: str

    crossingpodcast_api: str
    sv101_rss_url: str

    check_interval_minutes: int
    max_episodes_per_run: int
    min_backlog_interval_minutes: int
    temp_dir: str


def load_settings() -> Settings:
    return Settings(
        supabase_url=_require("SUPABASE_URL"),
        supabase_key=_require("SUPABASE_KEY"),
        groq_api_key=_require("GROQ_API_KEY"),
        groq_stt_model=os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo"),
        groq_llm_model=os.getenv("GROQ_LLM_MODEL", "qwen/qwen3.6-27b"),
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        telegram_channel_id=_require("TELEGRAM_CHANNEL_ID"),
        crossingpodcast_api=os.getenv(
            "CROSSINGPODCAST_API", "https://crossingpodcast.com/api/trpc/episodes.list"
        ),
        sv101_rss_url=os.getenv("SV101_RSS_URL", "https://feeds.fireside.fm/sv101/rss"),
        check_interval_minutes=int(os.getenv("CHECK_INTERVAL_MINUTES", "60")),
        max_episodes_per_run=int(os.getenv("MAX_EPISODES_PER_RUN", "3")),
        min_backlog_interval_minutes=int(os.getenv("MIN_BACKLOG_INTERVAL_MINUTES", "50")),
        temp_dir=os.getenv("TEMP_DIR", "./tmp_audio"),
    )
