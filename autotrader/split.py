"""분할매매 전용 자동매매.

사용자가 정한 종목·기준 가격·분할 %·분할 횟수·총 투입 금액을 받아,
기준 가격에서부터 분할 %만큼 내려갈 때마다 자동 분할매수, 기준 가격에서
분할 %만큼 올라갈 때마다 자동 분할매도를 수행한다.

- 1차 매수가 = 기준 가격, i차 매수가 = 기준가 × (1 - 분할% × (i-1))
- j차 매도가 = 기준가 × (1 + 분할% × j)
- 각 매수는 (총 투입 금액 ÷ 매수 분할 횟수)만큼, 각 매도는 보유 수량을
  남은 매도 횟수로 나눈 만큼(마지막 매도는 전량) 집행한다.
- 각 분할 레벨은 1회만 집행한다. 가격이 여러 레벨을 한 번에 지나가면
  해당 레벨들을 모두 집행한다(그 몫들을 더 유리한 가격에 사는 셈이다).
- 모든 매수·매도 레벨이 끝나고 보유가 0이 되면 계획 완료로 자동 종료한다.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

logger = logging.getLogger(__name__)

KR_OPEN = dt.time(9, 0)
KR_CLOSE = dt.time(15, 30)
US_OPEN = dt.time(9, 30)   # 미국 동부시간 기준
US_CLOSE = dt.time(16, 0)


class MarketAPI(Protocol):
    def current_price(self, code: str) -> float: ...
    def send_market_order(self, account_no: str, code: str, side: str,
                          quantity: int) -> None: ...


# ── 장 시간 판정 ──────────────────────────────────────────
def in_kr_market_hours(now: dt.datetime) -> bool:
    if now.weekday() >= 5:  # 토·일
        return False
    return KR_OPEN <= now.time() <= KR_CLOSE


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


def in_us_day_session(utc_now: dt.datetime) -> bool:
    """키움 미국주식 주간거래(블루오션 ATS) 시간 여부.

    한국시간 10:00~18:00, 미국 서머타임 기간에는 09:00~17:00.
    """
    kst = utc_now + dt.timedelta(hours=9)
    if kst.weekday() >= 5:
        return False
    if _us_dst_active(utc_now):
        return dt.time(9, 0) <= kst.time() <= dt.time(17, 0)
    return dt.time(10, 0) <= kst.time() <= dt.time(18, 0)


# ── 설정과 상태 ───────────────────────────────────────────
@dataclass
class SplitConfig:
    code: str                     # 종목코드 (국내: 005930, 미국: AAPL)
    base_price: float             # 기준 가격
    step_pct: float               # 분할 간격(%)
    buy_splits: int = 3           # 매수 분할 횟수
    sell_splits: int = 3          # 매도 분할 횟수
    total_cash: float = 1_000_000  # 매수에 쓸 총 금액
    market: str = "kr"            # kr / us
    poll_interval: float = 3.0    # 시세 폴링 주기(초)
    allow_real: bool = False      # True가 아니면 실전 서버에서 실행 거부
    us_day_session: bool = True   # 미국주식을 키움 주간거래 시간에도 매매


@dataclass
class Level:
    seq: int                      # 1차, 2차, ...
    side: str                     # buy / sell
    price: float                  # 발동 가격
    done: bool = False
    note: str = ""                # 체결 내용 또는 건너뛴 이유


@dataclass
class Fill:
    side: str
    quantity: int
    price: float
    timestamp: dt.datetime = field(default_factory=dt.datetime.now)


class SplitTrader:
    """기준 가격 기반 분할매수·분할매도 자동 집행 루프."""

    def __init__(self, api: MarketAPI, config: SplitConfig,
                 is_simulation: bool = True,
                 clock: Callable[[], dt.datetime] = dt.datetime.now,
                 utc_clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
                 sleep: Callable[[float], None] = time.sleep):
        if not is_simulation and not config.allow_real:
            raise PermissionError(
                "실전투자 접속이 감지되었다. 모의투자로 검증하거나 allow_real을 "
                "명시해야 한다."
            )
        if not config.code:
            raise ValueError("종목코드가 비어 있다")
        if config.base_price <= 0:
            raise ValueError("기준 가격은 0보다 커야 한다")
        if not 0 < config.step_pct < 100:
            raise ValueError("분할 %는 0보다 크고 100보다 작아야 한다")
        if config.buy_splits < 1 or config.sell_splits < 1:
            raise ValueError("분할 횟수는 1 이상이어야 한다")
        if config.total_cash <= 0:
            raise ValueError("총 투입 금액은 0보다 커야 한다")
        self.api = api
        self.config = config
        self.clock = clock
        self.utc_clock = utc_clock
        self.sleep = sleep

        step = config.step_pct / 100.0
        self.buy_levels = [
            Level(i + 1, "buy", config.base_price * (1 - step * i))
            for i in range(config.buy_splits)
        ]
        self.sell_levels = [
            Level(j, "sell", config.base_price * (1 + step * j))
            for j in range(1, config.sell_splits + 1)
        ]
        self.per_buy_cash = config.total_cash / config.buy_splits

        self.cash = config.total_cash
        self.position_qty = 0
        self.avg_price = 0.0
        self.realized_pnl = 0.0
        self.fills: list[Fill] = []
        self.events: list[str] = []
        self.last_price = 0.0
        self.last_error = ""
        self.completed = False
        self._running = False

    # ── 상태 조회 (UI 공용) ────────────────────────────────
    def equity(self) -> float:
        return self.cash + self.position_qty * (self.last_price or self.avg_price)

    @property
    def levels(self) -> list[Level]:
        return self.buy_levels + self.sell_levels

    def stop(self) -> None:
        self._running = False

    def _log(self, message: str) -> None:
        stamp = self.clock().strftime("%H:%M:%S")
        self.events.append(f"{stamp} {message}")
        del self.events[:-100]
        logger.info(message)

    # ── 체결 처리 ─────────────────────────────────────────
    def _apply_buy(self, quantity: int, price: float) -> None:
        cost = quantity * price
        total = self.avg_price * self.position_qty + cost
        self.position_qty += quantity
        self.avg_price = total / self.position_qty
        self.cash -= cost
        self.fills.append(Fill("buy", quantity, price, self.clock()))

    def _apply_sell(self, quantity: int, price: float) -> None:
        self.realized_pnl += (price - self.avg_price) * quantity
        self.position_qty -= quantity
        self.cash += quantity * price
        if self.position_qty == 0:
            self.avg_price = 0.0
        self.fills.append(Fill("sell", quantity, price, self.clock()))

    def _buy(self, level: Level, price: float) -> None:
        quantity = int(min(self.per_buy_cash, self.cash) // price)
        if quantity < 1:
            level.done = True
            level.note = "금액 부족으로 건너뜀"
            self._log(f"{level.seq}차 매수 건너뜀: 1주 금액({price:,.2f})이 "
                      f"분할 금액({self.per_buy_cash:,.2f})보다 크다")
            return
        self.api.send_market_order("", self.config.code, "buy", quantity)
        self._apply_buy(quantity, price)
        level.done = True
        level.note = f"{quantity}주 @{price:,.2f}"
        self._log(f"{level.seq}차 매수: {quantity}주 @{price:,.2f} "
                  f"(발동가 {level.price:,.2f})")

    def _sell(self, level: Level, price: float) -> None:
        pending = [l for l in self.sell_levels if not l.done]
        quantity = (self.position_qty if len(pending) == 1
                    else math.ceil(self.position_qty / len(pending)))
        if quantity < 1:
            return
        self.api.send_market_order("", self.config.code, "sell", quantity)
        self._apply_sell(quantity, price)
        level.done = True
        level.note = f"{quantity}주 @{price:,.2f}"
        self._log(f"{level.seq}차 매도: {quantity}주 @{price:,.2f} "
                  f"(발동가 {level.price:,.2f}, 실현손익 {self.realized_pnl:,.2f})")

    # ── 매매 루프 ─────────────────────────────────────────
    def _market_open(self) -> bool:
        if self.config.market == "us":
            utc_now = self.utc_clock()
            return in_us_market_hours(utc_now) or (
                self.config.us_day_session and in_us_day_session(utc_now))
        return in_kr_market_hours(self.clock())

    def _check_completed(self) -> None:
        if self.position_qty != 0:
            return
        buys_done = all(l.done for l in self.buy_levels)
        sells_done = all(l.done for l in self.sell_levels)
        # 보유가 0일 때: 매수가 다 끝났으면 남은 매도는 의미가 없고,
        # 매도가 다 끝났으면 남은 매수는 출구 계획이 없는 매수가 된다
        if buys_done or sells_done:
            for l in self.buy_levels + self.sell_levels:
                if not l.done:
                    l.done = True
                    l.note = "계획 종료"
            self.completed = True
            self._log(f"분할 계획 완료. 실현손익 {self.realized_pnl:,.2f}")
            self._running = False

    def step(self) -> None:
        if self.completed or not self._market_open():
            return
        price = self.api.current_price(self.config.code)
        self.last_price = price
        # 매수: 발동가 이하로 내려온 모든 미집행 레벨 (1차부터 순서대로)
        for level in self.buy_levels:
            if not level.done and price <= level.price:
                self._buy(level, price)
        # 매도: 발동가 이상으로 올라온 모든 미집행 레벨
        for level in self.sell_levels:
            if not level.done and price >= level.price and self.position_qty > 0:
                self._sell(level, price)
        self._check_completed()

    def reconcile(self) -> None:
        """실계좌 잔고와 내부 장부를 맞춘다 (미국주식, us_balances 지원 시)."""
        if self.config.market != "us" or not hasattr(self.api, "us_balances"):
            return
        try:
            balances = self.api.us_balances()
        except Exception as e:
            logger.warning("잔고 동기화 실패(다음 주기에 재시도): %s", e)
            return
        actual = balances.get(self.config.code, {}).get("qty", self.position_qty)
        if actual == self.position_qty:
            return
        ref_price = self.avg_price or self.last_price
        diff = self.position_qty - actual  # 양수 = 장부가 과대(미체결 매수 등)
        self.cash += diff * ref_price
        self._log(f"잔고 동기화: {self.position_qty}주 → 실제 {actual}주 "
                  f"(현금 {diff * ref_price:+,.2f} 보정)")
        self.position_qty = actual
        if actual == 0:
            self.avg_price = 0.0
        elif self.avg_price <= 0:
            self.avg_price = ref_price

    def run(self) -> None:
        self._running = True
        cfg = self.config
        self._log(
            f"분할매매 시작: {cfg.code} 기준가 {cfg.base_price:,.2f}, "
            f"간격 {cfg.step_pct}%, 매수 {cfg.buy_splits}분할 / "
            f"매도 {cfg.sell_splits}분할, 총 {cfg.total_cash:,.0f}")
        last_reconcile = 0.0
        try:
            while self._running:
                try:
                    self.step()
                    if time.monotonic() - last_reconcile >= 60:
                        self.reconcile()
                        last_reconcile = time.monotonic()
                except Exception as e:  # 루프는 어떤 오류에도 살아남는다
                    self.last_error = f"매매 루프 오류: {e}"
                    logger.exception("매매 루프 오류, 계속 진행: %s", e)
                self.sleep(cfg.poll_interval)
        except KeyboardInterrupt:
            logger.info("사용자 중단")
        finally:
            logger.info("종료. 실현손익 %.2f, 보유 %d주",
                        self.realized_pnl, self.position_qty)
