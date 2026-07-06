-- Run this once in the Supabase SQL editor (Project -> SQL Editor -> New query)
-- to create the table the agent uses to track processed episodes.

create table if not exists episodes (
    id                   bigserial primary key,
    source               text not null,
    external_id          text not null,
    title                text,
    episode_url          text,
    audio_url            text,
    published_at         timestamptz,
    status               text not null default 'pending',
    transcript           text,
    summary_html         text,
    telegram_message_id  bigint,
    error_message        text,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),
    unique (source, external_id)
);

create index if not exists episodes_status_idx on episodes (status);
