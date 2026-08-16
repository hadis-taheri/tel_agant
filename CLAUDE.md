# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Setup:
```bash
pip install -r requirements.txt
```
ffmpeg must also be on PATH (used by `pydub` to chunk long audio files before sending to Whisper).

Run:
```bash
python main.py                    # phase 1 (new episodes) + one phase-2 backlog episode, then exit
python main.py --loop              # same, repeated every CHECK_INTERVAL_MINUTES (see .env)
python main.py --process-backlog   # only the phase-2 backlog step
python main.py --seed-only         # mark current latest-page episodes as seen, skip processing them
```

There is no test suite, linter, or build step in this repo. To sanity-check a single module during
development, exercise it directly against the live services it wraps, e.g.:
```bash
python -c "import scraper; print(len(scraper.fetch_crossingpodcast_archive('https://crossingpodcast.com/api/trpc/episodes.list')))"
python -c "import config, summarizer; s = config.load_settings(); print(summarizer.summarize_to_persian_html('...', 'title', s.groq_api_key, s.groq_llm_model))"
```
Config (`config.load_settings()`) requires a populated `.env` (see `.env.example`), since it talks to
real Supabase/Groq/Telegram accounts — there is no mock/test mode.

## Architecture

Pipeline: `scraper.py` (find episodes) -> `transcriber.py` (audio -> text via Groq Whisper) ->
`summarizer.py` (text -> long-form Persian HTML via Groq LLM, itself a two-step pivot through
English — see below) -> `telegram_bot.py` (post to channel as plain text, no photo), all
coordinated by `main.py`, with `database.py` (Supabase) as the single source of truth for what has
and hasn't been processed. `config.py` loads all settings from `.env` and raises immediately if a
required var is missing — there's no partial/degraded startup.

`image_generator.py` (topic-image generation via a Groq-written prompt + Pollinations.ai) still
exists in the repo but is **not called** by `main.py` — posts are text-only by design (see the
Telegram delivery note below). It's dead code kept around in case image posts are re-enabled later;
same for `telegram_bot.py`'s former banner-fallback assets under `assets/topic_banner_*.jpg` and
`gen_banners.py`.

**Episode identity and dedup**: every episode is keyed by `(source, external_id)`, unique in the
`episodes` table. `external_id` is the crossingpodcast slug or the sv101 RSS GUID.
`EpisodeStore.is_known()` is the dedup check used everywhere before inserting a row — nothing else
in the codebase decides whether an episode is new.

**Status state machine** (single `status` column, no enum/CHECK constraint — see `database.py`
docstring for the full list): `pending` -> `downloading` -> `transcribed` -> `summarized` ->
(`posted` | `processed`) on success, or `failed` at any point. `seeded` is a separate terminal state
meaning "explicitly skipped, never process". The only branch is the final status: phase 1 uses
`posted`, phase 2 uses `processed` — everything else in `_run_pipeline()` (main.py) is shared.

**Two coordinated workflows in main.py**:
- Phase 1 (`run_once`): fetches only the latest page/feed from each source (cheap), processes
  every not-yet-known episode found there (capped by `MAX_EPISODES_PER_RUN`), oldest first.
- Phase 2 (`scrape_backlog` + `process_backlog_once`): walks each source's *entire* historical
  archive, queues anything not-yet-known as `pending`, then processes exactly **one**
  newest-`pending` row per invocation *at most once every `MIN_BACKLOG_INTERVAL_MINUTES`
  (default 75, chosen to land at ~18-19 real episodes/day -- see the Groq TPD note below)* — this
  is deliberate throttling, both to work through hundreds of old episodes
  one-at-a-time instead of all at once on first run, and to cap real Groq token spend per day
  (see the scheduling note below for why the second part matters: the GitHub Actions cron fires
  4x/hour, not once, so without a wall-clock throttle every firing that lands would process a
  *different* episode). `scrape_backlog` (queueing) always runs regardless of the throttle since
  it costs no LLM tokens; only the actual `_run_pipeline` call is gated. Newest-pending-first (not
  oldest-first) is deliberate, by request: each source's backlog catches up to its recent episodes
  quickly, with the oldest ones trickling in last instead of the channel spending months working
  through years-old episodes before ever reaching current ones.
  `EpisodeStore.get_newest_pending(exclude_source=...)` alternates which source that one episode
  comes from: `process_backlog_once` looks up the source of the most-recently-finalized episode
  (`get_last_finalized`) and prefers the *other* source's newest pending row, falling back to any
  pending row if that source's backlog is empty. This means consecutive backlog runs alternate
  crossingpodcast/sv101 instead of draining one source's entire archive before ever touching the
  other.
