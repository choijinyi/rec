"""브로커 어댑터.

PaperBroker는 수수료·슬리피지를 반영한 모의 체결기다.
실계좌 연동(예: 한국투자증권 KIS Developers OpenAPI)은 Broker 프로토콜을
구현한 어댑터를 추가해 엔진에 주입하면 된다. 이 저장소는 안전을 위해
실주문 코드를 포함하지 않는다.
"""

from __future__ import annotations

from typing import Protocol

from .models import Bar, Fill, Order, OrderType, Side


class Broker(Protocol):
    def execute(self, order: Order, bar: Bar) -> Fill | None:
        """주문을 집행하고 체결되면 Fill을, 미체결이면 None을 반환한다."""
        ...


class PaperBroker:
    """봉의 종가 기준으로 체결하는 모의 브로커."""

    def __init__(self, commission_rate: float = 0.00015, slippage_rate: float = 0.0005):
        self.commission_rate = commission_rate
        self.slippage_rate = slippage_rate

    def execute(self, order: Order, bar: Bar) -> Fill | None:
        base_price = bar.close
        if order.order_type is OrderType.LIMIT:
            assert order.limit_price is not None
            if order.side is Side.BUY and bar.low > order.limit_price:
                return None
            if order.side is Side.SELL and bar.high < order.limit_price:
                return None
            base_price = order.limit_price

        slip = 1 + self.slippage_rate if order.side is Side.BUY else 1 - self.slippage_rate
        price = round(base_price * slip, 2)
        commission = round(price * order.quantity * self.commission_rate, 2)
        return Fill(
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=price,
            commission=commission,
            timestamp=bar.timestamp,
        )
