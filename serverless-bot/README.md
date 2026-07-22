# Subscriber digest bot (Telegram Serverless)

Interactive add-on: users `/start` this bot, pick a daily alarm hour (Tehran
time), and `digest.py` (a separate Python script, see
`../.github/workflows/daily-digest.yml`) sends them the last 24h of channel
summaries at that hour. **Fully isolated from the rest of the repo** — see
`../subscribers_schema.sql` for the removal procedure if you ever want it gone.

Runs on Telegram's serverless platform (`@tgcloud/cli`) — no server to host,
no `node_modules` at runtime, code executes in Telegram's own infrastructure.

## One-time setup (you need to do this — not automatable from here)

1. **Run the SQL** in `../subscribers_schema.sql` via the Supabase SQL Editor
   (creates the `subscribers` table + RLS policies for the anon key below).

2. **Get your Supabase anon/public key**: Supabase dashboard → your project →
   Project Settings → API → "Project API keys" → copy the `anon` / `public`
   one (NOT `service_role` — that key must never go here, see the comment in
   `lib/supabase.js` for why).

3. **Paste it into `lib/supabase.js`**, replacing
   `PASTE_YOUR_SUPABASE_ANON_KEY_HERE`. This platform has no secrets/env-var
   support (checked directly against the CLI), so this is the only place it
   can live — safe specifically because it's the anon key, scoped by RLS to
   just the `subscribers` table.

4. **Link this project to your existing channel bot** — same bot, same
   token, no conflict (this only adds an inbound webhook for private-chat
   messages; it doesn't touch how `main.py` posts to the channel):
   ```bash
   cd serverless-bot
   npx tgcloud login
   ```
   When prompted, paste the same `TELEGRAM_BOT_TOKEN` value from `../.env`.

5. **Deploy**:
   ```bash
   npx tgcloud push
   ```
   (No `npx tgcloud migrate` needed — `schema.js` is intentionally empty; all
   state lives in Supabase, not this platform's own database.)

6. **Test**: open the bot in Telegram, send `/start`, tap "تنظیم ساعت آلارم",
   pick an hour. Then check the Supabase `subscribers` table — a row for your
   chat_id should exist with that `alarm_hour`.

## Local iteration

`npx tgcloud run <handler>` executes a handler in the cloud without a full
deploy — e.g. `npx tgcloud run handlers/message '{"chat":{"id":123},"text":"/start"}'`.
`npx tgcloud status` / `npx tgcloud diff` show what's changed locally vs. what's
deployed.

## Files

- `handlers/message.js` — `/start`, `/stop`, and the fallback that re-shows the menu.
- `handlers/callback_query.js` — inline-button taps (set hour, status, unsubscribe).
- `lib/menu.js` — shared menu text/keyboard builders.
- `lib/supabase.js` — REST calls to the `subscribers` table (anon key, RLS-scoped).
- `schema.js` — intentionally empty (see comment inside).
