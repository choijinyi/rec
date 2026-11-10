"""매매 허용 종목군: 대형주와 주요 ETF.

등락률·거래량 상위에서 아무 종목이나 집으면 주식병합·증자를 반복하는
초소형주가 섞여 들어온다(팔고 싶어도 받아 줄 상대가 없다). 그래서 여기에
적힌 종목만 매매 후보로 삼는다. 목록에 없는 종목은 순위 상위에 올라와도
사지 않는다.

config.ini [universe] 섹션으로 조정할 수 있다:
    add = COIN, HOOD        ; 추가할 종목
    remove = TSLA           ; 뺄 종목
    restrict = false        ; 종목군 제한을 끈다 (권장하지 않음)
"""

from __future__ import annotations

from typing import Iterable

# 구분: stock(대형주) / etf(지수·섹터 ETF) / lev(레버리지·인버스 ETF)
STOCK, ETF, LEVERAGED = "stock", "etf", "lev"
KIND_LABELS = {STOCK: "대형주", ETF: "ETF", LEVERAGED: "레버리지 ETF"}

US_STOCKS = """
AAPL MSFT NVDA AMZN GOOGL GOOG META TSLA AVGO LLY JPM V MA UNH XOM COST WMT
HD PG JNJ ORCL NFLX BAC CRM AMD ABBV KO CVX MRK ADBE PEP CSCO TMO ACN LIN MCD
ABT WFC IBM GE QCOM TXN INTU NOW DIS AMAT ISRG CAT GS MS AXP UBER BKNG PFE VZ
T CMCSA RTX HON LOW NKE SBUX BA MU INTC LRCX KLAC PANW ADI PLTR CRWD SHOP ARM
MRVL DELL PYPL C SCHW BLK TSM ASML BABA NVO
""".split()

US_ETFS = """
SPY QQQ IWM DIA VOO IVV VTI XLK XLF XLE XLV XLY XLI XLP XLU XLC SMH SOXX XBI
KRE GLD SLV TLT HYG EEM EFA FXI KWEB
""".split()

# 지수형 레버리지·인버스 ETF (단일 종목 레버리지 ETP는 넣지 않는다)
US_LEVERAGED = """
TQQQ SQQQ SOXL SOXS UPRO SPXU SPXL SPXS TNA TZA QLD SSO TECL
""".split()

KR_STOCKS = """
005930 000660 373220 207940 005380 000270 068270 105560 035420 055550 012330
005490 028260 035720 051910 006400 086790 066570 003670 032830 015760 034730
096770 033780 316140 009150 010130 012450 329180 042660 009540 034020 011200
017670 030200 018260 003550 010140 267260 402340 138040 000810 024110 259960
064350 079550 047810 010120 352820 323410
""".split()

KR_ETFS = """
069500 102110 229200 133690 360750 379800 091160 305720 381180
""".split()

KR_LEVERAGED = """
122630 114800 252670 233740 251340 462330
""".split()


def _codes(raw: Iterable[str] | str | None) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    return [str(c).strip().upper() for c in raw if str(c).strip()]


def build_universe(market: str, leveraged: bool = True,
                   add: Iterable[str] | str | None = None,
                   remove: Iterable[str] | str | None = None) -> dict[str, str]:
    """{종목코드: 구분}. leveraged=False면 레버리지·인버스 ETF를 뺀다."""
    if market == "us":
        stocks, etfs, lev = US_STOCKS, US_ETFS, US_LEVERAGED
    else:
        stocks, etfs, lev = KR_STOCKS, KR_ETFS, KR_LEVERAGED
    out: dict[str, str] = {}
    for code in stocks:
        out[code] = STOCK
    for code in etfs:
        out[code] = ETF
    if leveraged:
        for code in lev:
            out[code] = LEVERAGED
    for code in _codes(add):
        out.setdefault(code, STOCK)
    for code in _codes(remove):
        out.pop(code, None)
    return out
