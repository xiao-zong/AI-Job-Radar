"""Small Bot API client; never includes token-bearing URLs in exceptions."""
import json
import re
import urllib.error
import urllib.request


class TelegramError(RuntimeError):
    pass


class DeliveryUnknown(TelegramError):
    """Telegram might have accepted the message. Do not retry automatically."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Telegram:
    def __init__(self, token, opener=None):
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise TelegramError("Invalid token format; no request sent.")
        self._token = token
        self._opener = opener or urllib.request.build_opener(NoRedirect())

    def call(self, method, payload=None, timeout=20):
        if method not in {"getMe", "getWebhookInfo", "getUpdates", "getChat", "getChatMember", "sendMessage"}:
            raise TelegramError("Method is not permitted.")
        sending = method == "sendMessage"
        request = urllib.request.Request(
            "https://api.telegram.org/bot" + self._token + "/" + method,
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError("response too large")
                result = json.loads(raw)
        except urllib.error.HTTPError as error:
            code = error.code
            if sending and code >= 500:
                raise DeliveryUnknown("Telegram server error; inspect the channel before retrying.") from None
            raise TelegramError(f"Telegram returned HTTP {code}; no automatic retry.") from None
        except (OSError, ValueError, urllib.error.URLError):
            if sending:
                raise DeliveryUnknown("Delivery is uncertain; inspect the channel before retrying.") from None
            raise TelegramError("Telegram request failed; token and URL omitted.") from None
        if not isinstance(result, dict) or not result.get("ok") or "result" not in result:
            if sending:
                raise DeliveryUnknown("No confirmed message response; inspect the channel.")
            raise TelegramError("Telegram did not return a successful result.")
        return result["result"]

    def send(self, chat_id, text):
        if not isinstance(chat_id, int) or chat_id >= 0:
            raise TelegramError("Expected a negative private-channel chat ID.")
        if not isinstance(text, str) or not text or len(text.encode("utf-16-le")) // 2 > 3500:
            raise TelegramError("Message must contain 1-3500 UTF-16 units.")
        result = self.call("sendMessage", {
            "chat_id": chat_id, "text": text,
            "link_preview_options": {"is_disabled": True}
        })
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise DeliveryUnknown("No confirmed message ID; inspect the channel.")
        return result["message_id"]
