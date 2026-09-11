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

_API_PRICE = ("/api/dostk/stkinfo", "ka10001")
_API_ORDER = {"buy": ("/api/dostk/ordr", "kt10000"),
              "sell": ("/api/dostk/ordr", "kt10001")}

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
                 transport: Transport = _http_post):
        if not appkey or not secretkey:
            raise KiwoomRestError(
                "API 키가 없다. openapi.kiwoom.com 에서 발급받은 appkey/secretkey를 "
                "config.ini에 입력해야 한다."
            )
        self.appkey = appkey
        self.secretkey = secretkey
        self.mock = mock
        self.host = MOCK_HOST if mock else REAL_HOST
        self._transport = transport
        self._token: str | None = None
        self._token_expires_at = 0.0

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
            raise KiwoomRestError(f"토큰 발급 실패: {res}")
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
    def current_price(self, code: str) -> float:
        res = self._call(_API_PRICE[0], _API_PRICE[1], {"stk_cd": code})
        raw = res.get("cur_prc")
        if raw in (None, ""):
            raise KiwoomRestError(
                f"현재가(cur_prc)를 찾지 못했다. 응답 키: {sorted(res.keys())}")
        return abs(float(str(raw).replace(",", "").replace("+", "")))

    def send_market_order(self, account_no: str, code: str, side: str,
                          quantity: int) -> None:
        path, api_id = _API_ORDER[side]
        body = {
            "dmst_stex_tp": "KRX",   # 국내거래소
            "stk_cd": code,
            "ord_qty": str(quantity),
            "ord_uv": "",            # 시장가는 단가 없음
            "trde_tp": "3",          # 3 = 시장가
            "cond_uv": "",
        }
        res = self._call(path, api_id, body)
        logger.info("주문 전송: %s %s %d주 (시장가, 주문번호=%s)",
                    code, side, quantity, res.get("ord_no", "?"))


# ── 설정 파일 ───────────────────────────────────────────────
def load_config(path: str | Path) -> dict:
    """config.ini에서 [kiwoom] appkey/secretkey/mode를 읽는다.

    인코딩은 UTF-8과 CP949(한글 Windows 메모장 기본) 모두 허용한다.
    """
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
    section = parser["kiwoom"] if parser.has_section("kiwoom") else {}
    return {
        "appkey": section.get("appkey", "").strip(),
        "secretkey": section.get("secretkey", "").strip(),
        "mode": section.get("mode", "mock").strip().lower(),
    }
