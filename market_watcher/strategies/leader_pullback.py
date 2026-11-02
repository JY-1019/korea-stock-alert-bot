from __future__ import annotations

from dataclasses import dataclass

from ..domain import Bar, Position, Signal


@dataclass(frozen=True)
class LeaderPullbackStrategy:
    drawdown_levels: tuple[float, ...] = (8, 10, 12, 15)
    stop_loss: float = 5
    take_profit: float = 4
    tranche_pct: float = 25
    name: str = "previous_turnover_leader_pullback"

    def __post_init__(self) -> None:
        if not self.drawdown_levels or any(level <= 0 or level >= 100 for level in self.drawdown_levels):
            raise ValueError("고점 하락률은 0보다 크고 100보다 작아야 합니다")
        if tuple(sorted(set(self.drawdown_levels))) != self.drawdown_levels:
            raise ValueError("고점 하락률은 중복 없이 오름차순이어야 합니다")
        if self.stop_loss <= 0 or self.take_profit <= 0:
            raise ValueError("손절·익절 기준은 0보다 커야 합니다")
        if not 0 < self.tranche_pct <= 100:
            raise ValueError("분할매수 비율은 0보다 크고 100 이하여야 합니다")

    def evaluate(
        self,
        bar: Bar,
        day_high: float,
        day_open: float,
        fired_event_keys: set[str],
        position: Position | None,
    ) -> list[Signal]:
        if day_high <= 0 or day_open <= 0:
            return []
        signals: list[Signal] = []
        peak_drawdown = max(0.0, (day_high - bar.close) / day_high * 100)
        open_drawdown = max(0.0, (day_open - bar.close) / day_open * 100)
        for level in self.drawdown_levels:
            key = f"buy:{level:g}"
            if max(peak_drawdown, open_drawdown) >= level and key not in fired_event_keys:
                signals.append(
                    self._signal(
                        bar,
                        key,
                        "BUY_TRANCHE",
                        max(day_high, day_open),
                        level,
                        f"🟡 {bar.name}({bar.symbol}) {level:g}% 구간 "
                        f"초기 배정자본의 {self.tranche_pct:g}% 분할매수 알림 | 현재 {bar.close:,.0f}원 "
                        f"| 고점대비 -{peak_drawdown:.2f}% | 시가대비 -{open_drawdown:.2f}%",
                    )
                )

        if position is None:
            return signals

        return_pct = (bar.close - position.average_price) / position.average_price * 100
        stop_key = f"position:{position.id}:stop:{self.stop_loss:g}"
        if return_pct <= -self.stop_loss and stop_key not in fired_event_keys:
            signals.append(
                self._signal(
                    bar,
                    stop_key,
                    "STOP_LOSS",
                    position.average_price,
                    -self.stop_loss,
                    f"🛑 {bar.name}({bar.symbol}) 가중평단 대비 -{self.stop_loss:g}% 전량 손절 "
                    f"| 현재 {bar.close:,.0f}원 | 평단 {position.average_price:,.0f}원",
                )
            )

        take_profit_key = f"position:{position.id}:take_profit:{self.take_profit:g}"
        if return_pct >= self.take_profit and take_profit_key not in fired_event_keys:
            signals.append(
                self._signal(
                    bar,
                    take_profit_key,
                    "TAKE_PROFIT",
                    position.average_price,
                    self.take_profit,
                    f"✅ {bar.name}({bar.symbol}) 가중평단 대비 +{self.take_profit:g}% 전량 익절 "
                    f"| 현재 {bar.close:,.0f}원 | 평단 {position.average_price:,.0f}원",
                )
            )
        return signals

    def _signal(
        self,
        bar: Bar,
        event_key: str,
        kind: str,
        reference_price: float,
        threshold: float,
        message: str,
    ) -> Signal:
        return Signal(
            strategy=self.name,
            event_key=event_key,
            kind=kind,
            symbol=bar.symbol,
            name=bar.name,
            market=bar.market,
            timestamp=bar.timestamp,
            price=bar.close,
            reference_price=reference_price,
            threshold=threshold,
            message=message,
        )
