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

# 구분: stock(대형주) / etf(지수·섹터 ETF) / lev(레버리지 ETF) / inv(인버스 ETF)
STOCK, ETF, LEVERAGED, INVERSE = "stock", "etf", "lev", "inv"
KIND_LABELS = {STOCK: "대형주", ETF: "ETF", LEVERAGED: "레버리지 ETF",
               INVERSE: "인버스 ETF — 기초지수가 내리면 오른다"}

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

# 지수형 레버리지 ETF (단일 종목 레버리지 ETP는 넣지 않는다)
US_LEVERAGED = """
TQQQ SOXL UPRO SPXL TNA QLD SSO TECL
""".split()

# 지수형 인버스 ETF — 기초지수가 내릴 때 오른다. 하락장에서도 살 종목이 생긴다
US_INVERSE = """
SQQQ SOXS SPXU SPXS TZA SH PSQ SDS QID TECS
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
122630 233740 462330
""".split()

KR_INVERSE = """
114800 252670 251340
""".split()

# 같은 기초지수를 따르는 종목 묶음: {묶음: {코드: 방향}} (+1 정방향, -1 인버스).
# 한 묶음에서는 한 종목만 보유한다 — SOXL과 SOXS를 함께 들면 서로 상쇄되어
# 수수료만 내고, 같은 방향을 둘 들면 한 지수에 위험이 몰린다
GROUPS = {
    "나스닥100": {"QQQ": 1, "TQQQ": 1, "QLD": 1, "SQQQ": -1, "PSQ": -1, "QID": -1},
    "S&P500": {"SPY": 1, "VOO": 1, "IVV": 1, "UPRO": 1, "SPXL": 1, "SSO": 1,
               "SPXU": -1, "SPXS": -1, "SH": -1, "SDS": -1},
    "반도체": {"SMH": 1, "SOXX": 1, "SOXL": 1, "SOXS": -1},
    "러셀2000": {"IWM": 1, "TNA": 1, "TZA": -1},
    "기술주": {"XLK": 1, "TECL": 1, "TECS": -1},
    "코스피200": {"069500": 1, "102110": 1, "122630": 1,
                 "114800": -1, "252670": -1},
    "코스닥150": {"229200": 1, "233740": 1, "251340": -1},
    "2차전지": {"305720": 1, "462330": 1},
}
_GROUP_OF = {code: (name, direction)
             for name, members in GROUPS.items()
             for code, direction in members.items()}


def group_of(code: str) -> tuple[str, int] | None:
    """종목이 속한 (기초지수 묶음, 방향). 묶음이 없으면 None."""
    return _GROUP_OF.get(str(code).upper().split("_", 1)[0])


def _codes(raw: Iterable[str] | str | None) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    return [str(c).strip().upper() for c in raw if str(c).strip()]


def build_universe(market: str, leveraged: bool = True,
                   add: Iterable[str] | str | None = None,
                   remove: Iterable[str] | str | None = None,
                   inverse: bool | None = None) -> dict[str, str]:
    """{종목코드: 구분}. leveraged=False면 레버리지·인버스 ETF를 뺀다.

    inverse를 따로 주면 인버스 ETF만 넣거나 뺄 수 있다(기본은 leveraged를 따름).
    """
    if inverse is None:
        inverse = leveraged
    if market == "us":
        stocks, etfs, lev, inv = US_STOCKS, US_ETFS, US_LEVERAGED, US_INVERSE
    else:
        stocks, etfs, lev, inv = KR_STOCKS, KR_ETFS, KR_LEVERAGED, KR_INVERSE
    out: dict[str, str] = {}
    for code in stocks:
        out[code] = STOCK
    for code in etfs:
        out[code] = ETF
    if leveraged:
        for code in lev:
            out[code] = LEVERAGED
    if inverse:
        for code in inv:
            out[code] = INVERSE
    for code in _codes(add):
        out.setdefault(code, STOCK)
    for code in _codes(remove):
        out.pop(code, None)
    return out
