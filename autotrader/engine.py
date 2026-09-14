"""매매 엔진: 데이터 → 전략 신호 → 리스크 검증 → 브로커 체결.

백테스트와 (미래의) 실시간 루프가 같은 경로를 쓰도록 설계했다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from .broker import Broker
from .models import Account, Bar, Fill, Order, Side
from .risk import RiskManager
from .strategy import Signal, Strategy


@dataclass
class BacktestResult:
    initial_cash: float
    final_equity: float
    fills: list[Fill] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)

    @property
    def total_return_pct(self) -> float:
        return (self.final_equity / self.initial_cash - 1.0) * 100.0

    @property
    def max_drawdown_pct(self) -> float:
        peak = -math.inf
        max_dd = 0.0
        for eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak)
        return max_dd * 100.0

    @property
    def num_trades(self) -> int:
        return len(self.fills)

    def summary(self) -> str:
        return (
            f"초기 자본      : {self.initial_cash:,.0f}\n"
            f"최종 평가액    : {self.final_equity:,.0f}\n"
            f"총 수익률      : {self.total_return_pct:+.2f}%\n"
            f"최대 낙폭(MDD) : {self.max_drawdown_pct:.2f}%\n"
            f"체결 횟수      : {self.num_trades}"
        )


class TradingEngine:
    def __init__(self, strategy: Strategy, broker: Broker, risk: RiskManager,
                 account: Account, last_prices: dict[str, float] | None = None):
        self.strategy = strategy
        self.broker = broker
        self.risk = risk
        self.account = account
        self.fills: list[Fill] = []
        # 다중 종목 운용 시 여러 엔진이 가격 사전을 공유해 계좌 평가액을 함께 계산한다
        self._last_prices: dict[str, float] = (
            last_prices if last_prices is not None else {})
        self._bars_since_exit: dict[str, int] = {}   # 재매수 쿨다운용
        self._bars_since_entry: dict[str, int] = {}  # 최소 보유 시간용

    def equity(self) -> float:
        return self.account.equity(self._last_prices)

    def process_bar(self, bar: Bar, allow_buy: bool = True) -> Fill | None:
        """봉 하나를 처리한다. 실시간 루프에서도 이 메서드를 그대로 호출한다.

        allow_buy=False면 신규 매수만 막고 매도·손절은 그대로 수행한다
        (일일 주문 한도 등은 청산을 막아서는 안 된다).
        """
        self._last_prices[bar.symbol] = bar.close
        if bar.symbol in self._bars_since_exit:
            self._bars_since_exit[bar.symbol] += 1
        if bar.symbol in self._bars_since_entry:
            self._bars_since_entry[bar.symbol] += 1

        # 손절이 전략 신호보다 우선한다. 지표는 계속 갱신한다.
        pos = self.account.position(bar.symbol)
        if self.risk.should_stop_out(pos.avg_price, pos.quantity, bar.close):
            self.strategy.on_bar(bar)
            fill = self.broker.execute(
                Order(bar.symbol, Side.SELL, pos.quantity), bar)
            if fill is not None:
                self._apply_fill(fill)
                self._bars_since_exit[bar.symbol] = 0
            return fill

        signal = self.strategy.on_bar(bar)
        if signal is Signal.HOLD:
            return None

        side = Side.BUY if signal is Signal.BUY else Side.SELL
        if side is Side.BUY:
            if not allow_buy:
                return None
            cooldown = self.risk.config.reentry_cooldown_bars
            since_exit = self._bars_since_exit.get(bar.symbol)
            if cooldown > 0 and since_exit is not None and since_exit <= cooldown:
                return None  # 매도 후 cooldown개 봉 동안 재매수 금지 (과매매 완화)
        else:
            # 매수 직후의 전략 매도(1분 왕복 매매)를 막는다. 손절은 위에서
            # 이미 처리되므로 이 제한의 영향을 받지 않는다.
            hold = self.risk.config.min_hold_bars
            since_entry = self._bars_since_entry.get(bar.symbol)
            if hold > 0 and since_entry is not None and since_entry <= hold:
                return None
        qty = self.risk.size_order(self.account, bar.symbol, side, bar.close, self.equity())
        if qty <= 0:
            return None

        fill = self.broker.execute(Order(bar.symbol, side, qty), bar)
        if fill is None:
            return None
        self._apply_fill(fill)
        if side is Side.SELL:
            self._bars_since_exit[bar.symbol] = 0
        else:
            self._bars_since_entry[bar.symbol] = 0
        return fill

    def _apply_fill(self, fill: Fill) -> None:
        pos = self.account.position(fill.symbol)
        realized = pos.apply_fill(fill)
        cost = fill.price * fill.quantity
        if fill.side is Side.BUY:
            self.account.cash -= cost
        else:
            self.account.cash += cost
        self.account.cash -= fill.commission
        self.account.realized_pnl += realized - fill.commission
        self.fills.append(fill)


def run_backtest(strategy: Strategy, broker: Broker, risk: RiskManager,
                 bars: Iterable[Bar], initial_cash: float) -> BacktestResult:
    account = Account(cash=initial_cash)
    engine = TradingEngine(strategy, broker, risk, account)
    equity_curve: list[float] = []
    for bar in bars:
        engine.process_bar(bar)
        equity_curve.append(engine.equity())
    final_equity = equity_curve[-1] if equity_curve else initial_cash
    return BacktestResult(
        initial_cash=initial_cash,
        final_equity=final_equity,
        fills=engine.fills,
        equity_curve=equity_curve,
    )