- `daily_cycle()` just runs phase 1 then phase 2; `--loop` repeats `daily_cycle` on a timer.
  `main.py`'s own `_run_pipeline()` is the only place that actually calls transcriber/summarizer/
  telegram_bot — phase 1 and phase 2 both funnel through it with a different `final_status`.

**scraper.py source quirks** (both discovered empirically, not documented anywhere upstream):
- crossingpodcast.com has no public RSS; it's a client-rendered SPA backed by an undocumented tRPC
  endpoint (`/api/trpc/episodes.list?input={"json":{...}}`). Passing `{"page": N}` paginates it
  20-items-per-page — this is how `fetch_crossingpodcast_archive` walks the full history.
  `fetch_crossingpodcast_episodes` (no page param = page 1) is the cheap "latest only" call used by
  phase 1. The site's audio is Chinese (source podcast is hosted on xiaoyuzhoufm.com) despite
  English-translated titles/summaries being available in the API response.
- sv101.fireside.fm has a normal RSS feed, and feedparser returns the *entire* history in one
  request — so `fetch_sv101_episodes` doubles as both the "latest" and "archive" fetch for that
  source; there's no separate archive function for it.

**LLM model choice**: `GROQ_LLM_MODEL` defaults to `qwen/qwen3.6-27b`, not a Llama model. Both
podcast sources here are Chinese-language, and `llama-3.3-70b-versatile` proved unreliable at
translating Chinese specifically (see incident below) — Qwen is Alibaba-trained and handles
Chinese/Persian/English all noticeably better in practice. Qwen3 models are "hybrid reasoning"
models that default to an extremely verbose `<think>...</think>` preamble that can burn through
`max_tokens` before ever producing the real answer; `_generate_once()` passes
`reasoning_effort="none"` for any model matching `_REASONING_MODEL_PREFIXES` to disable that
(passing this param to a non-reasoning model like Llama is a hard 400 error, hence the
model-name check rather than passing it unconditionally). If you change `GROQ_LLM_MODEL` to another
reasoning model family, extend `_REASONING_MODEL_PREFIXES` accordingly. Also note `qwen/qwen3-32b`
(the other Qwen model on this Groq account) has a much lower per-model TPM limit (6,000 vs. this
project's usual ~8,000 for `qwen/qwen3.6-27b`, measured directly against a live request — see
`MAX_TRANSCRIPT_BYTES` in summarizer.py) and fails on transcripts at the current
`MAX_TRANSCRIPT_BYTES` — it was tested and rejected for that reason, not a typo.

**Proper-noun handling is imperfect even with an explicit rule**: the prompt's rule 1 lists concrete
wrong/right pairs (e.g. "تسلا/تلسا اشتباه، Tesla درست است") rather than just naming allowed English
terms, because a real incident showed the model transliterating company names (Tesla -> تسلا/تلسا,
inconsistently even spelled wrong) and mangling place names it didn't recognize as protected
(Wall Street -> nonsense like "دیووال استراک") when the examples list only covered AI-specific
tools. Broadening the rule to "any proper noun: companies, products, people, well-known
financial/tech place names" plus lowering `temperature` (0.4 -> 0.3) reduced but did not eliminate
this -- expect occasional exceptions (e.g. "سیلیکون‌ولی" instead of "Silicon Valley") on new
transcripts; this is LLM variance, not a regression, unless it starts producing garbled non-words
again as it did before the fix.

**summarizer.py translates via a two-step English pivot, not Chinese -> Persian directly**:
`summarize_to_persian_html` first calls `_translate_to_english()` to turn the raw transcript
(English or Chinese) into a detailed English summary, then rewrites *that* into the final Persian
post. This exists because direct Chinese -> long-form Persian proved unreliable specifically for
crossingpodcast: its transcripts are dense, almost entirely Chinese conversation (unlike sv101,
which reads as more English-anchored already), and two different crossingpodcast backlog episodes
came back 72-74% non-Persian after all 3 retries at the ~3400-3700-char target before this fix —
both succeeded on the first attempt afterward. Chinese -> English is a far more common/reliable
task for this model than Chinese -> Persian directly; English -> Persian was already the reliable
half of this pipeline. Applies uniformly to both sources (not just crossingpodcast) to keep the
pipeline logic simple — it doesn't hurt sv101's already-good results.

