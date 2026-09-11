"""키움 OpenAPI+ (OCX) 얇은 래퍼.

이 모듈만 Windows/PyQt5에 의존한다. PyQt5 임포트는 지연시켜,
다른 OS에서도 패키지 임포트와 테스트가 가능하도록 한다.

사전 준비(Windows):
  1) 키움증권 계좌 개설 + 모의투자 신청
  2) 키움 OpenAPI+ 사용 신청 및 모듈 설치
  3) 32비트 Python + `pip install PyQt5`
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 서버 구분: GetLoginInfo("GetServerGubun") == "1" 이면 모의투자 서버
SIMULATION_SERVER = "1"

_ORDER_TYPE = {"buy": 1, "sell": 2}  # 신규매수 1, 신규매도 2
_HOGA_MARKET = "03"  # 시장가


@dataclass
class AccountInfo:
    account_no: str
    user_id: str
    user_name: str
    is_simulation: bool


class KiwoomOpenAPI:
    """QAxWidget("KHOPENAPI.KHOpenAPICtrl.1") 래퍼.

    현재가 조회는 opt10001(주식기본정보) TR을 사용하고,
    주문은 SendOrder 시장가 주문만 지원한다(1차 버전).
    """

    TR_TIMEOUT_MS = 5_000

    def __init__(self) -> None:
        try:
            from PyQt5.QAxContainer import QAxWidget
            from PyQt5.QtCore import QEventLoop
            from PyQt5.QtWidgets import QApplication
        except ImportError as e:  # pragma: no cover - Windows 전용 경로
            raise RuntimeError(
                "PyQt5가 필요하다. Windows 32비트 파이썬에서 `pip install PyQt5` 후 "
                "키움 OpenAPI+ 모듈을 설치하고 실행해야 한다."
            ) from e

        self._QEventLoop = QEventLoop
        self._app = QApplication.instance() or QApplication([])
        self._ocx = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self._login_loop: object | None = None
        self._tr_loop: object | None = None
        self._tr_data: dict[str, str] = {}
        self._login_err = 0

        self._ocx.OnEventConnect.connect(self._on_event_connect)
        self._ocx.OnReceiveTrData.connect(self._on_receive_tr_data)

    # ── 로그인 ──────────────────────────────────────────────
    def connect(self) -> AccountInfo:
        """로그인 창을 띄우고 접속을 기다린다."""
        self._login_loop = self._QEventLoop()
        self._ocx.dynamicCall("CommConnect()")
        self._login_loop.exec_()
        if self._login_err != 0:
            raise ConnectionError(f"키움 로그인 실패 (err={self._login_err})")

        accounts = self._login_info("ACCLIST").rstrip(";").split(";")
        info = AccountInfo(
            account_no=accounts[0],
            user_id=self._login_info("USER_ID"),
            user_name=self._login_info("USER_NAME"),
            is_simulation=self._login_info("GetServerGubun") == SIMULATION_SERVER,
        )
        logger.info("로그인 완료: %s (%s, 모의투자=%s)",
                    info.user_name, info.account_no, info.is_simulation)
        return info

    def _login_info(self, tag: str) -> str:
        return self._ocx.dynamicCall("GetLoginInfo(QString)", tag).strip()

    def _on_event_connect(self, err_code: int) -> None:
        self._login_err = err_code
        if self._login_loop is not None:
            self._login_loop.exit()

    # ── 시세 조회 ────────────────────────────────────────────
    def current_price(self, code: str) -> float:
        """opt10001로 현재가를 조회한다."""
        self._ocx.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        self._tr_data = {}
        self._tr_loop = self._QEventLoop()
        ret = self._ocx.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            "현재가조회", "opt10001", 0, "0101",
        )
        if ret != 0:
            raise RuntimeError(f"시세 요청 실패 (ret={ret})")
        self._tr_loop.exec_()
        price = self._tr_data.get("현재가", "0")
        return abs(float(price))  # 하락 시 음수로 내려오므로 절대값

    def stock_name(self, code: str) -> str:
        return self._ocx.dynamicCall("GetMasterCodeName(QString)", code).strip()

    def _on_receive_tr_data(self, screen_no, rqname, trcode, record, prev_next,
                            *_args) -> None:
        if rqname == "현재가조회":
            for field in ("현재가", "거래량"):
                value = self._ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, rqname, 0, field,
                ).strip()
                self._tr_data[field] = value or "0"
        if self._tr_loop is not None:
            self._tr_loop.exit()

    # ── 주문 ────────────────────────────────────────────────
    def send_market_order(self, account_no: str, code: str, side: str,
                          quantity: int) -> None:
        """시장가 주문 전송. side는 'buy' 또는 'sell'."""
        ret = self._ocx.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            ["자동매매주문", "0102", account_no, _ORDER_TYPE[side], code,
             quantity, 0, _HOGA_MARKET, ""],
        )
        if ret != 0:
            raise RuntimeError(f"주문 전송 실패 (ret={ret})")
        logger.info("주문 전송: %s %s %d주 (시장가)", code, side, quantity)
