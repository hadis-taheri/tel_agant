// Thin Supabase REST helper for the `subscribers` table.
//
// Why fetch() to Supabase instead of the platform's own `db` (SQLite): the
// digest sender (digest.py) is a separate Python process running on GitHub
// Actions, and needs to read the same subscriber data this bot writes. The
// platform's built-in SQLite `db` only exists inside this bot's own runtime
// and isn't reachable from outside -- so the two sides need a shared external
// store, and this project already has one (Supabase; see ../subscribers_schema.sql
// for the table + RLS policies, and ../digest.py for the Python side).
//
// Why the anon key, not service_role: this platform has no secrets manager
// (confirmed against the tgcloud CLI -- no `secrets`/`env` subcommand), so
// whatever key goes here is embedded directly in deployed source. The
// service_role key (used server-side by digest.py/main.py) bypasses every
// permission check, so it must never go here. RLS policies in
// subscribers_schema.sql restrict the anon key to exactly what this bot
// needs on exactly this one table (see that file for the full reasoning).

import { fetch } from 'sdk';

const SUPABASE_URL = 'https://fighryhiidkztssqhvjd.supabase.co';
// TODO(you): paste your Supabase "anon" / "public" key here before deploying
// (Supabase dashboard -> Project Settings -> API -> Project API keys ->
// anon/public). Safe to embed: it's the key meant for untrusted contexts,
// and subscribers_schema.sql's RLS policies scope what it can do.
const SUPABASE_ANON_KEY =
  'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZpZ2hyeWhpaWRrenRzc3FodmpkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODMzNjQyOTMsImV4cCI6MjA5ODk0MDI5M30.A53or3hCCZAJJGJ9l3CCatGHf6n0zJ9eOwcOQxBTSC4';

const REST_URL = `${SUPABASE_URL}/rest/v1/subscribers`;

function headers(extra) {
  return {
    apikey: SUPABASE_ANON_KEY,
    Authorization: `Bearer ${SUPABASE_ANON_KEY}`,
    'Content-Type': 'application/json',
    ...extra,
  };
}

/** Fetch one subscriber row by chat_id, or null if none exists yet. */
export async function getSubscriber(chatId) {
  const res = await fetch(`${REST_URL}?chat_id=eq.${chatId}&select=*`, {
    headers: headers(),
  });
  if (!res.ok) throw new Error(`getSubscriber failed: ${res.status} ${await res.text()}`);
  const rows = await res.json();
  return rows[0] ?? null;
}

/** Create the subscriber row if it doesn't exist yet. Never overwrites an
 * existing row (so re-running /start doesn't wipe an already-set alarm_hour).
 * Returns the row (existing or newly created). */
export async function ensureSubscriber(chatId) {
  const existing = await getSubscriber(chatId);
  if (existing) return existing;

  const res = await fetch(REST_URL, {
    method: 'POST',
    headers: headers({ Prefer: 'return=representation' }),
    body: fetch.body.json({ chat_id: chatId, active: true }),
  });
  if (!res.ok) throw new Error(`ensureSubscriber insert failed: ${res.status} ${await res.text()}`);
  const rows = await res.json();
  return rows[0];
}

async function patchSubscriber(chatId, fields) {
  const res = await fetch(`${REST_URL}?chat_id=eq.${chatId}`, {
    method: 'PATCH',
    headers: headers({ Prefer: 'return=representation' }),
    body: fetch.body.json({ ...fields, updated_at: new Date().toISOString() }),
  });
  if (!res.ok) throw new Error(`patchSubscriber failed: ${res.status} ${await res.text()}`);
  const rows = await res.json();
  return rows[0];
}

/** Set the subscriber's daily alarm hour (0-23, Tehran local time) and
 * (re)activate them -- setting a new hour implies they want digests again. */
export function setAlarmHour(chatId, hour) {
  return patchSubscriber(chatId, { alarm_hour: hour, active: true });
}

/** Mark the subscriber inactive (/stop, or the "unsubscribe" menu button).
 * The row is kept (not deleted) so re-subscribing later remembers their
 * alarm_hour. */
export function setActive(chatId, active) {
  return patchSubscriber(chatId, { active });
}
