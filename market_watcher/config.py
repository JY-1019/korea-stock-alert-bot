from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

@dataclass(frozen=True)
class Settings:
    mode: str = "setup"
    host: str = "127.0.0.1"
    port: int = 8765
    poll_seconds: float = 3.0
    database: Path = Path("data/alerts.db")
    report_dir: Path = Path("reports")
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    kis_app_key: str = ""
    kis_app_secret: str = ""
    krx_id: str = ""
    krx_password: str = ""
    kis_base_url: str = "https://openapi.koreainvestment.com:9443"
    strategies: tuple[dict[str, Any], ...] = field(
        default_factory=lambda: (
            {
                "type": "previous_turnover_leader_pullback",
                "drawdown_levels": [8, 10, 12, 15],
                "stop_loss": 5,
                "take_profit": 4,
                "tranche_pct": 25,
            },
        )
    )

    @classmethod
    def load(cls, path: str | Path = "config.json") -> "Settings":
        config_path = Path(path)
        raw: dict[str, Any] = {}
        if config_path.exists():
            raw = json.loads(config_path.read_text(encoding="utf-8"))

        base_dir = config_path.resolve().parent
        mode = os.getenv("BOT_MODE", raw.get("mode", "setup")).lower()
        if mode not in {"setup", "kis"}:
            raise ValueError("mode는 setup 또는 kis여야 합니다")

        strategies = tuple(raw.get("strategies") or cls().strategies)
        database = _relative_to(base_dir, raw.get("database", "data/alerts.db"))
        report_dir = _relative_to(base_dir, raw.get("report_dir", "reports"))

        settings = cls(
            mode=mode,
            host=os.getenv("BOT_HOST", raw.get("host", "127.0.0.1")),
            port=int(os.getenv("BOT_PORT", raw.get("port", 8765))),
            poll_seconds=float(os.getenv("POLL_SECONDS", raw.get("poll_seconds", 3))),
            database=database,
            report_dir=report_dir,
            telegram_bot_token=os.getenv(
                "TELEGRAM_BOT_TOKEN", raw.get("telegram_bot_token", "")
            ),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", raw.get("telegram_chat_id", "")),
            kis_app_key=os.getenv("KIS_APP_KEY", raw.get("kis_app_key", "")),
            kis_app_secret=os.getenv("KIS_APP_SECRET", raw.get("kis_app_secret", "")),
            krx_id=os.getenv("KRX_ID", raw.get("krx_id", "")),
            krx_password=os.getenv("KRX_PW", raw.get("krx_password", "")),
            kis_base_url=os.getenv(
                "KIS_BASE_URL",
                raw.get("kis_base_url", "https://openapi.koreainvestment.com:9443"),
            ).rstrip("/"),
            strategies=strategies,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("port는 1~65535여야 합니다")
        if self.poll_seconds < 1:
            raise ValueError("poll_seconds는 1초 이상이어야 합니다")


def _relative_to(base: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path
