from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

from market_watcher.backtest import run_backtest, write_html_report
from market_watcher.config import Settings
from market_watcher.domain import Bar, Instrument, KST, Position
from market_watcher.market_backtest import MarketSession, run_market_backtest, write_market_report
from market_watcher.providers import KISProvider
from market_watcher.storage import Storage
from market_watcher.strategies.leader_pullback import LeaderPullbackStrategy


def bar(close: float, high: float = 100, minute: int = 0) -> Bar:
    return Bar(
        symbol="005930",
        name="테스트",
        market="KOSPI",
        timestamp=datetime(2026, 7, 17, 9, minute, tzinfo=KST),
        open=close,
        high=max(high, close),
        low=close,
        close=close,
        volume=1,
    )


class StrategyTest(unittest.TestCase):
    def test_paper_kis_requests_are_rate_limited(self) -> None:
        provider = KISProvider(
            Settings(kis_base_url="https://openapivts.koreainvestment.com:29443")
        )
        provider._last_request_at = 10
        with (
            patch("market_watcher.providers.time_module.monotonic", side_effect=[10.2, 11]),
            patch("market_watcher.providers.time_module.sleep") as sleep,
            patch("market_watcher.providers._read_json", return_value={"rt_cd": "0"}),
        ):
            provider._throttled_read(Request("https://example.test"))
        self.assertAlmostEqual(sleep.call_args.args[0], 0.8)

    def test_drawdown_thresholds_fire_once_and_gap_crosses_all(self) -> None:
        strategy = LeaderPullbackStrategy()
        first = strategy.evaluate(bar(91), 100, 100, set(), None)
        self.assertEqual([signal.threshold for signal in first], [8])
        fired = {signal.event_key for signal in first}
        second = strategy.evaluate(bar(84, minute=1), 100, 100, fired, None)
        self.assertEqual([signal.threshold for signal in second], [10, 12, 15])
        fired.update(signal.event_key for signal in second)
        self.assertEqual(strategy.evaluate(bar(80, minute=2), 100, 100, fired, None), [])

    def test_krx_alphanumeric_short_code_is_valid(self) -> None:
        self.assertEqual(Instrument("0134X0", "영문코드", "KOSPI").symbol, "0134X0")

    def test_position_stop_steps_and_take_profit(self) -> None:
        strategy = LeaderPullbackStrategy()
        position = Position(7, "005930", 100, datetime(2026, 7, 17, 8, tzinfo=KST))
        stops = strategy.evaluate(bar(89), 100, 100, set(), position)
        self.assertEqual(
            [signal.threshold for signal in stops if signal.kind == "STOP_LOSS"],
            [-5],
        )
        profits = strategy.evaluate(bar(104, high=105, minute=1), 105, 104, set(), position)
        self.assertEqual([signal.threshold for signal in profits if signal.kind == "TAKE_PROFIT"], [4])
        self.assertNotIn("2026-07-17", profits[-1].unique_key)

    def test_storage_deduplicates_and_position_update_creates_new_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(Path(folder) / "test.db")
            position1 = storage.set_position("005930", 100)
            position2 = storage.set_position("005930", 95)
            self.assertNotEqual(position1.id, position2.id)
            signal = LeaderPullbackStrategy().evaluate(bar(89), 100, 100, set(), position2)[0]
            self.assertTrue(storage.insert_signal(signal))
            self.assertFalse(storage.insert_signal(signal))

    def test_csv_style_backtest_writes_self_contained_report(self) -> None:
        bars = [bar(100), bar(91, minute=1), bar(84, minute=2)]
        results = run_backtest(
            bars,
            LeaderPullbackStrategy(),
            {"005930": 100},
        )
        self.assertEqual(len(results), 1)
        self.assertGreater(sum(len(result.signals) for result in results), 3)
        with tempfile.TemporaryDirectory() as folder:
            report = write_html_report(
                results,
                Path(folder) / "report.html",
                "sample.csv",
                "테스트 전략",
            )
            text = report.read_text(encoding="utf-8")
            self.assertIn("매매 전략 백테스트 리포트", text)
            self.assertIn("<svg", text)

    def test_market_backtest_staged_buys_and_exact_trade_report(self) -> None:
        instrument = Instrument("000001", "테스트회사", "KOSPI", date(2026, 7, 16), 10)
        bars = (
            market_bar(0, 100, 100, 99, 100),
            market_bar(1, 100, 100, 91, 92),
            market_bar(2, 92, 92, 89, 90),
            market_bar(3, 90, 96, 90, 95),
        )
        session = MarketSession(date(2026, 7, 16), date(2026, 7, 17), instrument, bars)
        result = run_market_backtest([session], 1_000_000, ("KOSPI",))
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual([fill.level for fill in trade.fills], [8, 10])
        self.assertEqual(trade.exit_reason, "익절 +4%")
        self.assertEqual(trade.exit_time.minute, 3)
        self.assertGreater(result.final_capital, result.initial_capital)
        with tempfile.TemporaryDirectory() as folder:
            report = write_market_report(result, Path(folder) / "market.html")
            text = report.read_text(encoding="utf-8")
            self.assertIn("2026-07-17 09:01", text)
            self.assertIn("테스트회사", text)
            self.assertIn("실제 KRX/KIS 데이터", text)

    def test_hold_strategy_keeps_position_overnight_until_take_profit(self) -> None:
        first = Instrument("000001", "첫회사", "KOSPI", date(2026, 7, 16), 10)
        second = Instrument("000002", "둘째회사", "KOSPI", date(2026, 7, 17), 9)
        sessions = [
            MarketSession(
                date(2026, 7, 16),
                date(2026, 7, 17),
                first,
                (
                    dated_bar(first, date(2026, 7, 17), 0, 100, 100, 99, 100),
                    dated_bar(first, date(2026, 7, 17), 1, 100, 100, 91, 92),
                ),
            ),
            MarketSession(
                date(2026, 7, 17),
                date(2026, 7, 20),
                second,
                (dated_bar(second, date(2026, 7, 20), 0, 100, 100, 99, 100),),
            ),
        ]

        def continuation(_: Instrument, day: date) -> list[Bar]:
            return [dated_bar(first, day, 0, 93, 96, 92, 95)]

        result = run_market_backtest(
            sessions,
            1_000_000,
            ("KOSPI",),
            close_at_end=False,
            bar_loader=continuation,
        )
        self.assertEqual(result.trades[0].exit_reason, "익절 +4%")
        self.assertEqual(result.trades[0].exit_time.date(), date(2026, 7, 20))
        self.assertEqual(result.open_positions, ())

        held = run_market_backtest(
            sessions,
            1_000_000,
            ("KOSPI",),
            close_at_end=False,
            bar_loader=lambda instrument, day: [
                dated_bar(instrument, day, 0, 92, 94, 90, 93)
            ],
        )
        self.assertEqual(len(held.open_positions), 1)
        self.assertEqual(held.trades, ())


def market_bar(minute: int, open_: float, high: float, low: float, close: float) -> Bar:
    return Bar(
        "000001",
        "테스트회사",
        "KOSPI",
        datetime(2026, 7, 17, 9, minute, tzinfo=KST),
        open_,
        high,
        low,
        close,
        1,
    )


def dated_bar(
    instrument: Instrument,
    day: date,
    minute: int,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> Bar:
    return Bar(
        instrument.symbol,
        instrument.name,
        instrument.market,
        datetime(day.year, day.month, day.day, 9, minute, tzinfo=KST),
        open_,
        high,
        low,
        close,
        1,
    )


if __name__ == "__main__":
    unittest.main()
