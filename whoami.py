"""One-time helper: prints the chat id of whoever messaged the bot last, so TELEGRAM_CHAT_ID can be set.
  TELEGRAM_BOT_TOKEN=... python3 -m bot.whoami   (send the bot any message first)"""
import json
import os
import urllib.request

tok = os.environ["TELEGRAM_BOT_TOKEN"]
j = json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=20))
seen = {}
for u in j.get("result", []):
    m = u.get("message") or {}
    chat = m.get("chat") or {}
    if chat.get("id"):
        seen[chat["id"]] = f"{chat.get('first_name','')} @{chat.get('username','')} ({chat.get('type')})"
if not seen:
    print("no messages yet. Open the bot in Telegram, press Start, send 'hi', then run this again.")
for k, v in seen.items():
    print(f"TELEGRAM_CHAT_ID={k}   <- {v}")
