"""지정 종목 분할 매매.

사용자가 종목·매수 지정가·총 금액을 정하면, 지정가에서 시작해 간격(기본 1%)만큼
내려갈 때마다 한 회차씩(기본 5회) 나눠 산다. 회차당 금액은 총 금액 ÷ 횟수이며,
어떤 경우에도 계획 가격보다 비싸게 사지 않으므로 쓴 돈이 총 금액을 넘지 않는다.

파는 방식은 두 가지다.

- each   : 회차별 매도. 각 회차를 자기 매수가보다 간격만큼 오른 가격에 판다.
           반복을 켜면 팔린 회차를 같은 가격에서 다시 산다.
- ladder : 매도 지정가 분할. 매도 지정가부터 간격만큼 올라갈 때마다 보유
           수량을 남은 회차 수로 나눠 판다. 이미 가진 주식에 분할 매도만 걸 수도
           있다(보유분 매도 수량).

AI 단타(scalper)와는 별개로 돌며, 같은 종목을 함께 다루지 않는다. 주문은 현재가가
그 가격에 닿은 뒤에 낸다(미리 걸어 두지 않는다). 미국주식은 호가가 맞으면 바로
체결되게 내고, 아니면 그 가격에 지정가로 걸어 둔 채 1분마다 다시 낸다.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from .scalper import kr_session, us_session

logger = logging.getLogger(__name__)

MODES = {"each": "회차별 매도", "ladder": "매도 지정가 분할"}
MAX_JOBS = 5
RUNNING, PAUSED, DONE, STOPPED, CUT = "진행", "멈춤", "완료", "종료", "손절"


@dataclass
class Level:
    """매수 회차 하나."""
    buy: float                # 매수가 (이 가격 이하에서만 산다)
    qty: int                  # 계획 수량
    sell: float = 0.0         # 매도가 (each 방식)
    bought: int = 0           # 이번 회전에 산 수량
    held: int = 0             # 지금 들고 있는 수량
    cost: float = 0.0         # 들고 있는 수량의 매입 금액
    rounds: int = 0           # 사고판 횟수
    done: bool = False        # 반복하지 않는 회차가 한 번 팔렸다


@dataclass
class SplitJob:
    code: str
    mode: str
    base_price: float
    budget: float
    steps: int = 5
    gap_pct: float = 1.0
    repeat: bool = False
    sell_start: float = 0.0
    stop_pct: float = 0.0
    levels: list = field(default_factory=list)        # list[Level]
    sell_prices: list = field(default_factory=list)   # ladder 방식 매도가
    sell_done: int = 0        # ladder: 끝난 매도 회차 수
    carry: int = 0            # ladder: 지금 회차에서 아직 못 판 수량
    carry_upto: int = 0
    extra_held: int = 0       # 편입한 기존 보유분
    extra_cost: float = 0.0
    baseline_qty: int | None = None   # 이 작업과 무관하게 계좌에 있던 수량
    realized: float = 0.0
    fees: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    rounds: int = 0
    status: str = RUNNING
    closing: str = ""         # liquidate(전량 매도 후 종료) / stop(손절)
    pending: dict | None = None
    last_price: float = 0.0
    note: str = ""
    missed_buy: int = 0       # 체결이 안 보였던 매수 수량 (잔고에 늦게 나타나면 편입)
    missed_price: float = 0.0
    missed_levels: list = field(default_factory=list)
    wait_until: str = ""      # 이 시각까지 새 주문을 내지 않는다 (ISO)
    short_count: int = 0      # 계좌 수량이 장부보다 적게 보인 연속 횟수
    created_at: str = ""

    @property
    def held(self) -> int:
        return self.extra_held + sum(lv.held for lv in self.levels)

    @property
    def cost(self) -> float:
        return self.extra_cost + sum(lv.cost for lv in self.levels)

    @property
    def active(self) -> bool:
        return self.status == RUNNING


def market_of_code(code: str, fallback: str = "") -> str:
    """종목코드로 시장을 알아낸다: 영문(SOXL, BRK.B)은 미국, 숫자로 시작하는
    6자리(005930, 0001A0)는 국내. 어느 쪽도 아니면 fallback."""
    code = str(code or "").strip().upper()
    if re.fullmatch(r"[A-Z][A-Z.\-]{0,6}", code):
        return "us"
    if re.fullmatch(r"[0-9][0-9A-Z]{5}", code):
        return "kr"
    return fallback


def _round_price(price: float, market: str) -> float:
    return float(round(price)) if market == "kr" else round(price, 2)


def build_plan(base_price: float, budget: float, steps: int = 5,
               gap_pct: float = 1.0, mode: str = "each",
               sell_start: float = 0.0, market: str = "us",
               hold_qty: int = 0) -> tuple[list[Level], list[float]]:
    """(매수 회차, 매도가 목록)을 만든다. 조건이 맞지 않으면 ValueError."""
    if mode not in MODES:
        raise ValueError(f"매도 방식을 알 수 없다: {mode}")
    if not 1 <= steps <= 10:
        raise ValueError("분할 횟수는 1~10회여야 한다")
    if not 0.1 <= gap_pct <= 20:
        raise ValueError("가격 간격은 0.1~20% 사이여야 한다")
    if budget < 0 or hold_qty < 0:
        raise ValueError("금액과 수량은 0 이상이어야 한다")
    if hold_qty and mode != "ladder":
        raise ValueError("보유분 매도 수량을 쓰려면 매도 지정가를 넣어야 한다")
    if budget == 0 and hold_qty == 0:
        raise ValueError("총 금액을 넣어야 한다 (보유분만 팔려면 보유분 매도 "
                         "수량을 넣는다)")
    levels: list[Level] = []
    if budget > 0:
        if base_price <= 0:
            raise ValueError("매수 지정가를 넣어야 한다")
        if gap_pct * (steps - 1) >= 100:
            raise ValueError("간격 × 횟수가 너무 커서 매수가가 0 이하가 된다")
        per_step = budget / steps
        for k in range(steps):
            buy = _round_price(base_price * (1 - gap_pct * k / 100), market)
            qty = int(per_step // buy) if buy > 0 else 0
            if qty < 1:
                raise ValueError(
                    f"회차당 금액 {per_step:,.2f}으로는 {k + 1}회차 가격 "
                    f"{buy:,.2f}에 1주도 살 수 없다 — 총 금액을 늘리거나 횟수를 "
                    f"줄여야 한다")
            sell = (_round_price(buy * (1 + gap_pct / 100), market)
                    if mode == "each" else 0.0)
            levels.append(Level(buy=buy, qty=qty, sell=sell))
    sell_prices: list[float] = []
    if mode == "ladder":
        if sell_start <= 0:
            raise ValueError("매도 지정가를 넣어야 한다")
        if levels and sell_start <= base_price:
            raise ValueError("매도 지정가는 매수 지정가보다 높아야 한다 "
                             "(낮으면 사자마자 손해 보고 판다)")
        sell_prices = [_round_price(sell_start * (1 + gap_pct * j / 100), market)
                       for j in range(steps)]
    return levels, sell_prices


class SplitTrader:
    """지정 종목 분할 매매 엔진. 한 시장(us/kr)의 작업들을 돌린다."""

    def __init__(self, api, market: str = "us", is_simulation: bool = True,
                 allow_real: bool = False, fee_pct: float = 0.0,
                 sell_tax_pct: float = 0.0, us_day_session: bool = True,
                 clock: Callable[[], dt.datetime] = dt.datetime.now,
                 utc_clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
                 sleep: Callable[[float], None] = time.sleep,
                 state_path: str | None = None, poll_interval: float = 3.0,
                 fill_timeout_sec: float = 20.0, rest_timeout_sec: float = 60.0):
        if not is_simulation and not allow_real:
            raise PermissionError(
                "실전투자 접속이 감지되었다. 모의투자로 검증하거나 allow_real을 "
                "명시해야 한다.")
        self.api = api
        self.market = market
        self.fee_pct = fee_pct
        self.sell_tax_pct = sell_tax_pct
        self.us_day_session = us_day_session
        self.clock = clock
        self.utc_clock = utc_clock
        self.sleep = sleep
        self.state_path = state_path
        self.poll_interval = poll_interval
        self.fill_timeout_sec = fill_timeout_sec
        # 회차 가격에 걸어 둔 지정가 주문을 거두고 다시 내기까지의 시간
        self.rest_timeout_sec = rest_timeout_sec
        self._said: dict[str, str] = {}     # 같은 안내를 되풀이해 적지 않는다
        self.jobs: dict[str, SplitJob] = {}
        self.events: list[str] = []
        self.last_error = ""
        self.buy_paused = False       # True면 새로 사지 않는다 (매도는 계속)
        self._verify = (market == "us" and hasattr(api, "us_open_orders")
                        and hasattr(api, "cancel_order"))
        self._balances = hasattr(api, "us_balances") and market == "us"
        self._lock = threading.RLock()
        self._requests: list[tuple[str, str]] = []
        self._running = False
        self._last_reconcile: dt.datetime | None = None

    # ── 공통 ──────────────────────────────────────────────
    def session(self) -> str:
        if self.market == "us":
            return us_session(self.utc_clock(), self.us_day_session, True)
        return kr_session(self.clock(), True)

    def _log(self, message: str) -> None:
        self.events.append(f"{self.clock().strftime('%H:%M:%S')} {message}")
        del self.events[:-200]
        logger.info(message)

    def _log_once(self, job: SplitJob, message: str) -> None:
        """같은 주문을 다시 낼 때마다 같은 줄이 쌓이지 않게 한 번만 적는다."""
        if self._said.get(job.code) != message:
            self._said[job.code] = message
            self._log(message)

    def codes(self) -> set[str]:
        """이 엔진이 다루는 종목 (AI 단타가 건드리지 않게 막는 데 쓴다)."""
        with self._lock:
            return {code for code, job in self.jobs.items()
                    if job.status in (RUNNING, PAUSED)}

    # ── 작업 등록 / 사용자 요청 ───────────────────────────
    def add_job(self, code: str, base_price: float, budget: float,
                steps: int = 5, gap_pct: float = 1.0, mode: str = "each",
                sell_start: float = 0.0, repeat: bool = False,
                stop_pct: float = 0.0, hold_qty: int = 0) -> SplitJob:
        code = (code or "").strip().upper()
        if not code:
            raise ValueError("종목코드가 비어 있다")
        if not 0 <= stop_pct <= 50:
            raise ValueError("손절 %는 0(없음)~50 사이여야 한다")
        levels, sell_prices = build_plan(base_price, budget, steps, gap_pct,
                                         mode, sell_start, self.market, hold_qty)
        old = self.jobs.get(code)
        if old is not None and old.status in (RUNNING, PAUSED):
            raise ValueError(f"{code}는 이미 분할 매매 중이다 — 먼저 끝내야 한다")
        if len(self.codes()) >= MAX_JOBS:
            raise ValueError(f"분할 매매는 동시에 {MAX_JOBS}종목까지다")
        baseline, extra_cost = None, 0.0
        if self._balances:
            holding = self.api.us_balances().get(code, {})
            actual = int(holding.get("qty") or 0)
            if hold_qty > actual:
                raise ValueError(f"{code} 계좌 보유는 {actual}주다 — 보유분 매도 "
                                 f"수량({hold_qty}주)이 더 많다")
            baseline = actual - hold_qty
            extra_cost = hold_qty * float(holding.get("avg_price") or 0)
        elif hold_qty:
            raise ValueError("이 시장은 계좌 보유를 확인할 수 없어 보유분 매도를 "
                             "쓸 수 없다 (미국주식만 가능)")
        job = SplitJob(code=code, mode=mode, base_price=base_price,
                       budget=budget, steps=steps, gap_pct=gap_pct,
                       repeat=bool(repeat) and budget > 0,
                       sell_start=sell_start, stop_pct=stop_pct, levels=levels,
                       sell_prices=sell_prices, extra_held=hold_qty,
                       extra_cost=extra_cost, baseline_qty=baseline,
                       created_at=self.clock().isoformat(timespec="seconds"))
        with self._lock:
            self.jobs[code] = job
            buys = ", ".join(f"{lv.buy:,.2f}({lv.qty}주)" for lv in levels)
            sells = (", ".join(f"{lv.sell:,.2f}" for lv in levels)
                     if mode == "each"
                     else ", ".join(f"{p:,.2f}" for p in sell_prices))
            self._log(f"분할 매매 등록: {code} [{MODES[mode]}"
                      f"{' · 반복' if job.repeat else ''}] "
                      + (f"매수 {buys} / " if buys else
                         f"보유분 {hold_qty}주 / ")
                      + f"매도 {sells}"
                      + (f" / 손절 -{stop_pct}%" if stop_pct else " / 손절 없음"))
            self._save_state()
        return job

    def request(self, code: str, action: str) -> None:
        """action: pause | resume | stop(주문만 끝내고 보유는 둔다) |
        liquidate(전량 매도 후 종료) | remove(끝난 작업을 목록에서 지운다)."""
        with self._lock:
            self._requests.append(((code or "").strip().upper(), action))

    def _process_requests(self) -> None:
        requests, self._requests = self._requests, []
        for code, action in requests:
            job = self.jobs.get(code)
            if job is None:
                continue
            if action == "pause" and job.status == RUNNING:
                job.status = PAUSED
                self._log(f"분할 매매 일시정지: {code} — 주문을 내지 않는다")
            elif action == "resume" and job.status == PAUSED:
                job.status = RUNNING
                job.note = ""
                self._log(f"분할 매매 재개: {code}")
            elif action == "stop" and job.status in (RUNNING, PAUSED):
                why = (f"보유 {job.held}주는 계좌에 그대로 둔다"
                       if job.held else "")
                if job.pending:
                    self._cancel_pending(job)
                    why = (why + " · " if why else "") + \
                        "확인 중이던 주문은 취소를 요청했다(계좌에서 확인 필요)"
                self._finish(job, STOPPED, why)
            elif action == "liquidate" and job.status in (RUNNING, PAUSED):
                if job.held == 0 and not job.pending:
                    self._finish(job, STOPPED)
                else:
                    job.status = RUNNING
                    job.closing = "liquidate"
                    self._log(f"분할 매매 정리 요청: {code} — 보유 {job.held}주를 "
                              f"모두 팔고 끝낸다")
            elif action == "remove" and job.status not in (RUNNING, PAUSED):
                del self.jobs[code]
            self._save_state()

    def _finish(self, job: SplitJob, status: str, why: str = "") -> None:
        job.status = status
        job.closing = ""
        job.pending = None
        self._log(f"분할 매매 {status}: {job.code} — 실현손익 "
                  f"{job.realized:+,.2f}"
                  f"{f' (수수료 {job.fees:,.2f} 차감)' if job.fees else ''}"
                  f"{' · ' + why if why else ''}")
        self._save_state()

    # ── 호가 ──────────────────────────────────────────────
    def _quote(self, code: str, price: float) -> tuple[float, float] | None:
        """(매수호가, 매도호가). 호가를 볼 수 없는 API면 현재가를 쓴다."""
        if not hasattr(self.api, "best_bid_ask"):
            return price, price
        try:
            bid, ask = self.api.best_bid_ask(code)
        except Exception as e:      # 호가를 모르면 주문하지 않는다
            logger.info("%s 호가 조회 실패(이번 주기는 건너뜀): %s", code, e)
            return None
        return bid, ask

    def _kr_route(self) -> str:
        return "KRX" if kr_session(self.clock(), False) == "정규장" else "NXT"

    # ── 주문 ──────────────────────────────────────────────
    def _send(self, job: SplitJob, side: str, qty: int, price: float,
              idxs: list[int] | None, strict: bool) -> None:
        now = self.clock()
        if side == "sell" and self._balances and job.baseline_qty is not None:
            # 계좌에 실제로 있는 이 작업 몫까지만 판다 (사용자의 다른 보유분 보호)
            try:
                actual = int(self.api.us_balances().get(job.code, {})
                             .get("qty") or 0)
            except Exception as e:
                job.note = f"잔고 조회 실패로 매도를 미뤘다: {str(e)[:60]}"
                return
            # 장부보다 적게 보이면 보이는 만큼만 판다. 장부를 줄이는 일은 잔고
            # 동기화가 두 번 확인한 뒤에 한다(잔고 반영이 늦었을 수 있다)
            qty = min(qty, max(0, actual - job.baseline_qty))
            if qty < 1:
                job.note = "계좌에 팔 수량이 보이지 않아 매도를 미뤘다"
                job.wait_until = (now + dt.timedelta(seconds=20)).isoformat()
                return
        limit = price if self._verify else None
        try:
            ord_no = self.api.send_market_order(
                "", job.code, side, qty, kr_exchange=self._kr_route(),
                limit_price=limit)
        except Exception as e:
            job.note = f"주문 실패: {str(e)[:80]}"
            job.wait_until = (now + dt.timedelta(seconds=30)).isoformat()
            self._log(f"분할 {'매수' if side == 'buy' else '매도'} 주문 실패"
                      f"({job.code}): {str(e)[:80]} — 30초 뒤 다시 본다")
            return
        job.note = ""
        ord_no = ord_no if isinstance(ord_no, str) else ""
        if not self._verify:
            self._book(job, side, qty, price, idxs, strict, ord_no)
            return
        job.pending = {"side": side, "qty": qty, "price": price,
                       "ord_no": ord_no, "sent_at": now.isoformat(),
                       "idxs": idxs, "strict": strict, "unconfirmed": 0}
        self._log_once(job, f"분할 {'매수' if side == 'buy' else '매도'} 주문: "
                            f"{job.code} {qty}주 @{price:,.2f} 지정가 — 체결 "
                            f"확인 중 (안 받아지면 1분마다 다시 낸다)"
                       if strict else
                       f"분할 매도 주문(정리): {job.code} {qty}주 "
                       f"@{price:,.2f} — 체결 확인 중")
        self._save_state()

    def _fill_price(self, ord_no: str, expected: float) -> float:
        fetch = getattr(self.api, "order_fill", None)
        if fetch is None or not ord_no:
            return expected
        try:
            got = fetch(ord_no)
        except Exception:
            return expected
        if not got or got[1] <= 0 or abs(got[1] / expected - 1) > 0.03:
            return expected
        return float(got[1])

    def _fee(self, buy_amount: float, sell_amount: float) -> float:
        return ((buy_amount + sell_amount) * self.fee_pct
                + sell_amount * self.sell_tax_pct) / 100

    def _book(self, job: SplitJob, side: str, filled: int, price: float,
              idxs: list[int] | None, strict: bool, ord_no: str = "") -> None:
        """체결분을 장부에 넣는다."""
        job.pending = None
        if filled <= 0:
            job.note = (f"{'매수' if side == 'buy' else '매도'} 주문 "
                        f"@{price:,.2f}이 아직 체결되지 않았다 — 가격이 그 자리에 "
                        f"있으면 다시 낸다")
            self._save_state()
            return
        self._said.pop(job.code, None)
        fill = self._fill_price(ord_no, price)
        if side == "buy":
            left = filled
            for i in idxs or []:
                lv = job.levels[i]
                take = min(left, lv.qty - lv.bought)
                lv.bought += take
                lv.held += take
                lv.cost += take * fill
                left -= take
            job.buy_count += 1
            self._log(f"분할 매수: {job.code} {filled}주 @{fill:,.2f} "
                      f"({self._level_names(idxs)}) — 보유 {job.held}주, 평균 "
                      f"{job.cost / job.held:,.2f}")
            self._save_state()
            return
        # 매도: 팔 몫을 회차에서 덜어 내고 손익을 계산한다
        left, gross, fee = filled, 0.0, 0.0
        lots: list = []
        if strict and job.mode == "each":
            lots = [job.levels[i] for i in sorted(idxs or [], reverse=True)]
        else:
            if job.extra_held:
                take = min(left, job.extra_held)
                avg = job.extra_cost / job.extra_held
                gross += (fill - avg) * take
                fee += self._fee(0.0, fill * take)   # 편입분은 매도 수수료만
                job.extra_held -= take
                job.extra_cost -= avg * take
                left -= take
            lots = list(job.levels)
        for lv in lots:
            if left <= 0:
                break
            take = min(left, lv.held)
            if take <= 0:
                continue
            avg = lv.cost / lv.held
            gross += (fill - avg) * take
            fee += self._fee(avg * take, fill * take)
            lv.held -= take
            lv.cost -= avg * take
            left -= take
        job.realized += gross - fee
        job.fees += fee
        job.sell_count += 1
        self._log(f"분할 매도: {job.code} {filled}주 @{fill:,.2f} — 손익 "
                  f"{gross - fee:+,.2f}, 남은 보유 {job.held}주, 누적 "
                  f"{job.realized:+,.2f}")
        self._after_sell(job, filled, idxs, strict)
        self._save_state()

    def _after_sell(self, job: SplitJob, filled: int, idxs: list[int] | None,
                    strict: bool) -> None:
        if job.closing:
            if job.held == 0:
                self._finish(job, CUT if job.closing == "stop" else STOPPED)
            return
        if job.mode == "each":
            for i in idxs or []:
                lv = job.levels[i]
                if lv.held == 0 and lv.bought > 0:
                    lv.rounds += 1
                    if job.repeat:
                        lv.bought = 0
                    else:
                        lv.done = True
            job.rounds = min((lv.rounds for lv in job.levels), default=0)
            if job.levels and all(lv.done for lv in job.levels):
                self._finish(job, DONE, "모든 회차가 한 번씩 사고팔렸다")
            return
        # ladder
        job.carry = max(0, job.carry - filled)
        if job.carry == 0 or job.held == 0:
            last = len(job.sell_prices) - 1
            if job.carry_upto >= last and job.held > 0:
                job.carry = job.held        # 마지막 회차는 남김없이 판다
            else:
                job.sell_done = job.carry_upto + 1
                job.carry = 0
        if job.held == 0:
            job.rounds += 1
            if job.repeat and job.levels:
                for lv in job.levels:
                    lv.bought, lv.rounds = 0, lv.rounds + 1
                job.sell_done = job.carry = 0
                self._log(f"분할 매매 한 바퀴 완료: {job.code} — 처음부터 다시 "
                          f"시작한다")
            else:
                self._finish(job, DONE, "보유분을 모두 팔았다")

    @staticmethod
    def _level_names(idxs: list[int] | None) -> str:
        return "·".join(f"{i + 1}회차" for i in idxs or []) or "-"

    def _shrink(self, job: SplitJob, count: int) -> None:
        """계좌에 없는 수량을 장부에서 덜어 낸다 (낮은 가격 회차부터)."""
        left = count
        for lv in reversed(job.levels):
            take = min(left, lv.held)
            if take > 0:
                lv.cost -= lv.cost / lv.held * take
                lv.held -= take
                left -= take
                if lv.held == 0 and job.mode == "each":
                    lv.done = True       # 밖에서 팔린 회차는 다시 사지 않는다
        if left > 0 and job.extra_held:
            take = min(left, job.extra_held)
            job.extra_cost -= job.extra_cost / job.extra_held * take
            job.extra_held -= take
        self._log(f"분할 매매 수량 조정: {job.code} — 계좌에 없는 {count}주를 "
                  f"장부에서 뺐다 (보유 {job.held}주)")
        self._save_state()

    # ── 체결 확인 (미국 지정가) ───────────────────────────
    @staticmethod
    def _norm(no) -> str:
        return str(no or "").strip().lstrip("0")

    def _find_order(self, orders: list[dict], job: SplitJob):
        ord_no = job.pending.get("ord_no") or ""
        if ord_no:
            return next((o for o in orders
                         if self._norm(o.get("ord_no")) == self._norm(ord_no)),
                        None)
        return next((o for o in orders if o.get("code") == job.code), None)

    def _cancel_pending(self, job: SplitJob) -> None:
        if not job.pending or not self._verify:
            return
        try:
            self.api.cancel_order(job.pending.get("ord_no", ""), job.code)
        except Exception as e:
            logger.info("%s 분할 주문 취소 실패(이미 끝났을 수 있다): %s",
                        job.code, e)

    def _check_pending(self, jobs: list[SplitJob]) -> None:
        try:
            orders = self.api.us_open_orders()
        except Exception as e:
            self.last_error = f"미체결 조회 실패: {e}"
            return
        now = self.clock()
        for job in jobs:
            try:
                self._check_one(job, orders, now)
            except Exception as e:
                self.last_error = f"{job.code} 체결 확인 오류: {e}"
                logger.warning("%s 분할 체결 확인 오류: %s", job.code, e)

    def _check_one(self, job: SplitJob, orders: list[dict],
                   now: dt.datetime) -> None:
        p = job.pending
        side, qty = p["side"], int(p["qty"])
        order = self._find_order(orders, job)
        if order is not None:
            sent = dt.datetime.fromisoformat(p["sent_at"])
            timeout = (self.rest_timeout_sec if p.get("strict")
                       else self.fill_timeout_sec)
            if (now - sent).total_seconds() < timeout:
                return
            try:
                self.api.cancel_order(order["ord_no"], job.code)
            except Exception as e:  # 그사이 체결됐을 수 있다 — 다음 주기에 재확인
                logger.warning("%s 분할 주문 취소 실패: %s", job.code, e)
                return
            remaining = int(order.get("remaining") or 0)
            if remaining <= 0:
                remaining = qty - int(order.get("filled") or 0)
            filled = qty - max(0, min(qty, remaining))
            self._book(job, side, filled, p["price"], p["idxs"], p["strict"],
                       p["ord_no"])
            return
        # 미체결 목록에서 사라졌다 — 체결 내역, 없으면 실계좌 보유로 확인한다
        filled = qty
        report = None
        fetch = getattr(self.api, "order_fill", None)
        if fetch is not None and p.get("ord_no"):
            try:
                report = fetch(p["ord_no"])
            except Exception as e:
                logger.info("%s 분할 체결 조회 실패: %s", job.code, e)
        if report and report[0] > 0:
            filled = min(qty, int(report[0]))
        elif self._balances and job.baseline_qty is not None:
            try:
                actual = int(self.api.us_balances().get(job.code, {})
                             .get("qty") or 0)
            except Exception as e:
                logger.info("%s 잔고 조회 실패(다음 주기에 재확인): %s",
                            job.code, e)
                return
            mine = max(0, actual - job.baseline_qty)
            change = mine - job.held if side == "buy" else job.held - mine
            filled = max(0, min(qty, change))
            if filled < qty:
                p["unconfirmed"] = int(p.get("unconfirmed") or 0) + 1
                if p["unconfirmed"] < 3:          # 잔고 반영 지연에 속지 않게
                    return
                short = qty - filled
                if side == "buy":
                    # 안 산 것으로 적되, 잔고에 늦게 나타나면 장부에 넣는다
                    job.missed_buy += short
                    job.missed_price = p["price"]
                    job.missed_levels = list(p["idxs"] or [])
                # 반영이 늦었을 수 있으니 같은 주문을 곧바로 다시 내지 않는다
                job.wait_until = (now + dt.timedelta(seconds=60)).isoformat()
        self._book(job, side, filled, p["price"], p["idxs"], p["strict"],
                   p["ord_no"])

    def _reconcile(self) -> None:
        """실계좌 보유와 장부를 맞춘다 (미국주식, 1분마다)."""
        now = self.clock()
        if not self._balances:
            return
        if (self._last_reconcile is not None
                and (now - self._last_reconcile).total_seconds() < 60):
            return
        jobs = [j for j in self.jobs.values()
                if j.active and not j.pending and j.baseline_qty is not None]
        if not jobs:
            return
        self._last_reconcile = now
        try:
            balances = self.api.us_balances()
        except Exception as e:
            logger.info("분할 매매 잔고 동기화 실패(다음에 재시도): %s", e)
            return
        for job in jobs:
            actual = int(balances.get(job.code, {}).get("qty") or 0)
            mine = max(0, actual - job.baseline_qty)
            if mine > job.held and job.missed_buy:
                absorb = min(job.missed_buy, mine - job.held)
                idxs, price = list(job.missed_levels), job.missed_price
                job.missed_buy, job.missed_levels = 0, []
                self._log(f"잔고 동기화: {job.code} 매수 {absorb}주가 뒤늦게 "
                          f"확인됐다 — 장부에 넣는다")
                self._book(job, "buy", absorb, price, idxs, True)
                job.short_count = 0
            elif mine < job.held:
                job.short_count += 1
                if job.short_count >= 2:          # 두 번 연속 확인한 뒤에만
                    job.short_count = 0
                    self._shrink(job, job.held - mine)
                    if job.held == 0 and not any(
                            not lv.done and lv.bought < lv.qty
                            for lv in job.levels):
                        self._finish(job, DONE, "보유분이 계좌에서 모두 나갔다")
            else:
                job.short_count = 0

    # ── 매매 판단 ─────────────────────────────────────────
    def _drive(self, job: SplitJob) -> None:
        now = self.clock()
        if job.wait_until:
            if now < dt.datetime.fromisoformat(job.wait_until):
                return
            job.wait_until = ""
        try:
            price = self.api.current_price(job.code)
        except Exception as e:
            job.note = f"시세 조회 실패: {str(e)[:60]}"
            return
        job.last_price = price
        job.note = ""
        held = job.held
        if job.closing:
            if held == 0:
                self._finish(job, CUT if job.closing == "stop" else STOPPED)
            else:
                self._drive_closing(job, price)
            return
        if job.stop_pct > 0 and held > 0 and job.levels:
            floor = job.levels[-1].buy * (1 - job.stop_pct / 100)
            if price <= floor:
                job.closing = "stop"
                self._log(f"분할 매매 손절: {job.code} 현재 {price:,.2f} ≤ 손절선 "
                          f"{floor:,.2f} — 보유 {held}주를 모두 판다")
                self._drive_closing(job, price)
                return
        if held > 0 and self._try_sell(job, price):
            return
        self._try_buy(job, price)

    def _drive_closing(self, job: SplitJob, price: float) -> None:
        quote = self._quote(job.code, price)
        bid = quote[0] if quote else 0.0
        limit = max(bid, price * 0.995) if bid > 0 else price * 0.999
        self._send(job, "sell", job.held, _round_price(limit, self.market),
                   None, strict=False)

    def _try_sell(self, job: SplitJob, price: float) -> bool:
        """현재가가 매도가에 닿은 몫을 판다. 주문을 냈으면 True.

        미국(지정가)은 매수호가가 매도가 이상이면 그 호가에, 아니면 매도가에
        지정가로 걸어 둔다. 시장가로 나가는 경로는 매수호가가 매도가 이상일
        때만 판다. 어느 쪽이든 정한 매도가보다 싸게 팔지 않는다.
        """
        if job.mode == "each":
            idxs = [i for i, lv in enumerate(job.levels)
                    if lv.held > 0 and price >= lv.sell]
            if not idxs:
                return False
            quote = self._quote(job.code, price)
            top = max(job.levels[i].sell for i in idxs)
            if self._verify:
                bid = quote[0] if quote else 0.0
                limit = bid if bid >= top else top
            else:
                if quote is None:
                    job.note = "호가를 받지 못해 매도를 미뤘다"
                    return True
                bid = quote[0]
                idxs = [i for i in idxs if bid >= job.levels[i].sell]
                if not idxs:
                    job.note = (f"매수호가 {bid:,.2f}가 매도가보다 낮아 "
                                f"기다린다")
                    return False
                limit = bid
            self._send(job, "sell", sum(job.levels[i].held for i in idxs),
                       limit, idxs, strict=True)
            return True
        n, j = len(job.sell_prices), job.sell_done
        if j >= n or price < job.sell_prices[j]:
            return False
        quote = self._quote(job.code, price)
        if self._verify:
            bid = quote[0] if quote else 0.0
            reached = price
        else:
            if quote is None:
                job.note = "호가를 받지 못해 매도를 미뤘다"
                return True
            bid = reached = quote[0]
            if bid < job.sell_prices[j]:
                job.note = f"매수호가 {bid:,.2f}가 매도가보다 낮아 기다린다"
                return False
        if job.carry <= 0:
            upto = j
            while upto + 1 < n and reached >= job.sell_prices[upto + 1]:
                upto += 1
            due = upto - j + 1
            job.carry = min(job.held, -(-job.held * due // (n - j)))
            job.carry_upto = upto
        limit = bid
        if self._verify:
            top = job.sell_prices[min(job.carry_upto, n - 1)]
            limit = bid if bid >= top else top
        self._send(job, "sell", min(job.carry, job.held), limit, None,
                   strict=True)
        return True

    def _try_buy(self, job: SplitJob, price: float) -> None:
        """현재가가 매수가에 닿은 회차를 산다.

        미국(지정가)은 매도호가가 회차 가격 이하면 그 호가에 사고, 아니면 회차
        가격에 지정가로 걸어 둔다(걸어 둔 주문은 rest_timeout_sec 뒤 거두고,
        가격이 그대로면 다시 건다). 시장가로 나가는 경로는 매도호가가 회차 가격
        이하일 때만 산다. 어느 쪽이든 회차 가격보다 비싸게 사지 않는다.
        """
        def wanted(limit: float) -> list[int]:
            return [i for i, lv in enumerate(job.levels)
                    if not lv.done and lv.bought < lv.qty and limit <= lv.buy]

        idxs = wanted(price)
        if not idxs:
            return
        if self.buy_paused:
            job.note = "매수 중단 상태라 새로 사지 않는다"
            return
        quote = self._quote(job.code, price)
        if self._verify:
            lowest = min(job.levels[i].buy for i in idxs)
            ask = quote[1] if quote else 0.0
            limit = ask if 0 < ask <= lowest else lowest
        else:
            if quote is None:
                job.note = "호가를 받지 못해 매수를 미뤘다"
                return
            ask = quote[1]
            idxs = wanted(ask)
            if not idxs:
                job.note = f"매도호가 {ask:,.2f}가 매수가보다 높아 기다린다"
                return
            limit = ask
        qty = sum(job.levels[i].qty - job.levels[i].bought for i in idxs)
        self._send(job, "buy", qty, limit, idxs, strict=True)

    # ── 루프 ──────────────────────────────────────────────
    def step(self) -> None:
        with self._lock:
            self._process_requests()
            if self.session() == "장외":
                return
            waiting = [j for j in self.jobs.values() if j.pending]
            if waiting and self._verify:
                for job in waiting:         # 기다리는 동안에도 현재가는 보여 준다
                    try:
                        job.last_price = self.api.current_price(job.code)
                    except Exception:
                        pass
                self._check_pending(waiting)
            for job in list(self.jobs.values()):
                if not job.active or job.pending:
                    continue
                try:
                    self._drive(job)
                except Exception as e:
                    job.note = f"오류: {str(e)[:80]}"
                    self.last_error = f"{job.code} 분할 매매 오류: {e}"
                    logger.exception("%s 분할 매매 오류: %s", job.code, e)
            self._reconcile()

    def run(self) -> None:
        self._running = True
        while self._running:
            try:
                self.step()
            except Exception as e:      # 루프는 어떤 오류에도 살아남는다
                self.last_error = f"분할 매매 루프 오류: {e}"
                logger.exception("분할 매매 루프 오류, 계속 진행: %s", e)
            self.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False

    # ── 상태 저장 / 복원 ──────────────────────────────────
    def _save_state(self) -> None:
        if not self.state_path:
            return
        data = {"market": self.market,
                "saved_at": self.clock().isoformat(timespec="seconds"),
                "jobs": [asdict(job) for job in self.jobs.values()]}
        try:
            tmp = f"{self.state_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)
        except OSError as e:
            logger.warning("분할 매매 상태 저장 실패: %s", e)

    def restore_state(self) -> int:
        """이전 실행의 작업을 불러온다. 진행 중이던 작업은 '멈춤'으로 돌아오며,
        사용자가 재개를 눌러야 다시 주문을 낸다."""
        if not self.state_path or not os.path.exists(self.state_path):
            return 0
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning("분할 매매 상태 복원 실패: %s", e)
            return 0
        if data.get("market") != self.market:
            return 0
        count = 0
        for item in data.get("jobs") or []:
            try:
                levels = [Level(**lv) for lv in item.pop("levels", [])]
                job = SplitJob(**{**item, "levels": levels})
            except TypeError as e:
                logger.warning("분할 매매 작업 복원 실패: %s", e)
                continue
            if (market_of_code(job.code, self.market) != self.market
                    and job.held == 0 and not job.pending):
                # 예전 버전에서 다른 시장으로 잘못 등록돼 한 주도 사지 못한 작업
                self._log(f"분할 매매 작업을 지웠다: {job.code} — 종목코드와 시장이 "
                          f"맞지 않아 주문이 나갈 수 없던 작업이다. 다시 등록한다")
                continue
            pending, job.pending = job.pending, None
            if pending and pending.get("side") == "buy":
                # 꺼지기 직전에 낸 매수는 체결 여부를 모른다 — 잔고에 보이면 넣는다
                job.missed_buy += int(pending.get("qty") or 0)
                job.missed_price = float(pending.get("price") or 0)
                job.missed_levels = list(pending.get("idxs") or [])
            if job.status == RUNNING:
                job.status = PAUSED
                job.note = "프로그램이 다시 켜졌다 — 재개를 눌러야 매매한다"
            job.wait_until = ""
            self.jobs[job.code] = job
            count += 1
        if count:
            self._log(f"분할 매매 작업 {count}건을 불러왔다 (멈춤 상태)")
        return count

    # ── 화면 표시 ─────────────────────────────────────────
    def snapshot(self) -> list[dict]:
        out = []
        for job in list(self.jobs.values()):
            held, cost = job.held, job.cost
            avg = cost / held if held else 0.0
            pending = ""
            if job.pending:
                pending = (f"{'매수' if job.pending['side'] == 'buy' else '매도'} "
                           f"{job.pending['qty']}주 @{job.pending['price']:,.2f} "
                           f"체결 확인 중")
            out.append({
                "code": job.code, "market": self.market, "mode": job.mode,
                "mode_label": MODES[job.mode], "status": job.status,
                "closing": job.closing, "repeat": job.repeat,
                "budget": job.budget, "used": round(cost, 2),
                "gap_pct": job.gap_pct, "stop_pct": job.stop_pct,
                "held": held, "avg": round(avg, 4), "last": job.last_price,
                "unrealized": (round((job.last_price - avg) * held, 2)
                               if held and job.last_price else 0.0),
                "realized": round(job.realized, 2), "fees": round(job.fees, 2),
                "rounds": job.rounds, "pending": pending, "note": job.note,
                "extra_held": job.extra_held,
                "levels": [{"n": i + 1, "buy": lv.buy, "qty": lv.qty,
                            "sell": lv.sell, "held": lv.held,
                            "bought": lv.bought, "rounds": lv.rounds,
                            "done": lv.done}
                           for i, lv in enumerate(job.levels)],
                "sells": [{"n": j + 1, "price": p, "done": j < job.sell_done}
                          for j, p in enumerate(job.sell_prices)],
            })
        return out
