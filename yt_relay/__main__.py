"""CLI entry point: `python -m yt_relay <command>`.

No web admin panel for this feature (spec section 5's admin-panel channel
switch is replaced by the toggle-channel command below) -- see CLAUDE.md
section 6 for the full command reference.
"""
import argparse
import logging
import sys

from . import channels, discover as discover_mod, publish as publish_mod
from .settings import load_settings
from .store import YtStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("yt_relay")


def cmd_add_channel(args: argparse.Namespace) -> None:
    settings = load_settings()
    store = YtStore(settings.supabase_url, settings.supabase_key)
    result = channels.add_channel(store, args.handle)
    _print_add_result(result)


def cmd_add_channels(args: argparse.Namespace) -> None:
    settings = load_settings()
    store = YtStore(settings.supabase_url, settings.supabase_key)
    entries = channels.parse_channel_list_file(args.file)
    print(f"{len(entries)} entr(y/ies) in {args.file}")
    for entry in entries:
        result = channels.add_channel(store, entry)
        _print_add_result(result)


def _print_add_result(result: channels.AddChannelResult) -> None:
    if result.error:
        print(f"  [ERROR] {result.input}: {result.error}")
    elif result.already_existed:
        print(f"  [SKIP]  {result.input} -> {result.channel_id} (already registered)")
    else:
        print(f"  [OK]    {result.input} -> {result.channel_id} (seeded {result.seeded_count} video(s))")


def cmd_list_channels(_args: argparse.Namespace) -> None:
    settings = load_settings()
    store = YtStore(settings.supabase_url, settings.supabase_key)
    for ch in store.all_channels():
        active = "active" if ch.get("is_active") else "inactive"
        seeded = "seeded" if ch.get("seeded_at") else "NOT SEEDED"
        print(
            f"{ch['channel_id']}  {ch.get('handle') or '-':<25} {active:<9} {seeded:<12} "
            f"last_checked={ch.get('last_checked_at') or '-'}  error={ch.get('last_error') or '-'}"
        )


def cmd_toggle_channel(args: argparse.Namespace) -> None:
    settings = load_settings()
    store = YtStore(settings.supabase_url, settings.supabase_key)
    handle = args.handle if args.handle.startswith("@") else f"@{args.handle}"
    match = next((c for c in store.all_channels() if c.get("handle") == handle), None)
    if not match:
        print(f"No channel found with handle {handle}")
        return
    active = args.state == "on"
    store.set_channel_active(match["channel_id"], active)
    print(f"{handle} ({match['channel_id']}) is now {'active' if active else 'inactive'}")


def cmd_discover(_args: argparse.Namespace) -> None:
    settings = load_settings()
    discover_mod.run_discover(settings)


def cmd_publish(_args: argparse.Namespace) -> None:
    settings = load_settings()
    publish_mod.run_publish(settings)


def cmd_status(_args: argparse.Namespace) -> None:
    settings = load_settings()
    store = YtStore(settings.supabase_url, settings.supabase_key)
    counts = store.counts_by_status()
    print("Queue status counts:")
    for status, count in sorted(counts.items()):
        print(f"  {status:<10} {count}")
    print(f"Posted today: {store.count_posted_today()} / {settings.max_posts_per_day}")
    print(f"Ready queue: {store.count_ready()} / {settings.ready_queue_max}")
    errors = store.recent_errors()
    if errors:
        print("Recent failures:")
        for err in errors:
            print(f"  video_id={err['video_id']} channel={err['channel_id']} error={err.get('last_error')}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m yt_relay")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add-channel", help="Resolve, register, and seed one channel")
    p.add_argument("handle", help="@handle, full URL, or raw UC... channel id")
    p.set_defaults(func=cmd_add_channel)

    p = sub.add_parser("add-channels", help="Same as add-channel, for every line of a text file")
    p.add_argument("file", help="Path to a plain-text channel list (see CLAUDE.md section 6)")
    p.set_defaults(func=cmd_add_channels)

    p = sub.add_parser("list-channels", help="List registered channels and their state")
    p.set_defaults(func=cmd_list_channels)

    p = sub.add_parser("toggle-channel", help="Enable/disable a registered channel")
    p.add_argument("handle")
    p.add_argument("state", choices=["on", "off"])
    p.set_defaults(func=cmd_toggle_channel)

    p = sub.add_parser("discover", help="Stage A: fetch feeds, queue new videos, caption a batch")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("publish", help="Stage B: publish one 'ready' video, if the guards allow it")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("status", help="Text dashboard: queue counts, today's posts, recent errors")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
