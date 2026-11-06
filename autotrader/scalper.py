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
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

logger = logging.getLogger(__name__)

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
    cooldown_min: float = 30.0    # 같은 종목 재매수 대기(분)
    stale_min: float = 60.0       # 이 시간(분) 안에 익절/손절 미도달 시 교체
    daily_loss_pct: float = 6.0   # 당일 누적 손실이 자본의 이 % 도달 시 신규 매수 중단
    max_trades_per_day: int = 40  # 당일 최대 청산 횟수
    allow_real: bool = False
    us_day_session: bool = True
    us_extended: bool = True      # 미국 프리마켓·애프터마켓 매매
    kr_extended: bool = True      # 국내 장전·장후(NXT) 매매
    min_price: float | None = None  # None이면 미국 $5 / 국내 1,000원


@dataclass
class Position:
    code: str
    quantity: int
    entry_price: float
    entry_time: dt.datetime
    last_price: float = 0.0

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
                 sleep: Callable[[float], None] = time.sleep):
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
        self._running = False

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

    def _order(self, code: str, side: str, quantity: int) -> None:
        self.api.send_market_order("", code, side, quantity,
                                   kr_exchange=self._kr_route())

    # ── 포지션 관리 ───────────────────────────────────────
    def _per_slot_budget(self) -> float:
        return self.config.total_cash / self.config.max_positions

    def _buy(self, code: str) -> bool:
        price = self.api.current_price(code)
        quantity = int(min(self._per_slot_budget(), self.cash) // price)
        if quantity < 1:
            self._log(f"{code} 매수 건너뜀: 1주 금액({price:,.2f})이 슬롯 예산보다 크다")
            return False
        self._order(code, "buy", quantity)
        self.positions[code] = Position(code, quantity, price, self.clock(),
                                        last_price=price)
        self.cash -= quantity * price
        self._log(f"매수: {code} {quantity}주 @{price:,.2f} "
                  f"(목표 +{self.config.target_pct}% / 손절 -{self.config.stop_pct}%)")
        return True

    def _close(self, pos: Position, price: float, reason: str) -> None:
        self._order(pos.code, "sell", pos.quantity)
        pnl = (price - pos.entry_price) * pos.quantity
        self.cash += pos.quantity * price
        self.trades.append(Trade(pos.code, pos.quantity, pos.entry_price,
                                 price, pnl, pos.pct(price), reason,
                                 self.clock()))
        del self.positions[pos.code]
        self._cooldown[pos.code] = self.clock() + dt.timedelta(
            minutes=self.config.cooldown_min)
        self._log(f"{reason}: {pos.code} {pos.quantity}주 @{price:,.2f} "
                  f"({pos.pct(price):+.2f}%, 손익 {pnl:+,.2f}, "
                  f"누적 {self.realized_pnl:+,.2f})")

    def _monitor_positions(self) -> None:
        for code in list(self.positions):
            pos = self.positions[code]
            try:
                price = self.api.current_price(code)
            except Exception as e:   # 한 종목의 시세 오류가 전체를 멈추지 않게
                self._fail_counts[code] = self._fail_counts.get(code, 0) + 1
                self.last_error = f"{code} 시세 오류: {e}"
                logger.warning("%s 시세 조회 실패(%d회): %s",
                               code, self._fail_counts[code], e)
                continue
            self._fail_counts[code] = 0
            pos.last_price = price
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

        def ok(row: dict) -> bool:
            code = row["code"]
            price = abs(self._row_float(row, "price"))
            if 0 < price < min_price:
                return False                      # 초저가 잡주 배제
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
                return
            codes: list[str] = []
            note = ""
            if self.selector is not None:
                try:
                    picked, note = self.selector(rows, slots)
                    valid = {r["code"] for r in rows}
                    codes = [c for c in picked if c in valid][:slots]
                except Exception as e:
                    logger.warning("AI 선정 실패, 모멘텀 상위로 대체: %s", e)
            if not codes:   # AI 실패/무효 → 등락률 모멘텀 상위로 대체
                ranked = sorted(rows, key=lambda r: self._row_float(r, "change_pct"),
                                reverse=True)
                codes = [r["code"] for r in ranked[:slots]]
                note = note or "AI 선정 불가 — 등락률 모멘텀 상위로 자동 대체"
            with self._scan_lock:
                self._scan_result = (codes, note)
        except Exception as e:
            logger.warning("종목 탐색 오류: %s", e)
        finally:
            self._scan_thread = None

    def _maybe_scan(self, slots: int) -> None:
        if slots < 1 or self._scan_thread is not None:
            return
        if time.monotonic() - self._last_scan < self.config.scan_interval:
            return
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
        for code in codes[:slots]:
            if code in self.positions:
                continue
            try:
                self._buy(code)
            except Exception as e:
                self.last_error = f"{code} 매수 오류: {e}"
                logger.warning("%s 매수 실패: %s", code, e)
                if "불가" in str(e):   # 매수 불가 종목은 당분간 제외
                    self._cooldown[code] = self.clock() + dt.timedelta(hours=4)

    # ── 일일 안전장치 ─────────────────────────────────────
    def _entries_allowed(self) -> bool:
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

    # ── 매매 루프 ─────────────────────────────────────────
    def step(self) -> None:
        if self.session() == "장외":
            return
        self._monitor_positions()
        slots = self.config.max_positions - len(self.positions)
        if slots > 0 and self._entries_allowed():
            self._consume_scan(slots)
            slots = self.config.max_positions - len(self.positions)
            self._maybe_scan(slots)

    def reconcile(self) -> None:
        """실계좌 잔고와 내부 장부를 맞춘다 (미국주식, us_balances 지원 시)."""
        if self.config.market != "us" or not hasattr(self.api, "us_balances"):
            return
        try:
            balances = self.api.us_balances()
        except Exception as e:
            logger.warning("잔고 동기화 실패(다음 주기에 재시도): %s", e)
            return
        for code in list(self.positions):
            pos = self.positions[code]
            actual = balances.get(code, {}).get("qty", pos.quantity)
            if actual == pos.quantity:
                continue
            ref = pos.last_price or pos.entry_price
            diff = pos.quantity - actual
            self.cash += diff * ref
            self._log(f"잔고 동기화: {code} {pos.quantity}주 → 실제 {actual}주 "
                      f"(현금 {diff * ref:+,.2f} 보정)")
            if actual == 0:
                del self.positions[code]
            else:
                pos.quantity = actual

    def run(self) -> None:
        self._running = True
        cfg = self.config
        self._log(f"AI 단타 시작: {'미국' if cfg.market == 'us' else '국내'} / "
                  f"목표 +{cfg.target_pct}% · 손절 -{cfg.stop_pct}% / "
                  f"동시 {cfg.max_positions}종목 / 총 {cfg.total_cash:,.0f}")
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
