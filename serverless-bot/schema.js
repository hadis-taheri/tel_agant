// This bot intentionally has no tables here. All state (the `subscribers`
// table: chat_id, alarm_hour, active, last_sent_date) lives in the project's
// existing Supabase database instead of this platform's built-in SQLite --
// see lib/supabase.js for why (digest.py, a separate Python process on
// GitHub Actions, needs to read the same data, and can't reach this
// platform's per-bot SQLite database).