Each step has its own foreign-script leak check, since each targets a different output language:
`_translate_to_english` retries (`BRIDGE_GENERATION_ATTEMPTS`) if too much non-ASCII leaks into
what should be all-English (`_NON_ASCII_RE`, `_MAX_NON_ASCII_RATIO`), and **raises** if that
persists — a broken bridge summary means the Persian step has nothing reliable to work from. The
Persian step keeps its original check: the LLM occasionally leaks stray non-Persian-script
characters (Chinese/Cyrillic/Hangul/Kana) into otherwise-Persian output; `summarize_to_persian_html`
regenerates up to `GENERATION_ATTEMPTS` times when `_FOREIGN_SCRIPT_RE` matches, and below
`_MAX_FOREIGN_SCRIPT_RATIO` (~15%) strips the foreign runs and ships the result, but above it
**raises** instead of stripping. That split exists because of a real incident: on one episode the
model didn't leak a few stray characters, it answered ~90% in Chinese outright, and stripping
"foreign" characters left behind only numbers/punctuation/English company names — a near-empty,
meaningless message that still got posted to the live channel before that fix. Don't remove either
half of either check without re-testing against real (not just English) transcripts. The prompt
also has an explicit, deliberate rule: tool/model/company names (OpenAI, Claude Code, Anthropic,
etc.) must stay in English/Latin script, never transliterated.

**Both prompt stages explicitly skip promotional/administrative filler**: a live post once included
host self-introductions and a New Year book-giveaway promo ("share this episode on WeChat
Moments/Jike, screenshot it in Xiaoyuzhou's comments") lifted straight from the podcast's own
intro — noise that doesn't belong in a content summary. `_ENGLISH_BRIDGE_SYSTEM_PROMPT` now
instructs the bridge step to skip host/guest introductions, show-format explainers, sponsor reads,
subscribe/follow requests, giveaways/contests, and closing pleasantries entirely rather than
summarizing them; `SYSTEM_PROMPT`'s rule 2 is a safety net in case any of that still slips through
into the English bridge text anyway.

**Transcript truncation is byte-based, not char-based, on purpose**: `MAX_TRANSCRIPT_BYTES` in
`summarizer.py` truncates by UTF-8 byte length before the request goes to Groq (only the bridge
step, `_translate_to_english`, ever sees the raw transcript — see the two-step-pivot note above).
This was a real production bug, not a style choice — a character-count cap sized for English
silently let Chinese transcripts (≈3 bytes/char, and denser tokenization than English) through at
2-3x the intended request size, which passed local testing (English-only) but hit Groq's
tokens-per-minute limit for this model (measured at 8,000 TPM, not the ~12,000 assumed earlier)
with a `413 Request too large` error on a real Chinese sv101 episode in production. 25,000 bytes
was re-verified directly against a live request to cost ~5,700 prompt tokens; combined with the
bridge step's own completion budget (1,500 tokens), that first call runs ~7,000-7,200 total tokens
— close to the 8,000 ceiling on its own. The second (Persian) call is much cheaper since its input
is the compact English bridge summary rather than the raw transcript, but the two calls landing in
the same 60-second window can still push the *combined* total over 8,000 and trigger a 429 —
`_generate_once`'s existing `@retry` with exponential backoff absorbs this automatically (confirmed
in practice), just adding some latency per episode rather than failing it. Re-verify the per-call
math against the actual account limit before raising `MAX_TRANSCRIPT_BYTES` or either step's
`max_tokens`.

There is *also* a separate, larger-window cap: **200,000 tokens/day (TPD)** for this model on this
account, confirmed by hitting it directly (`Limit 200000, Used 199151`) during a heavy day of
manual debugging. A normal two-step-pivot episode (bridge + Persian) costs roughly 10,000-11,000
tokens total, so 200k/day supports on the order of 18-19 real episodes/day *before accounting for
any retries* — plenty for the intended cadence (about one backlog episode/hour) but not something
to take for granted if `MIN_BACKLOG_INTERVAL_MINUTES` (see the phase-2 note above) is ever removed
or lowered carelessly. Manual one-off debugging/regeneration (like the kind used to verify each fix
in this history) burns through this same daily budget fast — expect to occasionally hit this cap
during active development and just wait for the daily reset rather than assume the code is broken.

**Failed episodes aren't automatically retried**: `EpisodeStore.is_known()` only checks
`(source, external_id)` existence, not status — a row with `status='failed'` is still "known" and
will never be picked up again by `run_once`/`process_backlog_once` on its own. To retry one,
either delete its row or manually reset its `status` back to `pending` (see README's "نکات مهم"
section) — there's no code path that does this automatically.

