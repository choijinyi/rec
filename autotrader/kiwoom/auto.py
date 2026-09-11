"""자동 선정 매매: 당일 순위 상위에서 종목을 골라 동시 매매한다.

장이 열리면 거래량(또는 등락률) 상위에서 상위 N종목을 자동 선정하고,
종목마다 독립된 전략 인스턴스와 봉 집계기를 붙인다. 계좌·리스크 한도
(종목당 비중, 1회 매수 비중, 낙폭 중단)와 일일 주문 한도는 전 종목이
공유하므로 총 노출이 통제된다.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..bars import BarAggregator
from ..engine import TradingEngine
from ..models import Account, Fill
from ..risk import RiskConfig, RiskManager
from ..strategy import Strategy
from .live import KiwoomBrokerAdapter, in_market_hours, in_us_market_hours

logger = logging.getLogger(__name__)


class RankedMarketAPI(Protocol):
    def current_price(self, code: str) -> float: ...
    def send_market_order(self, account_no: str, code: str, side: str,
                          quantity: int) -> None: ...
    def top_stocks(self, criteria: str = "volume", limit: int = 10) -> list[dict]: ...


@dataclass
class AutoConfig:
    market: str = "kr"            # kr / us
    criteria: str = "volume"      # volume(거래량) / change(등락률)
    num_symbols: int = 3          # 자동 선정 종목 수
    bar_interval: int = 60
    poll_interval: float = 5.0    # 종목 수만큼 조회하므로 단일 모드보다 여유 있게
    initial_cash: float = 10_000_000
    max_orders_per_day: int = 20  # 전 종목 합산 한도
    allow_real: bool = False


@dataclass
class _Unit:
    aggregator: BarAggregator
    engine: TradingEngine


class MultiLiveTrader:
    """순위 상위 자동 선정 + 다중 종목 실시간 매매 루프."""

    def __init__(self, api: RankedMarketAPI,
                 strategy_factory: Callable[[], Strategy],
                 config: AutoConfig,
                 risk: RiskManager | None = None,
                 is_simulation: bool = True,
                 clock: Callable[[], dt.datetime] = dt.datetime.now,
                 utc_clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
                 sleep: Callable[[float], None] = time.sleep):
        if not is_simulation and not config.allow_real:
            raise PermissionError(
                "실전투자 접속이 감지되었다. 모의투자로 검증하거나 allow_real을 "
                "명시해야 한다."
            )
        if config.num_symbols < 1:
            raise ValueError("num_symbols는 1 이상이어야 한다")
        self.api = api
        self.config = config
        self.strategy_factory = strategy_factory
        self.risk = risk or RiskManager(RiskConfig(), initial_equity=config.initial_cash)
        self.clock = clock
        self.utc_clock = utc_clock
        self.sleep = sleep
        self.account = Account(cash=config.initial_cash)
        self.last_prices: dict[str, float] = {}
        self.broker = KiwoomBrokerAdapter(api, account_no="")
        self.units: dict[str, _Unit] = {}
        self.symbols: list[str] = []
        self.orders_today = 0
        self._running = False

    # ── 상태 조회 (UI 공용 인터페이스) ─────────────────────
    def equity(self) -> float:
        return self.account.equity(self.last_prices)

    @property
    def fills(self) -> list[Fill]:
        merged: list[Fill] = []
        for unit in self.units.values():
            merged.extend(unit.engine.fills)
        return sorted(merged, key=lambda f: f.timestamp)

    def stop(self) -> None:
        self._running = False

    # ── 종목 선정 ──────────────────────────────────────────
    def ensure_selection(self) -> list[str]:
        if self.symbols:
            return self.symbols
        rows = self.api.top_stocks(self.config.criteria, limit=10)
        codes = [r["code"] for r in rows if r.get("code")][: self.config.num_symbols]
        if not codes:
            return []
        for code in codes:
            self.units[code] = _Unit(
                aggregator=BarAggregator(code, self.config.bar_interval),
                engine=TradingEngine(
                    strategy=self.strategy_factory(),
                    broker=self.broker,
                    risk=self.risk,
                    account=self.account,
                    last_prices=self.last_prices,
                ),
            )
        self.symbols = codes
        logger.info("자동 선정 종목(%s 기준): %s",
                    self.config.criteria, ", ".join(codes))
        return codes

    # ── 매매 루프 ──────────────────────────────────────────
    def _market_open(self) -> bool:
        if self.config.market == "us":
            return in_us_market_hours(self.utc_clock())
        return in_market_hours(self.clock())

    def step(self) -> list[Fill]:
        if not self._market_open():
            return []
        if not self.ensure_selection():
            return []
        now = self.clock()
        fills: list[Fill] = []
        for code in self.symbols:
            unit = self.units[code]
            price = self.api.current_price(code)
            self.last_prices[code] = price
            bar = unit.aggregator.add_tick(now, price)
            if bar is None:
                continue
            if self.orders_today >= self.config.max_orders_per_day:
                unit.engine.strategy.on_bar(bar)  # 지표는 계속 갱신
                continue
            fill = unit.engine.process_bar(bar)
            if fill is not None:
                self.orders_today += 1
                fills.append(fill)
                logger.info("체결 기록: %s %s %d주 @ %.2f (평가액 %.0f)",
                            fill.symbol, fill.side.value, fill.quantity,
                            fill.price, self.equity())
        return fills

    def run(self) -> None:
        self._running = True
        logger.info("자동 선정 매매 시작: %s / %s 기준 상위 %d종목",
                    "미국" if self.config.market == "us" else "국내",
                    self.config.criteria, self.config.num_symbols)
        try:
            while self._running:
                self.step()
                self.sleep(self.config.poll_interval)
        except KeyboardInterrupt:
            logger.info("사용자 중단")
        finally:
            logger.info("종료. 오늘 주문 %d건, 실현손익 %.0f",
                        self.orders_today, self.account.realized_pnl)
