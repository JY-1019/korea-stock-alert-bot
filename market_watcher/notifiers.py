from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from .config import Settings

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    label: str

    def send(self, message: str) -> None: ...


class ConsoleNotifier:
    label = "console"

    def send(self, message: str) -> None:
        logger.info("ALERT %s", message)


class TelegramNotifier:
    label = "telegram"

    def __init__(self, token: str, chat_id: str):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id

    def send(self, message: str) -> None:
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": message}).encode()
        request = urllib.request.Request(self.url, data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Telegram HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Telegram 연결 실패: {exc.reason}") from exc
        if not body.get("ok"):
            raise RuntimeError(f"Telegram 전송 실패: {body.get('description', body)}")


def build_notifier(settings: Settings) -> Notifier:
    if settings.telegram_bot_token and settings.telegram_chat_id:
        return TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    return ConsoleNotifier()
