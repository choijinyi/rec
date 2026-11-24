"""AI 단타 대시보드 (로컬 웹 UI).

파이썬 표준 라이브러리(http.server)만으로 동작하며 127.0.0.1 전용이다.
대형주·주요 ETF 가운데 AI(Claude Opus 5.5)가 분봉을 보고 진입·청산 시점을
판단하는 단타 루프와, 손실 보유 종목의 본전 감시를 제어한다.
실행: python -m autotrader ui
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .analysis import ClaudeAnalyst, ClaudeCodeAnalyst
from .kiwoom_rest import (
    KiwoomRestClient, KiwoomRestError, _read_parser, load_config,
)
from .scalper import ScalpConfig, Scalper
from .splitter import MODES as SPLIT_MODES
from .splitter import SplitTrader, build_plan, market_of_code
from .universe import build_universe

_EFFORTS = ("low", "medium", "high", "xhigh", "max")


class RecordingAPI:
    """MarketAPI 래퍼: 시세 이력을 차트용으로 기록한다."""

    def __init__(self, inner):
        self.inner = inner
        self.prices: list[tuple[str, str, float]] = []   # (시각, 종목, 가격)
        self.lock = threading.Lock()

    def current_price(self, code: str) -> float:
        price = self.inner.current_price(code)
        with self.lock:
            self.prices.append((datetime.now().strftime("%H:%M:%S"), code, price))
            del self.prices[:-1800]
        return price

    def __getattr__(self, name):
        # send_market_order, top_stocks, us_balances 등은 원본으로 위임
        return getattr(self.inner, name)


def _parse_float(value, name: str) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name} 값이 숫자가 아니다: {value!r}")


def _confirmed(value) -> bool:
    """실전 확인란: YES를 대소문자·앞뒤 공백과 상관없이 받는다."""
    return str(value or "").strip().upper() == "YES"


def _parse_int(value, name: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name} 값이 정수가 아니다: {value!r}")


class AppState:
    def __init__(self, config_path: str = "config.ini"):
        self.config_path = config_path
        self.trader: Scalper | None = None
        self.api: RecordingAPI | None = None
        self.thread: threading.Thread | None = None
        self.params: dict = {}
        self.last_error = ""
        self.lock = threading.Lock()
        # 테스트에서 가짜 클라이언트/분석기를 주입할 수 있게 팩토리로 분리
        self.client_factory = KiwoomRestClient
        self.ai_label = ""        # 최근 판단에 쓴 모델·호출 방식
        self.buy_paused = False   # 사용자가 매수만 멈춘 상태 (시작 전에도 설정 가능)
        self.analyst_factory = ClaudeAnalyst          # API 키 방식
        self.cli_analyst_factory = ClaudeCodeAnalyst  # Claude Code 구독 로그인
        # 지정 종목 분할 매매 엔진(시장별)과 공용 API 클라이언트·매수 가능 금액
        self.splitters: dict[str, SplitTrader] = {}
        self._split_lock = threading.Lock()
        self._clients: dict[tuple, object] = {}
        self._bp: dict[str, dict] = {}

    def _client(self, market: str, cfg: dict | None = None):
        """시장별 API 클라이언트. AI 단타와 분할 매매가 하나를 같이 쓴다."""
        cfg = cfg or load_config(self.config_path)
        mock = cfg["mode"] != "real"
        key = (market, mock, cfg["appkey"], self.client_factory)
        client = self._clients.get(key)
        if client is None:
            client = self.client_factory(cfg["appkey"], cfg["secretkey"],
                                         mock=mock, market=market)
            self._clients[key] = client
        return client

    # ── 매수 가능 금액 ─────────────────────────────────────
    def buying_power(self, market: str) -> dict:
        """지금 주문할 수 있는 금액. 30초마다 뒤에서 새로 읽는다."""
        entry = self._bp.setdefault(
            market, {"data": None, "error": "", "at": None, "busy": False})
        stale = entry["at"] is None or time.monotonic() - entry["at"] > 30
        if stale and not entry["busy"]:
            entry["busy"] = True

            def work():
                try:
                    data = dict(self._client(market).buying_power())
                    data["time"] = datetime.now().strftime("%H:%M:%S")
                    entry["data"], entry["error"] = data, ""
                except Exception as e:
                    entry["error"] = str(e)[:120]
                finally:
                    entry["at"] = time.monotonic()
                    entry["busy"] = False

            threading.Thread(target=work, daemon=True).start()
        return {"data": entry["data"], "error": entry["error"]}

    # ── 지정 종목 분할 매매 ────────────────────────────────
    def _splitter(self, market: str, create: bool = True) -> SplitTrader | None:
        with self._split_lock:
            engine = self.splitters.get(market)
            if engine is not None or not create:
                return engine
            cfg = load_config(self.config_path)
            mock = cfg["mode"] != "real"
            fee_pct = cfg.get("fee_pct")
            if fee_pct is None:
                fee_pct = 0.07 if market == "us" else 0.015
            base = Path(self.config_path).resolve()
            engine = SplitTrader(
                api=self._client(market, cfg), market=market,
                is_simulation=mock, allow_real=not mock,
                fee_pct=max(0.0, fee_pct),
                sell_tax_pct=max(0.0, cfg.get("sell_tax_pct") or 0.0),
                us_day_session=cfg.get("us_day_session", True),
                state_path=str(base.with_name(
                    f"autotrader_state_split_{market}.json")))
            engine.restore_state()
            threading.Thread(target=engine.run, daemon=True).start()
            self.splitters[market] = engine
            return engine

    def load_saved_splits(self) -> None:
        """이전 실행의 분할 매매 작업을 화면에 다시 보여 준다 (멈춤 상태)."""
        base = Path(self.config_path).resolve()
        for market in ("us", "kr"):
            if base.with_name(f"autotrader_state_split_{market}.json").exists():
                try:
                    self._splitter(market)
                except Exception as e:
                    self.last_error = f"분할 매매 작업 복원 실패: {e}"

    def split_codes(self, market: str) -> set[str]:
        engine = self.splitters.get(market)
        return engine.codes() if engine else set()

    def _split_params(self, market, code, price, budget, steps, gap_pct, mode,
                      sell_start, hold_qty, stop_pct) -> dict:
        def number(value, name, default=0.0):
            return (default if str(value or "").strip() == ""
                    else _parse_float(value, name))

        code = str(code or "").strip().upper()
        if not code:
            raise ValueError("종목코드를 넣어야 한다")
        if market not in ("us", "kr"):
            raise ValueError("종목코드로 시장을 알 수 없다 — 미국주식은 영문 "
                             "티커, 국내주식은 6자리 코드를 넣는다")
        # 매도 지정가를 넣으면 그 가격부터 나눠 팔고, 비우면 회차별로 판다
        sell = number(sell_start, "매도 지정가")
        if sell > 0 or mode == "ladder":
            mode = "ladder"
        elif mode not in SPLIT_MODES:
            mode = "each"
        return {"code": code,
                "base_price": number(price, "매수 지정가"),
                "budget": number(budget, "총 금액"),
                "steps": int(number(steps, "횟수", 5)),
                "gap_pct": number(gap_pct, "간격 %", 1.0),
                "mode": mode,
                "sell_start": sell,
                "hold_qty": int(number(hold_qty, "보유분 매도 수량")),
                "stop_pct": number(stop_pct, "손절 %")}

    def _split_conflict(self, market: str, code: str) -> str:
        trader = self.trader
        if (trader and self.thread and self.thread.is_alive()
                and self.params.get("market") == market
                and code in trader.positions):
            return (f"{code}는 AI 단타가 보유·관리 중이다 — 같은 종목을 두 기능이 "
                    f"함께 다루지 않는다")
        return ""

    def split_preview(self, market: str = "us", code: str = "", price="",
                      budget="", steps=5, gap_pct=1.0, mode: str = "",
                      sell_start="", hold_qty="", stop_pct="",
                      repeat: bool = False, **_ignored) -> dict:
        """시작 전에 계획과 주의할 점을 글로 돌려준다 (주문은 내지 않는다)."""
        market = market_of_code(code, market)     # 시장은 종목코드가 정한다
        try:
            p = self._split_params(market, code, price, budget, steps, gap_pct,
                                   mode, sell_start, hold_qty, stop_pct)
            mode = p["mode"]
            levels, sells = build_plan(p["base_price"], p["budget"], p["steps"],
                                       p["gap_pct"], mode, p["sell_start"],
                                       market, p["hold_qty"])
        except ValueError as e:
            return {"error": str(e)}
        conflict = self._split_conflict(market, p["code"])
        if conflict:
            return {"error": conflict}
        cfg = load_config(self.config_path)
        real = cfg["mode"] == "real"
        unit = "$" if market == "us" else ""
        lines = [f"{p['code']} ({'미국주식' if market == 'us' else '국내주식'}) "
                 f"분할 매매 계획 [{SPLIT_MODES[mode]}"
                 f"{' · 반복' if repeat and levels else ''}] — "
                 f"{'실전투자' if real else '모의투자'}"]
        if levels:
            total = sum(lv.buy * lv.qty for lv in levels)
            lines.append("매수: " + ", ".join(
                f"{lv.buy:,.2f}×{lv.qty}주" for lv in levels))
            lines.append(f"  전부 사면 {sum(lv.qty for lv in levels)}주, "
                         f"{unit}{total:,.2f} (정한 금액 {unit}{p['budget']:,.2f})")
        if p["hold_qty"]:
            lines.append(f"보유분 {p['hold_qty']}주를 함께 나눠 판다")
        if mode == "each":
            lines.append("매도: " + ", ".join(f"{lv.sell:,.2f}" for lv in levels)
                         + " (회차별로 자기 매수가 +"
                         f"{p['gap_pct']:g}%)")
        else:
            lines.append("매도: " + ", ".join(f"{s:,.2f}" for s in sells)
                         + " (닿을 때마다 남은 수량을 남은 회차로 나눠 판다)")
        warnings = []
        now_price = None
        try:
            now_price = float(self._client(market, cfg).current_price(p["code"]))
        except Exception as e:
            # 시세를 못 받는 종목은 등록해도 주문이 나가지 않는다 — 여기서 막는다
            return {"error": f"{p['code']} 현재가를 확인하지 못해 시작하지 않는다 "
                             f"({str(e)[:60]}). 종목코드를 확인하거나 잠시 뒤 다시 "
                             f"시도한다."}
        if now_price:
            lines.append(f"현재가: {now_price:,.2f}")
            if levels and abs(p["base_price"] / now_price - 1) > 0.2:
                warnings.append(
                    f"매수 지정가 {p['base_price']:,.2f}가 현재가와 20% 넘게 "
                    f"다르다 — 가격을 잘못 넣지 않았는지 확인한다")
            hit = [lv for lv in levels if now_price <= lv.buy]
            if hit:
                warnings.append(
                    f"현재가가 지정가 이하다 — 시작하면 1~{len(hit)}회차 "
                    f"{sum(lv.qty for lv in hit)}주(약 {unit}"
                    f"{now_price * sum(lv.qty for lv in hit):,.2f})를 곧바로 산다")
            if mode == "ladder" and p["hold_qty"] and now_price >= sells[0]:
                warnings.append("현재가가 매도 지정가 이상이다 — 시작하면 보유분을 "
                                "곧바로 팔기 시작한다")
        fee = cfg.get("fee_pct")
        fee = (0.07 if market == "us" else 0.015) if fee is None else fee
        if mode == "each" and p["gap_pct"] <= 2 * fee + (cfg.get("sell_tax_pct")
                                                         or 0):
            warnings.append("간격이 왕복 수수료·세금보다 크지 않아 팔아도 남지 않는다")
        power = (self._bp.get(market) or {}).get("data")
        if power:
            lines.append(f"매수 가능 금액: {unit}{power['orderable']:,.2f}")
            if p["budget"] > power["orderable"]:
                warnings.append("총 금액이 매수 가능 금액보다 많다 — 뒤 회차 주문이 "
                                "거부될 수 있다")
        if levels:
            warnings.append(
                f"손절 -{p['stop_pct']:g}%: 마지막 회차 {levels[-1].buy:,.2f}에서 "
                f"그만큼 더 내리면 전량 매도하고 끝낸다" if p["stop_pct"]
                else "손절 없음: 마지막 회차 아래로 내려가도 팔지 않고 보유한다")
        warnings.append("주문은 현재가가 그 가격에 닿은 뒤에 낸다(미리 걸어 두지 "
                        "않는다). 바로 체결되지 않으면 그 가격에 지정가로 걸어 "
                        "두고 1분마다 다시 낸다")
        return {"ok": True, "need_confirm": real,
                "text": "\n".join(lines + ["", "확인할 점:"]
                                  + [f"- {w}" for w in warnings])}

    def split_start(self, market: str = "us", code: str = "", price="",
                    budget="", steps=5, gap_pct=1.0, mode: str = "",
                    sell_start="", hold_qty="", stop_pct="",
                    repeat: bool = False, confirm: str = "",
                    **_ignored) -> dict:
        market = market_of_code(code, market)     # 시장은 종목코드가 정한다
        try:
            p = self._split_params(market, code, price, budget, steps, gap_pct,
                                   mode, sell_start, hold_qty, stop_pct)
            cfg = load_config(self.config_path)
            if cfg["mode"] == "real" and not _confirmed(confirm):
                return {"need_confirm": True,
                        "error": "실전투자 모드다. 분할 매매의 실전 확인란에 "
                                 "YES를 입력해야 시작된다."}
            conflict = self._split_conflict(market, p["code"])
            if conflict:
                return {"error": conflict}
            engine = self._splitter(market)
            engine.add_job(repeat=bool(repeat), **p)
        except (ValueError, PermissionError, KiwoomRestError) as e:
            return {"error": str(e)}
        return {"ok": True}

    def split_action(self, market: str = "us", code: str = "",
                     action: str = "", confirm: str = "", **_ignored) -> dict:
        if action not in ("pause", "resume", "stop", "liquidate", "remove"):
            return {"error": f"알 수 없는 요청: {action}"}
        engine = self.splitters.get(market)
        code = str(code or "").strip().upper()
        if engine is None or code not in engine.jobs:
            return {"error": "그런 분할 매매 작업이 없다."}
        if action == "resume":
            try:
                real = load_config(self.config_path)["mode"] == "real"
            except KiwoomRestError as e:
                return {"error": str(e)}
            if real and not _confirmed(confirm):
                return {"need_confirm": True,
                        "error": "실전투자 모드다. 분할 매매의 실전 확인란에 "
                                 "YES를 입력해야 재개된다."}
            conflict = self._split_conflict(market, code)
            if conflict:
                return {"error": conflict}
        engine.request(code, action)
        return {"ok": True}

    def _make_analyst(self):
        """분석 백엔드 선택: api_key가 있으면 API, 없으면 Claude Code 로그인.

        config.ini [claude] backend = auto(기본) | api | cli 로 강제할 수 있다.
        """
        parser = _read_parser(self.config_path)
        api_key, backend, kwargs = "", "auto", {}
        if parser.has_section("claude"):
            section = parser["claude"]
            api_key = section.get("api_key", "").strip()
            backend = (section.get("backend", "auto").strip().lower() or "auto")
            # 모델·판단 강도: 비워 두면 기본값(Opus 5.5, medium)
            model = section.get("model", "").strip()
            effort = section.get("effort", "").strip().lower()
            if model:
                kwargs["model"] = model
            if effort in _EFFORTS:
                kwargs["effort"] = effort
        if backend == "api" or (backend == "auto" and api_key):
            return self.analyst_factory(api_key, **kwargs)
        return self.cli_analyst_factory(**kwargs)

    # ── 실행 제어 ──────────────────────────────────────────
    def start(self, market: str, target_pct, max_positions, cash,
              confirm: str = "", stop_pct=2.0, require_ai: bool = True,
              risk_pct=0.5, leveraged: bool = True,
              trend: bool = True, view: str = "",
              ladder: bool = True) -> dict:
        with self.lock:
            if self.trader is not None and self.thread and self.thread.is_alive():
                return {"error": "이미 실행 중이다. 먼저 중지해야 한다."}
            cfg = load_config(self.config_path)
            mock = cfg["mode"] != "real"
            if not mock and not _confirmed(confirm):
                return {"need_confirm": True,
                        "error": "실전투자 모드다. 확인란에 YES를 입력해야 시작된다."}
            try:
                # 종목군: 대형주·주요 ETF만 매매한다 (config.ini [universe]로 조정)
                universe = None
                if cfg.get("universe_restrict", True):
                    universe = build_universe(
                        market, leveraged=bool(leveraged),
                        add=cfg.get("universe_add"),
                        remove=cfg.get("universe_remove"))
                # 수수료: 화면의 손익이 계좌와 맞도록 뺀다 (config.ini [strategy])
                fee_pct = cfg.get("fee_pct")
                if fee_pct is None:
                    fee_pct = 0.07 if market == "us" else 0.015
                scalp_cfg = ScalpConfig(
                    market=market,
                    fee_pct=max(0.0, fee_pct),
                    sell_tax_pct=max(0.0, cfg.get("sell_tax_pct") or 0.0),
                    target_pct=_parse_float(target_pct, "목표 수익 %"),
                    stop_pct=_parse_float(stop_pct, "손절 %"),
                    risk_pct=_parse_float(risk_pct, "1회 손실 한도 %"),
                    universe=universe,
                    trend_entries=bool(trend),
                    ladder_steps=3 if ladder else 1,
                    require_ai=bool(require_ai),
                    max_positions=_parse_int(max_positions, "동시 종목 수"),
                    total_cash=_parse_float(cash, "총 투입 금액"),
                    allow_real=not mock,
                    us_day_session=cfg.get("us_day_session", True),
                )
                client = self._client(market, cfg)
                api = RecordingAPI(client)

                holder: dict = {}

                def context() -> dict:
                    ctx = {"max_stop_pct": scalp_cfg.stop_pct,
                           "min_stop_pct": min(scalp_cfg.min_stop_pct,
                                               scalp_cfg.stop_pct),
                           "min_rr": scalp_cfg.min_rr,
                           "min_target_pct": scalp_cfg.min_target_pct,
                           "tighten_gap_pct": scalp_cfg.tighten_gap_pct,
                           "trend_entries": scalp_cfg.trend_entries,
                           "ladder": scalp_cfg.ladder_steps > 1,
                           "view": str(view or "").strip()[:300],
                           "stale_min": scalp_cfg.stale_min}
                    running = holder.get("trader")
                    if running is not None:
                        ctx["session"] = running.session()
                        ctx["minutes_to_close"] = running.minutes_to_close()
                    return ctx

                def selector(rows, num, _self=self):
                    # 판단 시점에 분석 백엔드를 만든다. 백엔드가 없으면 예외가
                    # 나고, 엔진은 새로 사지 않는다(AI 필수 모드).
                    analyst = _self._make_analyst()
                    _self.ai_label = analyst.label
                    return analyst.plan_entries(market, rows, num, context())

                def reviewer(rows, _self=self):
                    analyst = _self._make_analyst()
                    return analyst.review_positions(market, rows, context())

                # 프로그램이 산 포지션을 재시작 후에도 이어서 관리하기 위한 상태 파일
                base = Path(self.config_path).resolve()
                state_path = str(base.with_name(f"autotrader_state_{market}.json"))
                # AI 판단과 매매 결과 기록 (판단 품질 복기용)
                journal_path = str(base.with_name(
                    f"autotrader_journal_{market}.jsonl"))
                trader = Scalper(api=api, config=scalp_cfg, selector=selector,
                                 reviewer=reviewer, is_simulation=mock,
                                 state_path=state_path,
                                 journal_path=journal_path)
                holder["trader"] = trader
                trader.pause_entries(self.buy_paused)   # 멈춘 채로 시작할 수 있다
                # 분할 매매 중인 종목은 AI 단타가 사지도 편입하지도 않는다
                trader.blocked_codes = lambda m=market: self.split_codes(m)
            except (ValueError, PermissionError) as e:
                return {"error": str(e)}

            def run_guarded():
                try:
                    trader.run()
                except Exception as e:  # 스레드가 죽어도 UI에 이유를 남긴다
                    self.last_error = f"매매 스레드 종료: {e}"

            thread = threading.Thread(target=run_guarded, daemon=True)
            self.trader, self.api, self.thread = trader, api, thread
            self.params = {"market": market,
                           "target_pct": scalp_cfg.target_pct,
                           "stop_pct": scalp_cfg.stop_pct,
                           "risk_pct": scalp_cfg.risk_pct,
                           "fee_pct": scalp_cfg.fee_pct,
                           "trend": scalp_cfg.trend_entries,
                           "ladder": scalp_cfg.ladder_steps > 1,
                           "view": str(view or "").strip()[:300],
                           "universe": (len(universe) if universe is not None
                                        else None),
                           "require_ai": scalp_cfg.require_ai,
                           "max_positions": scalp_cfg.max_positions,
                           "cash": scalp_cfg.total_cash,
                           "mode": "mock" if mock else "real"}
            self.last_error = ""
            thread.start()
            return {"ok": True}

    def stop(self) -> dict:
        with self.lock:
            if self.trader is None:
                return {"error": "실행 중이 아니다."}
            self.trader.stop()
            return {"ok": True}

    def pause_buys(self, paused: bool) -> dict:
        """매수만 멈추거나 다시 연다 (보유 종목 관리는 계속).

        시작 전에 눌러 두면 매수를 멈춘 채로 시작한다 — 새로 사지 않고 보유
        종목(본전 감시 등)만 관리하고 싶을 때 쓴다. 지정 종목 분할 매매는
        사용자가 직접 건 주문이라 이 버튼의 영향을 받지 않는다(작업별 일시정지).
        """
        self.buy_paused = bool(paused)
        if self.trader and self.thread and self.thread.is_alive():
            self.trader.pause_entries(self.buy_paused)
        return {"ok": True, "buy_paused": self.buy_paused}

    def adopt(self, code: str, mode: str) -> dict:
        """계좌 보유 종목 처리 요청.

        mode: manage(익절·손절 규칙 적용) | watch(손절 없이 본전 감시) |
        unwatch(본전 감시 해제) | sell(즉시 매도). code가 "*LOSERS"이고 mode가 watch면 손실 중인 보유
        종목 전부를 본전 감시로 넣는다.
        """
        if not (self.trader and self.thread and self.thread.is_alive()):
            return {"error": "먼저 시작해야 한다 (장중에 처리된다)."}
        if mode not in ("manage", "sell", "watch", "unwatch"):
            return {"error": f"알 수 없는 요청: {mode}"}
        code = (code or "").strip().upper()
        if not code:
            return {"error": "종목코드가 비어 있다."}
        if code in self.split_codes(self.params.get("market", "")):
            return {"error": f"{code}는 지정 종목 분할 매매 중이다 — 분할 매매 "
                             f"쪽에서 중지하거나 정리해야 한다."}
        if code == "*LOSERS":
            if mode != "watch":
                return {"error": "전체 요청은 본전 감시만 가능하다."}
            watching = {c for c, p in self.trader.positions.items() if p.watch}
            losers = [c for c, h in self.trader.holdings.items()
                      if h.get("qty", 0) > 0 and h.get("pl_rt", 0) < 0
                      and c not in watching]
            if not losers:
                return {"error": "본전 감시로 넣을 손실 종목이 없다 "
                                 "(보유 목록은 시작 후 1분 안에 표시된다)."}
            for loser in losers:
                self.trader.request(loser, "watch")
            return {"ok": True, "codes": losers}
        self.trader.request(code, mode)
        return {"ok": True}

    # ── 상태 조회 ──────────────────────────────────────────
    def status(self, market: str | None = None) -> dict:
        mode = "mock"
        try:
            mode = load_config(self.config_path)["mode"]
        except KiwoomRestError:
            pass
        running = bool(self.trader and self.thread and self.thread.is_alive())
        out = {
            "running": running,
            "config_mode": mode,
            "params": self.params,
            "last_error": self.last_error
                          or getattr(self.trader, "last_error", ""),
            "prices": [], "events": [], "positions": [], "trades": [],
            "equity": None, "cash": None, "realized_pnl": None,
            "wins": 0, "losses": 0, "fees": 0.0, "session": "",
            "halt_reason": "", "buy_paused": self.buy_paused,
            "selection_note": "", "review_note": "", "ai": self.ai_label,
            "chart_code": "", "holdings": [],
            "split": {"jobs": [], "events": []}, "buying_power": None,
        }
        for engine in list(self.splitters.values()):
            out["split"]["jobs"] += engine.snapshot()
            out["split"]["events"] += engine.events[-12:]
        out["split"]["events"] = sorted(out["split"]["events"])[-12:]
        if market in ("us", "kr"):       # 화면이 고른 시장의 매수 가능 금액
            out["buying_power"] = self.buying_power(market)
        trader = self.trader
        if trader:
            out["equity"] = trader.equity()
            out["cash"] = trader.cash
            out["realized_pnl"] = trader.realized_pnl
            out["wins"] = trader.wins
            out["fees"] = round(sum(t.fee for t in trader.trades), 2)
            out["losses"] = trader.losses
            out["session"] = trader.session()
            left = trader.minutes_to_close()
            out["minutes_to_close"] = None if left is None else round(left)
            out["halt_reason"] = trader.halt_reason
            out["selection_note"] = trader.selection_note
            out["review_note"] = trader.review_note
            out["events"] = trader.events[-30:]
            now = trader.clock()
            out["positions"] = [
                {"code": p.code, "qty": p.quantity,
                 "entry": p.entry_price, "last": p.last_price,
                 "pct": round(p.pct(), 2),
                 "held_min": round((now - p.entry_time).total_seconds() / 60),
                 "state": p.state(), "watch": p.watch,
                 "adds": [list(step) for step in p.adds],
                 "stop": None if p.watch else round(trader.stop_price(p), 4),
                 "target": None if p.watch else round(trader.target_price(p), 4),
                 "breakeven": round(trader.breakeven_price(p), 4)}
                for p in list(trader.positions.values())
            ]
            managed = set(trader.positions)
            watching = {c for c, p in trader.positions.items() if p.watch}
            out["holdings"] = [
                {"code": code, "qty": h.get("qty", 0),
                 "sellable": h.get("sellable", 0),
                 "avg": h.get("avg_price", 0), "now": h.get("now_price", 0),
                 "pl_rt": h.get("pl_rt", 0), "managed": code in managed,
                 "watching": code in watching}
                for code, h in sorted(trader.holdings.items())
                if h.get("qty", 0) > 0
            ]
            out["trades"] = [
                f"{t.closed_at.strftime('%H:%M:%S')} {t.reason} {t.code} "
                f"{t.quantity}주 {t.pct:+.2f}% ({t.pnl:+,.0f})"
                for t in trader.trades[-20:]
            ]
            codes = list(trader.positions)
            chart_code = codes[0] if codes else ""
            out["chart_code"] = chart_code
            if self.api and chart_code:
                with self.api.lock:
                    out["prices"] = [(t, p) for t, c, p in self.api.prices
                                     if c == chart_code][-300:]
        return out


PAGE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AutoTrader AI 단타</title>
<style>
:root{--bg:#12151c;--card:#1b2029;--line:#2b3342;--txt:#e6e9ef;--dim:#8b93a3;
--up:#e5484d;--down:#3b82f6;--acc:#f0b429;--ok:#22c55e}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--txt);font:14px/1.6 'Malgun Gothic',sans-serif;
padding:20px;max-width:1080px;margin:0 auto}
h1{font-size:20px;margin-bottom:4px} .sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.card .k{color:var(--dim);font-size:12px} .card .v{font-size:18px;font-weight:700;margin-top:2px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
.row label{color:var(--dim);font-size:12px}
select,input,button{background:#232937;color:var(--txt);border:1px solid var(--line);
border-radius:8px;padding:8px 12px;font-size:14px}
button{cursor:pointer} button:hover{border-color:var(--acc)}
button.primary{background:var(--acc);color:#1a1a1a;font-weight:700;border:none}
button.danger{background:#7f1d1d;border:none}
button.paused{background:#1e3a8a;border:none;font-weight:700}
#chartWrap{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px;margin-bottom:14px} canvas{width:100%;height:200px;display:block}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:760px){.cols{grid-template-columns:1fr}}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;min-height:110px;margin-bottom:14px}
.panel h2{font-size:14px;color:var(--dim);margin-bottom:8px}
ul{list-style:none} li{padding:3px 0;border-bottom:1px solid var(--line);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:5px 6px;border-bottom:1px solid var(--line);text-align:right}
th{color:var(--dim);font-weight:400} td:first-child,th:first-child{text-align:left}
.pos{color:var(--ok)} .neg{color:#fca5a5}
.badge{padding:2px 10px;border-radius:999px;font-size:12px;font-weight:700}
.badge.mock{background:#14532d;color:#86efac} .badge.real{background:#7f1d1d;color:#fecaca}
#realConfirm{display:none} .warn{color:#fca5a5;font-size:12px;margin:6px 0}
#note{white-space:pre-wrap;font-size:13px}
.fixed{color:var(--dim);font-size:12px;align-self:flex-end;padding-bottom:8px}
</style></head><body>
<h1>AutoTrader AI 단타 <span id="modeBadge" class="badge mock">모의투자</span></h1>
<div class="sub">대형주·주요 ETF(인버스 포함)만 매매 · AI(Opus 5.5)가 분봉을 보고 진입·청산 시점 판단 · 1회 손실 한도에 맞춰 수량 결정 · 장 시작 15분 뒤(갭 큰 종목은 5분 뒤)~마감 1시간 전</div>

<div class="row">
  <select id="market"><option value="kr">국내주식</option><option value="us">미국주식</option></select>
  <span><label>목표 수익 %</label><br><input id="target" value="2" size="4" title="AI가 목표가를 주지 않을 때 쓰는 기본 목표입니다. AI 목표가는 최소 +1% 이상만 받습니다. 목표가에 닿으면 바로 익절합니다"></span>
  <span><label>최대 손절 %</label><br><input id="stop" value="2" size="4" title="AI가 종목마다 손절가를 정하되, 매수가에서 이 폭보다 멀리 두지 못합니다. 손절가에 닿으면 무조건 전량 매도"></span>
  <span><label>1회 손실 한도 %</label><br><input id="risk" value="0.5" size="4" title="한 번 손절했을 때 잃는 금액이 총 투입 금액의 이 %를 넘지 않게 수량을 정합니다"></span>
  <label title="켜면 AI 판단을 받을 수 없을 때 등락률 상위로 대신 사지 않고 매수를 쉽니다"><input type="checkbox" id="needAI" checked> AI 판단 필수</label>
  <label title="TQQQ·SOXL 같은 지수형 레버리지 ETF와, 지수가 내릴 때 오르는 인버스 ETF(SQQQ·SOXS 등)를 후보에 넣습니다. 같은 지수의 정방향과 인버스를 함께 들지는 않습니다"><input type="checkbox" id="lev" checked> 레버리지·인버스 ETF 포함</label>
  <label title="당일 크게 오르며 VWAP 위에서 저점을 높이는 종목은 눌림을 기다리지 않고 따라 살 수 있게 합니다. 분할 매매를 끈 경우 이렇게 산 종목은 목표가에서 절반만 팔고 나머지는 고점을 따라가며 팝니다. 추세가 이어지지 않는 날에는 손절이 늘 수 있습니다"><input type="checkbox" id="trend" checked> 추세 추종 진입</label>
  <label title="한 번에 사고팔지 않고 세 번에 나눕니다. 매수: 지금 1/3을 사고, 가격이 손절 폭의 1/3·2/3만큼 내려올 때마다 1/3씩 더 삽니다(손절가는 그대로, 전부 산 뒤 손절돼도 1회 손실 한도 안). 매도: 목표의 절반 지점에서 1/3, 목표가에서 1/3, 나머지는 고점을 따라가며 팝니다. 내려오지 않고 바로 오르는 종목은 1/3만 들고 가게 됩니다. 끄면 예전처럼 한 번에 사고팝니다"><input type="checkbox" id="ladder" checked> 분할 매매(3분할)</label>
  <span><label>동시 종목</label><br><input id="maxpos" value="2" size="3"></span>
  <span><label>총 투입 금액</label><br><input id="cash" value="1000000" size="10"></span>
  <span id="realConfirm"><label>실전 확인</label><br><input id="confirm" placeholder="YES 입력" size="6"></span>
  <button class="primary" onclick="start()">시작</button>
  <button id="pauseBtn" onclick="pauseBuys()" title="AI 단타가 새로 사는 것만 멈춥니다. 보유 종목의 손절·익절·본전 감시는 계속됩니다. 지정 종목 분할 매매는 영향을 받지 않습니다(작업별 일시정지로 멈춥니다)">매수 중단</button>
  <button class="danger" onclick="stopT()" title="프로그램의 매매 루프 전체를 멈춥니다. 보유 종목 감시도 멈춥니다">중지</button>
</div>
<div class="row"><span style="flex:1"><label>오늘의 관점 (선택)</label><br><input id="view" style="width:100%" maxlength="300" placeholder="예: 반도체 약세 예상, SOXS 쪽을 먼저 본다" title="AI가 어느 방향의 종목을 먼저 볼지 정할 때 참고합니다. 분봉이 그 방향을 확인해 줄 때만 진입합니다"></span></div>
<div class="warn" id="msg"></div>

<div class="grid">
  <div class="card"><div class="k">상태</div><div class="v" id="stRun">대기</div></div>
  <div class="card"><div class="k">평가액</div><div class="v" id="stEquity">-</div></div>
  <div class="card"><div class="k">실현손익 <span id="feeNote"></span></div><div class="v" id="stPnl">-</div></div>
  <div class="card"><div class="k">오늘 매매</div><div class="v" id="stTrades">-</div></div>
  <div class="card"><div class="k">승 / 패</div><div class="v" id="stWinLoss">-</div></div>
  <div class="card"><div class="k">현금</div><div class="v" id="stCash">-</div></div>
  <div class="card" id="bpCard" title="계좌에서 지금 새로 주문할 수 있는 금액입니다. 위에서 고른 시장 기준이며 30초마다 다시 읽습니다"><div class="k">매수 가능 금액 <span id="bpTime"></span></div><div class="v" id="stBP">-</div><div class="k" id="bpNote"></div></div>
</div>

<div class="panel"><h2>보유 포지션</h2>
  <table><thead><tr><th>종목</th><th>수량</th><th>진입가</th><th>현재가</th>
  <th>수익률</th><th>손절가</th><th>목표가</th><th title="프로그램이 이 종목을 관리하기 시작한 뒤 지난 시간">경과 시간</th><th>상태</th></tr></thead>
  <tbody id="positions"><tr><td colspan="9">없음</td></tr></tbody></table>
</div>

<div class="panel"><h2>실계좌 보유 종목 <span class="fixed">(1분마다 동기화 · 프로그램 밖의 보유는 손대지 않습니다)</span>
  <button id="watchAll" onclick="watchLosers()" title="손실 중인 보유 종목을 모두 손절 없이 지켜보다가 손익분기점에 닿으면 팝니다">손실 종목 모두 본전 감시</button></h2>
  <table><thead><tr><th>종목</th><th>보유</th><th>매입단가</th><th>현재가</th>
  <th>손익률</th><th>처리</th></tr></thead>
  <tbody id="holdings"><tr><td colspan="6">시작 후 1분 안에 표시됩니다 (미국주식)</td></tr></tbody></table>
</div>

<div class="panel"><h2>지정 종목 분할 매매 <span class="fixed">(내가 정한 종목·가격·금액으로 나눠 사고팝니다 · AI 단타와 따로 동작 · 영문 종목코드는 미국주식, 숫자 6자리는 국내주식으로 주문합니다)</span></h2>
  <div class="row">
    <span><label>종목코드</label><br><input id="spCode" size="7" placeholder="SOXL"></span>
    <span><label>매수 지정가</label><br><input id="spPrice" size="7" title="1회차를 사는 가격입니다. 이후 간격만큼 내려갈 때마다 다음 회차를 삽니다. 현재가가 이미 이 가격 아래면 해당 회차들을 곧바로 삽니다"></span>
    <span><label>매도 지정가</label><br><input id="spSell" size="10" placeholder="비우면 회차별" title="넣으면 이 가격부터 간격만큼 올라갈 때마다 남은 수량을 남은 회차로 나눠 팝니다. 비워 두면 각 회차를 자기 매수가보다 간격만큼 오른 가격에 팝니다"></span>
    <span><label>총 금액</label><br><input id="spBudget" size="8" title="이 금액을 횟수로 나눈 만큼씩 삽니다. 계획 가격보다 비싸게 사지 않으므로 쓴 돈이 이 금액을 넘지 않습니다"></span>
    <span><label>횟수</label><br><input id="spSteps" value="5" size="2"></span>
    <span><label>간격 %</label><br><input id="spGap" value="1" size="3"></span>
    <span><label>보유분 매도 수량</label><br><input id="spHold" size="4" placeholder="0" title="이미 계좌에 있는 주식 가운데 이 수량을 분할 매도에 넣습니다(미국주식, 매도 지정가 필요). 총 금액을 0으로 두면 사지 않고 팔기만 합니다"></span>
    <span><label>손절 %</label><br><input id="spStop" value="0" size="3" title="0이면 손절 없음. 값을 넣으면 마지막 회차 가격에서 그만큼 더 내렸을 때 전량 매도하고 끝냅니다"></span>
    <label title="매도 지정가를 비운 경우: 팔린 회차를 같은 가격에서 다시 삽니다. 매도 지정가를 넣은 경우: 전부 팔리면 처음부터 다시 시작합니다"><input type="checkbox" id="spRepeat"> 반복</label>
    <span id="spRealConfirm" style="display:none"><label>실전 확인</label><br><input id="spConfirm" placeholder="YES 입력" size="6"></span>
    <button id="spStartBtn" onclick="splitStart()">계획 확인 후 시작</button>
  </div>
  <div class="fixed" id="spHint"></div>
  <div class="warn" id="spMsg"></div>
  <div id="spJobs">등록된 분할 매매가 없습니다.</div>
  <ul id="spLog"></ul>
</div>

<div id="chartWrap"><canvas id="chart" width="1040" height="200"></canvas></div>

<div class="cols">
  <div class="panel"><h2>청산 기록 / 이벤트</h2><ul id="log"><li>기록 없음</li></ul></div>
  <div class="panel"><h2>AI 매매 판단 근거 <span class="fixed" id="aiLabel"></span></h2><div id="note">아직 판단 전입니다.
AI 판단을 받을 수 없으면 새로 사지 않습니다(보유 종목은 손절·목표 규칙으로 관리).
판단은 참고 자료이며 투자 자문이 아닙니다.</div></div>
</div>

<script>
const $=id=>document.getElementById(id);
const fmt=v=>v==null?"-":Number(v).toLocaleString(undefined,{maximumFractionDigits:2});
$("market").onchange=()=>{ $("cash").value = $("market").value==="us" ? "1000" : "1000000"; };
async function api(path,body){const r=await fetch(path,{method:body?"POST":"GET",
headers:{"Content-Type":"application/json"},body:body?JSON.stringify(body):undefined});
return r.json();}
async function start(){
  const r=await api("/api/start",{market:$("market").value,
    target_pct:$("target").value.trim(),stop_pct:$("stop").value.trim(),
    risk_pct:$("risk").value.trim(),leveraged:$("lev").checked,
    trend:$("trend").checked,ladder:$("ladder").checked,
    view:$("view").value.trim(),
    require_ai:$("needAI").checked,max_positions:$("maxpos").value.trim(),
    cash:$("cash").value.trim(),confirm:$("confirm").value.trim()});
  $("msg").textContent=r.error||"AI 단타 시작 - 장중에 대형주·주요 ETF에서 진입 시점을 판단해 매매합니다";
}
async function adopt(code,mode){
  const msg=mode==="sell"?code+" 보유 수량 전부를 지금 매도합니다. 진행할까요?"
    :mode==="unwatch"?code+"의 본전 감시를 해제합니다. 팔지 않고 그대로 두며, 프로그램은 이 종목을 더 이상 건드리지 않습니다(프로그램이 직접 산 종목이면 익절·손절 규칙이 다시 적용됩니다). 진행할까요?"
    :mode==="watch"?code+"을(를) 본전 감시로 넣습니다. 손절하지 않고 지켜보다가 손익분기점(매입단가+수수료 여유)에 닿으면 전량 매도합니다. 진행할까요?"
    :code+"을(를) 관리 대상으로 편입합니다. 익절·손절 규칙이 즉시 적용되어, 이미 손절선 아래면 바로 손절 매도됩니다. 진행할까요?";
  if(!confirm(msg))return;
  const r=await api("/api/adopt",{code:code,mode:mode});
  $("msg").textContent=r.error||(code+" 요청 접수 — 다음 주기(3초 내)에 처리됩니다");
}
async function watchLosers(){
  if(!confirm("손실 중인 보유 종목을 모두 본전 감시로 넣습니다. 손절하지 않고 지켜보다가 손익분기점에 닿으면 매도합니다. 진행할까요?"))return;
  const r=await api("/api/adopt",{code:"*LOSERS",mode:"watch"});
  $("msg").textContent=r.error||("본전 감시 요청 접수: "+r.codes.join(", "));
}
const esc=t=>String(t==null?"":t).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
function spHint(){const sell=$("spSell").value.trim(),gap=$("spGap").value.trim()||"1",n=$("spSteps").value.trim()||"5";
  $("spHint").textContent=sell
    ?"매도: 매도 지정가 "+sell+"부터 "+gap+"%씩 올라갈 때마다 "+n+"회에 나눠 팝니다."
    :"매도: 매도 지정가를 비워 두어, 각 회차를 자기 매수가보다 "+gap+"% 오른 가격에 팝니다. 매도 지정가를 넣으면 그 가격부터 "+gap+"%씩 올라가며 "+n+"회에 나눠 팝니다.";}
["spSell","spGap","spSteps"].forEach(id=>$(id).oninput=spHint);spHint();
function spBody(){return {market:$("market").value,code:$("spCode").value.trim(),
  price:$("spPrice").value.trim(),budget:$("spBudget").value.trim(),
  steps:$("spSteps").value.trim(),gap_pct:$("spGap").value.trim(),
  mode:$("spSell").value.trim()?"ladder":"each",sell_start:$("spSell").value.trim(),
  hold_qty:$("spHold").value.trim(),stop_pct:$("spStop").value.trim(),
  repeat:$("spRepeat").checked,confirm:$("spConfirm").value.trim()};}
async function splitStart(){
  const body=spBody();
  const p=await api("/api/split/preview",body);
  if(p.error){$("spMsg").textContent=p.error;return;}
  if(p.need_confirm&&body.confirm.toUpperCase()!=="YES"){
    $("spMsg").textContent="실전투자 모드입니다. 실전 확인란에 YES를 입력한 뒤 다시 눌러 주세요 (대소문자 무관)";return;}
  if(!confirm(p.text+"\\n\\n이대로 시작할까요?"))return;
  const r=await api("/api/split/start",body);
  $("spMsg").textContent=r.error||(body.code.toUpperCase()+" 분할 매매를 시작했습니다 - 장중에 가격이 오면 주문합니다");
}
async function splitAct(market,code,action){
  const ask={stop:"의 분할 매매를 끝냅니다. 더 사고팔지 않으며, 지금 가진 수량은 계좌에 그대로 둡니다. 진행할까요?",
    liquidate:"의 보유 수량을 지금 모두 팔고 분할 매매를 끝냅니다. 진행할까요?"}[action];
  if(ask&&!confirm(code+ask))return;
  const r=await api("/api/split/action",{market:market,code:code,action:action,confirm:$("spConfirm").value.trim()});
  $("spMsg").textContent=r.error||(code+" 요청 접수 - 3초 안에 처리됩니다");
}
function spJobHtml(j){
  const live=j.status==="진행"||j.status==="멈춤";
  const b=(a,t,c)=>"<button "+(c?"class='"+c+"' ":"")+"data-m='"+j.market+"' data-c='"+j.code+"' data-a='"+a+
    "' onclick='splitAct(this.dataset.m,this.dataset.c,this.dataset.a)'>"+t+"</button> ";
  const tone=v=>v>=0?"pos":"neg";
  let h="<div style='margin:12px 0 4px'><b>"+esc(j.code)+"</b> · "+(j.market==="us"?"미국":"국내")+" · "+j.mode_label+(j.repeat?" · 반복":"")+
    " · <b>"+j.status+(j.closing?" (정리 중)":"")+"</b> · 보유 "+j.held+"주"+(j.held?" @"+fmt(j.avg):"")+
    " · 현재 "+fmt(j.last||null)+(j.budget>0?" · 사용 "+fmt(j.used)+" / "+fmt(j.budget):" · 보유분 매도")+
    " · 실현 <span class='"+tone(j.realized)+"'>"+fmt(j.realized)+"</span>"+
    (j.held?" · 평가 <span class='"+tone(j.unrealized)+"'>"+fmt(j.unrealized)+"</span>":"")+
    (j.stop_pct?" · 손절 -"+j.stop_pct+"%":"")+
    (j.pending?" · "+esc(j.pending):"")+(j.note?" · <span class='neg'>"+esc(j.note)+"</span>":"")+"</div><div style='margin-bottom:6px'>"+
    (j.status==="진행"?b("pause","일시정지"):"")+(j.status==="멈춤"?b("resume","재개"):"")+
    (live?b("stop","중지(보유 유지)")+b("liquidate","전량 매도 후 종료","danger"):b("remove","목록에서 지우기"))+"</div>";
  h+="<table><thead><tr><th>회차</th><th>매수가</th><th>수량</th><th>보유</th><th>매도가</th><th>상태</th></tr></thead><tbody>";
  const n=Math.max(j.levels.length,j.sells.length);
  for(let i=0;i<n;i++){const l=j.levels[i],sl=j.sells[i];
    const sell=j.mode==="each"?(l?fmt(l.sell):"-"):(sl?fmt(sl.price)+(sl.done?" (매도됨)":""):"-");
    const st=!l?"-":l.done?"완료":l.held>0?"보유":l.bought>0?"일부 체결":"대기";
    h+="<tr><td>"+(i+1)+"</td><td>"+(l?fmt(l.buy):"-")+"</td><td>"+(l?l.qty:"-")+"</td><td>"+(l?l.held:"-")+
      "</td><td>"+sell+"</td><td>"+st+(l&&l.rounds?" · "+l.rounds+"회 완료":"")+"</td></tr>";}
  return h+"</tbody></table>";
}
let buyPaused=false;
async function pauseBuys(){
  const r=await api("/api/pause",{paused:!buyPaused});
  $("msg").textContent=r.error||(r.buy_paused?"매수를 멈췄습니다 - 새로 사지 않고 보유 종목 관리만 계속합니다":"매수를 다시 시작합니다");
}
async function stopT(){const r=await api("/api/stop",{});$("msg").textContent=r.error||"중지 요청 완료";}
function drawChart(prices,code){
  const c=$("chart"),x=c.getContext("2d");x.clearRect(0,0,c.width,c.height);
  if(prices.length<2){x.fillStyle="#8b93a3";x.fillText("보유 포지션이 생기면 해당 종목 시세가 표시됩니다",20,40);return;}
  const vals=prices.map(p=>p[1]),min=Math.min(...vals),max=Math.max(...vals),pad=(max-min)||1;
  x.strokeStyle="#f0b429";x.lineWidth=1.6;x.beginPath();
  prices.forEach((p,i)=>{const px=30+(c.width-50)*i/(prices.length-1),
    py=15+(c.height-40)*(1-(p[1]-min)/pad);i?x.lineTo(px,py):x.moveTo(px,py);});
  x.stroke();
  x.fillStyle="#8b93a3";x.fillText((code?code+"  ":"")+max.toLocaleString(),4,18);
  x.fillText(min.toLocaleString(),4,c.height-22);
}
async function refresh(){
  try{
    const s=await api("/api/status?market="+$("market").value);
    const real=s.config_mode==="real";
    $("modeBadge").textContent=real?"실전투자":"모의투자";
    $("modeBadge").className="badge "+(real?"real":"mock");
    $("realConfirm").style.display=real?"inline-block":"none";
    $("spRealConfirm").style.display=real?"inline-block":"none";
    const bp=s.buying_power,bd=bp&&bp.data;
    if(bd){const usd=bd.currency==="USD";
      $("stBP").textContent=(usd?"$":"")+Number(bd.orderable).toLocaleString(undefined,{maximumFractionDigits:2})+(usd?"":"원");
      $("bpTime").textContent="("+bd.time+")";
      $("bpNote").textContent=usd?("달러 예수금 $"+fmt(bd.deposit)+" · 원화 "+Math.round(bd.krw_deposit).toLocaleString()+"원"):("예수금 "+Math.round(bd.deposit).toLocaleString()+"원");
    }else{$("stBP").textContent=bp&&bp.error?"조회 실패":"조회 중";$("bpTime").textContent="";
      $("bpNote").textContent=bp&&bp.error?bp.error:"";}
    const jobs=(s.split&&s.split.jobs)||[];
    $("spJobs").innerHTML=jobs.length?jobs.map(spJobHtml).join(""):"등록된 분할 매매가 없습니다.";
    $("spLog").innerHTML=((s.split&&s.split.events)||[]).slice().reverse().map(e=>"<li>"+esc(e)+"</li>").join("");
    let run=s.running?("실행 중 · "+(s.session||"-")):"대기";
    if(s.running&&s.session==="장외")run="대기 (장외)";
    if(s.running&&s.minutes_to_close!=null)run+=" · 마감까지 "+s.minutes_to_close+"분";
    if(s.halt_reason)run+=" · "+s.halt_reason;
    $("stRun").textContent=run;
    buyPaused=!!s.buy_paused;
    $("pauseBtn").textContent=buyPaused?"매수 재개":"매수 중단";
    $("pauseBtn").className=buyPaused?"paused":"";
    $("stEquity").textContent=s.equity!=null?Math.round(s.equity).toLocaleString():"-";
    const pnl=s.realized_pnl;
    $("stPnl").textContent=pnl!=null?Math.round(pnl).toLocaleString():"-";
    $("stPnl").className="v "+(pnl>0?"pos":(pnl<0?"neg":""));
    $("feeNote").textContent=(s.params&&s.params.fee_pct>0)?"(수수료 "+
      Number(s.fees||0).toLocaleString(undefined,{maximumFractionDigits:2})+" 차감)":"";
    $("stTrades").textContent=(s.wins+s.losses)+"회";
    $("stWinLoss").textContent=s.wins+" / "+s.losses;
    $("stCash").textContent=s.cash!=null?Math.round(s.cash).toLocaleString():"-";
    $("positions").innerHTML=(s.positions&&s.positions.length)
      ?s.positions.map(p=>"<tr><td>"+p.code+"</td><td>"+p.qty+"</td><td>"+
        p.entry.toLocaleString(undefined,{maximumFractionDigits:2})+"</td><td>"+
        (p.last?p.last.toLocaleString(undefined,{maximumFractionDigits:2}):"-")+
        "</td><td class='"+(p.pct>=0?"pos":"neg")+"'>"+p.pct.toFixed(2)+"%</td><td>"+
        fmt(p.stop)+"</td><td>"+fmt(p.target)+"</td><td>"+
        p.held_min+"분</td><td>"+(p.state||"")+
        (p.adds&&p.adds.length?" · 다음 매수 "+Number(p.adds[0][0]).toLocaleString(
          undefined,{maximumFractionDigits:2})+"("+p.adds[0][1]+"주)":"")+
        (p.watch?" · 본전가 "+
        Number(p.breakeven).toLocaleString(undefined,{maximumFractionDigits:2}):"")+
        "</td></tr>").join("")
      :"<tr><td colspan='9'>없음</td></tr>";
    if(s.holdings&&s.holdings.length){
      $("holdings").innerHTML=s.holdings.map(h=>"<tr><td>"+h.code+"</td><td>"+h.qty+
        "</td><td>"+Number(h.avg).toLocaleString(undefined,{maximumFractionDigits:2})+
        "</td><td>"+Number(h.now).toLocaleString(undefined,{maximumFractionDigits:2})+
        "</td><td class='"+(h.pl_rt>=0?"pos":"neg")+"'>"+Number(h.pl_rt).toFixed(2)+
        "%</td><td>"+(h.watching?"본전 감시 중 "+
        "<button data-c='"+h.code+"' data-m='unwatch' onclick='adopt(this.dataset.c,this.dataset.m)'>감시 해제</button> "+
        "<button class='danger' data-c='"+h.code+"' data-m='sell' onclick='adopt(this.dataset.c,this.dataset.m)'>즉시 매도</button>"
        :h.managed?"관리 중 "+(h.pl_rt<0?
        "<button data-c='"+h.code+"' data-m='watch' onclick='adopt(this.dataset.c,this.dataset.m)'>본전 감시</button>":"")
        :(h.pl_rt<0?"<button data-c='"+h.code+"' data-m='watch' onclick='adopt(this.dataset.c,this.dataset.m)'>본전 감시</button> ":"")+
        "<button data-c='"+h.code+"' data-m='manage' onclick='adopt(this.dataset.c,this.dataset.m)'>관리 편입</button> "+
        "<button class='danger' data-c='"+h.code+"' data-m='sell' onclick='adopt(this.dataset.c,this.dataset.m)'>즉시 매도</button>")+
        "</td></tr>").join("");
    }
    drawChart(s.prices,s.chart_code);
    $("log").innerHTML=[...s.trades,...s.events].slice(-15).reverse()
      .map(e=>"<li>"+e+"</li>").join("")||"<li>기록 없음</li>";
    $("aiLabel").textContent=s.ai?"("+s.ai+")":"";
    if(s.selection_note||s.review_note)$("note").textContent=
      (s.selection_note?"[진입 판단]\\n"+s.selection_note:"")+
      (s.review_note?(s.selection_note?"\\n\\n":"")+"[보유 종목 점검]\\n"+s.review_note:"")+
      "\\n\\n※ 참고 자료이며 투자 자문이 아닙니다.";
    if(s.last_error)$("msg").textContent=s.last_error;
  }catch(e){}
}
setInterval(refresh,2000);refresh();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    state: AppState  # run_ui에서 주입

    def log_message(self, *args):  # 콘솔 소음 억제
        pass

    def _send(self, obj, code=200, content_type="application/json"):
        data = (obj if isinstance(obj, bytes)
                else json.dumps(obj, ensure_ascii=False).encode("utf-8"))
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        if self.path == "/":
            self._send(PAGE.encode("utf-8"), content_type="text/html")
        elif self.path.split("?")[0] == "/api/status":
            query = self.path.partition("?")[2]
            market = next((part[7:] for part in query.split("&")
                           if part.startswith("market=")), None)
            self._send(self.state.status(market))
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        body = self._body()
        try:
            if self.path == "/api/start":
                self._send(self.state.start(
                    market=body.get("market", "kr"),
                    target_pct=body.get("target_pct", 2.0),
                    stop_pct=body.get("stop_pct", 2.0),
                    require_ai=bool(body.get("require_ai", True)),
                    risk_pct=body.get("risk_pct", 0.5),
                    leveraged=bool(body.get("leveraged", True)),
                    trend=bool(body.get("trend", True)),
                    ladder=bool(body.get("ladder", True)),
                    view=str(body.get("view", "")),
                    max_positions=body.get("max_positions", 2),
                    cash=body.get("cash"),
                    confirm=body.get("confirm", ""),
                ))
            elif self.path == "/api/pause":
                self._send(self.state.pause_buys(bool(body.get("paused", True))))
            elif self.path == "/api/stop":
                self._send(self.state.stop())
            elif self.path == "/api/split/preview":
                self._send(self.state.split_preview(**body))
            elif self.path == "/api/split/start":
                self._send(self.state.split_start(**body))
            elif self.path == "/api/split/action":
                self._send(self.state.split_action(**body))
            elif self.path == "/api/adopt":
                self._send(self.state.adopt(body.get("code", ""),
                                            body.get("mode", "")))
            else:
                self._send({"error": "not found"}, 404)
        except Exception as e:  # 어떤 오류든 콘솔 스택 대신 화면에 안내한다
            self._send({"error": f"{type(e).__name__}: {e}"})


def run_ui(config_path: str = "config.ini", port: int = 8899,
           open_browser: bool = True) -> None:
    state = AppState(config_path)
    state.load_saved_splits()
    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"AutoTrader AI 단타 대시보드: {url}  (종료: Ctrl+C)")
    if open_browser:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if state.trader:
            state.trader.stop()
        for engine in state.splitters.values():
            engine.stop()
        server.server_close()
