"""Telegram Bot API over plain HTTPS (no library). Long-polling, one authorised chat only."""
import json
import urllib.error
import urllib.parse
import urllib.request

from . import config

BASE = f"https://api.telegram.org/bot{config.TG_TOKEN}"


def _call(method, payload=None, timeout=35):
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(f"{BASE}/{method}", data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"telegram {method}: HTTP {e.code} {e.read().decode(errors='replace')[:200]}")
    if not j.get("ok"):
        raise RuntimeError(f"telegram {method}: {j}")
    return j["result"]


def send(text, buttons=None, reply_to=None):
    """buttons: list of rows, each row a list of (label, callback_data). Returns message_id."""
    p = {"chat_id": config.TG_CHAT, "text": text[:4000], "disable_web_page_preview": True}
    if buttons:
        p["reply_markup"] = {"inline_keyboard": [[{"text": l, "callback_data": d} for l, d in row] for row in buttons]}
    if reply_to:
        p["reply_to_message_id"] = reply_to
    return _call("sendMessage", p)["message_id"]


def edit(msg_id, text, buttons=None):
    p = {"chat_id": config.TG_CHAT, "message_id": msg_id, "text": text[:4000], "disable_web_page_preview": True}
    p["reply_markup"] = {"inline_keyboard": [[{"text": l, "callback_data": d} for l, d in row] for row in (buttons or [])]}
    try:
        _call("editMessageText", p)
    except RuntimeError as e:
        if "message is not modified" not in str(e):
            raise


def delete(msg_id):
    """Delete one of the bot's own messages (Telegram allows this for 48h). Returns False if it could not."""
    try:
        _call("deleteMessage", {"chat_id": config.TG_CHAT, "message_id": msg_id})
        return True
    except Exception:
        return False


def answer_callback(cb_id, text=""):
    _call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text[:200]})


def get_updates(offset, timeout=25):
    return _call("getUpdates", {"offset": offset, "timeout": timeout,
                                "allowed_updates": ["message", "callback_query"]}, timeout=timeout + 10)


def from_owner(update):
    """Only Marcus's chat may drive the bot. Everything else is ignored silently."""
    src = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
    chat = src.get("chat") or {}
    actor = (update.get("message") or update.get("callback_query") or {}).get("from") or {}
    # private chat only: chat id == Marcus's user id, and the tapper/sender must be that same user
    return (chat.get("type") == "private" and str(chat.get("id")) == config.TG_CHAT
            and str(actor.get("id")) == config.TG_CHAT)
