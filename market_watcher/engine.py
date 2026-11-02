from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time, timedelta
from typing import Any

from .config import Settings
from .domain import Bar, Instrument, KST
from .notifiers import Notifier
from .providers import MarketDataProvider
from .storage import Storage
from .strategies import Strategy

logger = logging.getLogger(__name__)


class AlertEngine:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        provider: MarketDataProvider,
        notifier: Notifier,
        strategies: list[Strategy],
    ):
        self.settings = settings
        self.storage = storage
        self.provider = provider
        self.notifier = notifier
        self.strategies = strategies
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "running": False,
            "mode": settings.mode,
            "notifier": notifier.label,
            "message": "시작 대기",
            "last_poll": None,
            "last_error": None,
            "targets": [],
        }
        self._observed_session_day: date | None = None
        self._last_notification_retry: datetime | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="alert-engine", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    def process_bar(self, bar: Bar, instrument: Instrument | None = None) -> None:
        self.storage.upsert_bars([bar])
        instrument = instrument or Instrument(bar.symbol, bar.name, bar.market)
        day = bar.timestamp.astimezone(KST).date()
        day_high = max(
            self.storage.day_high(bar.symbol, day),
            self.provider.reported_day_high(instrument),
        )
        day_open = self.storage.day_open(bar.symbol, day)
        position = self.storage.position(bar.symbol)
        created = False
        for strategy in self.strategies:
            fired = self.storage.fired_event_keys(strategy.name, bar.symbol, day)
            for signal in strategy.evaluate(bar, day_high, day_open, fired, position):
                if self.storage.insert_signal(signal):
                    created = True
                    logger.info("신호 발생: %s", signal.message)
        if created:
            self._retry_notifications(force=True)

    def capture_leaders(self) -> list[Instrument]:
        leaders = self.provider.current_turnover_leaders()
        markets = {leader.market for leader in leaders}
        if markets != {"KOSPI", "KOSDAQ"}:
            raise RuntimeError("코스피·코스닥 거래대금 1위를 모두 가져오지 못했습니다")
        self.storage.save_leaders(leaders, datetime.now(KST).date())
        return leaders

    def _run(self) -> None:
        if self.settings.mode == "setup":
            self._set_status(
                running=True,
                message="실제 데이터 연결 설정 필요",
                targets=[],
            )
            self.stop_event.wait()
            self._set_status(running=False, message="중지됨")
            return
        self._set_status(running=True, message="감시 시작")
        while not self.stop_event.is_set():
            try:
                now = datetime.now(KST)
                self._capture_if_due(now)
                if _is_market_poll_time(now):
                    targets = self._targets(now.date())
                    self._set_status(targets=[_instrument_dict(item) for item in targets])
                    for instrument in targets:
                        bars = self.provider.recent_bars(instrument)
                        if not bars:
                            continue
                        self.storage.upsert_bars(bars)
                        latest = bars[-1]
                        if self.settings.mode == "kis" and latest.timestamp.date() != now.date():
                            continue
                        self._observed_session_day = latest.timestamp.date()
                        self.process_bar(latest, instrument)
                    self._set_status(
                        message="감시 중",
                        last_poll=now.isoformat(),
                        last_error=None,
                    )
                else:
                    self._set_status(message="장 운영시간 대기", last_poll=now.isoformat())
                self._retry_notifications()
            except Exception as exc:  # the service stays alive and exposes the failure in the dashboard
                logger.exception("감시 루프 오류")
                self._set_status(message="오류 후 재시도 대기", last_error=str(exc))
            self.stop_event.wait(self.settings.poll_seconds)
        self._set_status(running=False, message="중지됨")

    def _targets(self, day: date) -> list[Instrument]:
        stored = self.storage.latest_previous_leaders(day)
        if {item.market for item in stored} == {"KOSPI", "KOSDAQ"}:
            return stored
        from pykrx import stock

        calendar = stock.get_index_ohlcv_by_date(
            (day - timedelta(days=14)).strftime("%Y%m%d"),
            (day - timedelta(days=1)).strftime("%Y%m%d"),
            "1001",
        )
        if calendar.empty:
            raise RuntimeError("이전 거래일을 확인하지 못했습니다")
        previous_day = calendar.index[-1].date()
        leaders = self.provider.turnover_leaders(previous_day)
        self.storage.save_leaders(leaders, previous_day)
        return leaders

    def _capture_if_due(self, now: datetime) -> None:
        if self.settings.mode != "kis" or now.weekday() >= 5 or now.time() < time(15, 35):
            return
        if self._observed_session_day != now.date():
            return
        history = self.storage.leader_history(limit=2)
        if any(row["source_date"] == now.date().isoformat() for row in history):
            return
        leaders = self.capture_leaders()
        logger.info("익일 대상 저장: %s", ", ".join(item.name for item in leaders))

    def _retry_notifications(self, force: bool = False) -> None:
        now = datetime.now(KST)
        if not force and self._last_notification_retry:
            if (now - self._last_notification_retry).total_seconds() < 30:
                return
        self._last_notification_retry = now
        for row in self.storage.pending_signals():
            try:
                self.notifier.send(row["message"])
                self.storage.mark_notified(row["id"])
            except Exception as exc:
                logger.warning("알림 전송 실패: %s", exc)
                self.storage.mark_notification_error(row["id"], str(exc))
                break

    def _set_status(self, **values: Any) -> None:
        with self._status_lock:
            self._status.update(values)


def _is_market_poll_time(now: datetime) -> bool:
    return now.weekday() < 5 and time(8, 55) <= now.time() <= time(15, 40)


def _instrument_dict(instrument: Instrument) -> dict[str, Any]:
    return {
        "symbol": instrument.symbol,
        "name": instrument.name,
        "market": instrument.market,
        "source_date": instrument.source_date.isoformat() if instrument.source_date else None,
        "turnover": instrument.turnover,
    }
