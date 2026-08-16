-- Run once in the Supabase SQL Editor, same project as yt_relay_schema.sql.
--
-- State table for the polling-based Telegram admin bot (yt_relay/admin_bot.py)
-- that lets the channel owner add/remove yt_channels rows by chatting with
-- the bot instead of running CLI commands. Standalone and removable on its
-- own: drop this table, delete yt_relay/admin_bot.py, delete
-- .github/workflows/yt-relay-admin-bot.yml, and remove the admin_chat_id
-- field from yt_relay/settings.py -- nothing else in yt_relay/ depends on it.
--
-- Singleton row (id is pinned to 1): tracks the last Telegram update_id
-- already processed (so getUpdates isn't replayed from scratch every run --
-- each GitHub Actions job starts with a clean process, no in-memory state
-- survives between runs) and whether the admin is mid-"remove" flow
-- (pressed the delete button, bot is waiting for the next message to be
-- the channel to deactivate).
--
-- last_update_id is seeded to the update_id of the /start message already
-- sitting in this bot's update queue at the time this table was created
-- (confirmed via a live getUpdates call), so the first real poll doesn't
-- replay that old test message as if it were a channel-add attempt.
create table if not exists yt_admin_state (
    id              int primary key default 1,
    last_update_id  bigint not null default 0,
    pending_action  text,
    updated_at      timestamptz not null default now(),
    constraint yt_admin_state_singleton check (id = 1)
);

insert into yt_admin_state (id, last_update_id, pending_action)
values (1, 501121415, null)
on conflict (id) do nothing;

-- Same reasoning as yt_relay_schema.sql: only service_role (GitHub Actions
-- secret) ever touches this table.
alter table yt_admin_state enable row level security;
