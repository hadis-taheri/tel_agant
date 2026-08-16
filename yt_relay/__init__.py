"""YouTube -> Telegram relay add-on.

Standalone feature, deliberately isolated from the core podcast pipeline
(main.py/scraper.py/summarizer.py/database.py/config.py) -- see this
package's individual module docstrings, yt_relay_schema.sql, and CLAUDE.md
for the full architecture and the reasoning behind each deviation from
youtube-telegram-relay-spec.md (the original TypeScript/pnpm design this
was adapted from).

Nothing outside this package imports from it, and this package only ever
imports two things from the rest of the repo: telegram_bot.py (for sending)
and summarizer._generate_once (for a hardened Groq call). To remove this
feature entirely: drop the yt_channels/yt_queue tables (see
yt_relay_schema.sql), delete this package, and delete
.github/workflows/yt-relay.yml.
"""
