from __future__ import annotations

import csv
import html
import json
import math
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from .config import Settings
from .domain import Bar, Instrument, KST
from .providers import KISProvider

Progress = Callable[[int, int, str], None]


@dataclass(frozen=True)
class MarketSession:
    source_date: date
    trade_date: date
    instrument: Instrument
    bars: tuple[Bar, ...]


@dataclass(frozen=True)
class Fill:
    timestamp: datetime
    level: float
    quantity: int
    price: float
    amount: float
    peak_drawdown: float
    open_drawdown: float


@dataclass(frozen=True)
class Trade:
    market: str
    symbol: str
    name: str
    source_date: date
    trade_date: date
    fills: tuple[Fill, ...]
    exit_time: datetime
    exit_price: float
    exit_reason: str
    quantity: int
    average_price: float
    invested: float
    proceeds: float
    pnl: float
    return_pct: float
    bars: tuple[Bar, ...]


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    equity: float
    market: str
    company: str
    symbol: str
    event: str


@dataclass(frozen=True)
class OpenPosition:
    market: str
    symbol: str
    name: str
    entry_time: datetime
    quantity: int
    average_price: float
    last_price: float
    unrealized_pnl: float
    return_pct: float


@dataclass(frozen=True)
class MarketBacktestResult:
    initial_capital: float
    final_capital: float
    total_return_pct: float
    max_drawdown_pct: float
    trades: tuple[Trade, ...]
    equity: tuple[EquityPoint, ...]
    markets: tuple[str, ...]
    start: date
    end: date
    round_trip_cost_pct: float
    close_at_end: bool
    open_positions: tuple[OpenPosition, ...] = ()


@dataclass
class _Holding:
    instrument: Instrument
    source_date: date
    trade_date: date
    fills: list[Fill]
    quantity: int
    position_cost: float
    buy_fees: float
    bars: list[Bar]
    last_price: float


