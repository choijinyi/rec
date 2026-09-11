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


def _http_post(url: str, headers: dict, body: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers=headers, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


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

    def current_price(self, code: str) -> float:
        if self.market == "us":
            res = self._call(_API_PRICE_US[0], _API_PRICE_US[1],
                             {"stex_tp": self.exchange, "stk_cd": code})
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
            # 미국주식은 시장가 코드가 없어 해당 종목의 직전 시세 지정가로 집행한다
            last = self._last_prices.get(code)
            if last is None:
                raise KiwoomRestError(f"{code}의 직전 시세가 없어 주문 단가를 정할 수 없다")
            path, api_id = _API_ORDER_US[side]
            body = {
                "stex_tp": self.exchange,
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
            out.append({
                "code": pick("stk_cd", "code"),
                "name": pick("stk_nm", "name"),
                "price": pick("cur_prc", "last_pric", "now_pric"),
                "change_pct": pick("flu_rt", "updown_rt", "chg_rt"),
                "volume": pick("trde_qty", "now_trde_qty", "acc_trde_qty"),
            })
        return out

    def top_volume_stocks(self, limit: int = 10) -> list[dict]:
        return self.top_stocks("volume", limit)


# ── 설정 파일 ───────────────────────────────────────────────
def _read_parser(path: str | Path) -> configparser.ConfigParser:
    """UTF-8과 CP949(한글 Windows 메모장 기본)를 모두 허용해 ini를 읽는다."""
    path = Path(path)
    if not path.exists():
        raise KiwoomRestError(
            f"설정 파일이 없다: {path}\n"
            "config.ini를 만들고 [kiwoom] 섹션에 appkey/secretkey를 입력해야 한다."
        )
    parser = configparser.ConfigParser()
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

    def pct(key: str, default: float) -> float:
        value = float(section.get(key, default))
        if not 0 < value <= 100:
            raise KiwoomRestError(f"[risk] {key}는 0보다 크고 100 이하여야 한다: {value}")
        return value / 100.0

    return RiskConfig(
        max_position_pct=pct("max_position_pct", 20.0),
        order_cash_pct=pct("order_cash_pct", 10.0),
        max_drawdown_pct=pct("max_drawdown_pct", 15.0),
    )
