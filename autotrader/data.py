"""시세 데이터 피드.

CSVDataFeed는 `timestamp,open,high,low,close,volume` 형식의 CSV를 읽고,
generate_synthetic_bars는 데모·테스트용 가상 시세를 만든다.
실전에서는 증권사 API 시세를 같은 인터페이스(Iterable[Bar])로 감싸면 된다.
"""

from __future__ import annotations

import csv
import math
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator

from .models import Bar


class CSVDataFeed:
    def __init__(self, symbol: str, path: str | Path):
        self.symbol = symbol
        self.path = Path(path)

    def __iter__(self) -> Iterator[Bar]:
        with self.path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                yield Bar(
                    symbol=self.symbol,
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row["volume"]),
                )


def generate_synthetic_bars(
    symbol: str,
    days: int = 250,
    start_price: float = 70_000.0,
    seed: int | None = 42,
    start: datetime | None = None,
) -> list[Bar]:
    """기하 브라운 운동에 완만한 사이클을 더한 가상 일봉을 생성한다."""
    rng = random.Random(seed)
    ts = start or datetime(2025, 1, 2, 9, 0)
    price = start_price
    bars: list[Bar] = []
    for i in range(days):
        drift = 0.0003 + 0.002 * math.sin(i / 20.0)
        ret = rng.gauss(drift, 0.015)
        open_p = price
        close_p = max(1.0, price * math.exp(ret))
        high_p = max(open_p, close_p) * (1 + abs(rng.gauss(0, 0.004)))
        low_p = min(open_p, close_p) * (1 - abs(rng.gauss(0, 0.004)))
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=ts,
                open=round(open_p, 2),
                high=round(high_p, 2),
                low=round(low_p, 2),
                close=round(close_p, 2),
                volume=rng.randint(100_000, 2_000_000),
            )
        )
        price = close_p
        ts += timedelta(days=1)
    return bars


def write_bars_csv(bars: Iterable[Bar], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for b in bars:
            writer.writerow([b.timestamp.isoformat(), b.open, b.high, b.low, b.close, b.volume])
