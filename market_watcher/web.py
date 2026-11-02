from __future__ import annotations

import json
import logging
import threading
import urllib.parse
import uuid
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any

from .config import Settings
from .domain import is_stock_symbol
from .engine import AlertEngine
from .market_backtest import ActualMarketDataLoader, run_market_backtest, write_market_report
from .notifiers import Notifier
from .providers import KISProvider
from .storage import Storage
from .strategies import Strategy

logger = logging.getLogger(__name__)


class BacktestJobs:
    def __init__(self, settings: Settings, engine: AlertEngine, strategy: Strategy):
        self.settings = settings
        self.engine = engine
        self.strategy = strategy
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.settings.mode != "kis" or not isinstance(self.engine.provider, KISProvider):
            raise RuntimeError("실데이터 모드를 사용하려면 config.json의 mode를 kis로 설정하세요")
        start = date.fromisoformat(str(payload.get("start", "")))
        end = date.fromisoformat(str(payload.get("end", "")))
        capital = float(payload.get("capital", 0))
        cost = float(payload.get("cost", 0))
        close_at_end = payload.get("close_at_end", True)
        if not isinstance(close_at_end, bool):
            raise ValueError("전략의 장마감 청산 설정이 올바르지 않습니다")
        markets = tuple(
            market for market in payload.get("markets", []) if market in {"KOSPI", "KOSDAQ"}
        )
        if not markets:
            raise ValueError("KOSPI 또는 KOSDAQ을 하나 이상 선택하세요")
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "status": "queued",
            "done": 0,
            "total": 0,
            "message": "실제 시장 데이터 조회 대기",
            "url": None,
        }
        with self._lock:
            self._jobs[job_id] = job
        thread = threading.Thread(
            target=self._run,
            args=(job_id, start, end, capital, markets, cost, close_at_end),
            name=f"backtest-{job_id}",
            daemon=True,
        )
        thread.start()
        return dict(job)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(values)

    def _run(
        self,
        job_id: str,
        start: date,
        end: date,
        capital: float,
        markets: tuple[str, ...],
        cost: float,
        close_at_end: bool,
    ) -> None:
        try:
            self._update(job_id, status="running")
            loader = ActualMarketDataLoader(self.settings, self.engine.provider)
            sessions = loader.load(
                start,
                end,
                markets,
                lambda done, total, message: self._update(
                    job_id, done=done, total=total, message=message
                ),
            )
            result = run_market_backtest(
                sessions,
                capital,
                markets,
                tuple(float(value) for value in getattr(self.strategy, "drawdown_levels")),
                float(getattr(self.strategy, "tranche_pct")),
                float(getattr(self.strategy, "stop_loss")),
                float(getattr(self.strategy, "take_profit")),
                cost,
                close_at_end,
                loader.bars,
            )
            mode = "eod" if close_at_end else "hold"
            output = self.settings.report_dir / f"market_backtest_{mode}_{start}_{end}_{job_id}.html"
            write_market_report(result, output)
            self._update(
                job_id,
                status="complete",
                message=(
                    f"완료 · {len(result.trades)}회 청산 · "
                    f"{len(result.open_positions)}개 보유 · {result.total_return_pct:+.2f}%"
                ),
                url=f"/reports/{output.name}",
                summary={
                    "initial_capital": result.initial_capital,
                    "final_capital": result.final_capital,
                    "return_pct": result.total_return_pct,
                    "trades": len(result.trades),
                    "open_positions": len(result.open_positions),
                },
            )
        except Exception as exc:
            logger.exception("시장 백테스트 실패")
            self._update(job_id, status="error", message=str(exc), error=str(exc))


class DashboardServer:
    def __init__(
        self,
        settings: Settings,
        engine: AlertEngine,
        storage: Storage,
        notifier: Notifier,
        strategy: Strategy,
    ):
        jobs = BacktestJobs(settings, engine, strategy)
        handler = _handler(settings, engine, storage, notifier, strategy, jobs)
        self.server = ThreadingHTTPServer((settings.host, settings.port), handler)
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="dashboard-server", daemon=True
        )
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.thread:
            self.thread.join(timeout=5)


def _handler(
    settings: Settings,
    engine: AlertEngine,
    storage: Storage,
    notifier: Notifier,
    strategy: Strategy,
    jobs: BacktestJobs,
) -> type[BaseHTTPRequestHandler]:
    dashboard = files("market_watcher").joinpath("dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self._bytes(dashboard, "text/html; charset=utf-8")
                return
            if parsed.path == "/api/state":
                status = engine.status()
                self._json(
                    {
                        "status": status,
                        "signals": storage.signals(80),
                        "positions": storage.positions(),
                        "leader_history": storage.leader_history(),
                        "readiness": {
                            "actual_mode": settings.mode == "kis",
                            "kis": bool(settings.kis_app_key and settings.kis_app_secret),
                            "krx": bool(settings.krx_id and settings.krx_password),
                            "telegram": settings.telegram_bot_token != "" and settings.telegram_chat_id != "",
                        },
                    }
                )
                return
            if parsed.path == "/api/backtest/status":
                query = urllib.parse.parse_qs(parsed.query)
                job = jobs.get((query.get("id") or [""])[0])
                if job is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                else:
                    self._json(job)
                return
            if parsed.path == "/api/bars":
                query = urllib.parse.parse_qs(parsed.query)
                symbol = (query.get("symbol") or [""])[0].strip().upper()
                self._json(
                    {"bars": storage.bars(symbol, limit=500) if is_stock_symbol(symbol) else []}
                )
                return
            if parsed.path.startswith("/reports/"):
                filename = Path(parsed.path).name
                report = settings.report_dir / filename
                if report.is_file() and report.suffix == ".html":
                    self._bytes(report.read_bytes(), "text/html; charset=utf-8")
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            try:
                payload = self._payload()
                if self.path == "/api/position":
                    symbol = str(payload.get("symbol", "")).strip().upper()
                    if not is_stock_symbol(symbol):
                        raise ValueError("종목코드는 영숫자 6자리여야 합니다")
                    value = payload.get("average_price")
                    if value in (None, "", 0):
                        storage.clear_position(symbol)
                        self._json({"ok": True, "cleared": True})
                    else:
                        position = storage.set_position(symbol, float(value))
                        self._json({"ok": True, "position_id": position.id})
                    return
                if self.path == "/api/telegram/test":
                    notifier.send("🔔 국내주식 알림 봇 테스트 메시지입니다.")
                    self._json({"ok": True, "notifier": notifier.label})
                    return
                if self.path == "/api/backtest/market":
                    self._json(jobs.start(payload), HTTPStatus.ACCEPTED)
                    return
                if self.path == "/api/leaders/capture":
                    leaders = engine.capture_leaders()
                    self._json({"ok": True, "leaders": [item.__dict__ for item in leaders]})
                    return
                self.send_error(HTTPStatus.NOT_FOUND)
            except (ValueError, RuntimeError) as exc:
                self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                logger.exception("대시보드 요청 처리 실패")
                self._json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _payload(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65_536:
                raise ValueError("요청이 너무 큽니다")
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("JSON 요청이 올바르지 않습니다") from exc

        def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._bytes(
                json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"),
                "application/json; charset=utf-8",
                status,
            )

        def _bytes(
            self,
            value: bytes,
            content_type: str,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(value)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(value)

        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("dashboard: " + format, *args)

    return Handler
