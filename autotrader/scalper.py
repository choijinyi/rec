"""AI 단타 자동매매.

거래량·등락률 상위 후보를 AI(Claude Fable)가 분석해 매매 종목을 자동
선정하고, 선정 즉시 매수한 뒤 목표 수익(+1~2%, 설정 가능)에서 익절,
-3%에서 무조건 손절한다. 청산으로 슬롯이 비면 다시 후보를 찾아 같은
매매를 계속 반복한다.

- 매매 시간: 국내는 장전~장후(08:00~20:00, 정규장 외 시간은 NXT 대체거래소
  지정가), 미국은 주간거래(한국 낮)+정규장.
- 안전장치: 포지션이 일정 시간 익절/손절에 닿지 않으면 정리하고 교체(시간초과),
  당일 누적 손실 한도 도달 시 신규 매수 중단, 일일 매매 횟수 제한,
  같은 종목 재매수 대기(쿨다운), 실전 이중 확인.
- AI 선정이 실패하면 등락률 모멘텀 상위로 자동 대체되어 매매가 멈추지 않는다.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

logger = logging.getLogger(__name__)


class QuoteUnavailable(RuntimeError):
    """매수 직전 시세 조회 실패 — 종목의 문제가 아니므로 제외하지 않는다."""

KR_REG_OPEN = dt.time(9, 0)
KR_REG_CLOSE = dt.time(15, 30)
KR_EXT_OPEN = dt.time(8, 0)     # NXT 프리마켓 시작
KR_EXT_CLOSE = dt.time(20, 0)   # NXT 애프터마켓 종료
US_OPEN = dt.time(9, 30)        # 미국 동부시간 기준
US_CLOSE = dt.time(16, 0)


class RankedMarketAPI(Protocol):
    def current_price(self, code: str) -> float: ...
    def send_market_order(self, account_no: str, code: str, side: str,
                          quantity: int, kr_exchange: str = "KRX") -> None: ...
    def top_stocks(self, criteria: str = "volume", limit: int = 10) -> list[dict]: ...


# ── 장 시간 판정 ──────────────────────────────────────────
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
    offset = -4 if _us_dst_active(utc_now) else -5
    et = utc_now + dt.timedelta(hours=offset)
    if et.weekday() >= 5:
        return False
    return US_OPEN <= et.time() <= US_CLOSE


def in_us_day_session(utc_now: dt.datetime) -> bool:
    """키움 미국주식 주간거래(블루오션 ATS): KST 10~18시, 서머타임엔 9~17시."""
    kst = utc_now + dt.timedelta(hours=9)
    if kst.weekday() >= 5:
        return False
    if _us_dst_active(utc_now):
        return dt.time(9, 0) <= kst.time() <= dt.time(17, 0)
    return dt.time(10, 0) <= kst.time() <= dt.time(18, 0)


def kr_session(now: dt.datetime, extended: bool = True) -> str:
    """국내: 장전(NXT) / 정규장 / 장후(NXT) / 장외."""
    if now.weekday() >= 5:
        return "장외"
    t = now.time()
    if KR_REG_OPEN <= t <= KR_REG_CLOSE:
        return "정규장"
    if extended and KR_EXT_OPEN <= t < KR_REG_OPEN:
        return "장전"
    if extended and KR_REG_CLOSE < t <= KR_EXT_CLOSE:
        return "장후"
    return "장외"


def _us_extended_session(utc_now: dt.datetime) -> str:
    """미국 프리마켓(ET 04:00~09:30) / 애프터마켓(ET 16:00~20:00) 여부."""
    offset = -4 if _us_dst_active(utc_now) else -5
    et = utc_now + dt.timedelta(hours=offset)
    if et.weekday() >= 5:
        return ""
    if dt.time(4, 0) <= et.time() < US_OPEN:
        return "프리마켓"
    if US_CLOSE < et.time() <= dt.time(20, 0):
        return "애프터마켓"
    return ""


def us_session(utc_now: dt.datetime, day_session: bool = True,
               extended: bool = True) -> str:
    if in_us_market_hours(utc_now):
        return "정규장"
    if extended:
        label = _us_extended_session(utc_now)
        if label:
            return label
    if day_session and in_us_day_session(utc_now):
        return "주간거래"
    return "장외"


# ── 설정과 상태 ───────────────────────────────────────────
@dataclass
class ScalpConfig:
    market: str = "kr"            # kr / us
    target_pct: float = 1.5       # 익절 목표(%) — 1~2 권장
    stop_pct: float = 3.0         # 손절선(%) — 넘어가면 무조건 전량 매도
    max_positions: int = 2        # 동시 보유 종목 수
    total_cash: float = 1_000_000
    poll_interval: float = 3.0
    scan_interval: float = 120.0  # 후보 재탐색 주기(초)
    cooldown_min: float = 30.0    # 손절한 종목 재매수 대기(분)
    win_cooldown_min: float = 5.0  # 익절한 종목 재매수 대기(분) — 짧게 두어 반복 매매
    stale_min: float = 60.0       # 이 시간(분) 안에 익절/손절 미도달 시 교체
    daily_loss_pct: float = 6.0   # 당일 누적 손실이 자본의 이 % 도달 시 신규 매수 중단
    blind_exit_min: float = 5.0   # 시세 조회가 이 시간(분) 이상 막히면 보호 청산
    max_trades_per_day: int = 40  # 당일 최대 청산 횟수
    allow_real: bool = False
    us_day_session: bool = True
    us_extended: bool = True      # 미국 프리마켓·애프터마켓 매매
    kr_extended: bool = True      # 국내 장전·장후(NXT) 매매
    min_price: float | None = None  # None이면 미국 $5 / 국내 1,000원
    # 거래대금(가격×당일 거래량) 하한 — 저유동성 종목은 지정가 매도가 안 받아져
    # 손절이 불가능해지므로 후보에서 뺀다. None이면 미국 $3M / 국내 30억 원, 0이면 끔
    min_dollar_volume: float | None = None
    fill_timeout_sec: float = 45.0  # 지정가 주문이 이 시간 안에 체결되지 않으면 취소·재주문
    chase_step_pct: float = 0.3     # 추격 매도 회차마다 현재가 대비 낮추는 폭(%)
    chase_max_pct: float = 2.0      # 추격 매도 최대 할인 폭(%)


@dataclass
class Position:
    code: str
    quantity: int
    entry_price: float
    entry_time: dt.datetime
    last_price: float = 0.0
    last_quote_time: dt.datetime | None = None   # 마지막으로 시세를 받은 시각
    # 지정가 주문 체결 확인 (미국주식): 체결이 확인되기 전에는 포지션을
    # 장부에서 지우지 않는다 — 매도 주문만 내고 체결을 가정하면, 미체결 시
    # 아무도 감시하지 않는 '유령 보유'가 생긴다
    entry_pending: bool = False
    entry_order: str = ""
    entry_sent_at: dt.datetime | None = None
    exit_pending: bool = False
    exit_reason: str = ""
    exit_order: str = ""
    exit_sent_at: dt.datetime | None = None
    exit_price: float = 0.0
    exit_attempts: int = 0
    exit_needs_resend: bool = False
    orig_qty: int = 0
    partial_pnl: float = 0.0
    adopted: bool = False        # 계좌 기존 보유를 사용자가 편입한 포지션
    # 이 포지션과 무관하게 계좌에 원래 있던 수량 (체결 검증용, 모르면 None)
    baseline_qty: int | None = None
    exit_unconfirmed: int = 0    # 주문은 사라졌는데 잔고가 안 줄어든 확인 횟수

    def state(self) -> str:
        if self.entry_pending:
            return "매수 체결 확인 중"
        if self.exit_pending:
            return f"{self.exit_reason} 매도 체결 대기({self.exit_attempts}차)"
        return "편입 보유" if self.adopted else "보유"

    def pct(self, price: float | None = None) -> float:
        p = price if price is not None else self.last_price
        if not p or self.entry_price <= 0:
            return 0.0
        return (p / self.entry_price - 1) * 100


@dataclass
class Trade:
    code: str
    quantity: int
    entry_price: float
    exit_price: float
    pnl: float
    pct: float
    reason: str                   # 익절 / 손절 / 시간초과 / 중지
    closed_at: dt.datetime


class Scalper:
    """AI 종목 선정 + 익절/손절 반복 단타 루프."""

    def __init__(self, api: RankedMarketAPI, config: ScalpConfig,
                 selector: Callable[[list[dict], int],
                                    tuple[list[str], str]] | None = None,
                 is_simulation: bool = True,
                 clock: Callable[[], dt.datetime] = dt.datetime.now,
                 utc_clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
                 sleep: Callable[[float], None] = time.sleep,
                 state_path: str | None = None):
        if not is_simulation and not config.allow_real:
            raise PermissionError(
                "실전투자 접속이 감지되었다. 모의투자로 검증하거나 allow_real을 "
                "명시해야 한다.")
        if not 0 < config.target_pct <= 10:
            raise ValueError("목표 수익 %는 0보다 크고 10 이하여야 한다")
        if not 0 < config.stop_pct <= 20:
            raise ValueError("손절 %는 0보다 크고 20 이하여야 한다")
        if config.max_positions < 1:
            raise ValueError("동시 종목 수는 1 이상이어야 한다")
        if config.total_cash <= 0:
            raise ValueError("총 투입 금액은 0보다 커야 한다")
        self.api = api
        self.config = config
        self.selector = selector
        self.clock = clock
        self.utc_clock = utc_clock
        self.sleep = sleep

        self.cash = config.total_cash
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.events: list[str] = []
        self.selection_note = ""
        self.last_error = ""
        self.halt_reason = ""     # 일일 한도 도달 등으로 신규 매수가 멈춘 이유
        self._cooldown: dict[str, dt.datetime] = {}
        self._fail_counts: dict[str, int] = {}
        self._scan_lock = threading.Lock()
        self._scan_thread: threading.Thread | None = None
        self._scan_result: tuple[list[str], str] | None = None
        self._last_scan = 0.0
        self._rescan_asap = False
        self._no_candidates = False
        self._running = False
        # 미국주식은 지정가라 체결을 미체결 조회로 확인한다
        self._verify = (config.market == "us"
                        and hasattr(api, "us_open_orders")
                        and hasattr(api, "cancel_order"))
        self.state_path = state_path
        self.holdings: dict[str, dict] = {}     # 실계좌 보유 (잔고 동기화 시 갱신)
        self._requests: list[tuple[str, str]] = []   # UI의 편입/청산 요청
        self._req_lock = threading.Lock()
        self._entry_pause_until: dt.datetime | None = None

    # ── 상태 조회 (UI 공용) ────────────────────────────────
    @property
    def realized_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.pnl <= 0)

    def equity(self) -> float:
        held = sum(p.quantity * (p.last_price or p.entry_price)
                   for p in self.positions.values())
        return self.cash + held

    def session(self) -> str:
        if self.config.market == "us":
            return us_session(self.utc_clock(), self.config.us_day_session,
                              self.config.us_extended)
        return kr_session(self.clock(), self.config.kr_extended)

    def stop(self) -> None:
        self._running = False

    def _log(self, message: str) -> None:
        stamp = self.clock().strftime("%H:%M:%S")
        self.events.append(f"{stamp} {message}")
        del self.events[:-120]
        logger.info(message)

    # ── 주문 경로 ─────────────────────────────────────────
    def _kr_route(self) -> str:
        return "KRX" if kr_session(self.clock(), False) == "정규장" else "NXT"

    def _order(self, code: str, side: str, quantity: int,
               limit_price: float | None = None) -> str:
        kwargs = {"kr_exchange": self._kr_route()}
        if limit_price is not None:
            kwargs["limit_price"] = limit_price
        result = self.api.send_market_order("", code, side, quantity, **kwargs)
        return result if isinstance(result, str) else ""

    # ── 포지션 관리 ───────────────────────────────────────
    def _per_slot_budget(self) -> float:
        return self.config.total_cash / self.config.max_positions

    def _buy(self, code: str) -> bool:
        try:
            price = self.api.current_price(code)
        except Exception as e:
            raise QuoteUnavailable(f"{code} 시세 조회 실패: {e}") from e
        quantity = int(min(self._per_slot_budget(), self.cash) // price)
        if quantity < 1:
            self._log(f"{code} 매수 건너뜀: 1주 금액({price:,.2f})이 슬롯 예산보다 크다")
            return False
        ord_no = self._order(code, "buy", quantity)
        now = self.clock()
        pos = Position(code, quantity, price, now, last_price=price,
                       last_quote_time=now, orig_qty=quantity)
        if self._verify:
            pos.entry_pending = True
            pos.entry_order = ord_no
            pos.entry_sent_at = now
        self.positions[code] = pos
        self.cash -= quantity * price
        self._log(f"매수{' 주문' if self._verify else ''}: {code} {quantity}주 "
                  f"@{price:,.2f} (목표 +{self.config.target_pct}% / "
                  f"손절 -{self.config.stop_pct}%)")
        self._save_state()
        return True

    def _close(self, pos: Position, price: float, reason: str) -> None:
        """청산 주문. 미국(지정가)은 체결이 확인될 때까지 포지션을 유지한다."""
        if not self._verify:
            self._order(pos.code, "sell", pos.quantity)
            self._finalize(pos, price, reason)
            return
        ord_no = self._order(pos.code, "sell", pos.quantity, limit_price=price)
        pos.exit_pending = True
        pos.exit_reason = reason
        pos.exit_order = ord_no
        pos.exit_sent_at = self.clock()
        pos.exit_price = price
        pos.exit_attempts = 1
        pos.exit_needs_resend = False
        self._log(f"{reason} 매도 주문: {pos.code} {pos.quantity}주 @{price:,.2f} "
                  f"({pos.pct(price):+.2f}%) — 체결 확인 중")

    def _finalize(self, pos: Position, price: float, reason: str) -> None:
        """체결이 확인된 청산을 장부에 반영한다."""
        pnl = pos.partial_pnl + (price - pos.entry_price) * pos.quantity
        if not pos.adopted:     # 편입분은 프로그램 현금으로 산 것이 아니다
            self.cash += pos.quantity * price
        qty = max(pos.orig_qty, pos.quantity)
        self.trades.append(Trade(pos.code, qty, pos.entry_price, price, pnl,
                                 pos.pct(price), reason, self.clock()))
        self.positions.pop(pos.code, None)
        # 익절한 종목은 짧게 쉬고 재진입을 허용해 매매가 계속 이어지게 한다
        minutes = (self.config.win_cooldown_min if pnl > 0
                   else self.config.cooldown_min)
        self._cooldown[pos.code] = self.clock() + dt.timedelta(minutes=minutes)
        self._rescan_asap = True   # 슬롯이 비었으니 즉시 재탐색
        self._log(f"{reason}: {pos.code} {qty}주 @{price:,.2f} "
                  f"({pos.pct(price):+.2f}%, 손익 {pnl:+,.2f}, "
                  f"누적 {self.realized_pnl:+,.2f})"
                  f"{' — 체결 확인' if self._verify else ''}")
        self._save_state()

    # ── 지정가 체결 확인 (미국주식) ───────────────────────
    @staticmethod
    def _norm_no(no: str) -> str:
        return str(no or "").strip().lstrip("0")

    def _find_order(self, orders: list[dict], code: str, ord_no: str):
        if ord_no:
            key = self._norm_no(ord_no)
            for o in orders:
                if self._norm_no(o.get("ord_no")) == key:
                    return o
            return None
        return next((o for o in orders if o.get("code") == code), None)

    @staticmethod
    def _remaining(order: dict, qty: int) -> int:
        remaining = int(order.get("remaining") or 0)
        if remaining <= 0:
            remaining = qty - int(order.get("filled") or 0)
        return max(0, min(qty, remaining))

    def _actual_qty(self, code: str) -> int | None:
        if not hasattr(self.api, "us_balances"):
            return None
        try:
            balances = self.api.us_balances()
        except Exception as e:
            logger.warning("잔고 조회 실패(체결 검증 보류): %s", e)
            return None
        self.holdings = balances
        return int(balances.get(code, {}).get("qty") or 0)

    def _check_pending(self) -> None:
        pending = [p for p in self.positions.values()
                   if p.entry_pending or p.exit_pending]
        if not pending:
            return
        try:
            orders = self.api.us_open_orders()
        except Exception as e:
            self.last_error = f"미체결 조회 실패: {e}"
            logger.warning("미체결 조회 실패(다음 주기에 재확인): %s", e)
            return
        now = self.clock()
        for pos in pending:
            try:
                if pos.entry_pending:
                    self._check_entry(pos, orders, now)
                else:
                    self._check_exit(pos, orders, now)
            except Exception as e:
                self.last_error = f"{pos.code} 체결 확인 오류: {e}"
                logger.warning("%s 체결 확인 오류: %s", pos.code, e)

    def _check_entry(self, pos: Position, orders: list[dict],
                     now: dt.datetime) -> None:
        order = self._find_order(orders, pos.code, pos.entry_order)
        if order is None:
            pos.entry_pending = False
            pos.orig_qty = pos.quantity
            actual = self._actual_qty(pos.code)
            if actual is not None:
                pos.baseline_qty = max(0, actual - pos.quantity)
            self._log(f"매수 체결 확인: {pos.code} {pos.quantity}주")
            self._save_state()
            return
        if (now - pos.entry_sent_at).total_seconds() < self.config.fill_timeout_sec:
            return
        try:
            self.api.cancel_order(order["ord_no"], pos.code)
        except Exception as e:   # 그사이 체결됐을 수 있다 — 다음 주기에 재확인
            logger.warning("%s 매수 미체결 취소 실패: %s", pos.code, e)
            return
        remaining = self._remaining(order, pos.quantity)
        filled = pos.quantity - remaining
        self.cash += remaining * pos.entry_price
        if filled <= 0:
            self.positions.pop(pos.code, None)
            self._cooldown[pos.code] = now + dt.timedelta(minutes=10)
            self._rescan_asap = True
            self._log(f"매수 미체결 취소: {pos.code} — 체결 0주, 다음 후보로 교체")
        else:
            pos.quantity = filled
            pos.orig_qty = filled
            pos.entry_pending = False
            self._log(f"매수 일부 체결: {pos.code} {filled}주 체결, 잔량 {remaining}주 취소")
        self._save_state()

    def _check_exit(self, pos: Position, orders: list[dict],
                    now: dt.datetime) -> None:
        if pos.exit_needs_resend:
            self._resend_exit(pos, now)
            return
        order = self._find_order(orders, pos.code, pos.exit_order)
        if order is None:          # 미체결 목록에서 사라짐 — 체결 또는 소멸
            self._confirm_exit_by_balance(pos, now)
            return
        if (now - pos.exit_sent_at).total_seconds() < self.config.fill_timeout_sec:
            return
        try:
            self.api.cancel_order(order["ord_no"], pos.code)
        except Exception as e:   # 그사이 체결됐을 수 있다 — 다음 주기에 재확인
            logger.warning("%s 매도 미체결 취소 실패: %s", pos.code, e)
            return
        remaining = self._remaining(order, pos.quantity)
        filled = pos.quantity - remaining
        if remaining <= 0:
            self._finalize(pos, pos.exit_price, pos.exit_reason)
            return
        if filled > 0:
            pos.partial_pnl += (pos.exit_price - pos.entry_price) * filled
            if not pos.adopted:
                self.cash += filled * pos.exit_price
            pos.quantity = remaining
        pos.exit_needs_resend = True
        self._resend_exit(pos, now)

    def _confirm_exit_by_balance(self, pos: Position, now: dt.datetime) -> None:
        """주문이 사라진 뒤 실계좌 보유로 실제 매도 여부를 확인한다.

        지정가 주문은 장 마감 등으로 체결 없이 소멸할 수 있어, 사라졌다고
        체결로 단정하면 '팔았다고 믿는 보유'가 다시 생긴다. 잔고 반영이
        늦을 수 있으므로 안 팔린 것으로 보여도 두 번 연속 확인한 뒤에만
        재주문한다.
        """
        if pos.baseline_qty is None:
            self._finalize(pos, pos.exit_price, pos.exit_reason)
            return
        actual = self._actual_qty(pos.code)
        if actual is None:
            return                              # 잔고 조회 실패 — 다음 주기에 재확인
        unsold = max(0, min(pos.quantity, actual - pos.baseline_qty))
        if unsold == 0:
            self._finalize(pos, pos.exit_price, pos.exit_reason)
            return
        pos.exit_unconfirmed += 1
        if pos.exit_unconfirmed < 2:
            return
        pos.exit_unconfirmed = 0
        sold = pos.quantity - unsold
        if sold > 0:
            pos.partial_pnl += (pos.exit_price - pos.entry_price) * sold
            if not pos.adopted:
                self.cash += sold * pos.exit_price
            pos.quantity = unsold
        self._log(f"매도 주문이 체결 없이 사라짐: {pos.code} {unsold}주 미매도 확인 "
                  f"→ 재주문")
        pos.exit_needs_resend = True
        self._resend_exit(pos, now)

    def _resend_exit(self, pos: Position, now: dt.datetime) -> None:
        """미체결 매도를 현재가보다 조금씩 낮춰 다시 낸다 (손절은 무조건 나가야 한다)."""
        try:
            price = self.api.current_price(pos.code)
            pos.last_price = price
        except Exception:
            price = pos.last_price or pos.exit_price
        attempt = pos.exit_attempts + 1
        discount = min(self.config.chase_step_pct * (attempt - 1),
                       self.config.chase_max_pct)
        limit = round(price * (1 - discount / 100), 2)
        try:
            ord_no = self._order(pos.code, "sell", pos.quantity, limit_price=limit)
        except Exception as e:
            self.last_error = f"{pos.code} 추격 매도 실패: {e}"
            logger.warning("%s 추격 매도 실패(다음 주기에 재시도): %s", pos.code, e)
            return
        pos.exit_attempts = attempt
        pos.exit_order = ord_no
        pos.exit_sent_at = now
        pos.exit_price = limit
        pos.exit_needs_resend = False
        self._log(f"매도 미체결 → {attempt}차 추격 매도: {pos.code} {pos.quantity}주 "
                  f"@{limit:,.2f} (현재가 대비 -{discount:.1f}%)")

    def _monitor_positions(self) -> None:
        if self._verify:
            self._check_pending()
        for code in list(self.positions):
            pos = self.positions.get(code)
            if pos is None or pos.entry_pending or pos.exit_pending:
                continue
            try:
                price = self.api.current_price(code)
            except Exception as e:   # 한 종목의 시세 오류가 전체를 멈추지 않게
                count = self._fail_counts.get(code, 0) + 1
                self._fail_counts[code] = count
                self.last_error = f"{code} 시세 오류: {e}"
                if count == 1 or count % 10 == 0:   # 3초마다 쏟아지지 않게 간추림
                    logger.warning("%s 시세 조회 실패(%d회째): %s", code, count, e)
                # 시세 공백이 길어지면 익절·손절 감시가 눈을 감은 상태가 되므로
                # 마지막 시세 기준으로 보호 청산을 시도한다
                blind_min = (self.clock() - (pos.last_quote_time
                                             or pos.entry_time)
                             ).total_seconds() / 60
                if blind_min >= self.config.blind_exit_min and pos.last_price:
                    try:
                        self._close(pos, pos.last_price, "시세공백 보호청산")
                    except Exception as e2:
                        logger.warning("%s 보호 청산 실패(다음 주기에 재시도): %s",
                                       code, e2)
                continue
            self._fail_counts[code] = 0
            pos.last_price = price
            pos.last_quote_time = self.clock()
            pct = pos.pct(price)
            held_min = (self.clock() - pos.entry_time).total_seconds() / 60
            try:
                if pct >= self.config.target_pct:
                    self._close(pos, price, "익절")
                elif pct <= -self.config.stop_pct:
                    self._close(pos, price, "손절")
                elif held_min >= self.config.stale_min:
                    self._close(pos, price, "시간초과")
            except Exception as e:
                self.last_error = f"{code} 청산 주문 오류: {e}"
                logger.warning("%s 청산 실패(다음 주기에 재시도): %s", code, e)

    # ── 종목 탐색 (백그라운드: 포지션 감시를 막지 않는다) ──
    def _min_price(self) -> float:
        if self.config.min_price is not None:
            return self.config.min_price
        return 5.0 if self.config.market == "us" else 1000.0

    def _min_dollar_volume(self) -> float:
        if self.config.min_dollar_volume is not None:
            return self.config.min_dollar_volume
        return 3_000_000.0 if self.config.market == "us" else 3_000_000_000.0

    @staticmethod
    def _row_float(row: dict, key: str) -> float:
        try:
            return float(str(row.get(key, "")).replace(",", "")
                         .replace("+", "").replace("%", ""))
        except ValueError:
            return 0.0

    def _candidates(self) -> list[dict]:
        rows: list[dict] = []
        seen: set[str] = set()
        for criteria in ("change", "volume"):
            try:
                fetched = self.api.top_stocks(criteria, limit=10)
            except Exception as e:
                logger.warning("순위 조회 실패(%s): %s", criteria, e)
                continue
            for row in fetched:
                code = row.get("code", "")
                if code and code not in seen:
                    seen.add(code)
                    rows.append(row)
        now = self.clock()
        min_price = self._min_price()
        min_dv = self._min_dollar_volume()

        def ok(row: dict) -> bool:
            code = row["code"]
            price = abs(self._row_float(row, "price"))
            if 0 < price < min_price:
                return False                      # 초저가 잡주 배제
            if min_dv > 0:                        # 저유동성 배제(손절 체결 보장)
                volume = abs(self._row_float(row, "volume"))
                if volume <= 0 or price * volume < min_dv:
                    return False
            if self._row_float(row, "change_pct") <= 0:
                return False                      # 상승 모멘텀 종목만
            if code in self.positions:
                return False
            until = self._cooldown.get(code)
            return until is None or now >= until  # 쿨다운 중 재매수 금지

        return [r for r in rows if ok(r)]

    def _scan_worker(self, slots: int) -> None:
        try:
            rows = self._candidates()
            if not rows:
                if not self._no_candidates:
                    self._no_candidates = True
                    self._log("탐색: 매수 가능한 후보가 없다(필터·쿨다운 제외) "
                              "— 30초 간격으로 재탐색한다")
                self._retry_scan_soon()
                return
            self._no_candidates = False
            ranked = sorted(rows, key=lambda r: self._row_float(r, "change_pct"),
                            reverse=True)
            codes: list[str] = []
            note = ""
            if self.selector is not None:
                try:
                    picked, note = self.selector(rows, slots)
                    valid = {r["code"] for r in rows}
                    codes = [c for c in picked if c in valid][:slots]
                except Exception as e:
                    logger.warning("AI 선정 실패, 모멘텀 상위로 대체: %s", e)
                    note = f"AI 선정 불가({str(e)[:80]}) — 등락률 모멘텀 상위로 자동 대체"
            if not codes:   # AI 실패/무효 → 등락률 모멘텀 상위로 대체
                codes = [r["code"] for r in ranked[:slots]]
                note = note or "AI 선정 불가 — 등락률 모멘텀 상위로 자동 대체"
            # 주문이 거부된 종목을 즉시 대체할 수 있게 예비 후보를 뒤에 붙인다
            backup = [r["code"] for r in ranked if r["code"] not in codes]
            codes = (codes + backup)[: slots + 3]
            with self._scan_lock:
                self._scan_result = (codes, note)
        except Exception as e:
            logger.warning("종목 탐색 오류: %s", e)
        finally:
            self._scan_thread = None

    def _retry_scan_soon(self, seconds: float = 30.0) -> None:
        self._last_scan = time.monotonic() - self.config.scan_interval + seconds

    def _maybe_scan(self, slots: int) -> None:
        if slots < 1 or self._scan_thread is not None:
            return
        due = (self._rescan_asap
               or time.monotonic() - self._last_scan >= self.config.scan_interval)
        if not due:
            return
        self._rescan_asap = False
        self._last_scan = time.monotonic()
        thread = threading.Thread(target=self._scan_worker, args=(slots,),
                                  daemon=True)
        self._scan_thread = thread
        thread.start()

    def _consume_scan(self, slots: int) -> None:
        with self._scan_lock:
            result, self._scan_result = self._scan_result, None
        if result is None:
            return
        codes, note = result
        if note:
            self.selection_note = note
        filled = 0
        for code in codes:
            if filled >= slots:
                break
            if code in self.positions:
                continue
            try:
                if self._buy(code):
                    filled += 1
            except QuoteUnavailable as e:
                # 서버측 시세 오류일 뿐 종목의 문제가 아니다 — 제외 없이
                # 다음 후보로 넘어가고, 이 종목은 다음 탐색에서 다시 시도한다
                self.last_error = str(e)
                self._log(f"매수 보류({code}): 시세 조회 실패 — 제외 없이 잠시 후 재시도")
                continue
            except Exception as e:
                message = str(e)
                self.last_error = f"{code} 매수 오류: {message}"
                if "증거금" in message:
                    # 계좌 전체의 매수 여력 문제(미체결 주문·장부와 실제 차이) —
                    # 종목을 제외하지 않고 신규 매수만 잠시 멈춘다
                    self._entry_pause_until = self.clock() + dt.timedelta(minutes=3)
                    self._log(f"매수 보류({code}): 계좌 매수가능 금액 부족 — "
                              f"3분간 신규 매수 대기 (종목 제외 안 함)")
                    break
                # 거래 요건 미달(레버리지 ETP 예탁금 등)·매수 불가 종목은
                # 당일 제외하고, 그 외 실패도 1시간은 다시 시도하지 않는다
                hard = any(k in message for k in ("불가", "예탁금", "미만"))
                hours = 24 if hard else 1
                self._cooldown[code] = self.clock() + dt.timedelta(hours=hours)
                self._log(f"매수 실패({code}): {message[:90]}"
                          f" → {'당일' if hard else '1시간'} 제외, 다음 후보로 교체")
        if codes and filled == 0:
            self._retry_scan_soon()   # 전부 거부되면 2분을 기다리지 않고 재탐색

    # ── 일일 안전장치 ─────────────────────────────────────
    def _entries_allowed(self) -> bool:
        if self._entry_pause_until and self.clock() < self._entry_pause_until:
            return False
        limit = -self.config.total_cash * self.config.daily_loss_pct / 100
        if self.realized_pnl <= limit:
            if self.halt_reason != "손실 한도":
                self.halt_reason = "손실 한도"
                self._log(f"당일 손실 한도(-{self.config.daily_loss_pct}%) 도달 — "
                          f"신규 매수를 중단한다 (보유 청산은 계속)")
            return False
        if len(self.trades) >= self.config.max_trades_per_day:
            if self.halt_reason != "매매 횟수 한도":
                self.halt_reason = "매매 횟수 한도"
                self._log(f"당일 매매 횟수 한도({self.config.max_trades_per_day}회) "
                          f"도달 — 신규 매수를 중단한다")
            return False
        return True

    # ── 계좌 보유 편입 / 수동 청산 (UI 요청) ────────────────
    def request(self, code: str, mode: str) -> None:
        """mode: manage(편입 후 익절·손절 규칙 적용) | sell(즉시 전량 매도)."""
        with self._req_lock:
            self._requests.append((code.strip().upper(), mode))

    def _process_requests(self) -> None:
        with self._req_lock:
            requests, self._requests = self._requests, []
        for code, mode in requests:
            try:
                self._adopt(code, mode)
            except Exception as e:
                self.last_error = f"{code} 편입/청산 실패: {e}"
                self._log(f"편입/청산 실패({code}): {str(e)[:80]}")

    def _adopt(self, code: str, mode: str) -> None:
        pos = self.positions.get(code)
        if pos is not None:
            if mode == "sell" and not (pos.entry_pending or pos.exit_pending):
                price = pos.last_price or self.api.current_price(code)
                self._close(pos, price, "수동 청산")
            return
        holding = self.holdings.get(code)
        if holding is None and hasattr(self.api, "us_balances"):
            self.holdings = self.api.us_balances()
            holding = self.holdings.get(code)
        qty = int((holding or {}).get("sellable") or (holding or {}).get("qty") or 0)
        if qty <= 0:
            self._log(f"편입 불가({code}): 계좌에 매도 가능한 수량이 없다")
            return
        try:
            price = self.api.current_price(code)
        except Exception:
            price = float(holding.get("now_price") or 0)
        avg = float(holding.get("avg_price") or 0) or price
        now = self.clock()
        pos = Position(code, qty, avg, now, last_price=price,
                       last_quote_time=now, orig_qty=qty, adopted=True,
                       baseline_qty=max(0, int(holding.get("qty") or qty) - qty))
        self.positions[code] = pos
        self._log(f"계좌 보유 편입: {code} {qty}주 (매입단가 {avg:,.2f}, "
                  f"현재 {pos.pct(price):+.2f}%) — 익절·손절 규칙 적용")
        if mode == "sell":
            self._close(pos, price, "수동 청산")
        self._save_state()

    # ── 상태 저장 / 복원 (재시작해도 자기 포지션을 잃지 않는다) ──
    def _save_state(self) -> None:
        if not self.state_path:
            return
        data = {"market": self.config.market,
                "saved_at": self.clock().isoformat(),
                "positions": [
                    {"code": p.code, "quantity": p.quantity,
                     "entry_price": p.entry_price,
                     "entry_time": p.entry_time.isoformat(),
                     "adopted": p.adopted}
                    for p in self.positions.values()]}
        try:
            tmp = f"{self.state_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)
        except OSError as e:
            logger.warning("상태 저장 실패: %s", e)

    def restore_state(self) -> None:
        """이전 실행이 남긴 포지션을 실계좌 잔고와 대조해 복원한다 (미국주식)."""
        if (not self.state_path or not os.path.exists(self.state_path)
                or self.config.market != "us"
                or not hasattr(self.api, "us_balances")):
            return
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("market") != self.config.market:
                return
            balances = self.api.us_balances()
        except Exception as e:
            logger.warning("상태 복원 실패: %s", e)
            return
        self.holdings = balances
        for item in data.get("positions", []):
            code = item.get("code", "")
            actual = int(balances.get(code, {}).get("qty") or 0)
            qty = min(int(item.get("quantity", 0)), actual)
            if not code or qty <= 0 or code in self.positions:
                continue
            try:
                entry_time = dt.datetime.fromisoformat(item["entry_time"])
            except (KeyError, ValueError):
                entry_time = self.clock()
            pos = Position(code, qty, float(item.get("entry_price", 0)),
                           entry_time, orig_qty=qty,
                           adopted=bool(item.get("adopted")),
                           baseline_qty=max(0, actual - qty))
            self.positions[code] = pos
            if not pos.adopted:
                self.cash -= qty * pos.entry_price
            self._log(f"이전 실행 포지션 복원: {code} {qty}주 "
                      f"(매입단가 {pos.entry_price:,.2f}) — 익절·손절 감시 재개")
        self._save_state()

    # ── 매매 루프 ─────────────────────────────────────────
    def step(self) -> None:
        if self.session() == "장외":
            return
        self._process_requests()
        self._monitor_positions()
        slots = self.config.max_positions - len(self.positions)
        if slots > 0 and self._entries_allowed():
            self._consume_scan(slots)
            slots = self.config.max_positions - len(self.positions)
            self._maybe_scan(slots)

    def reconcile(self) -> None:
        """실계좌 잔고와 내부 장부를 맞춘다 (미국주식, us_balances 지원 시).

        장부 수량보다 실제 보유가 적을 때만(미체결 매수 등) 줄여서 맞춘다.
        실제 보유가 더 많아도 장부를 늘리지 않는다 — 그 초과분은 사용자가
        이 프로그램과 무관하게 들고 있는 주식이므로, 흡수하면 익절·손절
        때 사용자의 기존 보유분까지 팔아버리게 된다.
        """
        if self.config.market != "us" or not hasattr(self.api, "us_balances"):
            return
        try:
            balances = self.api.us_balances()
        except Exception as e:
            logger.warning("잔고 동기화 실패(다음 주기에 재시도): %s", e)
            return
        self.holdings = balances
        for code in list(self.positions):
            pos = self.positions[code]
            if pos.entry_pending or pos.exit_pending or pos.adopted:
                continue
            actual = balances.get(code, {}).get("qty", pos.quantity)
            if actual >= pos.quantity:
                continue   # 기존 보유분(초과)은 이 프로그램이 건드리지 않는다
            ref = pos.last_price or pos.entry_price
            diff = pos.quantity - actual
            self.cash += diff * ref
            self._log(f"잔고 동기화: {code} {pos.quantity}주 → 실제 {actual}주 "
                      f"(현금 {diff * ref:+,.2f} 보정)")
            if actual == 0:
                del self.positions[code]
            else:
                pos.quantity = actual
            self._save_state()

    def run(self) -> None:
        self._running = True
        cfg = self.config
        self._log(f"AI 단타 시작: {'미국' if cfg.market == 'us' else '국내'} / "
                  f"목표 +{cfg.target_pct}% · 손절 -{cfg.stop_pct}% / "
                  f"동시 {cfg.max_positions}종목 / 총 {cfg.total_cash:,.0f}")
        try:
            self.restore_state()
        except Exception as e:
            logger.warning("상태 복원 중 오류: %s", e)
        last_reconcile = 0.0
        try:
            while self._running:
                try:
                    self.step()
                    if time.monotonic() - last_reconcile >= 60:
                        self.reconcile()
                        last_reconcile = time.monotonic()
                except Exception as e:   # 루프는 어떤 오류에도 살아남는다
                    self.last_error = f"매매 루프 오류: {e}"
                    logger.exception("매매 루프 오류, 계속 진행: %s", e)
                self.sleep(cfg.poll_interval)
        except KeyboardInterrupt:
            logger.info("사용자 중단")
        finally:
            logger.info("종료. 매매 %d회 (승 %d / 패 %d), 실현손익 %.2f",
                        len(self.trades), self.wins, self.losses,
                        self.realized_pnl)
