#!/usr/bin/env python3
"""Hidden input in Cloud Shell; store the dedicated bot token in Secret Manager."""
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from radar.telegram import Telegram, TelegramError

PROJECT = "your-gcp-project-id"
SECRET = "radar-telegram-token"
EXPECTED_BOT = "YourAIJobRadarBot"


def main():
    if not sys.stdin.isatty():
        raise TelegramError("Use an interactive Cloud Shell terminal.")
    destination = ROOT / "config" / "telegram.json"
    if destination.exists():
        previous = json.loads(destination.read_text())
        if previous.get("cloud_shell_test") in {"sending", "passed"} or previous.get("test_message_id"):
            raise TelegramError("A test was already attempted. Review its recorded outcome before another send.")
    print("Paste the token for @" + EXPECTED_BOT + ". Input is hidden.")
    print("It will be verified with Telegram and stored in this project's Secret Manager.")
    while True:
        token = getpass.getpass("Bot token (hidden): ").strip()
        try:
            client = Telegram(token)
            break
        except TelegramError:
            print("Invalid format. Copy only the token line, for example digits:letters. Try again.")
    bot = client.call("getMe")
    if bot.get("username") != EXPECTED_BOT:
        raise TelegramError("Wrong bot. No message was sent and no secret was stored.")
    if client.call("getWebhookInfo").get("url"):
        raise TelegramError("The bot has an existing webhook; inspect before proceeding.")
    updates = client.call("getUpdates", {
        "timeout": 5, "limit": 100, "allowed_updates": ["channel_post"]})
    channels = {}
    for update in updates:
        chat = update.get("channel_post", {}).get("chat", {})
        if chat.get("type") == "channel" and not chat.get("username"):
            channels[chat["id"]] = chat.get("title", "Private channel")
    if not channels:
        raise TelegramError("Post a fresh message in the private channel, then rerun.")
    if len(channels) == 1:
        chat_id = next(iter(channels))
    else:
        for number, title in channels.items():
            print(f"{number}: {title}")
        chat_id = int(input("Destination channel ID: "))
        if chat_id not in channels:
            raise TelegramError("Select an observed private channel.")
    chat = client.call("getChat", {"chat_id": chat_id})
    member = client.call("getChatMember", {"chat_id": chat_id, "user_id": bot["id"]})
    if chat.get("type") != "channel" or chat.get("username"):
        raise TelegramError("Destination is not a private channel.")
    if member.get("status") != "administrator" or not member.get("can_post_messages"):
        raise TelegramError("The bot needs Post Messages permission.")
    print("Verified private channel: " + channels[chat_id])
    result = subprocess.run(
        ["gcloud", "secrets", "versions", "add", SECRET, "--project=" + PROJECT,
         "--data-file=-", "--format=value(name)"],
        input=token, text=True, capture_output=True)
    if result.returncode:
        raise TelegramError("Secret upload failed. No test message was sent.")
    version = result.stdout.strip()
    if not version:
        raise TelegramError("Secret version was not confirmed. Inspect before retrying.")
    record = {"chat_id": chat_id, "bot_username": bot["username"],
              "token_secret_version": version, "cloud_shell_test": "not_sent"}
    # Persist destination before the send so an unknown outcome is reviewable.
    with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(record, f, indent=2)
    record["cloud_shell_test"] = "sending"
    destination.write_text(json.dumps(record, indent=2))
    message_id = client.send(
        chat_id, "AI Job Radar: this test message comes from Google Cloud Shell. 云端通道测试成功。")
    record.update(cloud_shell_test="passed", test_message_id=message_id)
    destination.write_text(json.dumps(record, indent=2))
    print(f"TELEGRAM_TEST_PASSED message_id={message_id}; token stored only in Secret Manager.")


if __name__ == "__main__":
    try:
        main()
    except (TelegramError, ValueError, KeyboardInterrupt, EOFError) as error:
        print(str(error) or "Cancelled.", file=sys.stderr)
        sys.exit(1)
