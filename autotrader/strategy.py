"""매매 전략.

전략은 봉을 하나씩 받아 Signal(BUY/SELL/HOLD)을 돌려주는 순수 로직이며,
주문 수량·리스크는 엔진과 RiskManager가 책임진다.
"""

from __future__ import annotations

import enum
from collections import deque

from .models import Bar


class Signal(enum.Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class Strategy:
    name = "base"

    def on_bar(self, bar: Bar) -> Signal:  # pragma: no cover - 인터페이스
        raise NotImplementedError


class SMACrossoverStrategy(Strategy):
    """단기 이동평균이 장기 이동평균을 상향 돌파하면 매수, 하향 돌파하면 매도."""

    name = "sma_crossover"

    def __init__(self, short_window: int = 5, long_window: int = 20):
        if short_window >= long_window:
            raise ValueError("short_window는 long_window보다 작아야 한다")
        self.short_window = short_window
        self.long_window = long_window
        self._closes: deque[float] = deque(maxlen=long_window)
        self._prev_diff: float | None = None

    def on_bar(self, bar: Bar) -> Signal:
        self._closes.append(bar.close)
        if len(self._closes) < self.long_window:
            return Signal.HOLD
        closes = list(self._closes)
        short_ma = sum(closes[-self.short_window:]) / self.short_window
        long_ma = sum(closes) / self.long_window
        diff = short_ma - long_ma
        signal = Signal.HOLD
        if self._prev_diff is not None:
            if self._prev_diff <= 0 < diff:
                signal = Signal.BUY
            elif self._prev_diff >= 0 > diff:
                signal = Signal.SELL
        self._prev_diff = diff
        return signal


class RSIReversionStrategy(Strategy):
    """RSI가 과매도(기본 30) 아래로 내려가면 매수, 과매수(기본 70) 위로 올라가면 매도."""

    name = "rsi_reversion"

    def __init__(self, period: int = 14, oversold: float = 30.0, overbought: float = 70.0):
        self.period = period
        self.oversold = oversold
        self.overbought = overbought
        self._closes: deque[float] = deque(maxlen=period + 1)

    def _rsi(self) -> float | None:
        if len(self._closes) < self.period + 1:
            return None
        closes = list(self._closes)
        gains = losses = 0.0
        for prev, cur in zip(closes, closes[1:]):
            change = cur - prev
            if change > 0:
                gains += change
            else:
                losses -= change
        if losses == 0:
            return 100.0
        rs = (gains / self.period) / (losses / self.period)
        return 100.0 - 100.0 / (1.0 + rs)

    def on_bar(self, bar: Bar) -> Signal:
        self._closes.append(bar.close)
        rsi = self._rsi()
        if rsi is None:
            return Signal.HOLD
        if rsi < self.oversold:
            return Signal.BUY
        if rsi > self.overbought:
            return Signal.SELL
        return Signal.HOLD


STRATEGIES: dict[str, type[Strategy]] = {
    SMACrossoverStrategy.name: SMACrossoverStrategy,
    RSIReversionStrategy.name: RSIReversionStrategy,
}
