"""Standalone daily-digest sender for the interactive subscriber bot.

This is a self-contained add-on feature, deliberately kept separate from the
core podcast pipeline (main.py/config.py/database.py) so it can be deleted
without touching anything else -- see subscribers_schema.sql for the removal
procedure. The only thing it reuses from the existing codebase is
telegram_bot.send_summary() (a one-way import; telegram_bot.py has no
knowledge of this module).

What it does, once per run:
    1. Compute "now" in Asia/Tehran (Iran has no DST, so this is a fixed
       +03:30 offset -- no tzdata gymnastics needed).
    2. Find every active subscriber whose alarm_hour has arrived and who
       hasn't already been sent today's digest (see get_due_subscribers).
    3. For each, gather every episode summary that landed in the channel in
       the last 24 hours (by `updated_at`, not `published_at` -- backlog
       episodes carry their original publish date from years ago;
       `updated_at` is when the episode actually appeared in the channel).
    4. Send it (or a "nothing new today" message if empty), then mark
       last_sent_date so the same user isn't re-sent the same day.

Run directly: `python digest.py`. Scheduled via
.github/workflows/daily-digest.yml, a separate workflow from the one that
runs main.py -- this script is fully independent of that pipeline's state.
"""
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from supabase import create_client, Client

from telegram_bot import send_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("digest")

# A fixed offset, not zoneinfo.ZoneInfo("Asia/Tehran"): Iran has no DST, so
# the offset never changes, and this avoids a real portability trap --
# Python's zoneinfo has no bundled tz database on Windows (and on some slim
# Linux images), so ZoneInfo() raises ZoneInfoNotFoundError unless the
# `tzdata` PyPI package is installed. A fixed offset needs no extra
# dependency and works identically everywhere, keeping this script's only
# dependency on the rest of the repo a one-way import of telegram_bot.py
# (see module docstring) -- no shared requirements.txt entry needed either.
TEHRAN_TZ = timezone(timedelta(hours=3, minutes=30))

SUBSCRIBERS_TABLE = "subscribers"
EPISODES_TABLE = "episodes"
# Must match database.py's STATUS_POSTED / STATUS_PROCESSED. Duplicated here
# rather than imported, on purpose -- see module docstring on staying
# self-contained.
FINALIZED_STATUSES = ("posted", "processed")

NO_NEW_EPISODES_MESSAGE = "امروز پادکست تازه‌ای توی کانال منتشر نشده بود. 🙂"


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _load_client() -> Client:
    return create_client(_require_env("SUPABASE_URL"), _require_env("SUPABASE_KEY"))


def get_due_subscribers(client: Client, tehran_now: datetime) -> list[dict]:
    """Return active subscribers whose alarm hour has arrived and who
    haven't been sent today's (Tehran calendar date) digest yet.

    Uses `alarm_hour <= current hour` (not `==`) so a subscriber whose exact
    hour was missed -- e.g. the GitHub Actions schedule trigger dropping a
    tick, a known unreliability documented in this repo's CLAUDE.md -- still
    gets caught up later the same day instead of being skipped entirely.
    """
    today = tehran_now.date().isoformat()
    resp = (
        client.table(SUBSCRIBERS_TABLE)
        .select("*")
        .eq("active", True)
        .not_.is_("alarm_hour", "null")
        .lte("alarm_hour", tehran_now.hour)
        .execute()
    )
    return [row for row in resp.data if row.get("last_sent_date") != today]


def get_recent_summaries(client: Client, hours: int = 24) -> list[dict]:
    """Every finalized episode's summary_html that landed in the channel in
    the last `hours` hours, oldest first."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    resp = (
        client.table(EPISODES_TABLE)
        .select("id, title, summary_html, updated_at")
        .in_("status", FINALIZED_STATUSES)
        .gte("updated_at", since)
        .order("updated_at", desc=False)
        .execute()
    )
    return [row for row in resp.data if row.get("summary_html")]


def mark_sent(client: Client, chat_id: int, tehran_today: str) -> None:
    client.table(SUBSCRIBERS_TABLE).update(
        {"last_sent_date": tehran_today, "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("chat_id", chat_id).execute()


def send_due_digests() -> int:
    """Send today's digest to every subscriber whose alarm hour has arrived.
    Returns how many subscribers were sent to."""
    bot_token = _require_env("TELEGRAM_BOT_TOKEN")
    client = _load_client()

    tehran_now = datetime.now(TEHRAN_TZ)
    today = tehran_now.date().isoformat()

    due = get_due_subscribers(client, tehran_now)
    if not due:
        logger.info("No subscribers due at Tehran time %s.", tehran_now.strftime("%H:%M"))
        return 0

    summaries = get_recent_summaries(client, hours=24)
    logger.info(
        "%d subscriber(s) due; %d episode summary(ies) from the last 24h.",
        len(due), len(summaries),
    )

    sent = 0
    for subscriber in due:
        chat_id = subscriber["chat_id"]
        try:
            if summaries:
                for ep in summaries:
                    send_summary(bot_token=bot_token, channel_id=str(chat_id), summary_html=ep["summary_html"])
            else:
                send_summary(bot_token=bot_token, channel_id=str(chat_id), summary_html=NO_NEW_EPISODES_MESSAGE)
            mark_sent(client, chat_id, today)
            sent += 1
            logger.info("[%s] Sent digest (%d episode(s)).", chat_id, len(summaries))
        except Exception:  # noqa: BLE001 - one subscriber's failure shouldn't block the rest
            logger.exception("[%s] Failed to send digest", chat_id)

    return sent


if __name__ == "__main__":
    send_due_digests()
