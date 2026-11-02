from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .domain import Bar, Instrument, KST, Position, Signal


class Storage:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS bars (
                    symbol TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER NOT NULL,
                    PRIMARY KEY (symbol, timestamp)
                );
                CREATE INDEX IF NOT EXISTS bars_symbol_time ON bars(symbol, timestamp DESC);

                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL UNIQUE,
                    average_price REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    unique_key TEXT NOT NULL UNIQUE,
                    strategy TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    market TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    price REAL NOT NULL,
                    reference_price REAL NOT NULL,
                    threshold REAL NOT NULL,
                    message TEXT NOT NULL,
                    notified INTEGER NOT NULL DEFAULT 0,
                    notification_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS signals_time ON signals(timestamp DESC);

                CREATE TABLE IF NOT EXISTS leaders (
                    source_date TEXT NOT NULL,
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    turnover INTEGER,
                    PRIMARY KEY (source_date, market)
                );
                """
            )

    def upsert_bars(self, bars: list[Bar]) -> None:
        if not bars:
            return
        with self._connect() as db:
            db.executemany(
                """
                INSERT INTO bars(symbol, timestamp, name, market, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, timestamp) DO UPDATE SET
                    name=excluded.name, market=excluded.market, open=excluded.open,
                    high=excluded.high, low=excluded.low, close=excluded.close,
                    volume=excluded.volume
                """,
                [
                    (
                        bar.symbol,
                        bar.timestamp.isoformat(),
                        bar.name,
                        bar.market,
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                    )
                    for bar in bars
                ],
            )

    def bars(self, symbol: str, day: date | None = None, limit: int = 500) -> list[dict[str, Any]]:
        query = "SELECT * FROM bars WHERE symbol = ?"
        params: list[Any] = [symbol]
        if day:
            query += " AND timestamp >= ? AND timestamp < ?"
            params.extend(
                [
                    datetime.combine(day, datetime.min.time(), KST).isoformat(),
                    datetime.combine(day, datetime.max.time(), KST).isoformat(),
                ]
            )
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in reversed(rows)]

    def day_high(self, symbol: str, day: date) -> float:
        start = datetime.combine(day, datetime.min.time(), KST).isoformat()
        end = datetime.combine(day, datetime.max.time(), KST).isoformat()
        with self._connect() as db:
            row = db.execute(
                "SELECT MAX(high) AS high FROM bars WHERE symbol=? AND timestamp BETWEEN ? AND ?",
                (symbol, start, end),
            ).fetchone()
        return float(row["high"] or 0)

    def day_open(self, symbol: str, day: date) -> float:
        start = datetime.combine(day, datetime.min.time(), KST).isoformat()
        end = datetime.combine(day, datetime.max.time(), KST).isoformat()
        with self._connect() as db:
            row = db.execute(
                """
                SELECT open FROM bars WHERE symbol=? AND timestamp BETWEEN ? AND ?
                ORDER BY timestamp LIMIT 1
                """,
                (symbol, start, end),
            ).fetchone()
        return float(row["open"] if row else 0)

    def set_position(self, symbol: str, average_price: float) -> Position:
        if average_price <= 0:
            raise ValueError("평단가는 0보다 커야 합니다")
        now = datetime.now(KST)
        with self._connect() as db:
            existing = db.execute("SELECT id FROM positions WHERE symbol=?", (symbol,)).fetchone()
            if existing:
                # 평단 변경은 새 포지션 주기로 취급해 과거 알림과 분리합니다.
                db.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
            cursor = db.execute(
                "INSERT INTO positions(symbol, average_price, updated_at) VALUES (?, ?, ?)",
                (symbol, average_price, now.isoformat()),
            )
            position_id = int(cursor.lastrowid)
        return Position(position_id, symbol, average_price, now)

    def clear_position(self, symbol: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM positions WHERE symbol=?", (symbol,))

    def position(self, symbol: str) -> Position | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
        if not row:
            return None
        return Position(row["id"], row["symbol"], row["average_price"], datetime.fromisoformat(row["updated_at"]))

    def positions(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM positions ORDER BY symbol").fetchall()
        return [dict(row) for row in rows]

    def fired_event_keys(self, strategy: str, symbol: str, day: date) -> set[str]:
        prefix = f"{strategy}|{symbol}|{day.isoformat()}|%"
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT event_key FROM signals
                WHERE strategy=? AND symbol=?
                  AND (unique_key LIKE ? OR event_key LIKE 'position:%')
                """,
                (strategy, symbol, prefix),
            ).fetchall()
        return {row["event_key"] for row in rows}

    def insert_signal(self, signal: Signal) -> bool:
        values = signal.as_dict()
        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO signals(
                    unique_key, strategy, event_key, kind, symbol, name, market,
                    timestamp, price, reference_price, threshold, message
                ) VALUES (
                    :unique_key, :strategy, :event_key, :kind, :symbol, :name, :market,
                    :timestamp, :price, :reference_price, :threshold, :message
                )
                """,
                values,
            )
        return cursor.rowcount == 1

    def pending_signals(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM signals WHERE notified=0 ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_notified(self, signal_id: int) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE signals SET notified=1, notification_error='' WHERE id=?", (signal_id,)
            )

    def mark_notification_error(self, signal_id: int, error: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE signals SET notification_error=? WHERE id=?", (error[:500], signal_id)
            )

    def signals(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM signals ORDER BY timestamp DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def save_leaders(self, leaders: list[Instrument], source_date: date) -> None:
        with self._connect() as db:
            db.executemany(
                """
                INSERT INTO leaders(source_date, market, symbol, name, turnover)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_date, market) DO UPDATE SET
                    symbol=excluded.symbol, name=excluded.name, turnover=excluded.turnover
                """,
                [
                    (source_date.isoformat(), item.market, item.symbol, item.name, item.turnover)
                    for item in leaders
                ],
            )

    def latest_previous_leaders(self, day: date) -> list[Instrument]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT l.* FROM leaders l
                JOIN (
                    SELECT market, MAX(source_date) AS source_date
                    FROM leaders WHERE source_date < ? GROUP BY market
                ) latest ON latest.market=l.market AND latest.source_date=l.source_date
                ORDER BY l.market
                """,
                (day.isoformat(),),
            ).fetchall()
        return [
            Instrument(
                symbol=row["symbol"],
                name=row["name"],
                market=row["market"],
                source_date=date.fromisoformat(row["source_date"]),
                turnover=row["turnover"],
            )
            for row in rows
        ]

    def leader_history(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM leaders ORDER BY source_date DESC, market LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]