**Telegram delivery is plain text, by design (no photo)**: `telegram_bot.send_summary()` posts the
whole summary via `send_message`, not `send_photo`. This is why `summarizer.py`'s prompt targets
~3400-3700 chars across 6-9 paragraphs, deliberately written to be interactive (questions and
"تصور کن..." breaks scattered through the body, not just a hook-and-close) — Telegram's plain-text
message limit is 4096 characters, much roomier than the 1024-char photo-caption limit this project
used to target, so the summary can read like a full podcast digest rather than a short blurb. If the
LLM overshoots that budget anyway, `summarizer._fit_to_text_limit()` trims on a clean boundary
(paragraph break or sentence-ending punctuation) and re-closes any HTML tag left open by the cut;
`telegram_bot._split_message()` is a further fallback that splits into multiple messages on
paragraph breaks if a summary somehow still exceeds 4096 chars. Don't raise the ~3400-3700-char
prompt target without re-checking the token math in the `MAX_TRANSCRIPT_BYTES` comment above still
holds (raising the completion length eats into the same per-minute token budget as the transcript).

**`telegram_bot.SAFE_CHUNK_LEN` must stay >= `summarizer`'s own single-message ceiling**: a real
incident — `SAFE_CHUNK_LEN` was 3800 while `summarizer.py`'s own limit
(`TELEGRAM_TEXT_MAX_LEN - _TEXT_SAFETY_MARGIN`) is 3996, so a 3976-char summary that `summarizer.py`
already considered "fits in one message" got needlessly cut into two Telegram messages by
`_split_message`'s stricter threshold anyway. `_split_message` is documented as a fallback for if
summarizer's own guarantee "ever doesn't" hold, not a second, tighter limit — raised to 4000 so it
only fires for an actual guarantee failure. Keep it just under 4096 and above whatever
`summarizer.py` currently targets if either budget changes.

**Every paragraph is prefixed with a Unicode RTL mark before sending**: reported symptom was
Telegram posts not rendering right-to-left consistently, with odd-looking leading gaps on some
paragraphs. Root cause: Telegram (like most renderers) picks each paragraph's base text direction
from its first *strong-directional* character, and an opening `<b>` tag has none — so a paragraph
that happens to start with an English proper noun (common here: "Tesla ...", "OpenAI ...") gets
misdetected as LTR even though the rest is Persian. `summarizer._finalize()` now runs the output
through `_force_rtl_paragraphs()`, which prefixes every paragraph with U+200F (RIGHT-TO-LEFT MARK,
written as an explicit `‏` escape in the source rather than a literal invisible character —
see the bidi-corruption incident below for why that distinction matters here specifically). No
visible change to the text, just forces RTL regardless of the first character.

There's deliberately no link back to the source episode in the post: Telegram auto-previews any URL
in a message, and the source pages (crossingpodcast.com, fireside.fm) are Chinese, so a source link
pulled in a Chinese title/description/cover image via the auto-preview. Image generation
(`image_generator.py`, Pollinations.ai) and the local banner fallback (`assets/topic_banner_*.jpg`,
`gen_banners.py`) were built for an earlier version of this pipeline that posted photo+caption, but
are currently unused — `main.py` no longer calls `image_generator`, and `telegram_bot.py` no longer
falls back to a banner. Posts are text-only until/unless that's revisited.

**Every post ends with a source-attribution footer** (in lieu of the link back described above):
the episode's original title (accurately, literally translated to Persian — not the creative
headline `SYSTEM_PROMPT` generates for the post body), its episode number if the source embeds one,
its publish date, and which source (`Crossing Podcast` / `Silicon Valley 101 (SV101)`) it came from.
The publish date is shown Gregorian (`"YYYY-MM-DD"`, e.g. `2026-07-15`), by request — not Jalali —
so no new date-conversion dependency was needed. `scraper.format_published_date()` normalizes the
two sources' different raw formats (crossingpodcast's API gives ISO 8601 with a `Z` suffix; sv101's
RSS feed gives RFC 822) into that single display format, returning `None` (footer omits the line)
if `published_at` is missing or neither format parses.
`summarizer._translate_title_to_persian()` does a small, separate, non-creative LLM call for the
title translation (cheap relative to the main two-step pivot: short input/output, so it doesn't
meaningfully touch the per-episode or 200k-tokens/day budgets documented above); on failure it falls
back to the original untranslated title rather than raising, since a broken footer line isn't worth
failing an otherwise-good episode over. `scraper.extract_episode_number()` pulls the number from the
title text itself — sv101 embeds one (`"E244｜..."` on recent episodes, plain `"10: ..."` on older
ones), crossingpodcast doesn't (its API only exposes an internal db id, not a public episode number),
so the footer simply omits the number line for crossingpodcast rather than showing something
misleading. `scraper.strip_episode_number_prefix()` removes that same prefix before the title goes
to translation, so the number isn't duplicated inside the translated title text.
`summarizer._finalize()` reserves room for the footer's exact length *before* trimming the main body
to the Telegram character budget, then appends the footer last — so the footer is never itself
subject to truncation and the combined body+footer can't push the post over Telegram's hard limit.

## External accounts this project depends on