class ActualMarketDataLoader:
    """Selects actual KRX turnover leaders, then downloads their KIS one-minute bars."""

    def __init__(self, settings: Settings, provider: KISProvider):
        if not settings.krx_id or not settings.krx_password:
            raise RuntimeError("KRX_ID와 KRX_PW 환경변수가 필요합니다")
        if not settings.kis_app_key or not settings.kis_app_secret:
            raise RuntimeError("KIS_APP_KEY와 KIS_APP_SECRET 환경변수가 필요합니다")
        self.settings = settings
        self.provider = provider
        self.cache = settings.database.parent / "backtest_cache"
        self.cache.mkdir(parents=True, exist_ok=True)

    def load(
        self,
        start: date,
        end: date,
        markets: tuple[str, ...],
        progress: Progress,
    ) -> list[MarketSession]:
        if start > end:
            raise ValueError("시작일은 종료일보다 늦을 수 없습니다")
        if end >= datetime.now(KST).date():
            raise ValueError("완료된 실제 데이터만 사용하므로 종료일은 오늘 이전이어야 합니다")
        if (end - start).days > 365:
            raise ValueError("KIS 분봉 보관 범위에 맞춰 최대 1년까지 조회할 수 있습니다")

        from pykrx import stock

        calendar_start = start - timedelta(days=14)
        index = stock.get_index_ohlcv_by_date(
            calendar_start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "1001"
        )
        trading_days = [value.date() for value in index.index.to_pydatetime()]
        requested = [day for day in trading_days if start <= day <= end]
        pairs = [
            (trading_days[trading_days.index(day) - 1], day, market)
            for day in requested
            if trading_days.index(day) > 0
            for market in markets
        ]
        total = len(pairs)
        sessions: list[MarketSession] = []
        for current, (source_day, trade_day, market) in enumerate(pairs, start=1):
            progress(current - 1, total, f"{source_day} {market} 전종목 거래대금 순위 조회")
            leader = self._leader(stock, source_day, market)
            progress(current - 1, total, f"{trade_day} {leader.name} 1분봉 조회")
            bars = self.bars(leader, trade_day)
            if bars:
                sessions.append(MarketSession(source_day, trade_day, leader, tuple(bars)))
            progress(current, total, f"{trade_day} {market} 완료")
        if not sessions:
            raise RuntimeError("조회 기간에 사용할 실제 시장 데이터가 없습니다")
        return sessions

    def _leader(self, stock: object, day: date, market: str) -> Instrument:
        cache = self.cache / f"leader_{market}_{day:%Y%m%d}.json"
        if cache.exists():
            row = json.loads(cache.read_text(encoding="utf-8"))
            return Instrument(
                row["symbol"], row["name"], market, day, int(row["turnover"])
            )
        frame = stock.get_market_ohlcv_by_ticker(day.strftime("%Y%m%d"), market=market)
        if frame.empty or "거래대금" not in frame:
            raise RuntimeError(f"{day} {market} 전종목 거래대금 데이터가 없습니다")
        turnover = frame["거래대금"].fillna(0)
        symbol = str(turnover.idxmax()).upper().zfill(6)
        name = str(stock.get_market_ticker_name(symbol))
        instrument = Instrument(symbol, name, market, day, int(turnover.loc[symbol]))
        cache.write_text(
            json.dumps(
                {"symbol": symbol, "name": name, "turnover": instrument.turnover},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        time_module.sleep(0.5)
        return instrument

    def bars(self, instrument: Instrument, day: date) -> list[Bar]:
        cache = self.cache / f"bars_{instrument.symbol}_{day:%Y%m%d}.csv"
        if cache.exists():
            return _read_bars(cache, instrument)
        bars = self.provider.historical_bars(instrument, day)
        if bars:
            _write_bars(cache, bars)
        return bars


def run_market_backtest(
    sessions: list[MarketSession],
    initial_capital: float,
    markets: tuple[str, ...],
    levels: tuple[float, ...] = (8, 10, 12, 15),
    tranche_pct: float = 25,
    stop_loss_pct: float = 5,
    take_profit_pct: float = 4,
    round_trip_cost_pct: float = 0,
    close_at_end: bool = True,
    bar_loader: Callable[[Instrument, date], list[Bar]] | None = None,
) -> MarketBacktestResult:
    if initial_capital <= 0:
        raise ValueError("초기자본은 0보다 커야 합니다")
    if not markets:
        raise ValueError("최소 한 시장을 선택해야 합니다")
    if not 0 < tranche_pct <= 100 or tranche_pct * len(levels) > 100:
        raise ValueError("분할매수 비율 합계는 100% 이하여야 합니다")
    if min(stop_loss_pct, take_profit_pct) <= 0:
        raise ValueError("손절·익절 기준은 0보다 커야 합니다")
    if not 0 <= round_trip_cost_pct < 10:
        raise ValueError("왕복 비용률은 0% 이상 10% 미만이어야 합니다")
    if not close_at_end:
        return _run_hold_backtest(
            sessions,
            initial_capital,
            markets,
            levels,
            tranche_pct,
            stop_loss_pct,
            take_profit_pct,
            round_trip_cost_pct,
            bar_loader,
        )

    market_seed = initial_capital / len(markets)
    cash = {market: market_seed for market in markets}
    trade_list: list[Trade] = []
    equity: list[EquityPoint] = [
        EquityPoint(
            timestamp=datetime.combine(min(session.trade_date for session in sessions), datetime.min.time(), KST),
            equity=initial_capital,
            market="ALL",
            company="시작",
            symbol="",
            event="START",
        )
    ]
    side_cost = round_trip_cost_pct / 200

    for session in sorted(sessions, key=lambda item: (item.trade_date, item.instrument.market)):
        market = session.instrument.market
        if market not in cash or not session.bars:
            continue
        available = cash[market]
        tranche_cash = market_seed * tranche_pct / 100
        day_open = session.bars[0].open
        day_high = day_open
        filled: set[float] = set()
        fills: list[Fill] = []
        quantity = 0
        position_cost = 0.0
        closed = False
        exit_price = 0.0
        exit_time = session.bars[-1].timestamp
        exit_reason = "장마감 청산"

        for bar in session.bars:
            if quantity:
                average = position_cost / quantity
                stop_price = average * (1 - stop_loss_pct / 100)
                target_price = average * (1 + take_profit_pct / 100)
                if bar.low <= stop_price:
                    exit_price, exit_time, exit_reason, closed = stop_price, bar.timestamp, "손절 -5%", True
                elif bar.high >= target_price:
                    exit_price, exit_time, exit_reason, closed = target_price, bar.timestamp, "익절 +4%", True
                if closed:
                    break

            day_high = max(day_high, bar.high)
            peak_drawdown = (day_high - bar.low) / day_high * 100
            open_drawdown = (day_open - bar.low) / day_open * 100
            for level in levels:
                if level in filled or max(peak_drawdown, open_drawdown) < level:
                    continue
                reference = max(day_high, day_open)
                trigger = reference * (1 - level / 100)
                fill_price = min(bar.open, trigger) if bar.open <= trigger else trigger
                budget = min(tranche_cash, available)
                buy_quantity = math.floor(budget / (fill_price * (1 + side_cost)))
                if buy_quantity <= 0:
                    continue
                amount = buy_quantity * fill_price
                total_cost = amount * (1 + side_cost)
                available -= total_cost
                quantity += buy_quantity
                position_cost += amount
                filled.add(level)
                fills.append(
                    Fill(
                        bar.timestamp,
                        level,
                        buy_quantity,
                        fill_price,
                        amount,
                        peak_drawdown,
                        open_drawdown,
                    )
                )

        if not quantity:
            cash[market] = available
            continue
        if not closed:
            exit_price = session.bars[-1].close
        proceeds = quantity * exit_price * (1 - side_cost)
        invested = position_cost
        pnl = proceeds - invested - sum(fill.amount * side_cost for fill in fills)
        cash[market] = available + proceeds
        average = position_cost / quantity
        trade = Trade(
            market,
            session.instrument.symbol,
            session.instrument.name,
            session.source_date,
            session.trade_date,
            tuple(fills),
            exit_time,
            exit_price,
            exit_reason,
            quantity,
            average,
            invested,
            proceeds,
            pnl,
            pnl / (invested + sum(fill.amount * side_cost for fill in fills)) * 100,
            session.bars,
        )
        trade_list.append(trade)
        equity.append(
            EquityPoint(
                exit_time,
                sum(cash.values()),
                market,
                session.instrument.name,
                session.instrument.symbol,
                exit_reason,
            )
        )

    final_capital = sum(cash.values())
    peaks, max_drawdown = initial_capital, 0.0
    for point in equity:
        peaks = max(peaks, point.equity)
        max_drawdown = max(max_drawdown, (peaks - point.equity) / peaks * 100)
    start = min(session.trade_date for session in sessions)
    end = max(session.trade_date for session in sessions)
    return MarketBacktestResult(
        initial_capital,
        final_capital,
        (final_capital / initial_capital - 1) * 100,
        max_drawdown,
        tuple(trade_list),
        tuple(equity),
        markets,
        start,
        end,
        round_trip_cost_pct,
        True,
    )


def _run_hold_backtest(
    sessions: list[MarketSession],
    initial_capital: float,
    markets: tuple[str, ...],
    levels: tuple[float, ...],
    tranche_pct: float,
    stop_loss_pct: float,
    take_profit_pct: float,
    round_trip_cost_pct: float,
    bar_loader: Callable[[Instrument, date], list[Bar]] | None,
) -> MarketBacktestResult:
    sessions = sorted(sessions, key=lambda item: (item.trade_date, item.instrument.market))
    market_seed = initial_capital / len(markets)
    tranche_cash = market_seed * tranche_pct / 100
    side_cost = round_trip_cost_pct / 200
    cash = {market: market_seed for market in markets}
    holdings: dict[tuple[str, str], _Holding] = {}
    trades: list[Trade] = []
    equity = [
        EquityPoint(
            datetime.combine(sessions[0].trade_date, datetime.min.time(), KST),
            initial_capital,
            "ALL",
            "시작",
            "",
            "START",
        )
    ]
    by_day: dict[date, list[MarketSession]] = {}
    for session in sessions:
        by_day.setdefault(session.trade_date, []).append(session)

    for trade_day, day_sessions in sorted(by_day.items()):
        leaders = {(item.instrument.market, item.instrument.symbol): item for item in day_sessions}
        streams: dict[tuple[str, str], list[Bar]] = {
            key: list(session.bars) for key, session in leaders.items()
        }
        for key, holding in list(holdings.items()):
            if key not in streams:
                streams[key] = list(bar_loader(holding.instrument, trade_day)) if bar_loader else []

        bars_at: dict[datetime, dict[tuple[str, str], Bar]] = {}
        for key, bars in streams.items():
            for bar in bars:
                bars_at.setdefault(bar.timestamp, {})[key] = bar

        states = {
            key: {
                "session": session,
                "day_open": session.bars[0].open,
                "day_high": session.bars[0].open,
                "filled": set(),
                "seen": [],
                "holding": None,
                "closed": False,
            }
            for key, session in leaders.items()
            if session.bars
        }

        for timestamp, current in sorted(bars_at.items()):
            exited: set[tuple[str, str]] = set()
            for key, holding in list(holdings.items()):
                bar = current.get(key)
                if not bar:
                    continue
                holding.bars.append(bar)
                holding.last_price = bar.close
                average = holding.position_cost / holding.quantity
                stop_price = average * (1 - stop_loss_pct / 100)
                target_price = average * (1 + take_profit_pct / 100)
                if bar.low <= stop_price:
                    exit_price, reason = stop_price, f"손절 -{stop_loss_pct:g}%"
                elif bar.high >= target_price:
                    exit_price, reason = target_price, f"익절 +{take_profit_pct:g}%"
                else:
                    continue
                proceeds = holding.quantity * exit_price * (1 - side_cost)
                pnl = proceeds - holding.position_cost - holding.buy_fees
                cash[key[0]] += proceeds
                trades.append(
                    Trade(
                        key[0],
                        holding.instrument.symbol,
                        holding.instrument.name,
                        holding.source_date,
                        holding.trade_date,
                        tuple(holding.fills),
                        timestamp,
                        exit_price,
                        reason,
                        holding.quantity,
                        average,
                        holding.position_cost,
                        proceeds,
                        pnl,
                        pnl / (holding.position_cost + holding.buy_fees) * 100,
                        tuple(holding.bars),
                    )
                )
                del holdings[key]
                exited.add(key)
                state = states.get(key)
                if state and state["holding"] is holding:
                    state["closed"] = True

            for key, state in states.items():
                bar = current.get(key)
                if not bar:
                    continue
                state["seen"].append(bar)
                state["day_high"] = max(state["day_high"], bar.high)
                if state["closed"] or key in exited or (key in holdings and state["holding"] is None):
                    continue
                peak_drawdown = (state["day_high"] - bar.low) / state["day_high"] * 100
                open_drawdown = (state["day_open"] - bar.low) / state["day_open"] * 100
                for level in levels:
                    if level in state["filled"] or max(peak_drawdown, open_drawdown) < level:
                        continue
                    reference = max(state["day_high"], state["day_open"])
                    trigger = reference * (1 - level / 100)
                    fill_price = min(bar.open, trigger) if bar.open <= trigger else trigger
                    budget = min(tranche_cash, cash[key[0]])
                    quantity = math.floor(budget / (fill_price * (1 + side_cost)))
                    if quantity <= 0:
                        continue
                    amount = quantity * fill_price
                    fee = amount * side_cost
                    cash[key[0]] -= amount + fee
                    state["filled"].add(level)
                    fill = Fill(
                        timestamp,
                        level,
                        quantity,
                        fill_price,
                        amount,
                        peak_drawdown,
                        open_drawdown,
                    )
                    holding = state["holding"]
                    if holding is None:
                        session = state["session"]
                        holding = _Holding(
                            session.instrument,
                            session.source_date,
                            session.trade_date,
                            [],
                            0,
                            0,
                            0,
                            list(state["seen"]),
                            bar.close,
                        )
                        holdings[key] = holding
                        state["holding"] = holding
                    holding.fills.append(fill)
                    holding.quantity += quantity
                    holding.position_cost += amount
                    holding.buy_fees += fee

        value = sum(cash.values()) + sum(
            holding.quantity * holding.last_price for holding in holdings.values()
        )
        last_timestamp = max(
            (bar.timestamp for bars in streams.values() for bar in bars),
            default=datetime.combine(trade_day, datetime.min.time(), KST),
        )
        equity.append(EquityPoint(last_timestamp, value, "ALL", "보유 평가", "", "MARK"))

    open_positions = tuple(
        OpenPosition(
            holding.instrument.market,
            holding.instrument.symbol,
            holding.instrument.name,
            holding.fills[0].timestamp,
            holding.quantity,
            holding.position_cost / holding.quantity,
            holding.last_price,
            holding.quantity * holding.last_price - holding.position_cost - holding.buy_fees,
            (
                holding.last_price / (holding.position_cost / holding.quantity) - 1
            )
            * 100,
        )
        for holding in holdings.values()
    )
    final_capital = sum(cash.values()) + sum(
        holding.quantity * holding.last_price for holding in holdings.values()
    )
    peak, max_drawdown = initial_capital, 0.0
    for point in equity:
        peak = max(peak, point.equity)
        max_drawdown = max(max_drawdown, (peak - point.equity) / peak * 100)
    return MarketBacktestResult(
        initial_capital,
        final_capital,
        (final_capital / initial_capital - 1) * 100,
        max_drawdown,
        tuple(trades),
        tuple(equity),
        markets,
        sessions[0].trade_date,
        sessions[-1].trade_date,
        round_trip_cost_pct,
        False,
        open_positions,
    )


def write_market_report(result: MarketBacktestResult, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    equity_json = json.dumps(
        [
            {
                "time": point.timestamp.isoformat(),
                "value": round(point.equity),
                "market": point.market,
                "company": point.company,
                "symbol": point.symbol,
                "event": point.event,
            }
            for point in result.equity
        ],
        ensure_ascii=False,
    ).replace("</", "<\\/")
    rows = "".join(_trade_row(index, trade) for index, trade in enumerate(result.trades, 1))
    details = "".join(_trade_detail(index, trade) for index, trade in enumerate(result.trades, 1))
    open_rows = "".join(
        f"<tr><td>{position.market}</td><td>{html.escape(position.name)} ({position.symbol})</td>"
        f"<td>{position.entry_time:%Y-%m-%d %H:%M}</td><td>{position.quantity:,}주</td>"
        f"<td>{position.average_price:,.0f}원</td><td>{position.last_price:,.0f}원</td>"
        f"<td class='{'green' if position.unrealized_pnl >= 0 else 'red'}'>"
        f"{position.unrealized_pnl:+,.0f}원 ({position.return_pct:+.2f}%)</td></tr>"
        for position in result.open_positions
    )
    mode_label = "장마감 청산" if result.close_at_end else "TP/SL까지 익일 이월"
    ending_rule = (
        "미청산 포지션은 마지막 1분봉 종가에 청산합니다."
        if result.close_at_end
        else "장마감에는 청산하지 않고 다음 거래일에도 TP/SL을 추적하며, 기간 종료 보유분은 마지막 가격으로 평가만 합니다."
    )
    generated = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    document = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>전일 거래대금 1위 분할매수 백테스트</title><style>
:root{{--bg:#080d19;--card:#121a2b;--line:#26324b;--text:#eef4ff;--muted:#91a0bb;--blue:#43d5ff;--green:#3bd69b;--red:#ff6178;--yellow:#ffbd5a}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,"Malgun Gothic",sans-serif}}main{{max-width:1380px;margin:auto;padding:30px 18px}}
h1{{margin:0;font-size:28px}}h2{{font-size:18px;margin:0 0 12px}}.muted{{color:var(--muted)}}.metrics{{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin:22px 0}}.metric,.card,details{{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:16px}}.metric b{{display:block;font-size:22px}}.green{{color:var(--green)}}.red{{color:var(--red)}}
canvas{{width:100%;height:380px;background:#0b1221;border-radius:10px}}.table{{overflow:auto;max-height:560px}}table{{border-collapse:collapse;width:100%;min-width:1150px}}th,td{{padding:9px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}th{{position:sticky;top:0;background:var(--card);color:var(--muted)}}details{{margin-top:10px}}summary{{cursor:pointer;font-weight:700}}svg{{display:block;width:100%;height:auto;margin-top:12px;background:#0b1221;border-radius:9px}}
@media(max-width:900px){{.metrics{{grid-template-columns:repeat(2,1fr)}}}}
</style></head><body><main><h1>전일 거래대금 1위 분할매수 백테스트</h1><div class="muted">{result.start} ~ {result.end} · {', '.join(result.markets)} · {mode_label} · 실제 KRX/KIS 데이터 · 생성 {generated}</div>
<div class="metrics"><div class="metric">초기자본<b>{result.initial_capital:,.0f}원</b></div><div class="metric">최종 평가자산<b>{result.final_capital:,.0f}원</b></div><div class="metric">누적수익률<b class="{'green' if result.total_return_pct >= 0 else 'red'}">{result.total_return_pct:+.2f}%</b></div><div class="metric">청산 거래<b>{len(result.trades)}회</b></div><div class="metric">보유 포지션<b>{len(result.open_positions)}개</b></div><div class="metric">최대 낙폭<b>{result.max_drawdown_pct:.2f}%</b></div></div>
<section class="card"><h2>누적 자산과 거래 시점</h2><canvas id="equity"></canvas><div class="muted">점 위에 마우스를 올리면 거래 날짜·시간, 회사명, 종목코드와 청산 사유를 확인할 수 있습니다.</div></section>
<section class="card"><h2>체결 내역</h2><div class="table"><table><thead><tr><th>#</th><th>시장</th><th>회사</th><th>선정 기준일</th><th>매수 일시</th><th>분할 구간</th><th>가중평단</th><th>청산 일시</th><th>청산 사유</th><th>손익</th><th>수익률</th></tr></thead><tbody>{rows or '<tr><td colspan="11">매수 조건에 도달한 거래가 없습니다.</td></tr>'}</tbody></table></div></section>
{f'<section class="card"><h2>기간 종료 보유 포지션</h2><div class="table"><table><thead><tr><th>시장</th><th>회사</th><th>최초 매수</th><th>수량</th><th>가중평단</th><th>최종가</th><th>평가손익</th></tr></thead><tbody>{open_rows}</tbody></table></div></section>' if open_rows else ''}
<section><h2>거래별 1분봉 차트</h2>{details or '<div class="card muted">표시할 거래가 없습니다.</div>'}</section>
<section class="card"><h2>전략 및 체결 가정</h2><p>전일 KRX 전종목 거래대금 1위를 다음 거래일에 추적합니다. 당일 고점 또는 시가 대비 8·10·12·15% 하락 가격에 도달할 때마다 시장 배정 초기자본의 25%씩 매수하고, 가중평단 대비 -5% 손절 또는 +4% 익절합니다. 같은 1분봉에서 손절과 익절이 모두 닿으면 손절을 먼저 적용합니다. {ending_rule} 왕복 비용률 {result.round_trip_cost_pct:g}%를 매수·매도에 절반씩 적용했습니다.</p></section>
<script>const points={equity_json};const canvas=document.getElementById('equity'),ctx=canvas.getContext('2d');function draw(){{const d=devicePixelRatio||1,r=canvas.getBoundingClientRect(),w=r.width,h=r.height,p=48;canvas.width=w*d;canvas.height=h*d;ctx.scale(d,d);ctx.clearRect(0,0,w,h);if(points.length<2)return;const vals=points.map(x=>x.value),lo=Math.min(...vals),hi=Math.max(...vals),range=Math.max(1,hi-lo);const xy=(i,v)=>[p+i*(w-p*2)/(points.length-1),p+(hi-v)*(h-p*2)/range];ctx.strokeStyle='#26324b';ctx.beginPath();ctx.moveTo(p,h-p);ctx.lineTo(w-p,h-p);ctx.stroke();ctx.beginPath();points.forEach((q,i)=>{{const [x,y]=xy(i,q.value);i?ctx.lineTo(x,y):ctx.moveTo(x,y)}});ctx.strokeStyle='#43d5ff';ctx.lineWidth=2;ctx.stroke();ctx.font='11px system-ui';points.forEach((q,i)=>{{if(!i)return;const[x,y]=xy(i,q.value);ctx.fillStyle=q.event.includes('익절')?'#3bd69b':q.event.includes('손절')?'#ff6178':'#ffbd5a';ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);ctx.fill();if(points.length<=30){{ctx.save();ctx.translate(x+5,y-7);ctx.rotate(-.4);ctx.fillText(q.company,0,0);ctx.restore()}}}});ctx.fillStyle='#91a0bb';for(const i of [0,Math.floor((points.length-1)/2),points.length-1]){{const[x]=xy(i,points[i].value);ctx.fillText(new Date(points[i].time).toLocaleDateString('ko-KR'),x-25,h-14)}}}}draw();addEventListener('resize',draw);canvas.onmousemove=e=>{{const r=canvas.getBoundingClientRect(),i=Math.max(0,Math.min(points.length-1,Math.round((e.clientX-r.left-48)*(points.length-1)/(r.width-96)))),q=points[i];canvas.title=`${{new Date(q.time).toLocaleString('ko-KR')}} | ${{q.company}}(${{q.symbol}}) | ${{q.event}} | ${{q.value.toLocaleString()}}원`}};</script></main></body></html>"""
    output.write_text(document, encoding="utf-8")
    return output


def _trade_row(index: int, trade: Trade) -> str:
    levels = ", ".join(f"-{fill.level:g}%" for fill in trade.fills)
    first = trade.fills[0].timestamp.strftime("%Y-%m-%d %H:%M")
    color = "green" if trade.pnl >= 0 else "red"
    return (
        f"<tr><td>{index}</td><td>{trade.market}</td><td>{html.escape(trade.name)} ({trade.symbol})</td>"
        f"<td>{trade.source_date}</td><td>{first}</td><td>{levels}</td>"
        f"<td>{trade.average_price:,.0f}원</td><td>{trade.exit_time:%Y-%m-%d %H:%M}</td>"
        f"<td>{trade.exit_reason}</td><td class='{color}'>{trade.pnl:+,.0f}원</td>"
        f"<td class='{color}'>{trade.return_pct:+.2f}%</td></tr>"
    )


def _trade_detail(index: int, trade: Trade) -> str:
    fills = " · ".join(
        f"{fill.timestamp:%Y-%m-%d %H:%M} -{fill.level:g}% {fill.quantity:,}주 @ {fill.price:,.0f}원"
        for fill in trade.fills
    )
    return (
        f"<details><summary>#{index} {html.escape(trade.name)} ({trade.symbol}) · {trade.trade_date} · "
        f"{trade.return_pct:+.2f}%</summary><div class='muted'>매수: {fills}<br>청산: "
        f"{trade.exit_time:%Y-%m-%d %H:%M} {trade.exit_reason} @ {trade.exit_price:,.0f}원</div>"
        f"{_trade_svg(trade)}</details>"
    )


def _trade_svg(trade: Trade) -> str:
    width, height, pad = 1100, 230, 34
    bars = trade.bars
    prices = [bar.close for bar in bars]
    low, high = min(prices), max(prices)
    spread = max(1, high - low)

    def xy(index: int, price: float) -> tuple[float, float]:
        return (
            pad + index * (width - pad * 2) / max(1, len(bars) - 1),
            pad + (high - price) * (height - pad * 2) / spread,
        )

    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(i, p) for i, p in enumerate(prices)))
    index_by_time = {bar.timestamp: i for i, bar in enumerate(bars)}
    markers = []
    for fill in trade.fills:
        x, y = xy(index_by_time.get(fill.timestamp, 0), fill.price)
        markers.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#ffbd5a"/><text x="{x+6:.1f}" y="{y-6:.1f}" fill="#ffbd5a">B-{fill.level:g}%</text>')
    x, y = xy(index_by_time.get(trade.exit_time, len(bars) - 1), trade.exit_price)
    markers.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="#ff6178"/><text x="{x+7:.1f}" y="{y-7:.1f}" fill="#ff6178">S {trade.exit_reason}</text>')
    return (
        f'<svg viewBox="0 0 {width} {height}" aria-label="{html.escape(trade.name)} 1분봉 거래 차트">'
        f'<polyline points="{line}" fill="none" stroke="#43d5ff" stroke-width="2"/>{"".join(markers)}'
        f'<text x="{pad}" y="{height-8}" fill="#91a0bb">{bars[0].timestamp:%Y-%m-%d %H:%M}</text>'
        f'<text x="{width-pad}" y="{height-8}" text-anchor="end" fill="#91a0bb">{bars[-1].timestamp:%Y-%m-%d %H:%M}</text></svg>'
    )


def _write_bars(path: Path, bars: list[Bar]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("timestamp", "open", "high", "low", "close", "volume"))
        writer.writerows(
            (bar.timestamp.isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume)
            for bar in bars
        )


def _read_bars(path: Path, instrument: Instrument) -> list[Bar]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            Bar(
                instrument.symbol,
                instrument.name,
                instrument.market,
                datetime.fromisoformat(row["timestamp"]),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                int(row["volume"]),
            )
            for row in csv.DictReader(handle)
        ]
