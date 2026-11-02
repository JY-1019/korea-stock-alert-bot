from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

KST = timezone(timedelta(hours=9), "KST")


def is_stock_symbol(value: str) -> bool:
    return len(value) == 6 and value.isascii() and value.isalnum()


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    market: str
    source_date: date | None = None
    turnover: int | None = None

    def __post_init__(self) -> None:
        if self.market not in {"KOSPI", "KOSDAQ"}:
            raise ValueError(f"지원하지 않는 시장입니다: {self.market}")
        if not is_stock_symbol(self.symbol):
            raise ValueError(f"종목코드는 영숫자 6자리여야 합니다: {self.symbol}")


@dataclass(frozen=True)
class Bar:
    symbol: str
    name: str
    market: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("분봉 시간에는 시간대가 필요합니다")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("가격은 0보다 커야 합니다")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("고가는 시가·저가·종가보다 작을 수 없습니다")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("저가는 시가·고가·종가보다 클 수 없습니다")


@dataclass(frozen=True)
class Position:
    id: int
    symbol: str
    average_price: float
    updated_at: datetime


@dataclass(frozen=True)
class Signal:
    strategy: str
    event_key: str
    kind: str
    symbol: str
    name: str
    market: str
    timestamp: datetime
    price: float
    reference_price: float
    threshold: float
    message: str

    @property
    def unique_key(self) -> str:
        if self.event_key.startswith("position:"):
            return f"{self.strategy}|{self.symbol}|{self.event_key}"
        day = self.timestamp.astimezone(KST).date().isoformat()
        return f"{self.strategy}|{self.symbol}|{day}|{self.event_key}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "unique_key": self.unique_key,
            "strategy": self.strategy,
            "event_key": self.event_key,
            "kind": self.kind,
            "symbol": self.symbol,
            "name": self.name,
            "market": self.market,
            "timestamp": self.timestamp.isoformat(),
            "price": self.price,
            "reference_price": self.reference_price,
            "threshold": self.threshold,
            "message": self.message,
        }