Supabase (Postgres), Groq (STT + LLM), and a Telegram bot/channel are real, already-provisioned
accounts (not swappable test doubles) — see README.md for the current project's specific setup
steps and the GitHub Actions schedule (`.github/workflows/daily-post.yml`, secrets documented
there) that runs `python main.py`.

**GitHub's `schedule` trigger is not reliable enough to fire once/hour for this repo**: confirmed
directly against the Actions API on 2026-07-07 — only 2 of ~10 expected hourly runs fired over a
10-hour window, and even after moving off the top-of-hour minute (GitHub's documented congestion
advice), the very next scheduled run still didn't fire at all. GitHub's schedule event has no SLA
and can simply be dropped, independent of which minute it's queued for. The cron now fires 4x/hour
(`7,22,37,52 * * * *`) so a single dropped tick doesn't cost a whole hour. `main.py` won't
double-post from this (dedup by `(source, external_id)`), but extra runs landing in the same hour
*would* each process a different backlog episode and pay real Groq tokens for it if nothing else
stopped them — a real risk once discovered in practice: at ~10-11k tokens/episode (two-step
pivot, see summarizer.py notes above), 4 real episodes/hour for a full day would blow through
Groq's 200k-tokens/day cap in a matter of hours. `process_backlog_once`'s wall-clock throttle
(`MIN_BACKLOG_INTERVAL_MINUTES`, see the phase-2 note above) is what actually keeps this safe —
the 4x/hour cron is purely a safety net for GitHub dropping ticks, not a multiplier on real work.
If posts stop appearing again, check the Actions run list
(`https://api.github.com/repos/<owner>/<repo>/actions/workflows/daily-post.yml/runs`) before
assuming the pipeline code itself is broken.

**GitHub Actions secret gotcha**: if a secret value (e.g. `SUPABASE_KEY`) is copy-pasted from a
terminal/chat UI that renders mixed RTL (Persian) and LTR (English/token) text, invisible Unicode
bidi control characters can get copied along with it and silently corrupt the secret. Symptom in
Actions logs: `UnicodeEncodeError: 'ascii' codec can't encode characters in position N-M` inside
an HTTP client's header-construction code (e.g. `postgrest`/`httpx`), even though the source value
is verified pure-ASCII. Fix: re-copy the value from a plain source (e.g. the `.env` file opened in
a plain text editor), not from any RTL-rendering terminal/chat output.

