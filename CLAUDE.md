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
`summarizer.py` (text -> Persian HTML via Groq LLM) -> `telegram_bot.py` (post to channel), all
coordinated by `main.py`, with `database.py` (Supabase) as the single source of truth for what has
and hasn't been processed. `config.py` loads all settings from `.env` and raises immediately if a
required var is missing — there's no partial/degraded startup.

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
  oldest-`pending` row per invocation. This is deliberate throttling — it's how hundreds of old
  episodes get processed one-at-a-time (e.g. once/day via the GitHub Actions schedule) instead of
  all at once on first run.
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
project's usual ~12,000) and fails on transcripts at the current `MAX_TRANSCRIPT_BYTES` — it was
tested and rejected for that reason, not a typo.

**summarizer.py reliability note**: the LLM occasionally leaks stray non-Persian-script characters
(Chinese/Cyrillic/Hangul/Kana) into otherwise-Persian output, more often than plain temperature
tuning alone fixes. `summarize_to_persian_html` regenerates up to
`GENERATION_ATTEMPTS` times when `_FOREIGN_SCRIPT_RE` matches. Two different outcomes after that,
based on `_foreign_script_ratio()`: below `_MAX_FOREIGN_SCRIPT_RATIO` (~15%) it strips the foreign
runs and ships the result (minor stray leakage, still readable); above it, it **raises** instead of
stripping. This split exists because of a real incident: on one episode the model didn't leak a few
stray characters, it answered ~90% in Chinese outright, and stripping "foreign" characters left
behind only numbers/punctuation/English company names — a near-empty, meaningless message that
still got posted to the live channel before this fix. Don't remove either half of this behavior
without re-testing against real (not just English) transcripts. The prompt also has an explicit,
deliberate rule: tool/model/company names (OpenAI, Claude Code, Anthropic, etc.) must stay in
English/Latin script, never transliterated.

**Transcript truncation is byte-based, not char-based, on purpose**: `MAX_TRANSCRIPT_BYTES` in
`summarizer.py` truncates by UTF-8 byte length before the request goes to Groq. This was a real
production bug, not a style choice — a character-count cap sized for English silently let Chinese
transcripts (≈3 bytes/char, and denser tokenization than English) through at 2-3x the intended
request size, which passed local testing (English-only) but hit Groq's free-tier tokens-per-minute
limit (12,000 TPM) with a `413 Request too large` error on a real Chinese sv101 episode in
production. 25,000 bytes was empirically verified safe (~7,800 total tokens) against that limit —
re-verify against the actual account limit before raising it.

**Failed episodes aren't automatically retried**: `EpisodeStore.is_known()` only checks
`(source, external_id)` existence, not status — a row with `status='failed'` is still "known" and
will never be picked up again by `run_once`/`process_backlog_once` on its own. To retry one,
either delete its row or manually reset its `status` back to `pending` (see README's "نکات مهم"
section) — there's no code path that does this automatically.

**Telegram delivery**: `telegram_bot.py` splits long summaries across multiple messages at
paragraph boundaries (Telegram's 4096-char limit) and appends the source episode link as a
separate footer chunk rather than trusting the LLM to include a correct link in its own output.

## External accounts this project depends on

Supabase (Postgres), Groq (STT + LLM), and a Telegram bot/channel are real, already-provisioned
accounts (not swappable test doubles) — see README.md for the current project's specific setup
steps and the GitHub Actions daily schedule (`.github/workflows/daily-post.yml`, secrets documented
there) that runs `python main.py` once a day at noon Iran time.

**GitHub Actions secret gotcha**: if a secret value (e.g. `SUPABASE_KEY`) is copy-pasted from a
terminal/chat UI that renders mixed RTL (Persian) and LTR (English/token) text, invisible Unicode
bidi control characters can get copied along with it and silently corrupt the secret. Symptom in
Actions logs: `UnicodeEncodeError: 'ascii' codec can't encode characters in position N-M` inside
an HTTP client's header-construction code (e.g. `postgrest`/`httpx`), even though the source value
is verified pure-ASCII. Fix: re-copy the value from a plain source (e.g. the `.env` file opened in
a plain text editor), not from any RTL-rendering terminal/chat output.
