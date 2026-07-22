-- Run once in the Supabase SQL Editor (Project -> SQL Editor -> New query).
--
-- Standalone table for the interactive "daily digest" subscriber bot. This is
-- a self-contained add-on feature, NOT part of the core podcast pipeline
-- (episodes table / supabase_schema.sql) -- nothing in the existing pipeline
-- reads or writes this table, and nothing here touches the episodes table.
-- To remove the feature entirely: drop this table, delete digest.py,
-- delete .github/workflows/daily-digest.yml, and delete serverless-bot/.
--
-- Written to by the Telegram Serverless bot (serverless-bot/) when a user
-- interacts with the menu (/start, sets an alarm hour, /stop). Read by the
-- standalone digest.py script (run on its own GitHub Actions schedule,
-- daily-digest.yml) to decide who is due for their daily digest.

create table if not exists subscribers (
    chat_id        bigint primary key,       -- Telegram user chat id (private chat with the bot)
    alarm_hour     int,                       -- 0..23, Tehran local hour the user wants their digest; null = not set yet
    active         boolean not null default true,  -- false after /stop (unsubscribed, but row kept)
    last_sent_date date,                      -- Tehran calendar date the digest was last sent (per-day dedup)
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

create index if not exists subscribers_due_idx on subscribers (active, alarm_hour);

-- --- Row Level Security for the Telegram Serverless bot ---
--
-- The JS bot (serverless-bot/) has no secrets-manager on its platform (no env
-- vars, no vault -- confirmed against the tgcloud CLI: `init`/`push` take no
-- secret-setting subcommand). Its Supabase credentials are necessarily
-- embedded directly in the deployed module source, so it MUST use the
-- low-privilege `anon` key, never the `service_role` key digest.py/the main
-- pipeline use server-side. RLS is what makes that safe: anon can only touch
-- this one table, and only insert/select/update (no delete) -- not the rest
-- of the database (in particular not the `episodes` table's content).
--
-- digest.py keeps using the service_role key as before (server-side,
-- GitHub Actions secret) and is unaffected by these policies (service_role
-- bypasses RLS).
alter table subscribers enable row level security;

create policy "anon can read subscribers" on subscribers
    for select to anon using (true);

create policy "anon can insert subscribers" on subscribers
    for insert to anon with check (true);

create policy "anon can update subscribers" on subscribers
    for update to anon using (true) with check (true);

-- --- Security fix, discovered while testing the above ---
--
-- `episodes` (from supabase_schema.sql, the core pipeline's table) never had
-- RLS enabled. That was harmless as long as only the service_role key was
-- ever used (server-side, bypasses RLS anyway) -- but the moment an anon key
-- exists at all (as above, for this bot), it can read `episodes` in full,
-- including raw transcripts and not-yet-posted/failed rows, not just the
-- already-public channel summaries. Confirmed directly: before this line,
-- the anon key above could fetch real rows from `episodes`; after, empty.
--
-- No policies are added for `episodes` on purpose -- nothing needs anon
-- access to it; only service_role (main.py, digest.py) ever should, and
-- service_role bypasses RLS regardless. This line alone locks it down.
alter table episodes enable row level security;
