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
    # 초저가 잡주 배제 필터. None이면 시장별 기본값(미국 $5, 국내 1,000원)
    min_price: float | None = None


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
        self.last_error = ""
        self._fail_counts: dict[str, int] = {}
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
    def _min_price(self) -> float:
        if self.config.min_price is not None:
            return self.config.min_price
        return 5.0 if self.config.market == "us" else 1000.0

    @staticmethod
    def _row_price(row: dict) -> float:
        try:
            return abs(float(str(row.get("price", "")).replace(",", "").replace("+", "")))
        except ValueError:
            return 0.0

    def ensure_selection(self) -> list[str]:
        if self.symbols:
            return self.symbols
        rows = self.api.top_stocks(self.config.criteria, limit=10)
        min_price = self._min_price()

        def too_cheap(row: dict) -> bool:
            price = self._row_price(row)
            return 0 < price < min_price  # 가격을 모르는 행(0)은 배제하지 않는다

        eligible = [r for r in rows if r.get("code") and not too_cheap(r)]
        skipped = [r["code"] for r in rows if r.get("code") and too_cheap(r)]
        if skipped:
            logger.info("저가 필터로 제외(기준 %.2f): %s", min_price, ", ".join(skipped))
        codes = [r["code"] for r in eligible][: self.config.num_symbols]
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

    # ── 실계좌 동기화 ─────────────────────────────────────
    def reconcile_positions(self) -> None:
        """실제 계좌 잔고와 내부 장부를 맞춘다 (미체결 매수로 생긴 유령 포지션 정리).

        미국주식(us_balances 지원 API)에서만 동작한다.
        """
        if self.config.market != "us" or not self.symbols:
            return
        if not hasattr(self.api, "us_balances"):
            return
        try:
            balances = self.api.us_balances()
        except Exception as e:
            logger.warning("잔고 동기화 실패(다음 주기에 재시도): %s", e)
            return
        for code in self.symbols:
            pos = self.account.position(code)
            actual = balances.get(code, {}).get("qty", 0)
            if actual == pos.quantity:
                continue
            ref_price = pos.avg_price or self.last_prices.get(code, 0.0)
            diff = pos.quantity - actual  # 양수 = 장부가 과대(미체결 매수 등)
            self.account.cash += diff * ref_price
            logger.info("잔고 동기화: %s %d주 → 실제 %d주 (현금 %+.2f 보정)",
                        code, pos.quantity, actual, diff * ref_price)
            pos.quantity = actual
            if actual == 0:
                pos.avg_price = 0.0
            elif pos.avg_price <= 0:
                # 외부 체결로 늘어난 수량에 단가 0이 남으면 매도 시
                # 전액이 이익으로 잡히는 오류가 생기므로 시세로 채운다
                pos.avg_price = ref_price

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
        for code in list(self.symbols):
            unit = self.units[code]
            try:
                price = self.api.current_price(code)
            except Exception as e:  # 한 종목의 오류가 전체를 멈추지 않게 한다
                self._fail_counts[code] = self._fail_counts.get(code, 0) + 1
                self.last_error = f"{code} 시세 오류: {e}"
                logger.warning("%s 시세 조회 실패(%d회): %s",
                               code, self._fail_counts[code], e)
                if self._fail_counts[code] >= 3:
                    self.symbols.remove(code)
                    logger.warning("%s 연속 실패로 감시에서 제외", code)
                continue
            self._fail_counts[code] = 0
            self.last_prices[code] = price
            bar = unit.aggregator.add_tick(now, price)
            if bar is None:
                continue
            # 주문 한도는 신규 매수만 막는다. 매도·손절은 항상 나간다.
            allow_buy = self.orders_today < self.config.max_orders_per_day
            try:
                fill = unit.engine.process_bar(bar, allow_buy=allow_buy)
            except Exception as e:
                self.last_error = f"{code} 주문 오류: {e}"
                logger.warning("%s 주문 처리 실패: %s", code, e)
                # 애초에 매수가 불가능한 종목(예: 571242)은 즉시 감시 제외
                pos = self.account.positions.get(code)
                if "불가" in str(e) and (pos is None or pos.quantity == 0):
                    self.symbols.remove(code)
                    logger.warning("%s 매수 불가 종목으로 감시에서 제외", code)
                continue
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
        last_reconcile = 0.0
        try:
            while self._running:
                try:
                    self.step()
                    # 1분마다 실계좌 잔고와 동기화해 유령 포지션을 정리한다
                    if time.monotonic() - last_reconcile >= 60:
                        self.reconcile_positions()
                        last_reconcile = time.monotonic()
                except Exception as e:  # 루프는 어떤 오류에도 살아남는다
                    self.last_error = f"매매 루프 오류: {e}"
                    logger.exception("매매 루프 오류, 계속 진행: %s", e)
                self.sleep(self.config.poll_interval)
        except KeyboardInterrupt:
            logger.info("사용자 중단")
        finally:
            logger.info("종료. 오늘 주문 %d건, 실현손익 %.0f",
                        self.orders_today, self.account.realized_pnl)
