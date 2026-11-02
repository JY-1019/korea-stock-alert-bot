from __future__ import annotations

import csv
import html
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .domain import Bar, KST, Position, Signal
from .strategies import Strategy


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    name: str
    market: str
    bars: tuple[Bar, ...]
    signals: tuple[Signal, ...]
    entry_price: float | None
    max_peak_drawdown: float
    final_return: float | None


def load_csv(path: Path) -> list[Bar]:
    required = {"timestamp", "symbol", "name", "market", "open", "high", "low", "close", "volume"}
    bars: list[Bar] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV 필수 열이 없습니다: {', '.join(sorted(missing))}")
        for line, row in enumerate(reader, start=2):
            try:
                timestamp = datetime.fromisoformat(row["timestamp"])
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=KST)
                bars.append(
                    Bar(
                        symbol=row["symbol"].strip().upper().zfill(6),
                        name=row["name"],
                        market=row["market"],
                        timestamp=timestamp,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(float(row["volume"])),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"CSV {line}행이 올바르지 않습니다: {exc}") from exc
    if not bars:
        raise ValueError("CSV에 분봉이 없습니다")
    return sorted(bars, key=lambda item: (item.symbol, item.timestamp))


def run_backtest(
    bars: list[Bar],
    strategy: Strategy,
    entry_prices: dict[str, float] | None = None,
) -> list[BacktestResult]:
    entry_prices = entry_prices or {}
    by_symbol: dict[str, list[Bar]] = {}
    for bar in bars:
        by_symbol.setdefault(bar.symbol, []).append(bar)

    results: list[BacktestResult] = []
    for position_id, (symbol, symbol_bars) in enumerate(sorted(by_symbol.items()), start=1):
        symbol_bars.sort(key=lambda item: item.timestamp)
        entry_price = entry_prices.get(symbol)
        position = (
            Position(position_id, symbol, entry_price, symbol_bars[0].timestamp)
            if entry_price and entry_price > 0
            else None
        )
        day_high = 0.0
        fired: set[str] = set()
        signals: list[Signal] = []
        max_drawdown = 0.0
        current_day = symbol_bars[0].timestamp.astimezone(KST).date()
        day_open = symbol_bars[0].open
        for bar in symbol_bars:
            bar_day = bar.timestamp.astimezone(KST).date()
            if bar_day != current_day:
                current_day = bar_day
                day_high = 0.0
                day_open = bar.open
                fired = {key for key in fired if key.startswith("position:")}
            day_high = max(day_high, bar.high)
            max_drawdown = max(max_drawdown, (day_high - bar.close) / day_high * 100)
            new_signals = strategy.evaluate(bar, day_high, day_open, fired, position)
            signals.extend(new_signals)
            fired.update(signal.event_key for signal in new_signals)
        final_return = (
            (symbol_bars[-1].close - entry_price) / entry_price * 100 if entry_price else None
        )
        results.append(
            BacktestResult(
                symbol=symbol,
                name=symbol_bars[0].name,
                market=symbol_bars[0].market,
                bars=tuple(symbol_bars),
                signals=tuple(signals),
                entry_price=entry_price,
                max_peak_drawdown=max_drawdown,
                final_return=final_return,
            )
        )
    return results


def write_html_report(
    results: list[BacktestResult],
    output: Path,
    source_label: str,
    strategy_description: str,
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    total_signals = sum(len(result.signals) for result in results)
    cards = "".join(_result_card(result) for result in results)
    generated = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    document = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>매매 전략 백테스트 리포트</title>
<style>
:root{{--bg:#0b1020;--card:#141b2d;--text:#eaf0ff;--muted:#92a0bb;--line:#27324a;--cyan:#37d4ff;--red:#ff6178;--green:#41d698}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,"Malgun Gothic",sans-serif}}
main{{max-width:1120px;margin:auto;padding:36px 20px}} h1{{font-size:30px;margin:0 0 8px}} h2{{margin:0;font-size:20px}}
.sub,.note{{color:var(--muted)}} .summary{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:24px 0}}
.metric,.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px}} .metric b{{display:block;font-size:25px}}
.card{{margin:16px 0}} .head{{display:flex;justify-content:space-between;gap:12px;align-items:center}} .pill{{background:#22304c;padding:4px 9px;border-radius:999px}}
.stats{{display:flex;gap:20px;flex-wrap:wrap;color:var(--muted);margin:12px 0}} .stats b{{color:var(--text)}}
svg{{display:block;width:100%;height:auto;background:#0e1526;border-radius:10px}} table{{width:100%;border-collapse:collapse;margin-top:16px}}
th,td{{padding:9px;border-bottom:1px solid var(--line);text-align:left}} th{{color:var(--muted)}} .red{{color:var(--red)}} .green{{color:var(--green)}}
@media(max-width:700px){{.summary{{grid-template-columns:1fr}} .head{{align-items:flex-start;flex-direction:column}}}}
</style></head><body><main>
<h1>매매 전략 백테스트 리포트</h1><div class="sub">생성 {generated} · 데이터 {html.escape(source_label)}</div>
<div class="summary"><div class="metric"><span>대상 종목</span><b>{len(results)}</b></div><div class="metric"><span>총 알림</span><b>{total_signals}</b></div><div class="metric"><span>전략</span><b>1</b></div></div>
<div class="card"><h2>검증 조건</h2><p>{html.escape(strategy_description)}</p>
<p class="note">봉이 임계값을 건너뛰면 건너간 모든 구간을 기록합니다. 고점 하락은 각 1분봉 종가와 그 시점까지의 장중 최고 고가로 계산합니다. 이 리포트는 신호 검증용이며 수수료·세금·슬리피지와 주문 체결을 포함하지 않습니다.</p></div>
{cards}</main></body></html>"""
    output.write_text(document, encoding="utf-8")
    return output


def _result_card(result: BacktestResult) -> str:
    return_label = "미입력" if result.final_return is None else f"{result.final_return:+.2f}%"
    return_class = "green" if (result.final_return or 0) >= 0 else "red"
    rows = "".join(
        f"<tr><td>{signal.timestamp.strftime('%H:%M')}</td><td>{html.escape(signal.kind)}</td>"
        f"<td>{signal.threshold:+g}%</td><td>{signal.price:,.0f}원</td>"
        f"<td>{html.escape(signal.message)}</td></tr>"
        for signal in result.signals
    ) or '<tr><td colspan="5" class="note">발생한 알림이 없습니다.</td></tr>'
    return f"""<section class="card"><div class="head"><h2>{html.escape(result.name)} <small>({result.symbol})</small></h2><span class="pill">{result.market}</span></div>
<div class="stats"><span>분봉 <b>{len(result.bars)}개</b></span><span>최대 고점 하락 <b>{result.max_peak_drawdown:.2f}%</b></span><span>평단 기준 최종 수익률 <b class="{return_class}">{return_label}</b></span><span>알림 <b>{len(result.signals)}건</b></span></div>
{_chart(result)}
<table><thead><tr><th>시간</th><th>종류</th><th>기준</th><th>가격</th><th>내용</th></tr></thead><tbody>{rows}</tbody></table></section>"""


def _chart(result: BacktestResult) -> str:
    width, height, padding = 920, 270, 30
    prices = [bar.close for bar in result.bars]
    low, high = min(prices), max(prices)
    spread = max(high - low, high * 0.01)

    def xy(index: int, price: float) -> tuple[float, float]:
        x = padding + index * (width - padding * 2) / max(1, len(prices) - 1)
        y = padding + (high - price) * (height - padding * 2) / spread
        return x, y

    points = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(i, p) for i, p in enumerate(prices)))
    indexes = {bar.timestamp: index for index, bar in enumerate(result.bars)}
    markers = []
    colors = {"PEAK_DRAWDOWN": "#ffb84d", "STOP_LOSS": "#ff6178", "TAKE_PROFIT": "#41d698"}
    for signal in result.signals:
        index = indexes.get(signal.timestamp)
        if index is None:
            continue
        x, y = xy(index, signal.price)
        markers.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{colors.get(signal.kind, "#fff")}"><title>{html.escape(signal.message)}</title></circle>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(result.name)} 가격과 알림 차트">'
        f'<line x1="{padding}" y1="{height-padding}" x2="{width-padding}" y2="{height-padding}" stroke="#27324a"/>'
        f'<polyline points="{points}" fill="none" stroke="#37d4ff" stroke-width="3"/>'
        + "".join(markers)
        + f'<text x="{padding}" y="20" fill="#92a0bb">최고 종가 {high:,.0f}</text>'
        + f'<text x="{width-padding}" y="{height-8}" text-anchor="end" fill="#92a0bb">최저 종가 {low:,.0f}</text></svg>'
    )
