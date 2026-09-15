"""키움증권 REST API 클라이언트 (OCX 불필요, 표준 라이브러리만 사용).

- 토큰: POST {host}/oauth2/token  (grant_type=client_credentials, appkey, secretkey)
- 현재가: POST {host}/api/dostk/stkinfo, api-id=ka10001, body {stk_cd} → cur_prc
- 주문:  POST {host}/api/dostk/ordr,  api-id=kt10000(매수)/kt10001(매도)

모의투자 도메인(mockapi.kiwoom.com)이 기본이며, 실전(api.kiwoom.com)은
호출부에서 명시적으로 선택해야 한다. LiveTrader의 MarketAPI 프로토콜
(current_price, send_market_order)을 그대로 구현하므로 실시간 루프를
OCX 버전과 동일하게 재사용한다.
"""

from __future__ import annotations

import configparser
import json
import logging
import time
import urllib.request
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

MOCK_HOST = "https://mockapi.kiwoom.com"
REAL_HOST = "https://api.kiwoom.com"

# 국내주식
_API_PRICE_KR = ("/api/dostk/stkinfo", "ka10001")
_API_ORDER_KR = {"buy": ("/api/dostk/ordr", "kt10000"),
                 "sell": ("/api/dostk/ordr", "kt10001")}
# 미국주식 (주문은 지정가 방식: ord_uv 필수)
_API_PRICE_US = ("/api/us/mrkcond", "usa20100")
_API_ORDER_US = {"buy": ("/api/us/ordr", "ust20000"),
                 "sell": ("/api/us/ordr", "ust20001")}
# 미국주식 현재가 응답에서 시도할 필드 후보
_US_PRICE_KEYS = ("cur_prc", "last_pric", "cur_pric", "now_pric", "prpr")

# 순위 조회 (추천 후보 수집용): (market, criteria) → (경로, api-id, 본문)
_API_RANK = {
    ("kr", "volume"): ("/api/dostk/rkinfo", "ka10030", {   # 당일거래량상위
        "mrkt_tp": "000", "sort_tp": "1", "mang_stk_incls": "0", "crd_tp": "0",
        "trde_qty_tp": "0", "pric_tp": "0", "trde_prica_tp": "0",
        "mrkt_open_tp": "0", "stex_tp": "3",
    }),
    ("kr", "change"): ("/api/dostk/rkinfo", "ka10027", {   # 전일대비등락률상위
        "mrkt_tp": "000", "sort_tp": "1", "trde_qty_cnd": "0000",
        "stk_cnd": "0", "crd_cnd": "0", "updown_incls": "1",
        "pric_cnd": "0", "trde_prica_cnd": "0", "stex_tp": "3",
    }),
    ("us", "volume"): ("/api/us/rkinfo", "usa20530", {     # 당일 거래량 상위
        "stex_tp": "0", "inds_cd": "", "stk_tp": "0", "trde_qty_tp": "0",
        "qry_tp": "0", "stk_cnd": "0", "pric_cnd": "0", "trde_prica_cnd": "0",
    }),
    ("us", "change"): ("/api/us/rkinfo", "usa20910", {     # 전일대비 등락률상위
        "stex_tp": "0", "inds_cd": "", "inds_cls_tp": "0", "sort_tp": "1",
        "stk_tp": "0", "stk_cnd": "0", "pric_cnd": "0",
        "trde_prica_cnd": "0", "trde_qty_tp": "",
    }),
}
RANK_CRITERIA_LABELS = {"volume": "당일 거래량 상위", "change": "등락률 상위"}

Transport = Callable[[str, dict, dict], dict]


class KiwoomRestError(RuntimeError):
    pass


