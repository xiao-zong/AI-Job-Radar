#!/usr/bin/env python3
"""Run interactively in Google Cloud Shell. Never persist the token here."""
import getpass
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from radar.telegram import Telegram, TelegramError


def main():
    if not sys.stdin.isatty():
        raise TelegramError("Use an interactive Cloud Shell terminal for hidden token input.")
    print("Use a dedicated bot. Add it to your PRIVATE channel as an administrator with Post Messages permission.")
    print("Post a new message yourself in that channel, then continue. The token will not be saved.")
    client = Telegram(getpass.getpass("Bot token (hidden): ").strip())
    bot = client.call("getMe")
    print("Connected bot: @" + bot["username"])
    if client.call("getWebhookInfo").get("url"):
        raise TelegramError("This bot already has a webhook. Use a new dedicated bot; nothing was changed.")
    updates = client.call("getUpdates", {"timeout": 5, "limit": 100, "allowed_updates": ["channel_post"]})
    channels = {}
    for update in updates:
        chat = update.get("channel_post", {}).get("chat", {})
        if chat.get("type") == "channel" and not chat.get("username"):
            channels[chat["id"]] = chat.get("title", "Private channel")
    for chat_id, title in channels.items():
        print(f"{chat_id}: {title}")
    if not channels:
        raise TelegramError("No private-channel post found. Post a fresh message after adding the bot, then rerun.")
    try:
        chat_id = int(input("Type the destination channel ID from the list above: ").strip())
    except ValueError:
        raise TelegramError("The channel ID must be a negative integer.") from None
    if chat_id not in channels:
        raise TelegramError("Destination must be one of the observed private channels.")
    chat = client.call("getChat", {"chat_id": chat_id})
    membership = client.call("getChatMember", {"chat_id": chat_id, "user_id": bot["id"]})
    if chat.get("type") != "channel" or chat.get("username"):
        raise TelegramError("Destination is not a private channel.")
    if membership.get("status") != "administrator" or not membership.get("can_post_messages"):
        raise TelegramError("Bot needs administrator Post Messages permission.")
    print("Destination: " + channels[chat_id])
    if input("Send the Cloud Shell test message to this channel? Type SEND: ").strip() != "SEND":
        print("No test message sent.")
        return
    message_id = client.send(chat_id, "AI Job Radar: this test message comes from Google Cloud Shell. 云端通道测试成功。")
    destination = ROOT / "config" / "telegram.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    with os.fdopen(os.open(destination, flags, 0o600), "w") as output:
        json.dump({"chat_id": chat_id, "bot_username": bot["username"], "test_message_id": message_id}, output, indent=2)
    print(f"Confirmed message_id={message_id}. Destination saved in git-ignored config/telegram.json. Token not saved.")


if __name__ == "__main__":
    try:
        main()
    except (TelegramError, KeyboardInterrupt, EOFError) as error:
        print(str(error) or "Cancelled.", file=sys.stderr)
        sys.exit(1)
