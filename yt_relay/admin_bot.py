"""Polling-based Telegram admin bot for managing the yt_channels list.

Telegram Serverless (the real-time, webhook-style option -- see CLAUDE.md's
digest-feature notes) isn't available on this account's BotFather yet, so
this can't respond instantly. Instead it polls Telegram's plain HTTP
`getUpdates` API from its own GitHub Actions cron (see
.github/workflows/yt-relay-admin-bot.yml), replaying only messages from a
single hardcoded chat (`settings.admin_chat_id`) so a random person DMing
the bot (it's the same bot that posts the podcast channel, so it's not
private) can't add or remove channels. State -- which update_id has already
been processed, whether the admin is mid-"remove" flow after pressing the
delete button -- lives in the yt_admin_state singleton table (see
yt_relay_admin_schema.sql) since each GitHub Actions run starts a fresh
process with no memory of the last one.
"""
import logging
from typing import Optional

import requests

from . import channels
from .settings import YtSettings
from .store import YtStore

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/{method}"
HTTP_TIMEOUT = 20

BTN_ADD = "➕ افزودن کانال"
BTN_REMOVE = "🗑 حذف کانال"
BTN_LIST = "📋 لیست کانال‌ها"

_KEYBOARD = {
    "keyboard": [[{"text": BTN_ADD}, {"text": BTN_REMOVE}], [{"text": BTN_LIST}]],
    "resize_keyboard": True,
}


def _call(token: str, method: str, **params) -> dict:
    url = API_BASE.format(token=token, method=method)
    resp = requests.post(url, json=params, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _reply(token: str, chat_id: int, text: str) -> None:
    _call(token, "sendMessage", chat_id=chat_id, text=text, reply_markup=_KEYBOARD)


def _format_channel_list(store: YtStore) -> str:
    all_channels = store.all_channels()
    if not all_channels:
        return "هیچ کانالی ثبت نشده."
    lines = []
    for ch in all_channels:
        mark = "✅" if ch.get("is_active") else "⛔"
        lines.append(f"{mark} {ch.get('handle') or ch['channel_id']}")
    return "\n".join(lines)


def _handle_add(store: YtStore, token: str, chat_id: int, text: str) -> None:
    result = channels.add_channel(store, text)
    if result.error:
        _reply(token, chat_id, f"❌ خطا: {result.error}")
        return
    if result.already_existed:
        store.set_channel_active(result.channel_id, True)
        _reply(token, chat_id, f"✅ از قبل ثبت بود، دوباره فعالش کردم: {result.handle or result.channel_id}")
        return
    _reply(
        token, chat_id,
        f"✅ اضافه شد: {result.handle or result.channel_id}\n"
        f"{result.seeded_count} ویدیوی فعلی‌اش seed شد (پست نمی‌شوند)، از ویدیوی بعدی به بعد پیگیری می‌شود.",
    )


def _handle_remove(store: YtStore, token: str, chat_id: int, text: str) -> None:
    try:
        channel_id, handle = channels.resolve_channel_id(text)
    except Exception as exc:  # noqa: BLE001 - any resolution failure just gets reported back
        _reply(token, chat_id, f"❌ پیدا نشد: {exc}")
        return
    existing = store.get_channel_by_id(channel_id)
    if not existing:
        _reply(token, chat_id, f"این کانال ({handle or channel_id}) اصلاً ثبت نشده بود.")
        return
    store.set_channel_active(channel_id, False)
    _reply(token, chat_id, f"⛔ غیرفعال شد: {handle or channel_id}\n(هر وقت خواستی با «{BTN_ADD}» دوباره فعالش کن.)")


def _handle_message(store: YtStore, token: str, chat_id: int, text: str, pending: Optional[str]) -> Optional[str]:
    """Process one message; returns the new pending_action (None clears it)."""
    text = text.strip()

    if text.lower() in ("/start", "start"):
        _reply(token, chat_id, "سلام! از دکمه‌های پایین برای مدیریت لیست کانال‌های یوتیوب استفاده کن.")
        return None

    if text == BTN_LIST:
        _reply(token, chat_id, _format_channel_list(store))
        return None

    if text == BTN_ADD:
        _reply(token, chat_id, "لینک یا هندل کانال یوتیوب رو بفرست.")
        return None

    if text == BTN_REMOVE:
        _reply(token, chat_id, "لینک یا هندل کانالی که می‌خوای غیرفعال بشه رو بفرست.")
        return "remove"

    if pending == "remove":
        _handle_remove(store, token, chat_id, text)
        return None

    _handle_add(store, token, chat_id, text)
    return None


def poll_once(settings: YtSettings, store: YtStore) -> None:
    if not settings.admin_chat_id:
        logger.info("YT_RELAY_ADMIN_CHAT_ID not set; admin bot disabled")
        return

    state = store.get_admin_state()
    offset = state.get("last_update_id", 0) + 1

    resp = _call(settings.telegram_bot_token, "getUpdates", offset=offset, timeout=0)
    updates = resp.get("result", [])
    if not updates:
        return

    max_update_id = state.get("last_update_id", 0)
    pending = state.get("pending_action")

    for update in updates:
        max_update_id = max(max_update_id, update["update_id"])
        message = update.get("message")
        if not message:
            continue
        chat = message.get("chat", {})
        if chat.get("id") != settings.admin_chat_id:
            logger.warning("Ignoring message from unauthorized chat_id=%s", chat.get("id"))
            continue
        text = message.get("text")
        if not text:
            continue
        pending = _handle_message(store, settings.telegram_bot_token, chat["id"], text, pending)

    store.set_admin_state(last_update_id=max_update_id, pending_action=pending)
