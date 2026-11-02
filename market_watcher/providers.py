from __future__ import annotations

import json
import os
import threading
import time as time_module
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta
from typing import Protocol

from .config import Settings
from .domain import Bar, Instrument, KST


class MarketDataProvider(Protocol):
    def recent_bars(self, instrument: Instrument) -> list[Bar]: ...

    def current_turnover_leaders(self) -> list[Instrument]: ...

    def turnover_leaders(self, day: date) -> list[Instrument]: ...

    def historical_bars(self, instrument: Instrument, day: date) -> list[Bar]: ...

    def reported_day_high(self, instrument: Instrument) -> float: ...


class SetupProvider:
    """Keeps the dashboard available without ever manufacturing market data."""

    message = "실제 데이터 연결을 위해 KIS·KRX 자격정보를 설정하세요"

    def recent_bars(self, instrument: Instrument) -> list[Bar]:
        return []

    def current_turnover_leaders(self) -> list[Instrument]:
        raise RuntimeError(self.message)

    def turnover_leaders(self, day: date) -> list[Instrument]:
        raise RuntimeError(self.message)

    def historical_bars(self, instrument: Instrument, day: date) -> list[Bar]:
        raise RuntimeError(self.message)

    def reported_day_high(self, instrument: Instrument) -> float:
        return 0


class KISProvider:
    """Korea Investment & Securities REST quote adapter (no order endpoints)."""

    def __init__(self, settings: Settings):
        self.base_url = settings.kis_base_url
        self.app_key = settings.kis_app_key
        self.app_secret = settings.kis_app_secret
        if settings.krx_id:
            os.environ.setdefault("KRX_ID", settings.krx_id)
        if settings.krx_password:
            os.environ.setdefault("KRX_PW", settings.krx_password)
        self._token = ""
        self._token_expires_at = datetime.min.replace(tzinfo=KST)
        self._token_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._request_interval = 1.0 if "openapivts" in self.base_url else 0.06
        self._last_request_at = 0.0
        self._day_highs: dict[str, float] = {}

    def recent_bars(self, instrument: Instrument) -> list[Bar]:
        now = datetime.now(KST)
        body = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            "FHKST03010200",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": instrument.symbol,
                "FID_INPUT_HOUR_1": now.strftime("%H%M%S"),
                "FID_PW_DATA_INCU_YN": "Y",
                "FID_ETC_CLS_CODE": "",
            },
        )
        output1 = body.get("output1") or {}
        self._day_highs[instrument.symbol] = float(output1.get("stck_hgpr") or 0)
        return self._parse_bars(body.get("output2", []), instrument, now.date())

    def current_turnover_leaders(self) -> list[Instrument]:
        return self.turnover_leaders(datetime.now(KST).date())

    def turnover_leaders(self, day: date) -> list[Instrument]:
        from pykrx import stock

        leaders: list[Instrument] = []
        for market in ("KOSPI", "KOSDAQ"):
            frame = stock.get_market_ohlcv_by_ticker(day.strftime("%Y%m%d"), market=market)
            if frame.empty or "거래대금" not in frame:
                raise RuntimeError(f"{day} {market} 전종목 거래대금 데이터가 없습니다")
            turnover = frame["거래대금"].fillna(0)
            symbol = str(turnover.idxmax()).upper().zfill(6)
            leaders.append(
                Instrument(
                    symbol=symbol,
                    name=str(stock.get_market_ticker_name(symbol)),
                    market=market,
                    source_date=day,
                    turnover=int(turnover.loc[symbol]),
                )
            )
        return leaders

    def historical_bars(self, instrument: Instrument, day: date) -> list[Bar]:
        unique: dict[datetime, Bar] = {}
        for hour in ("153000", "133000", "113000", "093000"):
            body = self._get(
                "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
                "FHKST03010230",
                {
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": instrument.symbol,
                    "FID_INPUT_HOUR_1": hour,
                    "FID_INPUT_DATE_1": day.strftime("%Y%m%d"),
                    "FID_PW_DATA_INCU_YN": "N",
                    "FID_FAKE_TICK_INCU_YN": "",
                },
            )
            for bar in self._parse_bars(body.get("output2", []), instrument, day):
                unique[bar.timestamp] = bar
        return sorted(unique.values(), key=lambda bar: bar.timestamp)

    def reported_day_high(self, instrument: Instrument) -> float:
        return self._day_highs.get(instrument.symbol, 0)

    def _get(self, path: str, tr_id: str, params: dict[str, str]) -> dict:
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"{self.base_url}{path}?{query}",
            headers={
                "authorization": f"Bearer {self._access_token()}",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
                "tr_id": tr_id,
                "custtype": "P",
                "content-type": "application/json; charset=utf-8",
            },
        )
        body = self._throttled_read(request)
        if str(body.get("rt_cd", "0")) != "0":
            raise RuntimeError(f"KIS API 오류: {body.get('msg_cd')} {body.get('msg1')}")
        return body

    def _access_token(self) -> str:
        with self._token_lock:
            if self._token and datetime.now(KST) < self._token_expires_at:
                return self._token
            payload = json.dumps(
                {
                    "grant_type": "client_credentials",
                    "appkey": self.app_key,
                    "appsecret": self.app_secret,
                }
            ).encode()
            request = urllib.request.Request(
                f"{self.base_url}/oauth2/tokenP",
                data=payload,
                headers={"content-type": "application/json; charset=utf-8"},
                method="POST",
            )
            body = self._throttled_read(request)
            try:
                self._token = str(body["access_token"])
                expires_in = max(60, int(body.get("expires_in", 3600)) - 60)
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"KIS 접근 토큰 발급 실패: {body}") from exc
            self._token_expires_at = datetime.now(KST) + timedelta(seconds=expires_in)
            return self._token

    def _throttled_read(self, request: urllib.request.Request) -> dict:
        with self._request_lock:
            delay = self._request_interval - (time_module.monotonic() - self._last_request_at)
            if delay > 0:
                time_module.sleep(delay)
            try:
                return _read_json(request)
            finally:
                self._last_request_at = time_module.monotonic()

    @staticmethod
    def _parse_bars(rows: list[dict], instrument: Instrument, fallback_day: date) -> list[Bar]:
        parsed: list[Bar] = []
        for row in rows:
            raw_close = row.get("stck_prpr")
            raw_time = str(row.get("stck_cntg_hour") or "")
            raw_day = str(row.get("stck_bsop_date") or fallback_day.strftime("%Y%m%d"))
            if not raw_close or len(raw_time) < 6:
                continue
            timestamp = datetime.strptime(raw_day[:8] + raw_time[:6], "%Y%m%d%H%M%S").replace(
                tzinfo=KST
            )
            try:
                parsed.append(
                    Bar(
                        symbol=instrument.symbol,
                        name=instrument.name,
                        market=instrument.market,
                        timestamp=timestamp,
                        open=float(row["stck_oprc"]),
                        high=float(row["stck_hgpr"]),
                        low=float(row["stck_lwpr"]),
                        close=float(raw_close),
                        volume=int(float(row.get("cntg_vol") or 0)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(parsed, key=lambda bar: bar.timestamp)


def _read_json(request: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"네트워크 연결 실패: {exc.reason}") from exc


def build_provider(settings: Settings) -> MarketDataProvider:
    return KISProvider(settings) if settings.mode == "kis" else SetupProvider()