def _decode_json(raw: bytes, url: str) -> dict:
    """서버 응답을 JSON으로 해석한다. 빈/비정상 응답은 원인 안내와 함께 실패."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise KiwoomRestError(
            f"키움 서버가 빈 응답을 반환했다 ({url}). "
            "서버 점검 시간이거나 일시 장애일 수 있으니 잠시 후 다시 시도해 달라."
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise KiwoomRestError(
            f"키움 서버 응답이 JSON이 아니다 ({url}): {text[:200]}"
        ) from None


def _http_post(url: str, headers: dict, body: dict) -> dict:
    import urllib.error

    data = json.dumps(body).encode("utf-8")
    error: KiwoomRestError | None = None
    for attempt in (1, 2):  # 일시 장애는 1회 재시도
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return _decode_json(resp.read(), url)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return json.loads(raw.decode("utf-8"))  # JSON 오류 응답은 그대로 전달
            except (json.JSONDecodeError, UnicodeDecodeError):
                error = KiwoomRestError(
                    f"HTTP {e.code} 오류 ({url}): {raw[:200]!r}")
        except urllib.error.URLError as e:
            error = KiwoomRestError(f"네트워크 오류 ({url}): {e.reason}")
        except KiwoomRestError as e:
            error = e
        if attempt == 1:
            logger.warning("요청 실패, 1초 후 재시도: %s", error)
            time.sleep(1.0)
    assert error is not None
    raise error


class KiwoomRestClient:
    """MarketAPI 프로토콜 구현. transport 주입으로 테스트 가능."""

    def __init__(self, appkey: str, secretkey: str, mock: bool = True,
                 market: str = "kr", exchange: str = "ND",
                 transport: Transport = _http_post):
        if not appkey or not secretkey:
            raise KiwoomRestError(
                "API 키가 없다. openapi.kiwoom.com 에서 발급받은 appkey/secretkey를 "
                "config.ini에 입력해야 한다."
            )
        if market not in ("kr", "us"):
            raise KiwoomRestError(f"market은 'kr' 또는 'us'여야 한다: {market}")
        self.appkey = appkey
        self.secretkey = secretkey
        self.mock = mock
        self.market = market
        self.exchange = exchange  # 미국주식 거래소 구분 (ND=나스닥 등)
        self.host = MOCK_HOST if mock else REAL_HOST
        self._transport = transport
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._last_prices: dict[str, float] = {}  # 종목별 직전 시세 (미국 지정가 주문용)
        self._exchanges: dict[str, str] = {}      # 종목별 거래소구분 캐시 (NA/ND/NY)

    # ── 인증 ────────────────────────────────────────────────
    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        res = self._transport(
            self.host + "/oauth2/token",
            {"Content-Type": "application/json;charset=UTF-8"},
            {"grant_type": "client_credentials",
             "appkey": self.appkey, "secretkey": self.secretkey},
        )
        token = res.get("token")
        if not token:
            hint = ""
            if "투자구분" in str(res):
                hint = (
                    "\n→ 키 종류와 접속 서버가 어긋났다. 모의투자(mode=mock)에는 "
                    "openapi.kiwoom.com에서 발급한 '모의투자용' 키가, "
                    "실전(mode=real)에는 '실전용' 키가 필요하다. "
                    "config.ini의 mode와 키를 같은 종류로 맞춰야 한다."
                )
            raise KiwoomRestError(f"토큰 발급 실패: {res}{hint}")
        self._token = token
        # expires_dt(yyyyMMddHHmmss)가 있으면 사용, 없으면 23시간으로 가정
        expires_dt = str(res.get("expires_dt", ""))
        if len(expires_dt) == 14:
            expires = time.mktime(time.strptime(expires_dt, "%Y%m%d%H%M%S"))
        else:
            expires = time.time() + 23 * 3600
        self._token_expires_at = expires
        logger.info("접근토큰 발급 완료 (%s)", "모의투자" if self.mock else "실전")
        return token

    def _call(self, path: str, api_id: str, body: dict) -> dict:
        token = self._ensure_token()
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "api-id": api_id,
        }
        res = self._transport(self.host + path, headers, body)
        code = res.get("return_code")
        if code is not None and int(code) != 0:
            raise KiwoomRestError(
                f"{api_id} 실패 (return_code={code}): {res.get('return_msg', '')}")
        return res

    # ── MarketAPI 구현 ──────────────────────────────────────
    @staticmethod
    def _parse_price(raw) -> float:
        return abs(float(str(raw).replace(",", "").replace("+", "")))

    def resolve_exchange(self, code: str) -> str:
        """종목의 거래소구분(NA=AMEX, ND=NASDAQ, NY=NYSE)을 조회·캐시한다."""
        cached = self._exchanges.get(code)
        if cached:
            return cached
        try:
            res = self._call("/api/us/stkinfo", "usa10098", {"stk_cd": code})
            rows = res.get("list") or []
            ex = str(rows[0].get("stex_tp", "")).strip() if rows else ""
        except (KiwoomRestError, IndexError, AttributeError):
            ex = ""
        ex = ex or self.exchange
        self._exchanges[code] = ex
        return ex

    def current_price(self, code: str) -> float:
        if self.market == "us":
            res = self._call(_API_PRICE_US[0], _API_PRICE_US[1],
                             {"stex_tp": self.resolve_exchange(code),
                              "stk_cd": code})
            for key in _US_PRICE_KEYS:
                if res.get(key) not in (None, ""):
                    price = self._parse_price(res[key])
                    break
            else:
                raise KiwoomRestError(
                    f"미국주식 현재가 필드를 찾지 못했다. 응답 키: {sorted(res.keys())}")
        else:
            res = self._call(_API_PRICE_KR[0], _API_PRICE_KR[1], {"stk_cd": code})
            raw = res.get("cur_prc")
            if raw in (None, ""):
                raise KiwoomRestError(
                    f"현재가(cur_prc)를 찾지 못했다. 응답 키: {sorted(res.keys())}")
            price = self._parse_price(raw)
        self._last_prices[code] = price
        return price

    def send_market_order(self, account_no: str, code: str, side: str,
                          quantity: int) -> None:
        if self.market == "us":
            # 같은 종목의 미체결 주문이 남아 있으면 자전거래 거부(999999)와
            # 유령 포지션의 원인이 되므로 먼저 취소한다
            try:
                self.cancel_open_orders(code)
            except Exception as e:
                logger.warning("%s 미체결 취소 중 오류(주문은 계속 진행): %s", code, e)
            # 미국주식은 시장가 코드가 없어 해당 종목의 직전 시세 지정가로 집행한다
            last = self._last_prices.get(code)
            if last is None:
                raise KiwoomRestError(f"{code}의 직전 시세가 없어 주문 단가를 정할 수 없다")
            path, api_id = _API_ORDER_US[side]
            body = {
                "stex_tp": self.resolve_exchange(code),
                "stk_cd": code,
                "ord_qty": str(quantity),
                "ord_uv": f"{last:.2f}",
                "trde_tp": "00",     # 00 = 지정가
            }
            if side == "sell":
                body["stop_pric"] = ""
        else:
            path, api_id = _API_ORDER_KR[side]
            body = {
                "dmst_stex_tp": "KRX",   # 국내거래소
                "stk_cd": code,
                "ord_qty": str(quantity),
                "ord_uv": "",            # 시장가는 단가 없음
                "trde_tp": "3",          # 3 = 시장가
                "cond_uv": "",
            }
        res = self._call(path, api_id, body)
        logger.info("주문 전송: [%s] %s %s %d주 (주문번호=%s)",
                    self.market, code, side, quantity, res.get("ord_no", "?"))

    def top_stocks(self, criteria: str = "volume", limit: int = 10) -> list[dict]:
        """순위 상위 종목 목록(추천 후보). criteria: volume(거래량) | change(등락률)."""
        try:
            path, api_id, body = _API_RANK[(self.market, criteria)]
        except KeyError:
            raise KiwoomRestError(f"지원하지 않는 순위 기준이다: {criteria}")
        res = self._call(path, api_id, dict(body))
        rows = next(
            (v for v in res.values() if isinstance(v, list) and v
             and isinstance(v[0], dict)),
            [],
        )
        out = []
        for row in rows[:limit]:
            def pick(*keys):
                for k in keys:
                    if row.get(k) not in (None, ""):
                        return str(row[k]).strip()
                return ""
            # 국내 순위 응답은 NXT 통합 표기(예: 005930_AL)로 올 수 있는데,
            # 주문·시세 API는 순수 6자리 코드만 받으므로 접미사를 뗀다
            code = pick("stk_cd", "code")
            if self.market != "us" and "_" in code:
                code = code.split("_", 1)[0]
            item = {
                "code": code,
                "name": pick("stk_nm", "name"),
                "price": pick("cur_prc", "last_pric", "now_pric"),
                "change_pct": pick("flu_rt", "updown_rt", "chg_rt"),
                "volume": pick("trde_qty", "now_trde_qty", "acc_trde_qty"),
                "exchange": pick("stex_tp"),
            }
            # 순위 응답이 알려준 거래소를 캐시해 두면 시세·주문에 그대로 쓴다
            if item["code"] and item["exchange"]:
                self._exchanges[item["code"]] = item["exchange"]
            out.append(item)
        return out

    def top_volume_stocks(self, limit: int = 10) -> list[dict]:
        return self.top_stocks("volume", limit)

    # ── 미국주식 계좌·주문 관리 ─────────────────────────────
    @staticmethod
    def _to_int(raw) -> int:
        try:
            return int(float(str(raw).strip() or 0))
        except ValueError:
            return 0

    def us_balances(self) -> dict[str, dict]:
        """실제 계좌의 미국주식 보유 현황(ust21070). {종목: {qty, sellable}}"""
        res = self._call("/api/us/acnt", "ust21070", {"stex_tp": "", "stk_cd": ""})
        out: dict[str, dict] = {}
        for row in res.get("result_list") or []:
            code = str(row.get("stk_cd", "")).strip()
            if code:
                out[code] = {"qty": self._to_int(row.get("poss_qty")),
                             "sellable": self._to_int(row.get("sell_alowq"))}
        return out

    def us_open_orders(self, code: str = "") -> list[dict]:
        """미체결 주문 목록(ust21050).

        종목을 지정해 조회하면 stex_tp가 필수라 1517 오류가 나므로,
        항상 전체를 조회한 뒤 파이썬에서 종목을 걸러낸다.
        """
        res = self._call("/api/us/acnt", "ust21050",
                         {"ord_dt": "", "slby_tp": "0", "stex_tp": "",
                          "stk_cd": ""})
        out = []
        for row in res.get("result_list") or []:
            ord_no = str(row.get("ord_no", "")).strip()
            row_code = str(row.get("stk_cd", "")).strip()
            if ord_no and (not code or row_code == code):
                out.append({"ord_no": ord_no, "code": row_code})
        return out

    def cancel_open_orders(self, code: str) -> int:
        """해당 종목의 미체결 주문을 전부 취소(ust20003)한다. 취소 건수 반환."""
        count = 0
        for order in self.us_open_orders(code):
            try:
                self._call("/api/us/ordr", "ust20003",
                           {"orig_ord_no": order["ord_no"],
                            "stex_tp": self.resolve_exchange(code),
                            "stk_cd": code})
                count += 1
            except KiwoomRestError as e:
                logger.warning("%s 미체결 취소 실패(%s): %s",
                               code, order["ord_no"], e)
        if count:
            logger.info("%s 미체결 %d건 취소", code, count)
        return count


# ── 설정 파일 ───────────────────────────────────────────────
def _read_parser(path: str | Path) -> configparser.ConfigParser:
    """UTF-8과 CP949(한글 Windows 메모장 기본)를 모두 허용해 ini를 읽는다."""
    path = Path(path)
    if not path.exists():
        raise KiwoomRestError(
            f"설정 파일이 없다: {path}\n"
            "config.ini를 만들고 [kiwoom] 섹션에 appkey/secretkey를 입력해야 한다."
        )
    # interpolation=None: 값/주석에 %가 있어도 오류 없이 그대로 읽는다.
    # inline_comment_prefixes: "값 ; 설명" 형태의 줄 끝 주석을 허용한다.
    parser = configparser.ConfigParser(
        interpolation=None, inline_comment_prefixes=(";", "#"))
    for enc in ("utf-8-sig", "cp949"):
        try:
            parser.read(path, encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    return parser


def load_config(path: str | Path) -> dict:
    """config.ini에서 [kiwoom] appkey/secretkey/mode를 읽는다."""
    parser = _read_parser(path)
    section = parser["kiwoom"] if parser.has_section("kiwoom") else {}
    return {
        "appkey": section.get("appkey", "").strip(),
        "secretkey": section.get("secretkey", "").strip(),
        "mode": section.get("mode", "mock").strip().lower(),
        # 미국주식 주간거래(한국 낮) 매매 허용. 기본 켬 — 끄려면 false
        "us_day_session": section.get("us_day_session", "true").strip().lower()
                          in ("1", "true", "yes", "y", "on"),
    }


def load_risk_config(path: str | Path):
    """config.ini의 [risk] 섹션을 RiskConfig로 읽는다. 값은 퍼센트 숫자.

    섹션이 없으면 None을 반환한다(기본 리스크 설정 사용).
    """
    from .risk import RiskConfig

    parser = _read_parser(path)
    if not parser.has_section("risk"):
        return None
    section = parser["risk"]

    def pct(key: str, default: float, allow_zero: bool = False) -> float:
        value = float(section.get(key, default))
        low_ok = value >= 0 if allow_zero else value > 0
        if not (low_ok and value <= 100):
            raise KiwoomRestError(f"[risk] {key} 값이 범위를 벗어났다: {value}")
        return value / 100.0

    def bars(key: str, default: int) -> int:
        value = int(float(section.get(key, default)))
        if value < 0:
            raise KiwoomRestError(f"[risk] {key}는 0 이상이어야 한다: {value}")
        return value

    return RiskConfig(
        max_position_pct=pct("max_position_pct", 20.0),
        order_cash_pct=pct("order_cash_pct", 10.0),
        max_drawdown_pct=pct("max_drawdown_pct", 15.0),
        stop_loss_pct=pct("stop_loss_pct", 3.0, allow_zero=True),  # 0 = 손절 끔
        reentry_cooldown_bars=bars("reentry_cooldown_bars", 5),  # 매도 후 재매수 대기
        min_hold_bars=bars("min_hold_bars", 3),  # 매수 후 최소 보유 (손절 예외)
    )
