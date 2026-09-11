"""키움 API 기반 실시간(폴링) 자동매매 루프.

구조: 현재가 폴링 → BarAggregator로 봉 완성 → TradingEngine(전략+리스크)
→ KiwoomBrokerAdapter가 시장가 주문 전송.

백테스트와 동일한 TradingEngine.process_bar 경로를 쓰므로,
백테스트로 검증한 전략이 그대로 운용된다.

안전장치:
- 모의투자 서버가 아니면 allow_real=True를 명시하지 않는 한 실행 거부
- 장중(09:00~15:30 KST)에만 매매
- 하루 최대 주문 횟수 제한
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from ..bars import BarAggregator
from ..engine import TradingEngine
from ..models import Account, Bar, Fill, Order, Side
from ..risk import RiskManager
from ..strategy import Strategy

logger = logging.getLogger(__name__)

MARKET_OPEN = dt.time(9, 0)
MARKET_CLOSE = dt.time(15, 30)
US_OPEN = dt.time(9, 30)   # 미국 동부시간 기준
US_CLOSE = dt.time(16, 0)


class MarketAPI(Protocol):
    """LiveTrader가 필요로 하는 최소 API 표면. 테스트에서는 가짜 구현을 쓴다."""

    def current_price(self, code: str) -> float: ...
    def send_market_order(self, account_no: str, code: str, side: str,
                          quantity: int) -> None: ...


@dataclass
class LiveConfig:
    account_no: str
    code: str                     # 종목코드 (국내: 005930, 미국: AAPL)
    bar_interval: int = 60        # 봉 주기(초)
    poll_interval: float = 2.0    # 시세 폴링 주기(초)
    initial_cash: float = 10_000_000
    max_orders_per_day: int = 20
    allow_real: bool = False      # True가 아니면 실전 서버에서 실행 거부
    market: str = "kr"            # kr = 국내(09:00~15:30 KST), us = 미국(09:30~16:00 ET)


class KiwoomBrokerAdapter:
    """Broker 프로토콜 구현: 시장가 주문을 전송하고 봉 종가 기준 체결로 기록한다.

    1차 버전은 체결 통보를 기다리지 않고 즉시 체결로 가정한다(모의투자
    시장가 기준 근사). 실체결가 반영은 OnReceiveChejanData 연동으로 확장한다.
    """

    def __init__(self, api: MarketAPI, account_no: str,
                 commission_rate: float = 0.00015):
        self.api = api
        self.account_no = account_no
        self.commission_rate = commission_rate

    def execute(self, order: Order, bar: Bar) -> Fill | None:
        side = "buy" if order.side is Side.BUY else "sell"
        self.api.send_market_order(self.account_no, order.symbol, side, order.quantity)
        price = bar.close
        return Fill(
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=price,
            commission=round(price * order.quantity * self.commission_rate, 2),
            timestamp=bar.timestamp,
        )


def in_market_hours(now: dt.datetime) -> bool:
    if now.weekday() >= 5:  # 토·일
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def _us_dst_active(utc: dt.datetime) -> bool:
    """미국 서머타임(EDT) 여부: 3월 둘째 일요일 ~ 11월 첫째 일요일."""
    def nth_sunday(month: int, n: int) -> dt.datetime:
        first = dt.datetime(utc.year, month, 1, tzinfo=dt.timezone.utc)
        offset = (6 - first.weekday()) % 7
        return first + dt.timedelta(days=offset + 7 * (n - 1))

    start = nth_sunday(3, 2).replace(hour=7)   # EST 02:00 = 07:00 UTC
    end = nth_sunday(11, 1).replace(hour=6)    # EDT 02:00 = 06:00 UTC
    return start <= utc < end


def in_us_market_hours(utc_now: dt.datetime) -> bool:
    """미국 정규장(09:30~16:00 ET) 여부. utc_now는 timezone-aware UTC."""
    offset = -4 if _us_dst_active(utc_now) else -5
    et = utc_now + dt.timedelta(hours=offset)
    if et.weekday() >= 5:
        return False
    return US_OPEN <= et.time() <= US_CLOSE


class LiveTrader:
    def __init__(self, api: MarketAPI, strategy: Strategy, config: LiveConfig,
                 risk: RiskManager | None = None,
                 is_simulation: bool = True,
                 clock: Callable[[], dt.datetime] = dt.datetime.now,
                 utc_clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
                 sleep: Callable[[float], None] = time.sleep):
        if not is_simulation and not config.allow_real:
            raise PermissionError(
                "실전투자 서버 접속이 감지되었다. 모의투자 계좌로 로그인하거나, "
                "충분한 검증 후 allow_real=True(--allow-real)를 명시해야 한다."
            )
        self.api = api
        self.config = config
        self.clock = clock
        self.utc_clock = utc_clock
        self.sleep = sleep
        self.aggregator = BarAggregator(config.code, config.bar_interval)
        broker = KiwoomBrokerAdapter(api, config.account_no)
        self.engine = TradingEngine(
            strategy=strategy,
            broker=broker,
            risk=risk or _default_risk(config),
            account=Account(cash=config.initial_cash),
        )
        self.orders_today = 0
        self._running = False

    def stop(self) -> None:
        self._running = False

    def step(self) -> Fill | None:
        """한 번의 폴링 사이클. 봉이 완성되면 전략을 평가하고 주문까지 처리한다."""
        now = self.clock()
        if self.config.market == "us":
            if not in_us_market_hours(self.utc_clock()):
                return None
        elif not in_market_hours(now):
            return None
        price = self.api.current_price(self.config.code)
        completed = self.aggregator.add_tick(now, price)
        if completed is None:
            return None
        if self.orders_today >= self.config.max_orders_per_day:
            logger.warning("일일 주문 한도(%d) 도달, 신호 무시", self.config.max_orders_per_day)
            self.engine.strategy.on_bar(completed)  # 지표는 계속 갱신
            return None
        fill = self.engine.process_bar(completed)
        if fill is not None:
            self.orders_today += 1
            logger.info("체결 기록: %s %s %d주 @ %.0f (평가액 %.0f)",
                        fill.symbol, fill.side.value, fill.quantity, fill.price,
                        self.engine.equity())
        return fill

    def run(self) -> None:
        """장중 폴링 루프. Ctrl+C 로 중단한다."""
        self._running = True
        logger.info("자동매매 시작: %s, 봉 %d초, 폴링 %.1f초",
                    self.config.code, self.config.bar_interval,
                    self.config.poll_interval)
        try:
            while self._running:
                self.step()
                self.sleep(self.config.poll_interval)
        except KeyboardInterrupt:
            logger.info("사용자 중단")
        finally:
            logger.info("종료. 오늘 주문 %d건, 실현손익 %.0f",
                        self.orders_today, self.engine.account.realized_pnl)


def _default_risk(config: LiveConfig) -> RiskManager:
    from ..risk import RiskConfig
    return RiskManager(RiskConfig(), initial_equity=config.initial_cash)
