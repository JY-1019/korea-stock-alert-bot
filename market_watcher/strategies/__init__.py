from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from ..domain import Bar, Position, Signal
from .leader_pullback import LeaderPullbackStrategy


class Strategy(Protocol):
    name: str

    def evaluate(
        self,
        bar: Bar,
        day_high: float,
        day_open: float,
        fired_event_keys: set[str],
        position: Position | None,
    ) -> list[Signal]: ...


StrategyBuilder = Callable[[dict[str, Any]], Strategy]


def _leader_pullback(options: dict[str, Any]) -> Strategy:
    return LeaderPullbackStrategy(
        drawdown_levels=tuple(float(x) for x in options.get("drawdown_levels", [8, 10, 12, 15])),
        stop_loss=float(options.get("stop_loss", options.get("stop_step", 5))),
        take_profit=float(options.get("take_profit", 4)),
        tranche_pct=float(options.get("tranche_pct", 25)),
    )


REGISTRY: dict[str, StrategyBuilder] = {
    "previous_turnover_leader_pullback": _leader_pullback,
}


def build_strategies(configs: tuple[dict[str, Any], ...]) -> list[Strategy]:
    strategies: list[Strategy] = []
    for options in configs:
        strategy_type = options.get("type", "")
        try:
            strategies.append(REGISTRY[strategy_type](options))
        except KeyError as exc:
            raise ValueError(f"등록되지 않은 전략입니다: {strategy_type}") from exc
    return strategies
