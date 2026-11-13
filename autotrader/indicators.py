"""분봉 요약: AI에 넘길 장중 흐름 지표를 계산한다 (표준 라이브러리만 사용)."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

GAP_MINUTES = 30      # 봉 사이가 이만큼 비면 장이 쉬었다가 다시 열린 것으로 본다


def _pct(now: float, then: float) -> float | None:
    if not now or not then:
        return None
    return round((now / then - 1) * 100, 2)


def _parse_time(raw) -> datetime | None:
    try:
        return datetime.strptime(str(raw)[:12], "%Y%m%d%H%M")
    except ValueError:
        return None


def open_gap(rows: Sequence[dict]) -> dict | None:
    """가장 최근에 장이 쉬었다 열린 지점의 갭.

    {"gap_pct": 쉬기 전 종가 대비 다시 열린 뒤 첫 종가의 변화(%),
     "bars_since": 다시 열린 뒤 지난 봉 수}. 쉰 구간이 없으면 None.
    """
    for i in range(len(rows) - 1, 0, -1):
        after, before = _parse_time(rows[i].get("t")), _parse_time(rows[i - 1].get("t"))
        if after is None or before is None:
            return None
        if (after - before).total_seconds() / 60 >= GAP_MINUTES:
            prev, first = float(rows[i - 1]["c"]), float(rows[i]["c"])
            if prev <= 0:
                return None
            return {"gap_pct": round((first / prev - 1) * 100, 2),
                    "bars_since": len(rows) - i}
    return None


def summarize_bars(bars: Sequence[dict],
                   session_min: float | None = None) -> dict | None:
    """1분봉(오래된 것→최근)을 요약한다.

    bars 원소: {"t": "YYYYMMDDHHMMSS", "o", "h", "l", "c", "v"}.
    session_min: 이번 장이 열린 뒤 지난 시간(분). 주면 그만큼의 봉만 "이번 장"으로
    본다 (프리마켓과 정규장처럼 봉이 끊기지 않고 이어지는 장을 가르기 위해서다).
    쓸 만한 봉이 5개 미만이면 None.
    """
    rows = [b for b in bars if b.get("c")]
    if len(rows) < 5:
        return None
    last = rows[-1]
    close = float(last["c"])
    # 이번 장의 봉만 쓴다: 장이 쉬었다 열린 지점(30분 이상 공백) 뒤부터.
    # 공백이 없으면 받은 봉 전체가 한 장이다
    gap = open_gap(rows) or {}
    since = gap.get("bars_since")
    today = rows[-since:] if since and since >= 5 else rows
    if session_min is not None and session_min >= 1:
        today = today[-max(5, int(session_min) + 1):]
    before = rows[:len(rows) - len(today)]

    volume = sum(float(b.get("v") or 0) for b in today)
    if volume > 0:
        vwap = sum((float(b["h"]) + float(b["l"]) + float(b["c"])) / 3
                   * float(b.get("v") or 0) for b in today) / volume
    else:
        vwap = sum(float(b["c"]) for b in today) / len(today)

    def ago(n: int) -> float | None:
        return float(rows[-1 - n]["c"]) if len(rows) > n else None

    recent = rows[-20:]
    ranges = [(float(b["h"]) - float(b["l"])) / float(b["c"]) * 100
              for b in recent if float(b["c"]) > 0]
    five = [float(b["c"]) for b in rows[-60:][4::5]] or [close]
    if five[-1] != close:
        five.append(close)
    digits = 2 if close < 1000 else 0
    high = max(float(b["h"]) for b in today)
    low = min(float(b["l"]) for b in today)
    # 장 전체의 모양: 30분 간격 종가 (최근 90분만 보면 큰 흐름을 놓친다)
    half_hour = [float(b["c"]) for b in today[::-1][::30]][::-1]
    return {
        "gap_pct": gap.get("gap_pct"),
        "bars_since_gap": gap.get("bars_since"),
        "last": close,
        "time": str(last.get("t", ""))[8:12],
        "day_high": high,
        "day_low": low,
        "window_min": len(today),          # 위 고가·저가·VWAP을 계산한 구간(분)
        # 이번 장이 열리기 직전 가격 (직전 장의 마지막 종가)
        "prior_close": float(before[-1]["c"]) if before else None,
        "from_prior_pct": _pct(close, float(before[-1]["c"])) if before else None,
        "from_start_pct": _pct(close, float(today[0]["o"] if today[0].get("o")
                                            else today[0]["c"])),
        "off_high_pct": _pct(close, high),
        "from_low_pct": _pct(close, low),
        "closes_30m": [round(c, digits) for c in half_hour[-14:]],
        "vwap": round(vwap, 4),
        "vs_vwap_pct": _pct(close, vwap),
        "ret_5m": _pct(close, ago(5)),
        "ret_15m": _pct(close, ago(15)),
        "ret_60m": _pct(close, ago(60)),
        "avg_range_pct": round(sum(ranges) / len(ranges), 3) if ranges else 0.0,
        "closes_1m": [round(float(b["c"]), digits) for b in rows[-15:]],
        "volumes_1m": [int(float(b.get("v") or 0)) for b in rows[-15:]],
        "closes_5m": [round(c, digits) for c in five[-12:]],
        "bars": len(today),
    }
