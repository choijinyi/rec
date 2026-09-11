"""실시간 시세(체결가 폴링)를 봉(Bar)으로 집계하는 모듈.

키움 API에서 주기적으로 받아온 현재가를 interval 초 단위 봉으로 묶어,
백테스트와 동일한 Bar 객체를 전략 엔진에 공급한다.
"""

from __future__ import annotations

from datetime import datetime

from .models import Bar


class BarAggregator:
    def __init__(self, symbol: str, interval_seconds: int = 60):
        if interval_seconds <= 0:
            raise ValueError("interval_seconds는 1 이상이어야 한다")
        self.symbol = symbol
        self.interval = interval_seconds
        self._bucket: int | None = None
        self._open = self._high = self._low = self._close = 0.0
        self._volume = 0
        self._bucket_start: datetime | None = None

    def _bucket_of(self, ts: datetime) -> int:
        return int(ts.timestamp()) // self.interval

    def add_tick(self, ts: datetime, price: float, volume: int = 0) -> Bar | None:
        """틱을 추가한다. 봉이 완성되는 시점이면 완성된 봉을 반환한다."""
        bucket = self._bucket_of(ts)
        completed: Bar | None = None

        if self._bucket is None:
            self._start_bucket(bucket, ts, price, volume)
            return None

        if bucket != self._bucket:
            completed = self._finish_bar()
            self._start_bucket(bucket, ts, price, volume)
            return completed

        self._high = max(self._high, price)
        self._low = min(self._low, price)
        self._close = price
        self._volume += volume
        return None

    def _start_bucket(self, bucket: int, ts: datetime, price: float, volume: int) -> None:
        self._bucket = bucket
        self._bucket_start = ts
        self._open = self._high = self._low = self._close = price
        self._volume = volume

    def _finish_bar(self) -> Bar:
        assert self._bucket_start is not None
        return Bar(
            symbol=self.symbol,
            timestamp=self._bucket_start,
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
            volume=self._volume,
        )

    def flush(self) -> Bar | None:
        """진행 중인 봉을 강제로 완성한다(장 마감 등)."""
        if self._bucket is None:
            return None
        bar = self._finish_bar()
        self._bucket = None
        return bar
