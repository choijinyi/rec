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


def summarize_bars(bars: Sequence[dict]) -> dict | None:
    """1분봉(오래된 것→최근)을 요약한다.

    bars 원소: {"t": "YYYYMMDDHHMMSS", "o", "h", "l", "c", "v"}.
    쓸 만한 봉이 5개 미만이면 None.
    """
    rows = [b for b in bars if b.get("c")]
    if len(rows) < 5:
        return None
    last = rows[-1]
    close = float(last["c"])
    day = str(last.get("t", ""))[:8]
    today = [b for b in rows if str(b.get("t", ""))[:8] == day] or rows

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
    gap = open_gap(rows) or {}
    return {
        "gap_pct": gap.get("gap_pct"),
        "bars_since_gap": gap.get("bars_since"),
        "last": close,
        "time": str(last.get("t", ""))[8:12],
        "day_high": max(float(b["h"]) for b in today),
        "day_low": min(float(b["l"]) for b in today),
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
