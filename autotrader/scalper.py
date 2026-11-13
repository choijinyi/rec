"""AI 단타 자동매매.

대형주·주요 ETF 가운데 거래가 활발한 후보를 고르고, AI(Claude Opus 5.5)가
분봉 흐름을 보고 진입 시점과 손절가·목표가를 제안한다. 엔진은 그 제안을
검증한 뒤(손절 폭 상한, 손익비, 판단 유효 시간) 1회 손실 한도에 맞춘
수량으로 매수하고, 손절·익절·시간초과로 청산한다. 보유 중에는 AI가 주기적으로
청산 시점을 점검해 손절가를 올리거나 조기 청산을 제안한다.

- AI는 제안만 한다. 주문 집행과 손절은 코드가 맡고, AI가 응답하지 않으면
  새로 사지 않는다(보유 종목은 코드 규칙으로 계속 관리).
- AI는 손절가를 올릴 수만 있다. 넓히거나 없앨 수 없다.
- 안전장치: 종목군 제한, 유동성·호가 간격 필터, 장 마감 가드, 체결 확인,
  당일 손실·횟수 한도, 재매수 대기, 실전 이중 확인.
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

from .indicators import summarize_bars
from .universe import GROUPS, INVERSE, KIND_LABELS, LEVERAGED, group_of

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


def kr_session_end(now: dt.datetime, extended: bool = True) -> dt.datetime | None:
    """지금 국내 장(장전/정규장/장후)이 끝나는 시각. 장외면 None."""
    end = {"장전": KR_REG_OPEN, "정규장": KR_REG_CLOSE,
           "장후": KR_EXT_CLOSE}.get(kr_session(now, extended))
    if end is None:
        return None
    return now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)


def us_session_end(utc_now: dt.datetime, day_session: bool = True,
                   extended: bool = True) -> dt.datetime | None:
    """지금 미국 장(주간거래/프리마켓/정규장/애프터마켓)이 끝나는 UTC 시각."""
    label = us_session(utc_now, day_session, extended)
    if label == "장외":
        return None
    if label == "주간거래":
        kst = utc_now + dt.timedelta(hours=9)
        end_hour = 17 if _us_dst_active(utc_now) else 18
        end_kst = kst.replace(hour=end_hour, minute=0, second=0, microsecond=0)
        return end_kst - dt.timedelta(hours=9)
    offset = -4 if _us_dst_active(utc_now) else -5
    et = utc_now + dt.timedelta(hours=offset)
    end = {"프리마켓": US_OPEN, "정규장": US_CLOSE,
           "애프터마켓": dt.time(20, 0)}[label]
    end_et = et.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    return end_et - dt.timedelta(hours=offset)


def kr_session_start(now: dt.datetime, extended: bool = True) -> dt.datetime | None:
    """지금 국내 장이 시작된 시각. 장외면 None."""
    start = {"장전": KR_EXT_OPEN, "정규장": KR_REG_OPEN,
             "장후": KR_REG_CLOSE}.get(kr_session(now, extended))
    if start is None:
        return None
    return now.replace(hour=start.hour, minute=start.minute, second=0,
                       microsecond=0)


def us_session_start(utc_now: dt.datetime, day_session: bool = True,
                     extended: bool = True) -> dt.datetime | None:
    """지금 미국 장이 시작된 UTC 시각. 장외면 None."""
    label = us_session(utc_now, day_session, extended)
    if label == "장외":
        return None
    if label == "주간거래":
        kst = utc_now + dt.timedelta(hours=9)
        start_hour = 9 if _us_dst_active(utc_now) else 10
        start_kst = kst.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        return start_kst - dt.timedelta(hours=9)
    offset = -4 if _us_dst_active(utc_now) else -5
    et = utc_now + dt.timedelta(hours=offset)
    start = {"프리마켓": dt.time(4, 0), "정규장": US_OPEN,
             "애프터마켓": US_CLOSE}[label]
    start_et = et.replace(hour=start.hour, minute=start.minute, second=0,
                          microsecond=0)
    return start_et - dt.timedelta(hours=offset)


# ── 설정과 상태 ───────────────────────────────────────────
@dataclass
class ScalpConfig:
    market: str = "kr"            # kr / us
    # 손익비: 손절을 목표보다 좁게 둔다. 목표 +1.5% / 손절 -3%는 한 번 지면
    # 두 번 이겨야 메워져 승률 67% 이상이 필요했다 (실전 12회 승률 42%)
    target_pct: float = 2.0       # 익절 목표(%) — 도달하면 추적 익절 시작
    stop_pct: float = 2.0         # 손절선(%) — 넘어가면 무조건 전량 매도
    # 목표가에 닿으면 바로 익절한다(0). 0보다 크면 목표 도달 뒤 고점에서 이 %만큼
    # 밀릴 때 판다 — 목표 폭이 1%대일 때는 이익의 절반을 돌려주게 된다
    trail_pct: float = 0.0
    max_spread_pct: float = 0.3   # 매수·매도 호가 간격이 이보다 넓으면 매수하지 않는다 (0이면 끔)
    open_delay_min: float = 15.0  # 각 장 시작 후 이 시간(분) 동안은 매수하지 않는다
    # 갭이 큰 날의 조기 판단: 장이 다시 열리며 early_gap_pct 이상 뛰어오른 종목은
    # 개장 대기를 early_open_min 분으로 줄이고 AI가 초반 분봉을 보고 판단한다.
    # 그 밖의 종목은 open_delay_min을 그대로 기다린다 (0이면 끔)
    early_open_min: float = 5.0
    early_gap_pct: float = 2.0
    # 추세 추종 진입: 당일 크게 오르며 VWAP 위에서 저점을 높이는 종목은 VWAP에서
    # 멀어도 AI가 진입을 제안할 수 있다. 이렇게 산 종목은 목표가에서 절반만 팔고
    # 나머지는 고점에서 trend_trail_pct만큼 밀릴 때 판다 (추세가 길게 가는 날을
    # 목표가 한 번으로 끝내지 않기 위해서다)
    trend_entries: bool = True
    # 추적 폭은 종목의 흔들림에 맞춘다: 진입 때의 1분봉 평균 변동폭의
    # trend_trail_mult배, 단 trend_trail_pct 이상 trend_trail_max_pct 이하.
    # (3배 ETF는 1분에 1% 가까이 흔들려, 좁은 추적 폭은 몇 분 만에 걸린다)
    trend_trail_pct: float = 1.0
    trend_trail_mult: float = 3.0
    trend_trail_max_pct: float = 4.0
    max_chase_reentries: int = 1  # 같은 종목을 직전 매수가보다 비싸게 다시 사는 횟수(하루)
    require_ai: bool = True       # AI 판단을 받을 수 없으면 등락률 상위로 대체하지 않고 매수를 쉰다
    # 1회 손실 한도: 손절까지 갔을 때 잃는 금액이 총 투입 금액의 이 %를 넘지
    # 않도록 수량을 정한다 (손절이 먼 종목은 자동으로 적게 산다). 0이면 끔
    risk_pct: float = 0.5
    min_stop_pct: float = 0.4     # AI가 준 손절 폭이 이보다 좁으면 이 폭으로 넓힌다
    min_rr: float = 1.2           # AI 목표 폭 ÷ 손절 폭이 이 배수에 못 미치면 사지 않는다
    # AI 목표 폭 하한(%) — 수수료와 호가 비용을 빼고도 남을 만큼 움직일 자리만 산다
    min_target_pct: float = 1.0
    # 수익이 손절 폭의 이 배수에 닿으면 손절가를 본전 위로 올린다 (0이면 끔)
    breakeven_at_r: float = 1.0
    plan_ttl_sec: float = 90.0    # AI 판단의 유효 시간(초) — 지나면 버린다
    max_drift_pct: float = 0.4    # AI가 본 가격에서 이 % 넘게 움직였으면 판단을 버린다
    review_interval: float = 240.0  # 보유 종목 AI 청산 점검 주기(초, 0이면 끔)
    # AI의 손절가 상향은 수익이 손절 폭만큼 난 뒤에만 받고, 그때도 현재가에서
    # 이 %만큼은 떨어뜨린다 — 작은 흔들림에 본전으로 나오는 것을 막는다
    tighten_gap_pct: float = 0.5
    # AI가 "살 자리 없음"을 이어서 답하면 탐색 간격을 이 값(초)까지 늘린다
    scan_backoff_max: float = 360.0
    # 손익에 반영할 편도 수수료(%)와 매도 시 세금(%). 0이면 반영하지 않는다
    fee_pct: float = 0.0
    sell_tax_pct: float = 0.0
    shortlist_size: int = 6       # AI에 분봉과 함께 넘길 후보 수
    # 매매 허용 종목군 {코드: 구분}. None이면 제한하지 않는다
    universe: dict | None = None
    # 본전 감시: 손실 중인 보유 종목을 손절하지 않고 지켜보다가 매입단가에
    # 이 여유(%)를 더한 가격(수수료 감안 손익분기점)에 닿으면 판다
    breakeven_buffer_pct: float = 0.3
    max_positions: int = 2        # 동시 보유 종목 수
    total_cash: float = 1_000_000
    poll_interval: float = 3.0
    scan_interval: float = 120.0  # 후보 재탐색 주기(초)
    cooldown_min: float = 30.0    # 손절한 종목 재매수 대기(분)
    win_cooldown_min: float = 5.0  # 익절한 종목 재매수 대기(분) — 짧게 두어 반복 매매
    stale_min: float = 60.0       # 이 시간(분) 안에 익절/손절 미도달 시 교체
    daily_loss_pct: float = 2.0   # 당일 누적 손실이 자본의 이 % 도달 시 신규 매수 중단
    blind_exit_min: float = 5.0   # 시세 조회가 이 시간(분) 이상 막히면 보호 청산
    max_trades_per_day: int = 20  # 당일 최대 청산 횟수
    allow_real: bool = False
    us_day_session: bool = True
    us_extended: bool = True      # 미국 프리마켓·애프터마켓 매매
    # 애프터마켓에서는 새로 사지 않는다. 키움은 애프터마켓이 일찍 끝나고
    # (한국 08시 무렵) 주간거래(10시)까지 거래가 비어, 산 뒤 팔 길이 막힌다
    us_after_entries: bool = False
    kr_extended: bool = True      # 국내 장전·장후(NXT) 매매
    min_price: float | None = None  # None이면 미국 $5 / 국내 1,000원
    # 유동성 하한 — 저유동성 종목은 매도가 안 받아져 손절이 불가능해지므로
    # 후보에서 뺀다. 거래대금(가격×당일 거래량)은 None이면 미국 $10M / 국내
    # 100억 원, 거래량은 None이면 미국 50만 주 / 국내 30만 주. 거래대금을 0으로
    # 두면 유동성 필터 전체를 끈다
    min_dollar_volume: float | None = None
    min_volume: float | None = None
    # 장 마감 가드 — 각 장이 끝나기 이 시간(분) 전부터 신규 매수를 멈추고,
    # 그 시점에 남은 보유는 전량 정리한다 (마감 직전 매수로 물리는 것 방지)
    entry_cutoff_min: float = 60.0
    flatten_min: float = 60.0
    fill_timeout_sec: float = 45.0  # 지정가 주문이 이 시간 안에 체결되지 않으면 취소·재주문
    exit_timeout_sec: float = 15.0  # 청산 매도는 더 짧게 기다리고 추격한다
    exit_slip_pct: float = 0.5      # 청산 매도 지정가를 현재가에서 이 %보다 낮추지 않는다
    chase_step_pct: float = 0.3     # 추격 매도 회차마다 현재가 대비 낮추는 폭(%)
    chase_max_pct: float = 2.0      # 추격 매도 최대 할인 폭(%)


@dataclass
class EntryPlan:
    """AI(또는 대체 규칙)가 낸 진입 제안. 엔진이 검증한 뒤에만 주문으로 이어진다."""

    code: str
    max_price: float | None = None   # 이 가격을 넘으면 쫓아 사지 않는다
    stop: float | None = None        # 제안 손절가
    target: float | None = None      # 제안 목표가
    reason: str = ""
    mode: str = "pullback"           # pullback(눌림·돌파) / trend(추세 추종)
    volatility: float = 0.0          # 판단 시점의 1분봉 평균 변동폭(%)
    ref_price: float = 0.0           # AI가 판단할 때 본 가격
    decided_at: dt.datetime | None = None

    @property
    def has_levels(self) -> bool:
        return bool(self.max_price or self.stop or self.target)


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
    # 본전 감시: 손절·시간초과·장마감 정리 없이 손익분기점 도달만 기다린다
    watch: bool = False
    missing_count: int = 0       # 계좌에서 안 보인 연속 확인 횟수 (감시 종료 판단)
    peak_price: float = 0.0      # 보유 중 최고가 (추적 익절용)
    trailing: bool = False       # 목표 수익 도달 후 추적 익절 중
    # 종목별 손절가·목표가 (0이면 설정의 손절 %·목표 %를 쓴다)
    stop_price: float = 0.0
    target_price: float = 0.0
    init_risk: float = 0.0       # 진입 시 1주당 위험(매입가 - 손절가)
    be_moved: bool = False       # 손절가를 본전 위로 올렸는지
    last_review: dt.datetime | None = None   # 마지막 AI 청산 점검 시각
    mode: str = "pullback"       # 진입 유형: pullback / trend(추세 추종)
    volatility: float = 0.0      # 진입 때의 1분봉 평균 변동폭(%)
    trail_pct: float = 0.0       # 이 종목의 추적 폭(%) — 0이면 설정값
    scaled: bool = False         # 목표가에서 절반을 이미 팔았는지
    # 절반 익절 주문의 체결 확인 (나머지 수량은 계속 보유한다)
    scale_pending: bool = False
    scale_order: str = ""
    scale_qty: int = 0
    scale_price: float = 0.0
    scale_sent_at: dt.datetime | None = None
    scale_unconfirmed: int = 0

    def state(self) -> str:
        if self.entry_pending:
            return "매수 체결 확인 중"
        if self.exit_pending:
            return f"{self.exit_reason} 매도 체결 대기({self.exit_attempts}차)"
        if self.scale_pending:
            return "절반 익절 체결 대기"
        if self.scaled and not self.watch:
            return "절반 익절 후 추적 중"
        if self.watch:
            return "본전 감시 중"
        if self.trailing:
            return "추적 익절 중"
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
    day: str = ""                 # 거래일 키 (당일 한도 계산용)
    fee: float = 0.0              # 수수료·세금 (pnl은 이 금액을 뺀 값)


class Scalper:
    """AI 종목 선정 + 익절/손절 반복 단타 루프."""

    def __init__(self, api: RankedMarketAPI, config: ScalpConfig,
                 selector: Callable[[list[dict], int],
                                    tuple[list, str]] | None = None,
                 reviewer: Callable[[list[dict]],
                                    tuple[list[dict], str]] | None = None,
                 is_simulation: bool = True,
                 clock: Callable[[], dt.datetime] = dt.datetime.now,
                 utc_clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
                 sleep: Callable[[float], None] = time.sleep,
                 state_path: str | None = None,
                 journal_path: str | None = None):
        if not is_simulation and not config.allow_real:
            raise PermissionError(
                "실전투자 접속이 감지되었다. 모의투자로 검증하거나 allow_real을 "
                "명시해야 한다.")
        if not 0 < config.target_pct <= 10:
            raise ValueError("목표 수익 %는 0보다 크고 10 이하여야 한다")
        if not 0 < config.stop_pct <= 20:
            raise ValueError("손절 %는 0보다 크고 20 이하여야 한다")
        if not 0 <= config.risk_pct <= 5:
            raise ValueError("1회 손실 한도 %는 0 이상 5 이하여야 한다")
        if config.max_positions < 1:
            raise ValueError("동시 종목 수는 1 이상이어야 한다")
        if config.total_cash <= 0:
            raise ValueError("총 투입 금액은 0보다 커야 한다")
        self.api = api
        self.config = config
        self.selector = selector
        self.reviewer = reviewer
        self.journal_path = journal_path
        self.clock = clock
        self.utc_clock = utc_clock
        self.sleep = sleep

        self.cash = config.total_cash
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.events: list[str] = []
        self.selection_note = ""
        self.review_note = ""     # 최근 AI 청산 점검 근거
        self.last_error = ""
        self.halt_reason = ""     # 일일 한도 도달 등으로 신규 매수가 멈춘 이유
        self._cooldown: dict[str, dt.datetime] = {}
        self._fail_counts: dict[str, int] = {}
        self._scan_lock = threading.Lock()
        self._scan_thread: threading.Thread | None = None
        self._scan_result: tuple[list[EntryPlan], str] | None = None
        self._review_thread: threading.Thread | None = None
        self._review_result: tuple[list[dict], str, dt.datetime] | None = None
        self._review_state = ""           # AI 청산 점검 상태 변화 로그용
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
        self._flatten_notice_for = None   # 마감 정리를 알린 장 (중복 로그 방지)
        self._sym_day: dict[str, dict] = {}   # 종목별 당일 매수 이력(추격 재매수 제한)
        self._ai_state = ""               # AI 선정 상태 변화 로그용
        self._none_streak = 0             # AI가 이어서 "살 자리 없음"이라 답한 횟수
        self._rank_change: dict[str, float] = {}   # 순위에서 본 종목별 당일 등락률

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
        # 편입·감시 종목은 프로그램 자금으로 산 것이 아니므로 평가액에서 뺀다
        held = sum(p.quantity * (p.last_price or p.entry_price)
                   for p in self.positions.values() if not p.adopted)
        return self.cash + held

    def breakeven_price(self, pos: Position) -> float:
        return pos.entry_price * (1 + self.config.breakeven_buffer_pct / 100)

    def stop_price(self, pos: Position) -> float:
        return pos.stop_price or pos.entry_price * (1 - self.config.stop_pct / 100)

    def target_price(self, pos: Position) -> float:
        return pos.target_price or pos.entry_price * (
            1 + self.config.target_pct / 100)

    def _plan_levels(self, price: float,
                     plan: EntryPlan | None) -> tuple[float, float] | str:
        """매수가 기준 (손절가, 목표가). AI 제안은 허용 범위 안으로 맞춘다.

        손절 폭은 [min_stop_pct, stop_pct] 안으로 제한한다. AI가 준 목표 폭이
        너무 작거나(min_target_pct) 손절 폭의 min_rr배에 못 미치면 진입하지
        않으며, 그때는 이유를 문자열로 돌려준다.
        """
        cfg = self.config
        stop_pct = cfg.stop_pct
        if plan is not None and plan.stop and 0 < plan.stop < price:
            stop_pct = (1 - plan.stop / price) * 100
            stop_pct = min(max(stop_pct, min(cfg.min_stop_pct, cfg.stop_pct)),
                           cfg.stop_pct)
        target_pct = cfg.target_pct
        if plan is not None and plan.target and plan.target > price:
            target_pct = min((plan.target / price - 1) * 100, 10.0)
            if target_pct < cfg.min_target_pct:
                return (f"목표까지 {target_pct:.2f}%뿐이라 수수료를 빼면 남지 않는다 "
                        f"(최소 {cfg.min_target_pct}%)")
            if target_pct < stop_pct * cfg.min_rr:
                return (f"목표 폭이 손절 폭의 {cfg.min_rr}배에 못 미친다 "
                        f"(손익비 부족)")
        return price * (1 - stop_pct / 100), price * (1 + target_pct / 100)

    def _size(self, code: str, price: float, stop: float) -> int:
        """매수 수량: 슬롯 예산과 1회 손실 한도 가운데 더 작은 쪽."""
        cfg = self.config
        quantity = int(min(self._per_slot_budget(), self.cash) // price)
        if quantity < 1:
            self._log(f"{code} 매수 건너뜀: 1주 금액({price:,.2f})이 슬롯 예산보다 크다")
            return 0
        if cfg.risk_pct > 0:
            per_share = price - stop
            allowed = cfg.total_cash * cfg.risk_pct / 100
            risk_qty = int(allowed // per_share) if per_share > 0 else 0
            if risk_qty < 1:
                self._cooldown[code] = self.clock() + dt.timedelta(minutes=30)
                self._log(f"{code} 매수 건너뜀: 1주만 사도 손절 시 손실"
                          f"({per_share:,.2f})이 1회 손실 한도({allowed:,.2f})를 넘는다")
                return 0
            quantity = min(quantity, risk_qty)
        return quantity

    def _journal(self, kind: str, **data) -> None:
        """AI 판단과 매매 결과를 한 줄씩 남긴다 (나중에 판단 품질을 복기하는 용도)."""
        if not self.journal_path:
            return
        try:
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"time": self.clock().isoformat(),
                                    "kind": kind, **data},
                                   ensure_ascii=False, default=str) + "\n")
        except OSError as e:
            logger.warning("매매 기록 저장 실패: %s", e)

    def _breakeven_limit(self, pos: Position, price: float) -> float:
        """본전 매도 지정가: 손익분기점 아래로는 절대 내지 않는다.

        매수호가가 손익분기점 이상이면 그 호가에 내서 바로 체결시키고,
        아니면 손익분기점에 걸어 둔다. 호가를 못 받으면 현재가에 낸다.
        """
        floor = self.breakeven_price(pos)
        if hasattr(self.api, "best_bid_ask"):
            try:
                bid, _ask = self.api.best_bid_ask(pos.code)
            except Exception:
                bid = 0.0
            if bid > 0:
                return max(floor, bid)
        return max(floor, price)

    def _open_slots(self) -> int:
        """신규 매수에 쓸 수 있는 자리. 본전 감시 종목은 자리를 차지하지 않는다."""
        trading = sum(1 for p in self.positions.values() if not p.watch)
        return self.config.max_positions - trading

    def session(self) -> str:
        if self.config.market == "us":
            return us_session(self.utc_clock(), self.config.us_day_session,
                              self.config.us_extended)
        return kr_session(self.clock(), self.config.kr_extended)

    def minutes_to_close(self) -> float | None:
        """지금 장이 끝날 때까지 남은 분. 장외면 None."""
        if self.config.market == "us":
            now = self.utc_clock()
            end = us_session_end(now, self.config.us_day_session,
                                 self.config.us_extended)
        else:
            now = self.clock()
            end = kr_session_end(now, self.config.kr_extended)
        if end is None:
            return None
        return (end - now).total_seconds() / 60

    def minutes_since_open(self) -> float | None:
        """지금 장이 시작된 뒤 지난 분. 장외면 None."""
        if self.config.market == "us":
            now = self.utc_clock()
            start = us_session_start(now, self.config.us_day_session,
                                     self.config.us_extended)
        else:
            now = self.clock()
            start = kr_session_start(now, self.config.kr_extended)
        if start is None:
            return None
        return (now - start).total_seconds() / 60

    def _day_key(self) -> str:
        """거래일. 미국은 주간거래(ET 전날 20시)부터 다음 날로 센다."""
        if self.config.market == "us":
            utc = self.utc_clock()
            offset = -4 if _us_dst_active(utc) else -5
            return (utc + dt.timedelta(hours=offset + 4)).date().isoformat()
        return self.clock().date().isoformat()

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

    def _buy(self, code: str, plan: EntryPlan | None = None) -> bool:
        try:
            price = self.api.current_price(code)
        except Exception as e:
            raise QuoteUnavailable(f"{code} 시세 조회 실패: {e}") from e
        if plan is not None and plan.has_levels and plan.ref_price:
            drift = abs(price / plan.ref_price - 1) * 100
            if drift > self.config.max_drift_pct:
                self._log(f"매수 건너뜀({code}): AI 판단 이후 가격이 {drift:.2f}% "
                          f"움직였다 — 판단을 버린다")
                return False
        if self._chase_blocked(code, price):
            self._cooldown[code] = self.clock() + dt.timedelta(minutes=60)
            self._log(f"매수 건너뜀({code}): 오늘 이미 더 낮은 가격에 샀던 종목을 "
                      f"또 올려 사지 않는다 (추격 재매수 제한)")
            return False
        limit = None
        cfg = self.config
        if cfg.max_spread_pct > 0 and hasattr(self.api, "best_bid_ask"):
            try:
                bid, ask = self.api.best_bid_ask(code)
            except Exception as e:   # 호가를 확인하지 못하면 사지 않는다
                raise QuoteUnavailable(f"{code} 호가 조회 실패: {e}") from e
            spread = (ask - bid) / ((ask + bid) / 2) * 100
            if spread > cfg.max_spread_pct:
                self._cooldown[code] = self.clock() + dt.timedelta(minutes=15)
                self._log(f"매수 건너뜀({code}): 호가 간격 {spread:.2f}%가 "
                          f"기준 {cfg.max_spread_pct}%보다 넓다 "
                          f"(매수 {bid:,.2f} / 매도 {ask:,.2f})")
                return False
            price = ask            # 매도호가에 사야 바로 체결된다
            if self._verify:
                limit = ask
        if plan is not None and plan.max_price and price > plan.max_price:
            self._log(f"매수 건너뜀({code}): 현재 {price:,.2f}가 AI 진입 상한 "
                      f"{plan.max_price:,.2f}보다 높다 — 쫓아 사지 않는다")
            return False
        levels = self._plan_levels(price, plan)
        if isinstance(levels, str):
            self._log(f"매수 건너뜀({code}): {levels}")
            return False
        stop, target = levels
        quantity = self._size(code, price, stop)
        if quantity < 1:
            return False
        ord_no = self._order(code, "buy", quantity, limit_price=limit)
        self._note_entry(code, price)
        now = self.clock()
        pos = Position(code, quantity, price, now, last_price=price,
                       last_quote_time=now, orig_qty=quantity, peak_price=price,
                       stop_price=stop, target_price=target,
                       init_risk=price - stop,
                       mode=plan.mode if plan is not None else "pullback",
                       volatility=plan.volatility if plan is not None else 0.0)
        if self._verify:
            pos.entry_pending = True
            pos.entry_order = ord_no
            pos.entry_sent_at = now
        self.positions[code] = pos
        self.cash -= quantity * price
        self._log(f"매수{' 주문' if self._verify else ''}"
                  f"{'(추세 추종)' if pos.mode == 'trend' else ''}: "
                  f"{code} {quantity}주 "
                  f"@{price:,.2f} (손절 {stop:,.2f} / 목표 {target:,.2f}, "
                  f"손절 시 손실 약 {(price - stop) * quantity:,.2f})")
        self._journal("buy", code=code, quantity=quantity, price=price,
                      stop=stop, target=target, mode=pos.mode,
                      reason=plan.reason if plan else "")
        self._save_state()
        return True

    def _chase_blocked(self, code: str, price: float) -> bool:
        hist = self._sym_day.get(code)
        if not hist or hist["day"] != self._day_key():
            return False
        return (price > hist["last_entry"]
                and hist["chases"] >= self.config.max_chase_reentries)

    def _note_entry(self, code: str, price: float) -> None:
        day = self._day_key()
        hist = self._sym_day.get(code)
        if not hist or hist["day"] != day:
            self._sym_day[code] = {"day": day, "last_entry": price, "chases": 0}
            return
        if price > hist["last_entry"]:
            hist["chases"] += 1
        hist["last_entry"] = price

    def _close(self, pos: Position, price: float, reason: str) -> None:
        """청산 주문. 미국(지정가)은 체결이 확인될 때까지 포지션을 유지한다."""
        if not self._verify:
            self._order(pos.code, "sell", pos.quantity)
            self._finalize(pos, price, reason)
            return
        if reason != "본전 매도":      # 본전 매도는 손익분기점 아래로 내지 않는다
            price = self._exit_limit(pos.code, price)
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

    def _exit_limit(self, code: str, price: float) -> float:
        """청산 매도 지정가. 매수호가에 맞춰 바로 체결되게 낸다.

        내려가는 중에 직전 체결가로 지정가를 내면 받아 줄 상대가 없어 한참
        뒤 더 낮은 가격에 팔게 된다. 호가가 지나치게 낮으면 현재가에서
        exit_slip_pct보다 낮추지는 않는다(나머지는 추격 매도가 처리).
        """
        if not hasattr(self.api, "best_bid_ask"):
            return price
        try:
            bid, _ask = self.api.best_bid_ask(code)
        except Exception:
            bid = 0.0
        if bid > 0:
            return max(bid, price * (1 - self.config.exit_slip_pct / 100))
        return price * (1 - 0.001)       # 호가를 못 받으면 0.1% 낮춰 낸다

    def _order_fill(self, ord_no: str, expected: float) -> float | None:
        """주문의 실제 평균 체결가. 조회할 수 없거나 값이 이상하면 None."""
        fetch = getattr(self.api, "order_fill", None)
        if fetch is None or not ord_no or expected <= 0:
            return None
        try:
            filled = fetch(ord_no)
        except Exception as e:
            logger.info("체결가 조회 실패(주문가로 기록): %s", e)
            return None
        if not filled:
            return None
        _qty, fill_price = filled
        if fill_price <= 0 or abs(fill_price / expected - 1) > 0.03:
            return None
        return float(fill_price)

    def _fees(self, pos: Position, qty: int, exit_price: float) -> float:
        cfg = self.config
        if cfg.fee_pct <= 0 and cfg.sell_tax_pct <= 0:
            return 0.0
        sell = qty * exit_price
        buy = 0.0 if pos.adopted else qty * pos.entry_price   # 편입분은 매도분만
        return ((buy + sell) * cfg.fee_pct + sell * cfg.sell_tax_pct) / 100

    def _scale_out(self, pos: Position, price: float) -> None:
        """추세 추종 종목이 목표가에 닿았다: 절반을 팔고 나머지는 고점을 따라간다."""
        cfg = self.config
        half = pos.quantity // 2
        pos.trailing = True
        pos.scaled = True
        pos.trail_pct = round(min(max(cfg.trend_trail_pct,
                                      pos.volatility * cfg.trend_trail_mult),
                                  max(cfg.trend_trail_max_pct,
                                      cfg.trend_trail_pct)), 2)
        if half < 1:                      # 1주뿐이면 나누지 않고 전부 추적한다
            self._log(f"목표 도달(추세): {pos.code} {pos.pct(price):+.2f}% — "
                      f"고점 대비 -{pos.trail_pct}%에서 매도")
            self._save_state()
            return
        if not self._verify:
            self._order(pos.code, "sell", half)
            self._book_partial(pos, half, price)
            return
        limit = self._exit_limit(pos.code, price)
        pos.scale_order = self._order(pos.code, "sell", half, limit_price=limit)
        pos.scale_pending = True
        pos.scale_qty = half
        pos.scale_price = limit
        pos.scale_sent_at = self.clock()
        pos.scale_unconfirmed = 0
        self._log(f"목표 도달(추세): {pos.code} 절반 {half}주 매도 주문 "
                  f"@{limit:,.2f} ({pos.pct(limit):+.2f}%) — 체결 확인 중")

    def _book_partial(self, pos: Position, qty: int, price: float) -> None:
        """절반 익절 체결분을 장부에 반영한다. 팔리지 않은 몫은 계속 보유한다."""
        pos.scale_pending = False
        if qty > 0:
            price = self._order_fill(pos.scale_order, price) or price
            pos.partial_pnl += (price - pos.entry_price) * qty
            if not pos.adopted:
                self.cash += qty * price
            pos.quantity -= qty
            self._log(f"절반 익절: {pos.code} {qty}주 @{price:,.2f} "
                      f"({pos.pct(price):+.2f}%) — 남은 {pos.quantity}주는 고점 "
                      f"대비 -{pos.trail_pct}%에서 매도")
            self._journal("scale_out", code=pos.code, quantity=qty, price=price,
                          remaining=pos.quantity)
        else:
            self._log(f"절반 익절 미체결: {pos.code} — {pos.quantity}주 전부 "
                      f"고점을 따라가며 판다")
        pos.scale_order = ""
        pos.scale_qty = 0
        self._save_state()

    def _check_scale(self, pos: Position, orders: list[dict],
                     now: dt.datetime) -> None:
        """절반 익절 주문의 체결 확인. 안 받아진 몫은 쫓지 않고 추적 익절에 맡긴다."""
        order = self._find_order(orders, pos.code, pos.scale_order)
        if order is not None:
            waited = (now - pos.scale_sent_at).total_seconds()
            if waited < self.config.exit_timeout_sec:
                return
            try:
                self.api.cancel_order(order["ord_no"], pos.code)
            except Exception as e:   # 그사이 체결됐을 수 있다 — 다음 주기에 재확인
                logger.warning("%s 절반 익절 취소 실패: %s", pos.code, e)
                return
            remaining = self._remaining(order, pos.scale_qty)
            self._book_partial(pos, pos.scale_qty - remaining, pos.scale_price)
            return
        # 미체결 목록에서 사라졌다 — 실계좌 보유로 실제 매도 수량을 확인한다
        sold = pos.scale_qty
        if pos.baseline_qty is not None:
            actual = self._actual_qty(pos.code)
            if actual is None:
                return
            held = max(0, actual - pos.baseline_qty)
            sold = max(0, min(pos.scale_qty, pos.quantity - held))
            if sold < pos.scale_qty:
                pos.scale_unconfirmed += 1
                if pos.scale_unconfirmed < 2:     # 잔고 반영 지연에 속지 않게
                    return
        self._book_partial(pos, sold, pos.scale_price)

    def _finalize(self, pos: Position, price: float, reason: str) -> None:
        """체결이 확인된 청산을 장부에 반영한다."""
        if self._verify and pos.exit_order:
            price = self._order_fill(pos.exit_order, price) or price
        gross = pos.partial_pnl + (price - pos.entry_price) * pos.quantity
        qty = max(pos.orig_qty, pos.quantity)
        fee = self._fees(pos, qty, price)
        pnl = gross - fee
        if not pos.adopted:     # 편입분은 프로그램 현금으로 산 것이 아니다
            self.cash += pos.quantity * price - fee
        if reason in ("익절", "수익 보전") and gross < 0:
            # 주문이 제때 받아지지 않아 낮춰 팔면서 손실로 끝났다
            reason = f"{reason} 미체결 손실"
        if pos.partial_pnl and qty > 0 and pos.entry_price > 0:
            # 나눠 판 매매는 평균 매도가로 기록한다
            price = pos.entry_price + gross / qty
        self.trades.append(Trade(pos.code, qty, pos.entry_price, price, pnl,
                                 pos.pct(price), reason, self.clock(),
                                 day=self._day_key(), fee=fee))
        self.positions.pop(pos.code, None)
        # 익절한 종목은 짧게 쉬고 재진입을 허용해 매매가 계속 이어지게 한다
        minutes = (self.config.win_cooldown_min if pnl > 0
                   else self.config.cooldown_min)
        self._cooldown[pos.code] = self.clock() + dt.timedelta(minutes=minutes)
        self._rescan_asap = True   # 슬롯이 비었으니 즉시 재탐색
        self._log(f"{reason}: {pos.code} {qty}주 @{price:,.2f} "
                  f"({pos.pct(price):+.2f}%, 손익 {pnl:+,.2f}"
                  f"{f', 수수료 {fee:,.2f} 차감' if fee else ''}, "
                  f"누적 {self.realized_pnl:+,.2f})"
                  f"{' — 체결 확인' if self._verify else ''}")
        self._journal("trade", code=pos.code, quantity=qty,
                      entry=pos.entry_price, exit=price, pnl=round(pnl, 4),
                      fee=round(fee, 4),
                      pct=round(pos.pct(price), 3), reason=reason,
                      held_min=round((self.clock() - pos.entry_time)
                                     .total_seconds() / 60, 1))
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
                   if p.entry_pending or p.exit_pending or p.scale_pending]
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
                elif pos.scale_pending:
                    self._check_scale(pos, orders, now)
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
            fill = self._order_fill(pos.entry_order, pos.entry_price)
            if fill and fill != pos.entry_price:
                # 실제 체결가로 장부를 맞춘다 (손절가·목표가는 계획한 가격 그대로)
                self.cash += (pos.entry_price - fill) * pos.quantity
                pos.entry_price = fill
            self._log(f"매수 체결 확인: {pos.code} {pos.quantity}주"
                      f"{f' @{fill:,.2f}' if fill else ''}")
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
        timeout = (self.config.fill_timeout_sec if pos.watch
                   else self.config.exit_timeout_sec)
        if (now - pos.exit_sent_at).total_seconds() < timeout:
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
        if pos.watch:
            self._back_to_watch(pos, "본전 매도 주문이 체결되지 않았다")
            return
        pos.exit_needs_resend = True
        self._resend_exit(pos, now)

    def _back_to_watch(self, pos: Position, why: str) -> None:
        """본전 매도가 안 받아지면 가격을 낮추지 않고 다시 감시한다."""
        pos.exit_pending = False
        pos.exit_needs_resend = False
        pos.exit_order = ""
        pos.exit_unconfirmed = 0
        self._log(f"{why}: {pos.code} {pos.quantity}주 — 본전 감시를 계속한다")
        self._save_state()

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
        if pos.watch:
            self._back_to_watch(pos, "본전 매도 주문이 체결 없이 사라졌다")
            return
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
            if (pos is None or pos.entry_pending or pos.exit_pending
                    or pos.scale_pending):
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
                if pos.watch:
                    continue           # 감시 종목은 시세가 끊겨도 던지지 않는다
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
            cfg = self.config
            pos.peak_price = max(pos.peak_price, price)
            if pos.watch:
                if price >= self.breakeven_price(pos):
                    try:
                        self._close(pos, self._breakeven_limit(pos, price),
                                    "본전 매도")
                    except Exception as e:
                        self.last_error = f"{code} 본전 매도 주문 오류: {e}"
                        logger.warning("%s 본전 매도 실패(다음 주기에 재시도): %s",
                                       code, e)
                continue
            stop = self.stop_price(pos)
            target = self.target_price(pos)
            # 수익이 손절 폭만큼 나면 손절가를 본전 위로 올린다 — 이기던 매매가
            # 손실로 끝나지 않게 한다
            if cfg.breakeven_at_r > 0 and not pos.be_moved:
                risk = pos.init_risk or pos.entry_price * cfg.stop_pct / 100
                floor = self.breakeven_price(pos)
                if (price >= pos.entry_price + risk * cfg.breakeven_at_r
                        and stop < floor < price):
                    pos.stop_price = stop = floor
                    pos.be_moved = True
                    self._log(f"손절가 상향: {code} → {floor:,.2f} (본전 확보, "
                              f"현재 {pct:+.2f}%)")
                    self._save_state()
            try:
                if price <= stop:
                    self._close(pos, price, "손절" if price < pos.entry_price
                                else "수익 보전")
                elif pos.trailing:
                    # 고점에서 추적 폭만큼 밀리면 이익을 확정한다
                    trail = pos.trail_pct or cfg.trail_pct
                    if price <= pos.peak_price * (1 - trail / 100):
                        self._close(pos, price, "익절")
                elif price >= target:
                    if (pos.mode == "trend" and cfg.trend_entries
                            and cfg.trend_trail_pct > 0):
                        self._scale_out(pos, price)
                    elif cfg.trail_pct > 0:
                        pos.trailing = True
                        self._log(f"목표 도달: {code} {pct:+.2f}% — 추적 익절 "
                                  f"시작 (고점 대비 -{cfg.trail_pct}%에서 매도)")
                    else:
                        self._close(pos, price, "익절")
                elif held_min >= cfg.stale_min:
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
        return 10_000_000.0 if self.config.market == "us" else 10_000_000_000.0

    def _min_volume(self) -> float:
        if self.config.min_volume is not None:
            return self.config.min_volume
        return 500_000.0 if self.config.market == "us" else 300_000.0

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
        universe = self.config.universe
        # 종목군을 제한하면 대형주가 모이는 거래대금 순위를 넓게 훑는다
        criteria_list = (("value", "volume", "change") if universe is not None
                         else ("change", "volume"))
        limit = 100 if universe is not None else 10
        for criteria in criteria_list:
            try:
                fetched = self.api.top_stocks(criteria, limit=limit)
            except Exception as e:
                logger.warning("순위 조회 실패(%s): %s", criteria, e)
                continue
            for row in fetched:
                code = row.get("code", "")
                if code and row.get("change_pct") not in (None, ""):
                    self._rank_change[code.upper()] = self._row_float(
                        row, "change_pct")
                if universe is not None and code.upper() not in universe:
                    continue                      # 대형주·주요 ETF만 매매한다
                if code and code not in seen:
                    seen.add(code)
                    rows.append(row)
        now = self.clock()
        min_price = self._min_price()
        min_dv = self._min_dollar_volume()
        min_vol = self._min_volume()

        def ok(row: dict) -> bool:
            code = row["code"]
            price = abs(self._row_float(row, "price"))
            if 0 < price < min_price:
                return False                      # 초저가 잡주 배제
            if min_dv > 0:                        # 저유동성 배제(손절 체결 보장)
                volume = abs(self._row_float(row, "volume"))
                if volume <= 0 or volume < min_vol or price * volume < min_dv:
                    return False
            if self._row_float(row, "change_pct") <= 0:
                return False                      # 상승 모멘텀 종목만
            if price > 0 and self._chase_blocked(code, price):
                return False                      # 오늘 더 싸게 샀던 종목 추격 금지
            if code in self.positions:
                return False
            if self._group_conflict(code):
                return False                      # 같은 지수는 한 종목만 든다
            until = self._cooldown.get(code)
            return until is None or now >= until  # 쿨다운 중 재매수 금지

        return [r for r in rows if ok(r)]

    def _group_conflict(self, code: str) -> str:
        """같은 기초지수 묶음의 종목을 이미 들고 있으면 그 종목 코드를 돌려준다.

        SOXL과 SOXS처럼 방향이 반대인 둘을 함께 들면 서로 상쇄되어 수수료만
        내고, 같은 방향 둘은 한 지수에 위험이 몰린다. 본전 감시 종목은 세지
        않는다.
        """
        group = group_of(code)
        if group is None:
            return ""
        for held, pos in self.positions.items():
            if held == code or pos.watch:
                continue
            other = group_of(held)
            if other is not None and other[0] == group[0]:
                return held
        return ""

    def _early_window(self) -> bool:
        """개장 대기 중이지만 갭이 큰 종목은 먼저 판단해도 되는 구간인가."""
        cfg = self.config
        if cfg.early_gap_pct <= 0 or cfg.early_open_min >= cfg.open_delay_min:
            return False
        since = self.minutes_since_open()
        return since is not None and cfg.early_open_min <= since < cfg.open_delay_min

    def _shortlist(self, ranked: list[dict], size: int) -> list[dict]:
        """AI에 넘길 후보. 레버리지 ETF는 등락률이 커서 상위를 독차지하므로
        2개까지만 넣어 대형주·일반 ETF가 함께 검토되게 한다."""
        universe = self.config.universe or {}
        out, counts = [], {LEVERAGED: 0, INVERSE: 0}
        for row in ranked:
            kind = universe.get(str(row.get("code", "")).upper())
            if kind in counts:          # 레버리지·인버스 각각 2개까지
                if counts[kind] >= 2:
                    continue
                counts[kind] += 1
            out.append(row)
            if len(out) >= size:
                break
        return out

    def _enrich(self, rows: list[dict]) -> list[dict]:
        """후보·보유 종목에 분봉 요약과 종목 구분을 붙인다 (AI 판단 입력)."""
        universe = self.config.universe or {}
        fetch = getattr(self.api, "minute_bars", None)
        out = []
        for row in rows:
            item = dict(row)
            kind = universe.get(str(row.get("code", "")).upper())
            if kind:
                item["kind"] = KIND_LABELS.get(kind, kind)
            group = group_of(row.get("code", ""))
            if group is not None:
                # 같은 지수를 따르는 종목의 당일 등락률: 지수가 한 방향으로 가는
                # 날인지 AI가 알 수 있게 한다 (SOXS를 볼 때 SOXL -11% 등)
                peers = [f"{code} {self._rank_change[code]:+.1f}%"
                         for code in GROUPS[group[0]]
                         if code != str(row.get("code", "")).upper()
                         and code in self._rank_change]
                if peers:
                    item["group"] = group[0]
                    item["peers"] = ", ".join(peers[:4])
            if fetch is not None:
                try:
                    # 장 전체를 보려고 넉넉히 받는다 (90개만 받으면 최근 90분의
                    # 고가·저가를 당일 값으로 잘못 알게 된다)
                    summary = summarize_bars(fetch(row["code"], 1, 400),
                                             self.minutes_since_open())
                    if summary:
                        item["bars"] = summary
                        # 순위 응답의 가격은 분봉보다 늦을 수 있다 — AI가 서로
                        # 어긋난 숫자를 보지 않게 분봉의 최근 종가로 맞춘다
                        if "price" in item:
                            item["price"] = f"{summary['last']:,.2f}"
                except Exception as e:   # 분봉이 없어도 판단은 진행한다
                    logger.info("%s 분봉 조회 실패(분봉 없이 판단): %s",
                                row.get("code"), e)
            out.append(item)
        return out

    def _gapped(self, row: dict) -> bool:
        """장이 다시 열리며 early_gap_pct 이상 뛰어오른 종목인가 (분봉 기준)."""
        bars = row.get("bars") or {}
        gap, since = bars.get("gap_pct"), bars.get("bars_since_gap")
        return (gap is not None and gap >= self.config.early_gap_pct
                and since is not None and since <= 20)

    def _to_plans(self, picked: list, rows: list[dict]) -> list[EntryPlan]:
        """선정 결과(코드 문자열 또는 AI 계획 dict)를 진입 계획으로 맞춘다."""
        by_code = {r["code"]: r for r in rows}
        plans: list[EntryPlan] = []
        now = self.clock()
        for item in picked or []:
            if isinstance(item, EntryPlan):
                plan = item
            elif isinstance(item, dict):
                plan = EntryPlan(str(item.get("code", "")),
                                 max_price=item.get("max_price"),
                                 stop=item.get("stop"),
                                 target=item.get("target"),
                                 reason=str(item.get("reason") or ""),
                                 mode=("trend" if item.get("mode") == "trend"
                                       and self.config.trend_entries
                                       else "pullback"))
            else:
                plan = EntryPlan(str(item))
            row = by_code.get(plan.code)
            if row is None or any(p.code == plan.code for p in plans):
                continue
            plan.volatility = float((row.get("bars") or {})
                                    .get("avg_range_pct") or 0)
            if plan.has_levels:
                plan.ref_price = ((row.get("bars") or {}).get("last")
                                  or abs(self._row_float(row, "price")))
            plan.decided_at = now
            plans.append(plan)
        return plans

    def _scan_worker(self, slots: int) -> None:
        try:
            rows = self._candidates()
            if not rows:
                if not self._no_candidates:
                    self._no_candidates = True
                    self._log("탐색: 매수 가능한 후보가 없다(종목군·필터·쿨다운 제외) "
                              "— 30초 간격으로 재탐색한다")
                self._retry_scan_soon()
                return
            self._no_candidates = False
            ranked = sorted(rows, key=lambda r: self._row_float(r, "change_pct"),
                            reverse=True)
            plans: list[EntryPlan] = []
            note = ""
            ai_only = self.selector is not None and self.config.require_ai
            early = self._early_window()
            if early and not ai_only:
                return               # 개장 초반 조기 진입은 AI 판단이 있을 때만
            if self.selector is not None:
                # AI 필수 모드에서는 예비 후보도 AI가 고른 것만 쓰므로 넉넉히 받는다
                want = slots + 2 if ai_only else slots
                short = self._shortlist(
                    ranked, max(self.config.shortlist_size, want))
                try:
                    short = self._enrich(short)
                    if early:
                        # 개장 대기 중에는 갭이 크게 뜬 종목만 AI에 넘긴다
                        short = [r for r in short if self._gapped(r)]
                        if not short:
                            left = (self.config.open_delay_min
                                    - (self.minutes_since_open() or 0)) * 60
                            self._retry_scan_soon(max(30.0, left))
                            return
                        self._log("개장 초반 조기 판단: 갭 "
                                  + ", ".join(f"{r['code']} "
                                              f"{r['bars']['gap_pct']:+.1f}%"
                                              for r in short)
                                  + " — AI가 초반 분봉으로 진입 여부를 본다")
                    picked, note = self.selector(short, want)
                    plans = self._to_plans(picked, short)[:want]
                    self._journal(
                        "entry_decision",
                        candidates=[{"code": r["code"], "price": r.get("price"),
                                     "change_pct": r.get("change_pct")}
                                    for r in short],
                        plans=[{"code": p.code, "mode": p.mode,
                                "max_price": p.max_price,
                                "stop": p.stop, "target": p.target,
                                "reason": p.reason} for p in plans],
                        note=note)
                except Exception as e:
                    if ai_only:
                        self.selection_note = (
                            f"AI 판단 불가 — 매수를 쉬는 중이다.\n사유: {str(e)[:300]}")
                        if self._ai_state != "down":
                            self._ai_state = "down"
                            self._log("AI 판단 불가 — 등락률 상위로 대체하지 않고 "
                                      f"매수를 쉰다 ({str(e)[:60]})")
                        self._back_off_scan()
                        return
                    logger.warning("AI 선정 실패, 모멘텀 상위로 대체: %s", e)
                    note = f"AI 선정 불가({str(e)[:80]}) — 등락률 모멘텀 상위로 자동 대체"
            if ai_only:
                if not plans:
                    self.selection_note = note or "AI가 적합한 종목이 없다고 판단했다."
                    if self._ai_state != "none":
                        self._ai_state = "none"
                        self._log("AI 판단: 지금은 살 만한 자리가 없다 — 매수를 쉰다")
                    self._back_off_scan()
                    return
                self._ai_state = "ok"
                self._none_streak = 0
            else:
                if not plans:   # AI 실패/무효 → 등락률 모멘텀 상위로 대체
                    plans = [EntryPlan(r["code"]) for r in ranked[:slots]]
                    note = note or "AI 선정 불가 — 등락률 모멘텀 상위로 자동 대체"
                # 주문이 거부된 종목을 즉시 대체할 수 있게 예비 후보를 뒤에 붙인다
                chosen = {p.code for p in plans}
                backup = [EntryPlan(r["code"]) for r in ranked
                          if r["code"] not in chosen]
                plans = (plans + backup)[: slots + 3]
            with self._scan_lock:
                self._scan_result = (plans, note)
        except Exception as e:
            logger.warning("종목 탐색 오류: %s", e)
        finally:
            self._scan_thread = None

    def _back_off_scan(self) -> None:
        """AI가 살 자리가 없다고 답할수록 다음 판단 요청을 늦춘다.

        조용한 장에서 2분마다 묻는 것은 구독 사용 한도만 쓴다. 간격을
        탐색 주기의 1.5배, 2배, … 로 늘려 scan_backoff_max에서 멈춘다.
        포지션이 청산되면(_rescan_asap) 곧바로 다시 묻는다.
        """
        cfg = self.config
        self._none_streak += 1
        delay = min(cfg.scan_interval * (1 + 0.5 * self._none_streak),
                    max(cfg.scan_backoff_max, cfg.scan_interval))
        self._retry_scan_soon(delay)

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
        plans, note = result
        if note:
            self.selection_note = note
        filled = 0
        now = self.clock()
        for plan in plans:
            code = plan.code
            if filled >= slots:
                break
            if code in self.positions:
                continue
            held = self._group_conflict(code)
            if held:
                self._log(f"매수 건너뜀({code}): 같은 지수를 따르는 {held}를 이미 "
                          f"들고 있다")
                continue
            if (plan.has_levels and plan.decided_at is not None
                    and (now - plan.decided_at).total_seconds()
                    > self.config.plan_ttl_sec):
                self._log(f"매수 건너뜀({code}): AI 판단이 나온 지 오래됐다 "
                          f"— 다시 판단을 받는다")
                continue
            try:
                if self._buy(code, plan):
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
        if plans and filled == 0:
            # 전부 거부되면 탐색 주기를 다 기다리지 않고 다시 찾는다
            self._retry_scan_soon(60.0 if any(p.has_levels for p in plans)
                                  else 30.0)

    # ── AI 청산 점검 (보유 종목) ───────────────────────────
    def _review_rows(self) -> list[dict]:
        now = self.clock()
        rows = []
        for pos in self.positions.values():
            if (pos.entry_pending or pos.exit_pending or pos.scale_pending
                    or pos.watch or not pos.last_price):
                continue
            since = pos.last_review or pos.entry_time
            if (now - since).total_seconds() < self.config.review_interval:
                continue
            rows.append({
                "code": pos.code, "entry": pos.entry_price,
                "price": pos.last_price, "pct": round(pos.pct(), 2),
                "held_min": round((now - pos.entry_time).total_seconds() / 60),
                "stop": round(self.stop_price(pos), 4),
                "target": round(self.target_price(pos), 4),
                "peak": pos.peak_price, "trailing": pos.trailing,
                "can_tighten": self._reached_one_r(pos)})
        return rows

    def _reached_one_r(self, pos: Position) -> bool:
        """수익이 처음 손절 폭만큼 난 적이 있는가 (AI 손절가 상향 허용 조건)."""
        risk = pos.init_risk or pos.entry_price * self.config.stop_pct / 100
        return pos.be_moved or pos.peak_price >= pos.entry_price + risk

    def _maybe_review(self) -> None:
        if (self.reviewer is None or self.config.review_interval <= 0
                or self._review_thread is not None):
            return
        rows = self._review_rows()
        if not rows:
            return
        now = self.clock()
        for row in rows:
            self.positions[row["code"]].last_review = now
        thread = threading.Thread(target=self._review_worker, args=(rows,),
                                  daemon=True)
        self._review_thread = thread
        thread.start()

    def _review_worker(self, rows: list[dict]) -> None:
        try:
            reviews, note = self.reviewer(self._enrich(rows))
            with self._scan_lock:
                self._review_result = (reviews, note, self.clock())
            self._review_state = "ok"
        except Exception as e:
            if self._review_state != "down":
                self._review_state = "down"
                self._log("AI 청산 점검 불가 — 손절·목표 규칙만으로 관리한다 "
                          f"({str(e)[:60]})")
        finally:
            self._review_thread = None

    def _consume_review(self) -> None:
        with self._scan_lock:
            result, self._review_result = self._review_result, None
        if result is None:
            return
        reviews, note, decided_at = result
        if (self.clock() - decided_at).total_seconds() > self.config.plan_ttl_sec:
            return                               # 늦게 도착한 판단은 쓰지 않는다
        if note:
            self.review_note = note
        for review in reviews:
            pos = self.positions.get(review.get("code", ""))
            if (pos is None or pos.entry_pending or pos.exit_pending
                    or pos.scale_pending or pos.watch or not pos.last_price):
                continue
            action = review.get("action")
            reason = str(review.get("reason") or "")[:80]
            price = pos.last_price
            if action == "exit":
                try:
                    self._log(f"AI 청산 판단: {pos.code} — {reason or '흐름 약화'}")
                    self._close(pos, price, "AI 청산")
                except Exception as e:
                    self.last_error = f"{pos.code} AI 청산 주문 오류: {e}"
            elif action == "tighten":
                new_stop = float(review.get("stop") or 0)
                # 수익이 손절 폭만큼 나기 전의 상향 제안은 받지 않는다 — 진입
                # 직후 손절가를 바짝 올리면 작은 흔들림에 본전으로 나오고
                # 수수료만 낸다
                if not self._reached_one_r(pos):
                    self._log(f"AI 손절가 상향 보류: {pos.code} — 수익이 아직 "
                              f"손절 폭에 못 미친다 (손절가 "
                              f"{self.stop_price(pos):,.2f} 유지)")
                    continue
                # 현재가에 너무 붙이지 않는다. 추적 중인 추세 종목은 추적 폭보다
                # 가깝게 올리지 않는다 (그러면 추적이 의미가 없어진다)
                gap = max(self.config.tighten_gap_pct,
                          pos.trail_pct if pos.scaled else 0.0)
                new_stop = min(new_stop, price * (1 - gap / 100))
                # 손절가는 올릴 수만 있다 — 낮추는 제안은 버린다
                if new_stop > self.stop_price(pos):
                    pos.stop_price = new_stop
                    self._log(f"AI 손절가 상향: {pos.code} → {new_stop:,.2f}"
                              f"{' — ' + reason if reason else ''}")
                    self._save_state()
        self._journal("review", reviews=reviews, note=note)

    # ── 일일 안전장치 ─────────────────────────────────────
    def _entries_allowed(self) -> bool:
        if self._entry_pause_until and self.clock() < self._entry_pause_until:
            return False
        if (self.config.market == "us" and not self.config.us_after_entries
                and self.session() == "애프터마켓"):
            if self.halt_reason != "애프터마켓 매수 안 함":
                self.halt_reason = "애프터마켓 매수 안 함"
                self._log("애프터마켓에서는 새로 사지 않는다 (다음 장에 재개)")
            with self._scan_lock:
                self._scan_result = None
            return False
        left = self.minutes_to_close()
        if left is not None and left <= self.config.entry_cutoff_min:
            if self.halt_reason != "마감 1시간 전 매수 중단":
                self.halt_reason = "마감 1시간 전 매수 중단"
                self._log(f"{self.session()} 마감 {left:.0f}분 전 — 신규 매수를 "
                          f"중단한다 (다음 장에 재개)")
            with self._scan_lock:
                self._scan_result = None   # 마감 전에 받아 둔 선정은 버린다
            return False
        since = self.minutes_since_open()
        if (since is not None and since < self.config.open_delay_min
                and not self._early_window()):
            if self.halt_reason != "개장 직후 대기":
                self.halt_reason = "개장 직후 대기"
                self._log(f"{self.session()} 시작 {self.config.open_delay_min:.0f}분 "
                          f"동안은 매수하지 않는다 (개장 직후 급등락 회피)")
            with self._scan_lock:
                self._scan_result = None
            return False
        today = self._day_key()
        pnl_today = sum(t.pnl for t in self.trades if t.day == today)
        count_today = sum(1 for t in self.trades if t.day == today)
        limit = -self.config.total_cash * self.config.daily_loss_pct / 100
        if pnl_today <= limit:
            if self.halt_reason != "손실 한도":
                self.halt_reason = "손실 한도"
                self._log(f"당일 손실 한도(-{self.config.daily_loss_pct}%) 도달 — "
                          f"신규 매수를 중단한다 (보유 청산은 계속, 다음 거래일 재개)")
            return False
        if count_today >= self.config.max_trades_per_day:
            if self.halt_reason != "매매 횟수 한도":
                self.halt_reason = "매매 횟수 한도"
                self._log(f"당일 매매 횟수 한도({self.config.max_trades_per_day}회) "
                          f"도달 — 신규 매수를 중단한다")
            return False
        # 모든 제한이 풀렸다. 개장 대기 중의 조기 판단 구간은 상태로 알린다
        self.halt_reason = ("개장 초반 — 갭 큰 종목만 판단"
                            if self._early_window() else "")
        return True

    # ── 계좌 보유 편입 / 수동 청산 (UI 요청) ────────────────
    def request(self, code: str, mode: str) -> None:
        """mode: manage(편입 후 익절·손절 규칙 적용) | watch(손절 없이 본전 감시)
        | unwatch(본전 감시 해제) | sell(즉시 전량 매도)."""
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
            if pos.entry_pending or pos.exit_pending or pos.scale_pending:
                return
            if mode == "sell":
                price = pos.last_price or self.api.current_price(code)
                pos.watch = False
                self._close(pos, price, "수동 청산")
            elif mode == "watch" and not pos.watch:
                ref = pos.last_price or pos.entry_price
                if ref >= self.breakeven_price(pos):
                    self._log(f"본전 감시 불가({code}): 이미 손익분기점 이상이다 "
                              f"— 손실 중인 종목만 넣을 수 있다")
                    return
                pos.watch = True
                pos.trailing = False
                self._log(f"본전 감시로 전환: {code} {pos.quantity}주 — 손절 없이 "
                          f"{self.breakeven_price(pos):,.2f} 이상에서 매도")
                self._save_state()
            elif mode == "manage" and pos.watch:
                pos.watch = False
                self._log(f"본전 감시 해제: {code} — 익절·손절 규칙을 다시 적용한다")
                self._save_state()
            elif mode == "unwatch" and pos.watch:
                if pos.adopted:
                    # 사용자가 원래 들고 있던 주식 — 감시만 끝내고 그대로 둔다
                    del self.positions[code]
                    self._log(f"본전 감시 해제: {code} — 프로그램은 이 종목을 "
                              f"더 이상 건드리지 않는다 (계좌 보유는 그대로)")
                else:
                    pos.watch = False
                    self._log(f"본전 감시 해제: {code} — 익절·손절 규칙을 다시 "
                              f"적용한다")
                self._save_state()
            return
        if mode == "unwatch":
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
        if mode == "watch" and float(holding.get("avg_price") or 0) <= 0:
            self._log(f"본전 감시 불가({code}): 계좌에서 매입단가를 받지 못했다")
            return
        if mode == "watch" and price >= avg * (
                1 + self.config.breakeven_buffer_pct / 100):
            # 넣는 즉시 팔려 버리므로 받지 않는다 (수익 종목을 실수로 파는 것 방지)
            self._log(f"본전 감시 불가({code}): 이미 손익분기점 이상이다 "
                      f"— 손실 중인 종목만 넣을 수 있다")
            return
        now = self.clock()
        pos = Position(code, qty, avg, now, last_price=price,
                       last_quote_time=now, orig_qty=qty, adopted=True,
                       baseline_qty=max(0, int(holding.get("qty") or qty) - qty),
                       peak_price=price, watch=(mode == "watch"))
        self.positions[code] = pos
        if pos.watch:
            self._log(f"본전 감시 시작: {code} {qty}주 (매입단가 {avg:,.2f}, "
                      f"현재 {pos.pct(price):+.2f}%) — 손절 없이 "
                      f"{self.breakeven_price(pos):,.2f} 이상에서 매도")
        else:
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
                     "adopted": p.adopted, "watch": p.watch,
                     "stop_price": p.stop_price,
                     "target_price": p.target_price,
                     "init_risk": p.init_risk, "be_moved": p.be_moved,
                     "mode": p.mode, "scaled": p.scaled,
                     "volatility": p.volatility,
                     "trailing": p.trailing, "trail_pct": p.trail_pct,
                     "peak_price": p.peak_price,
                     "partial_pnl": p.partial_pnl, "orig_qty": p.orig_qty}
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
                           watch=bool(item.get("watch")),
                           stop_price=float(item.get("stop_price") or 0),
                           target_price=float(item.get("target_price") or 0),
                           init_risk=float(item.get("init_risk") or 0),
                           be_moved=bool(item.get("be_moved")),
                           mode=str(item.get("mode") or "pullback"),
                           volatility=float(item.get("volatility") or 0),
                           scaled=bool(item.get("scaled")),
                           trailing=bool(item.get("trailing")),
                           trail_pct=float(item.get("trail_pct") or 0),
                           peak_price=float(item.get("peak_price") or 0),
                           partial_pnl=float(item.get("partial_pnl") or 0),
                           baseline_qty=max(0, actual - qty))
            pos.orig_qty = max(qty, int(item.get("orig_qty") or qty))
            self.positions[code] = pos
            if not pos.adopted:
                self.cash -= qty * pos.entry_price
            # 프로그램이 꺼져 있는 동안 이미 손절선을 지나 버린 종목은 재시작과
            # 동시에 큰 손실로 던지지 않고 본전 감시로 돌린다 (해제·즉시 매도 가능)
            now_price = float(balances.get(code, {}).get("now_price") or 0)
            if (not pos.watch and now_price > 0 and pos.entry_price > 0
                    and now_price <= self.stop_price(pos)):
                pos.watch = True
                pos.last_price = now_price
                self._log(f"{code}: 이미 손절선 아래({pos.pct(now_price):+.2f}%)"
                          f" — 손절하지 않고 본전 감시로 둔다 "
                          f"({self.breakeven_price(pos):,.2f} 이상에서 매도)")
            self._log(f"이전 실행 포지션 복원: {code} {qty}주 "
                      f"(매입단가 {pos.entry_price:,.2f}) — "
                      f"{'본전 감시' if pos.watch else '익절·손절 감시'} 재개")
        self._save_state()

    def _flatten_for_close(self, left: float) -> None:
        """장 마감 1시간 전 시점에 남은 보유를 전량 정리한다 (손익 무관)."""
        targets = [p for p in self.positions.values()
                   if not (p.entry_pending or p.exit_pending
                           or p.scale_pending or p.watch)]
        if not targets:
            return
        session = self.session()
        if self._flatten_notice_for != session:
            self._flatten_notice_for = session
            self._log(f"{session} 마감 {left:.0f}분 전 — 보유 "
                      f"{len(targets)}종목을 전량 정리한다")
        for pos in targets:
            try:
                price = pos.last_price or self.api.current_price(pos.code)
                self._close(pos, price, "장마감 정리")
            except Exception as e:
                self.last_error = f"{pos.code} 장마감 정리 실패: {e}"
                logger.warning("%s 장마감 정리 실패(다음 주기에 재시도): %s",
                               pos.code, e)

    # ── 매매 루프 ─────────────────────────────────────────
    def step(self) -> None:
        if self.session() == "장외":
            return
        self._process_requests()
        self._monitor_positions()
        self._consume_review()
        left = self.minutes_to_close()
        if left is not None and left <= self.config.flatten_min:
            self._flatten_for_close(left)
        slots = self._open_slots()
        if slots > 0 and self._entries_allowed():
            self._consume_scan(slots)
            slots = self._open_slots()
            self._maybe_scan(slots)
        self._maybe_review()

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
            if pos.watch and not pos.exit_pending:
                actual = int(balances.get(code, {}).get("qty") or 0)
                if actual >= pos.quantity:
                    pos.missing_count = 0
                    continue
                pos.missing_count += 1
                if pos.missing_count < 2:      # 잔고 반영 지연에 속지 않게 두 번 확인
                    continue
                pos.missing_count = 0
                if not pos.adopted:    # 프로그램이 샀던 종목이면 현금을 되돌린다
                    self.cash += ((pos.quantity - max(actual, 0))
                                  * (pos.last_price or pos.entry_price))
                if actual <= 0:
                    del self.positions[code]
                    self._log(f"본전 감시 종료: {code} — 계좌에 더 이상 없다")
                else:
                    self._log(f"본전 감시 수량 조정: {code} {pos.quantity}주 → "
                              f"{actual}주 (계좌 보유 기준)")
                    pos.quantity = actual
                    pos.orig_qty = actual
                self._save_state()
                continue
            if (pos.entry_pending or pos.exit_pending or pos.scale_pending
                    or pos.adopted):
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
                  f"목표 +{cfg.target_pct}%"
                  f"({f'추적 {cfg.trail_pct}%' if cfg.trail_pct > 0 else '도달 시 익절'}) · "
                  f"손절 최대 -{cfg.stop_pct}% / "
                  f"1회 손실 한도 {cfg.risk_pct}% / "
                  f"동시 {cfg.max_positions}종목 / 총 {cfg.total_cash:,.0f}"
                  + (f" / 종목군 {len(cfg.universe)}개(대형주·주요 ETF)"
                     if cfg.universe is not None else ""))
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
