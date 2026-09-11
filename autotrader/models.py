"""도메인 모델: 시세 봉, 주문, 체결, 포지션."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class Side(enum.Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(enum.Enum):
    MARKET = "market"
    LIMIT = "limit"


@dataclass(frozen=True)
class Bar:
    """하나의 시세 봉(OHLCV)."""

    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class Order:
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("주문 수량은 1 이상이어야 한다")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("지정가 주문에는 limit_price가 필요하다")


@dataclass(frozen=True)
class Fill:
    """체결 내역."""

    symbol: str
    side: Side
    quantity: int
    price: float
    commission: float
    timestamp: datetime


@dataclass
class Position:
    symbol: str
    quantity: int = 0
    avg_price: float = 0.0

    def apply_fill(self, fill: Fill) -> float:
        """체결을 반영하고 실현 손익을 반환한다."""
        realized = 0.0
        if fill.side is Side.BUY:
            total_cost = self.avg_price * self.quantity + fill.price * fill.quantity
            self.quantity += fill.quantity
            self.avg_price = total_cost / self.quantity
        else:
            if fill.quantity > self.quantity:
                raise ValueError("보유 수량을 초과한 매도는 허용하지 않는다(공매도 미지원)")
            realized = (fill.price - self.avg_price) * fill.quantity
            self.quantity -= fill.quantity
            if self.quantity == 0:
                self.avg_price = 0.0
        return realized

    def market_value(self, price: float) -> float:
        return self.quantity * price


@dataclass
class Account:
    """현금과 포지션을 묶은 계좌 상태."""

    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0

    def position(self, symbol: str) -> Position:
        return self.positions.setdefault(symbol, Position(symbol))

    def equity(self, prices: dict[str, float]) -> float:
        value = self.cash
        for sym, pos in self.positions.items():
            if pos.quantity:
                value += pos.market_value(prices[sym])
        return value