**A third source (Lenny's Podcast, lennysnewsletter.com) was added and then reverted** -- worth
knowing before trying again with any Substack-hosted podcast. Discovery worked fine (Substack's
dedicated podcast RSS feed, `api.substack.com/feed/podcast/<show-id>.rss`, returns full history
same as sv101), but actually downloading the audio consistently failed with `403 Forbidden`
specifically when run from GitHub Actions runners -- confirmed via direct comparison: the exact
same `api.substack.com/feed/podcast/.../*.mp3` URL that 403'd on Actions returned `200 OK` from a
residential/dev network every time. This matches Cloudflare's known pattern of blocking/challenging
requests from cloud-provider IP ranges (AWS/Azure/GCP/GitHub Actions) more aggressively than from
consumer networks. Giving `download_audio()` a browser-style User-Agent and retry (see
`transcriber.py` -- kept, since it's a reasonable general hardening and crossingpodcast has hit its
own unrelated connection failures before) did **not** fix it; the block persisted across retries,
meaning it's IP-reputation-based, not a transient rate limit or a UA fingerprint issue. Before
re-adding a Substack-hosted (or any Cloudflare-fronted) source, test the actual audio *download*
specifically from a GitHub Actions run, not just from a dev machine -- discovery/RSS endpoints
being reachable does not mean the CDN-hosted audio file will be.

## Subscriber daily-digest (standalone add-on, currently dormant)

A second, independent feature: a user sets an alarm hour (Tehran time) via an interactive bot menu,
and once a day at that hour gets every episode summary posted to the channel in the last 24h, sent
to them in their private chat. Deliberately isolated from the core pipeline above -- it reads from
`episodes` but nothing in `main.py`/`scraper.py`/etc. knows this feature exists, so it can be fully
removed (drop the `subscribers` table, delete `digest.py`, `serverless-bot/`, and
`.github/workflows/daily-digest.yml`) without touching anything else. See `subscribers_schema.sql`'s
header for the exact removal steps.

**Two halves, two different runtimes**:
- `digest.py` -- the sender. Plain Python, runs via its own GitHub Actions workflow
  (`daily-digest.yml`, same 4x/hour cron-reliability pattern as `daily-post.yml`, fully separate so a
  failure here can't affect the podcast pipeline's schedule). Reads due subscribers from Supabase,
  reuses `telegram_bot.send_summary()` (only one-way dependency on the existing codebase) to deliver.
- `serverless-bot/` -- the interactive half (`/start`, alarm-hour menu, `/stop`), meant to run on
  **Telegram Serverless** (`core.telegram.org/bots/serverless`, JavaScript, no server to host). It's
  the *same* bot as the channel (same token) -- confirmed safe: the bot's webhook (serverless side)
  only ever replies to the chat_id of whoever messaged it (Telegram supplies this server-side, not
  spoofable by the user), and never touches the channel-posting code path at all, so there's no
  cross-talk between "a user clicked a button" and "something gets posted to the channel."

**Not deployed as of 2026-07-19**: Telegram Serverless has no "Serverless" menu item on this bot's
BotFather yet (checked both the bot's main menu and Bot Settings submenu) -- most likely a gradual
platform rollout that hasn't reached this account/bot. `npx tgcloud login` (the one-time step that
links a project to a bot and can't be scripted around) also turned out to need two things not
obvious from the docs: it hard-requires an interactive terminal (fails immediately in any
programmatic/non-TTY shell, i.e. an agent can't run it), and its non-interactive escape hatch
(`TGCLOUD_TOKEN` env var) is **not** the Telegram bot token -- it's a separate `app<id>:<secret>`
CLI Access token that BotFather issues once Serverless is turned on for that bot, which is exactly
the menu item that's missing. Until Serverless appears in BotFather, this half can't be deployed at
all. `subscribers` has zero rows either way, so leaving the digest workflow's schedule enabled with
nothing to send is harmless (confirmed: public repo, so GitHub Actions minutes are unlimited here
regardless -- see below).

**RLS gap found and fixed while testing the `anon` key**: `episodes` (the core pipeline's table)
never had row-level security enabled -- harmless as long as only `service_role` (server-side,
bypasses RLS) was ever used, which was true until this feature introduced an `anon` key embedded in
public-ish JS bot code. Confirmed directly: before enabling RLS, the anon key could fetch real rows
from `episodes` (including raw transcripts and not-yet-posted/failed episodes, not just what's
already public in the channel); after `alter table episodes enable row level security;` with zero
policies added, the same request returns nothing. See `subscribers_schema.sql` for the fix -- if you
ever add another feature that needs its own low-privilege Supabase key, check this table's RLS state
again rather than assuming it's still open the old (pre-digest-feature) way.

**Tehran time uses a fixed UTC+03:30 offset, not `zoneinfo.ZoneInfo("Asia/Tehran")`**: Iran has no
DST, so the offset never changes, and a fixed `timezone(timedelta(hours=3, minutes=30))` needs zero
external data. This sidesteps a real portability trap: Python's `zoneinfo` has no bundled IANA tz
database on Windows (and on some slim Linux images) -- `ZoneInfo("Asia/Tehran")` raises
`ZoneInfoNotFoundError` unless the `tzdata` PyPI package is installed, which would have meant either
adding a new shared `requirements.txt` entry (breaking this feature's "touches nothing else"
isolation) or risking it silently working in CI (Ubuntu, usually has system tzdata) but crashing on
a contributor's Windows machine. Confirmed by hitting the crash locally before switching to a fixed
offset.

**GitHub Actions minutes are unlimited for this repo specifically because it's public**: confirmed
against the GitHub API (`private: false`). The well-known 2,000-minutes/month free-tier cap only
applies to private repositories, and is shared across all of an account's private repos -- it has
nothing to do with usage on this repo, and running `daily-digest.yml` every 15 minutes indefinitely
(even while `subscribers` is empty) costs nothing. Re-check this if the repo (or any other repo
sharing the account's Actions quota) is ever made private.

## YouTube -> Telegram relay (standalone add-on)

A third, independent feature: monitors a list of YouTube channels, and for each new long-form video
posts a Persian-language link post (headline + summary + takeaways, generated from the video's
title/description only -- no transcript) to a **separate** Telegram channel from the podcast one.
Adapted from `youtube-telegram-relay-spec.md`, which was written for a different (TypeScript/pnpm)
monorepo that doesn't exist in this repo -- ported to Python and this repo's existing
Supabase/Groq/Telegram/GitHub Actions stack instead of introducing a second language/toolchain.
Deliberately isolated like the digest feature above: nothing in `main.py`/`scraper.py`/
`summarizer.py`/`database.py`/`config.py` knows this feature exists. The only two imports this
package makes into the rest of the repo are `telegram_bot.py` (its new `send_link_post`/
`send_photo_post` functions) and `summarizer._generate_once` (the hardened Groq call wrapper --
reused so this feature doesn't have to re-solve the Qwen `reasoning_effort`/429-retry issues already
fixed there). To remove the feature entirely: drop the `yt_channels`/`yt_queue` tables, delete the
`yt_relay/` package, delete `.github/workflows/yt-relay.yml`, and remove the two `send_*_post`
functions from `telegram_bot.py`.

**Two-stage pipeline, one Groq call per video, no transcript**: `yt_relay/discover.py` (stage A)
fetches each active channel's Atom feed, queues new videos, then captions a bounded batch of pending
rows with a single Groq call each (title + cleaned description in, structured JSON out --
`yt_relay/caption.py`). `yt_relay/publish.py` (stage B) posts exactly one `ready` row per invocation.
This is not a multi-agent or tool-calling system -- every video's path through the pipeline is fixed
and deterministic (feed -> deterministic cleanup -> one LLM call -> render -> send); the LLM is used
exactly once per video, purely as a text generator with a validated JSON contract, never as a
decision-maker over what to do next.

**`caption.py` shares the podcast pipeline's Groq account and its ~200k-tokens/day budget by
default -- `YT_RELAY_GROQ_API_KEY` exists specifically to opt out of that.** `yt_relay/settings.py`
uses `GROQ_LLM_MODEL` (same default, `qwen/qwen3.6-27b`) via the *same* `GROQ_API_KEY` unless
`YT_RELAY_GROQ_API_KEY` is set, in which case that dedicated key takes priority. This matters because
Groq's daily cap is account-wide per model, not per project, and the podcast pipeline's own
`MIN_BACKLOG_INTERVAL_MINUTES` math (see above) already assumes it has the *entire* 200k-token budget
to itself (~18-19 episodes/day at ~10-11k tokens/episode lands right at the ceiling by design). A
caption call here costs roughly 1k tokens (title + up to 800 chars of cleaned description, no
transcript), and with `YT_READY_QUEUE_MAX=8`/`MAX_POSTS_PER_DAY=8` gating how many get captioned per
day, this feature adds on the order of 5,000-15,000 tokens/day on top of that -- real (confirmed via
the same math both pipelines already document) but modest, roughly 3-8% of the cap. Both pipelines
share `_generate_once`'s existing `@retry`/backoff, so a shared-budget 429 just adds latency rather
than failing outright, and the cap resets daily regardless. `YT_RELAY_GROQ_API_KEY` (a second, free
Groq account) removes this shared-budget risk entirely instead of just accepting it -- same
dedicated-key-per-agent pattern the original spec called for with `ANTHROPIC_API_KEY_YT_RELAY`, kept
optional here only because creating that second account is a manual step, not something any script in
this repo can do on its own.

**Long-form filtering is a URL prefix swap, not an API call**: `yt_relay/feed.py` fetches
`https://www.youtube.com/feeds/videos.xml?playlist_id=UULF{channel_id[2:]}` -- YouTube's own
"uploads minus Shorts" virtual playlist -- instead of the plain per-channel feed. The feed carries no
duration field, so this is the only reliable way to exclude Shorts without a paid/quota-limited API
call. Parsed with the standard library's `xml.etree.ElementTree` against explicit Atom/yt/media
namespaces, not `feedparser` (already a dependency, used by the podcast pipeline) -- `feedparser`
doesn't reliably surface `media:group > media:description`, which is the one field this whole
feature depends on for caption input.

**Channel identity is the same "extract once, store forever" pattern as podcast episode dedup**:
`yt_relay/channels.py` resolves a `@handle` (or full URL) to its `UC...` channel id once, by scraping
the channel page for `<meta itemprop="channelId">`, then the canonical `<link>`, then a raw
`"channelId":"UC..."` occurrence in the page's embedded JSON, in that order -- a handle can be
changed by its owner, the channel id never can. `yt_queue.video_id` carries a database-level unique
index (mirrors `episodes`' `unique (source, external_id)`), not just an in-code `is_known()` check.

**Seeding, and why re-running the same channel-list file is safe**: adding a channel marks its
current feed window (up to ~15 videos) as `seeded` rather than `pending`, exactly like the podcast
pipeline's own seeding step -- without it, the first run on a freshly-added channel would post its
entire recent history at once. `yt_relay/channels.py add_channel()` is a no-op if the channel is
already registered, and deliberately does **not** re-seed on a repeat call: re-seeding an existing
channel would silently mark videos published between the two runs as `seeded`, and they would then
never be posted. This makes `python -m yt_relay add-channels <file>` safe to re-run on the same list
repeatedly (e.g. after adding a few new lines) -- already-registered channels are simply skipped.

**A full 10-30 channel list breaks the spec's implicit "low-volume queue" assumption**: a single
active tech channel posts roughly 2-3 videos/week, so 30 channels can produce on the order of 10-11
videos/day -- well above a `MAX_POSTS_PER_DAY` sized for a handful of channels. With the queue
chronically fuller than it can drain, three problems the original spec doesn't address show up in
practice, and are handled explicitly:
  - **Staleness must be re-checked at publish time, not just at discovery time.** A `ready` row can
    sit queued for days once the queue is chronically backed up; `publish.py` re-applies
    `MAX_AGE_HOURS` immediately before picking a row and skips (`skip_reason='stale'`) anything too
    old, rather than posting a days-old video as if it were fresh.
  - **Newest-`ready`-first, not oldest-first.** With a backed-up queue, "oldest ready row" would mean
    the channel perpetually shows videos from days ago while today's videos rot at the back of the
    queue. `YtStore.next_ready()` picks the newest by `published_at` -- the same reasoning and the
    same fix already applied to the podcast pipeline's own backlog (`get_newest_pending` in
    `database.py`).
  - **Channel rotation.** `next_ready(exclude_channel=...)` prefers a row from a channel other than
    whichever channel's video was posted last (falling back to any `ready` row if that channel's
    queue is empty), so one high-output channel can't monopolize the day's post budget -- directly
    mirrors `get_newest_pending(exclude_source=...)` in `database.py`.
  - **LLM spend backpressure.** `discover.py`'s captioning step is gated by `YT_READY_QUEUE_MAX`: if
    the `ready` queue already holds that many rows, captioning is skipped for the run (rows stay
    `pending`) rather than spending a Groq call on a video likely to go stale before it's ever
    posted. Discovery/queueing itself is never gated (it costs no LLM tokens) -- only the captioning
    step is, the same split as `scrape_backlog` vs. the throttled `_run_pipeline` call in `main.py`.

  `MAX_POSTS_PER_DAY` is set to 8 (not the spec's 6) specifically for this channel-count range, paired
  with `MIN_HOURS_BETWEEN_POSTS=3` so the two limits agree (3h spacing already implies ~8 posts/day)
  rather than one silently overriding the other.

**Post format deliberately isn't the spec's plain-bullet template**: the user supplied a real example
post from a similar Persian channel and asked for that visual style instead --
`yt_relay/render.py` produces a bold headline, then the summary and takeaways collapsed into a
`<blockquote expandable>` (Bot API 7+; the channel stays scannable while scrolling, expands on tap),
then a channel-name/link line, with **no promotional footer** (the example post's own footer of
social links was explicitly excluded -- that was the point of the request). `render.py` itself stays
agnostic to which delivery path is used and is kept under a 900-char budget (below Telegram's
1024-char photo-caption cap) so the choice below stays cheap to change later.

**`YT_POST_STYLE=photo`, decided in milestone M3 against two real posts in the actual test
channel** (not from description -- see CLAUDE.md's general delivery-note style above for why this
matters): `send_link_post` (`preview`) was tried first and technically worked (clickable cover,
opens in Telegram's built-in player), but Telegram's own link-preview card renders a "YouTube"
label plus **the video's own English title and description** above the thumbnail (confirmed
directly: a post for an Alexandr Wang video showed `Alexandr Wang: "This is a Once-in-a-Civilization
Opportunity" / Alexandr Wang's advice to his 18-year-old self: develop your own internal compass...`
in English, sourced from YouTube's OpenGraph metadata, not from anything this codebase generates or
controls). That's a real cost in a channel meant to be fully Persian. `send_photo_post` (`photo`) was
posted right after for the same comparison and has none of that -- just the bare thumbnail image and
the Persian caption below it, no card, no English text, not clickable-to-play. `photo` was chosen for
exactly that reason. If clickability ever matters more than staying fully Persian, `preview` is a
one-line env var change, not a code change -- that's the whole point of keeping `render.py` agnostic
to which path sends it.

**What was explicitly not built: uploading the video file itself.** The user's reference post
example was an *uploaded* video (79.6 MB / 120.1 MB, playable inline) -- not a link. Bot API's
standard `sendVideo` caps out at 50 MB, well under those file sizes, and the spec itself locks "no
downloading or uploading the video" as a decision in its own section 1. Both the working link-preview
approach (clickable cover, opens in Telegram's built-in player, no size limit since nothing is
uploaded) and the file-upload approach the reference post actually used were evaluated; only the
former is buildable within Bot API's normal limits.

**Out of scope, same as the spec's own section 12 (future work), not attempted here**: transcript-
based captions (would need a translation firewall so raw English/foreign transcript text never
reaches the Persian-writing prompt directly, mirroring the podcast pipeline's own two-step pivot),
`views_per_day` scoring to pick the best video when several are queued at once (the `views` field is
already captured and stored per video specifically so this is a later `order by` change, not a schema
migration), Reels-script conversion, and an optional human-approval gate via inline buttons in a
private channel.
