"""Central configuration for the yt_relay add-on, loaded from environment
variables (.env).

Deliberately does NOT use config.load_settings() from the core podcast
pipeline -- that loader requires podcast-specific variables (Groq STT model,
crossingpodcast/sv101 URLs, etc.) that have nothing to do with this feature,
and importing it would break this package's isolation from main.py's world.
Same pattern as digest.py's own _require_env().
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class YtSettings:
    supabase_url: str
    supabase_key: str

    groq_api_key: str
    groq_llm_model: str

    telegram_bot_token: str
    telegram_channel_id: str

    enabled: bool
    dry_run: bool
    post_style: str  # "preview" | "photo" -- see CLAUDE.md section 5.1

    max_age_hours: int
    max_posts_per_day: int
    min_hours_between_posts: int
    max_captions_per_run: int
    ready_queue_max: int


def load_settings() -> YtSettings:
    return YtSettings(
        supabase_url=_require("SUPABASE_URL"),
        supabase_key=_require("SUPABASE_KEY"),
        # YT_RELAY_GROQ_API_KEY is optional and takes priority when set --
        # the podcast pipeline's own MIN_BACKLOG_INTERVAL_MINUTES math
        # (see CLAUDE.md) already assumes it has the *entire* 200k-tokens/day
        # Groq cap to itself; sharing GROQ_API_KEY with this feature eats a
        # real (if modest, ~3-8%) slice of that margin. Falls back to the
        # shared GROQ_API_KEY if no dedicated key is configured -- same
        # dedicated-key pattern the original spec called for with
        # ANTHROPIC_API_KEY_YT_RELAY, just optional here since a second Groq
        # account has to be created by hand (this loader can't do that).
        groq_api_key=os.getenv("YT_RELAY_GROQ_API_KEY") or _require("GROQ_API_KEY"),
        groq_llm_model=os.getenv("GROQ_LLM_MODEL", "qwen/qwen3.6-27b"),
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        telegram_channel_id=_require("YT_TELEGRAM_CHANNEL_ID"),
        enabled=os.getenv("YT_RELAY_ENABLED", "true").lower() == "true",
        dry_run=os.getenv("YT_RELAY_DRY_RUN", "true").lower() == "true",
        post_style=os.getenv("YT_POST_STYLE", "photo"),
        max_age_hours=int(os.getenv("MAX_AGE_HOURS", "48")),
        max_posts_per_day=int(os.getenv("MAX_POSTS_PER_DAY", "8")),
        min_hours_between_posts=int(os.getenv("MIN_HOURS_BETWEEN_POSTS", "3")),
        max_captions_per_run=int(os.getenv("YT_MAX_CAPTIONS_PER_RUN", "3")),
        ready_queue_max=int(os.getenv("YT_READY_QUEUE_MAX", "8")),
    )
