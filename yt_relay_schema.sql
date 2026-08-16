-- Run once in the Supabase SQL Editor (Project -> SQL Editor -> New query).
--
-- Standalone tables for the YouTube -> Telegram relay add-on (yt_relay/). This
-- is a self-contained feature, NOT part of the core podcast pipeline (episodes
-- table / supabase_schema.sql) -- nothing in main.py/scraper.py/summarizer.py/
-- database.py reads or writes these tables, and nothing here touches the
-- episodes table. To remove the feature entirely: drop these two tables,
-- delete the yt_relay/ package, and delete
-- .github/workflows/yt-relay.yml.
--
-- Spec this schema implements: youtube-telegram-relay-spec.md section 3,
-- adapted from the original TypeScript/pnpm design to this repo's Python +
-- Supabase stack -- see yt_relay/ module docstrings and CLAUDE.md for the
-- deviations and why.

create table if not exists yt_channels (
    id                uuid primary key default gen_random_uuid(),
    channel_id        text not null unique,        -- UC...
    handle            text,
    title             text,
    is_active         boolean not null default true,
    seeded_at         timestamptz,                 -- until set, nothing from this channel is ever posted
    last_checked_at   timestamptz,
    last_error        text,
    created_at        timestamptz not null default now()
);

create table if not exists yt_queue (
    id                  uuid primary key default gen_random_uuid(),
    video_id            text not null,
    channel_id          text not null references yt_channels(channel_id),
    title               text not null,
    raw_description     text,
    cleaned_description text,
    published_at        timestamptz not null,
    thumbnail_url       text,
    views                bigint,
    status              text not null default 'pending',
    -- status values: seeded (pre-existing, never posted) - pending (awaiting
    -- caption) - ready (captioned, awaiting publish) - posted - skipped (a
    -- guard rejected it -- see skip_reason) - failed (technical error, retryable)
    skip_reason         text,
    caption_json        jsonb,                     -- structured LLM output
    rendered_text        text,                      -- final Telegram text
    posted_at           timestamptz,
    telegram_message_id bigint,
    attempts            int not null default 0,
    last_error          text,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

-- Dedupe guard at the database level, not just an is_known() check in code --
-- mirrors the `unique (source, external_id)` constraint on the podcast
-- pipeline's own `episodes` table (see database.py).
create unique index if not exists yt_queue_video_id_idx on yt_queue (video_id);
create index if not exists yt_queue_status_published_idx on yt_queue (status, published_at);
create index if not exists yt_queue_channel_idx on yt_queue (channel_id);

-- --- Row Level Security ---
--
-- Only the service_role key (server-side, GitHub Actions secret, bypasses
-- RLS) ever touches these tables -- there is no anon-key client for this
-- feature. RLS is enabled with zero policies added on purpose, the same
-- pattern used for `episodes` in subscribers_schema.sql after that project's
-- RLS gap was found: enabling RLS with no policies locks a table down to
-- service_role only, closing off any anon key that might exist elsewhere in
-- this Supabase project (e.g. the digest feature's serverless bot) from ever
-- reading raw captions, not-yet-posted videos, or per-channel error state.
alter table yt_channels enable row level security;
alter table yt_queue enable row level security;
