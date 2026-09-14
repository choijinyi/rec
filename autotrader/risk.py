"""리스크 관리: 포지션 사이징과 손실 한도.

전략 신호가 곧바로 주문이 되지 않도록, 모든 주문은 RiskManager를 거친다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Account, Side


@dataclass
class RiskConfig:
    # 종목당 최대 투자 비중 (계좌 평가액 대비)
    max_position_pct: float = 0.2
    # 계좌 평가액이 초기 자본 대비 이 비율 아래로 떨어지면 신규 매수 중단
    max_drawdown_pct: float = 0.15
    # 한 번의 매수에 쓸 현금 비중
    order_cash_pct: float = 0.1
    # 손절: 평균 매수단가 대비 이만큼 하락하면 전량 청산 (0이면 비활성)
    stop_loss_pct: float = 0.03
    # 매도 후 같은 종목 재매수까지 기다릴 봉 수 (횡보장 과매매 완화, 0이면 끔)
    reentry_cooldown_bars: int = 5
    # 매수 후 전략 매도까지 최소 보유 봉 수 (손절은 예외, 0이면 끔)
    min_hold_bars: int = 3


class RiskManager:
    def __init__(self, config: RiskConfig, initial_equity: float):
        self.config = config
        self.initial_equity = initial_equity

    def drawdown_exceeded(self, equity: float) -> bool:
        return equity < self.initial_equity * (1.0 - self.config.max_drawdown_pct)

    def should_stop_out(self, avg_price: float, quantity: int, price: float) -> bool:
        """손절 조건: 보유 중이고 평균단가 대비 stop_loss_pct 이상 하락."""
        if self.config.stop_loss_pct <= 0 or quantity <= 0 or avg_price <= 0:
            return False
        return price <= avg_price * (1.0 - self.config.stop_loss_pct)

    def size_order(self, account: Account, symbol: str, side: Side, price: float,
                   equity: float) -> int:
        """리스크 한도 안에서 허용되는 주문 수량을 계산한다. 0이면 주문 금지."""
        if price <= 0:
            return 0
        pos = account.position(symbol)
        if side is Side.SELL:
            return pos.quantity  # 전량 청산

        if self.drawdown_exceeded(equity):
            return 0

        budget = min(account.cash, equity * self.config.order_cash_pct)
        qty = int(budget // price)
        if qty <= 0:
            return 0

        # 종목당 최대 비중 제한
        max_value = equity * self.config.max_position_pct
        current_value = pos.market_value(price)
        room = max_value - current_value
        qty = min(qty, int(room // price)) if room > 0 else 0
        return max(qty, 0)
