from __future__ import annotations

import argparse
import csv
import logging
import signal
import sys
import threading
import webbrowser
from pathlib import Path

from .backtest import load_csv, run_backtest, write_html_report
from .config import Settings
from .domain import Instrument, is_stock_symbol
from .engine import AlertEngine
from .notifiers import build_notifier
from .providers import build_provider
from .storage import Storage
from .strategies import build_strategies
from .web import DashboardServer


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    try:
        if args.command == "backtest":
            _backtest(args)
        elif args.command == "fetch":
            _fetch(args)
        elif args.command == "capture":
            _capture(args)
        else:
            _serve(args)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KOSPI/KOSDAQ 전략 알림 및 백테스트")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="감시 엔진과 대시보드 실행")
    serve.add_argument("--config", default="config.json")
    serve.add_argument("--no-browser", action="store_true")

    backtest = sub.add_parser("backtest", help="1분봉 CSV 백테스트 및 HTML 리포트")
    backtest.add_argument("--config", default="config.json")
    backtest.add_argument("--csv", required=True, type=Path)
    backtest.add_argument("--entry", action="append", default=[], metavar="종목코드=평단가")
    backtest.add_argument("--report", type=Path, default=Path("reports/backtest.html"))

    fetch = sub.add_parser("fetch", help="KIS 과거 1분봉을 CSV로 저장")
    fetch.add_argument("--config", default="config.json")
    fetch.add_argument("--symbol", required=True)
    fetch.add_argument("--name", required=True)
    fetch.add_argument("--market", choices=("KOSPI", "KOSDAQ"), required=True)
    fetch.add_argument("--date", required=True, help="YYYY-MM-DD")
    fetch.add_argument("--output", required=True, type=Path)

    capture = sub.add_parser("capture", help="현재 거래대금 1위를 다음 거래일용으로 저장")
    capture.add_argument("--config", default="config.json")
    return parser


def _components(config: str) -> tuple[Settings, Storage, object, object, list]:
    settings = Settings.load(config)
    storage = Storage(settings.database)
    provider = build_provider(settings)
    notifier = build_notifier(settings)
    strategies = build_strategies(settings.strategies)
    if not strategies:
        raise ValueError("최소 한 개의 전략이 필요합니다")
    return settings, storage, provider, notifier, strategies


def _serve(args: argparse.Namespace) -> None:
    settings, storage, provider, notifier, strategies = _components(args.config)
    engine = AlertEngine(settings, storage, provider, notifier, strategies)
    dashboard = DashboardServer(settings, engine, storage, notifier, strategies[0])
    engine.start()
    dashboard.start()
    url = f"http://{settings.host}:{settings.port}"
    logging.getLogger(__name__).info("대시보드: %s", url)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    done = threading.Event()

    def stop(*_: object) -> None:
        done.set()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    done.wait()
    dashboard.stop()
    engine.stop()


def _backtest(args: argparse.Namespace) -> None:
    settings = Settings.load(args.config)
    strategy = build_strategies(settings.strategies)[0]
    entries: dict[str, float] = {}
    for value in args.entry:
        try:
            symbol, price = value.split("=", 1)
            symbol = symbol.strip().upper()
            if not is_stock_symbol(symbol) or float(price) <= 0:
                raise ValueError
            entries[symbol] = float(price)
        except ValueError as exc:
            raise ValueError(f"--entry 형식 오류: {value} (예: 005930=70000)") from exc
    results = run_backtest(load_csv(args.csv), strategy, entries)
    output = write_html_report(
        results,
        args.report,
        str(args.csv),
        "전일 거래대금 1위 종목의 고점 대비 단계별 하락, 평단 손절·익절 알림",
    )
    print(output.resolve())


def _fetch(args: argparse.Namespace) -> None:
    from datetime import date

    settings = Settings.load(args.config)
    if settings.mode != "kis":
        raise ValueError("fetch는 config.json의 mode를 kis로 설정해야 합니다")
    instrument = Instrument(args.symbol, args.name, args.market)
    provider = build_provider(settings)
    day = date.fromisoformat(args.date)
    bars = provider.historical_bars(instrument, day)
    if not bars:
        raise RuntimeError("해당 날짜의 분봉을 가져오지 못했습니다")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("timestamp", "symbol", "name", "market", "open", "high", "low", "close", "volume"))
        writer.writerows(
            (
                bar.timestamp.isoformat(),
                bar.symbol,
                bar.name,
                bar.market,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
            )
            for bar in bars
        )
    print(args.output.resolve())


def _capture(args: argparse.Namespace) -> None:
    settings, storage, provider, notifier, strategies = _components(args.config)
    engine = AlertEngine(settings, storage, provider, notifier, strategies)
    leaders = engine.capture_leaders()
    for leader in leaders:
        print(leader.market, leader.symbol, leader.name, leader.turnover)


if __name__ == "__main__":
    main()
