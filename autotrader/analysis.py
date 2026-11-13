"""Claude(Opus 5.5) 기반 매매 시점 판단.

두 가지를 묻는다.
  - 진입 판단: 후보 종목의 분봉 흐름을 보고 지금 살지, 산다면 어느 가격
    이하에서 사고 어디서 손절·익절할지.
  - 청산 점검: 보유 종목을 계속 들고 갈지, 손절가를 올릴지, 지금 팔지.

AI는 제안만 한다. 손절 폭 상한, 손익비, 수량(1회 손실 한도), 판단의 유효
시간은 모두 매매 엔진(scalper)이 다시 검사하고, AI가 응답하지 않으면 엔진은
새로 사지 않는다. 판단은 참고 자료일 뿐 투자 자문이 아니다.

호출 방식은 둘이다: PC에 설치된 Claude Code 구독 로그인(건당 과금 없음),
또는 API 키(anthropic SDK, 건당 과금).
"""

from __future__ import annotations

import json
import re
from typing import Sequence

MODEL = "claude-opus-5-5"
EFFORT = "medium"

_SYSTEM = (
    "당신은 주식 단타 자동매매 프로그램에 내장된 매매 시점 판단 모듈이다. "
    "제공된 수치만 근거로 판단하고, 수치 밖의 사실(뉴스·실적 등)은 지어내지 "
    "않는다. 확신이 서지 않으면 매매하지 않는 쪽을 고른다 — 관망이 기본값이다. "
    "응답은 지시된 형식의 JSON 객체 하나만 출력하고, 그 밖의 문장이나 코드 "
    "블록 표시는 붙이지 않는다."
)


# ── 프롬프트 ────────────────────────────────────────────────
def _num(value, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "?"


def _signed(value) -> str:
    return "?" if value is None else f"{value:+.2f}%"


def _series(values: Sequence) -> str:
    return " ".join(str(v) for v in values) if values else "(없음)"


def _bars_block(bars: dict | None) -> str:
    if not bars:
        return "  (분봉을 받지 못했다 — 흐름을 확인할 수 없으니 보수적으로 판단할 것)"
    gap = ""
    if bars.get("gap_pct") is not None and (bars.get("bars_since_gap") or 99) <= 30:
        gap = (f"  장이 다시 열리며 갭 {_signed(bars['gap_pct'])} "
               f"(열린 뒤 {bars['bars_since_gap']}분 경과)\n")
    window = bars.get("window_min")
    span = f"이번 장 최근 {window}분" if window else "이번 장"
    shape = ""
    if len(bars.get("closes_30m") or []) >= 3:
        shape = (f"  30분 간격 종가(오래된→최근): "
                 f"{_series(bars.get('closes_30m'))}\n")
    return (
        f"{gap}"
        f"  {span}: 고가 {_num(bars.get('day_high'))} / 저가 "
        f"{_num(bars.get('day_low'))} / VWAP {_num(bars.get('vwap'))}\n"
        f"  현재가 위치: VWAP 대비 {_signed(bars.get('vs_vwap_pct'))} / 그 구간 "
        f"고가 대비 {_signed(bars.get('off_high_pct'))} / 저가 대비 "
        f"{_signed(bars.get('from_low_pct'))} / 구간 시작 대비 "
        f"{_signed(bars.get('from_start_pct'))}"
        + (f" / 직전 장 종가 대비 {_signed(bars.get('from_prior_pct'))}"
           if bars.get("from_prior_pct") is not None else "") + "\n"
        f"{shape}"
        f"  최근 5분 {_signed(bars.get('ret_5m'))} / 15분 "
        f"{_signed(bars.get('ret_15m'))} / 60분 {_signed(bars.get('ret_60m'))}"
        f" / 1분봉 평균 변동폭 {bars.get('avg_range_pct', '?')}%\n"
        f"  5분 간격 종가(오래된→최근): {_series(bars.get('closes_5m'))}\n"
        f"  1분봉 종가(최근 15개): {_series(bars.get('closes_1m'))}\n"
        f"  1분봉 거래량(최근 15개): {_series(bars.get('volumes_1m'))}"
    )


def _context_block(context: dict | None) -> str:
    ctx = context or {}
    parts = []
    if ctx.get("session"):
        parts.append(f"현재 장: {ctx['session']}")
    if ctx.get("minutes_to_close") is not None:
        parts.append(f"마감까지 {ctx['minutes_to_close']:.0f}분")
    if ctx.get("stale_min"):
        parts.append(f"보유 한도 {ctx['stale_min']:.0f}분")
    return " / ".join(parts) or "(장 정보 없음)"


def _entry_prompt(market: str, num: int, rows: Sequence[dict],
                  context: dict | None) -> str:
    ctx = context or {}
    max_stop = ctx.get("max_stop_pct", 2.0)
    min_stop = ctx.get("min_stop_pct", 0.4)
    min_rr = ctx.get("min_rr", 1.2)
    min_target = ctx.get("min_target_pct", 1.0)
    trend = ctx.get("trend_entries", True)
    blocks = []
    for r in rows:
        kind = f" ({r['kind']})" if r.get("kind") else ""
        peers = (f"\n  같은 지수({r['group']})를 따르는 종목의 당일 등락률: "
                 f"{r['peers']}" if r.get("peers") else "")
        blocks.append(
            f"[{r.get('code', '?')}] {r.get('name', '')}{kind} | 현재가 "
            f"{r.get('price', '?')} | 당일 등락률(전일 종가 대비) "
            f"{r.get('change_pct', '?')}% | 거래량 {r.get('volume', '?')}"
            f"{peers}\n{_bars_block(r.get('bars'))}")
    body = "\n\n".join(blocks)
    view = str(ctx.get("view") or "").strip()
    view_block = ""
    if view:
        view_block = (
            f"운용자의 오늘 관점(참고): {view[:300]}\n"
            "이 관점은 어느 방향의 종목을 먼저 볼지 정하는 데만 쓴다. 분봉이 그 "
            "방향을 확인해 줄 때만 진입하고, 분봉이 반대로 가면 따르지 않는다.\n\n")
    if trend:
        trend_rules = (
            "- [추세 추종] 당일 등락률이 크고(대략 +3% 이상) 이번 장이 열린 뒤 "
            "VWAP 위에서 종가의 저점과 고점이 함께 높아지는 종목은, VWAP에서 "
            "멀리 떨어져 있어도 진입 후보다. 장이 열린 지 얼마 안 돼 봉이 적으면 "
            "열린 뒤 저점을 높이며 직전 고점을 넘는지로 판단한다(직전 장의 "
            "흐름은 조건에 넣지 않는다). 같은 지수 종목의 등락률이 "
            "이 방향을 뒷받침하면(예: 인버스가 오르는데 정방향은 크게 내림) 더 "
            "믿을 만하다. 이때는 mode를 \"trend\"로 답하고, 손절가는 최근 "
            "5~10분의 눌림 저점 바로 아래에, 목표가는 손절 폭의 1.5배 이상으로 "
            "잡는다. 프로그램은 목표가에서 절반을 팔고 나머지는 고점을 따라가며 "
            "판다(그러니 목표가는 첫 번째 이익 확정 지점으로 잡으면 된다).\n"
            "- 추세가 강한 날은 깊은 눌림이 오지 않는다. 0.5~1% 정도의 얕은 "
            "눌림 뒤 다시 고점을 넘거나, 잠깐 쉬었다가 거래량이 붙어 고점을 "
            "넘는 순간이면 진입하기에 충분하다. VWAP까지 내려오기를 기다리다 "
            "추세 전체를 놓치지 않는다.\n"
            "- 추세 추종이라도 다음은 고르지 않는다: 직전 한두 개 봉이 유난히 "
            "크게 뛰어 그 꼭대기에 있는 순간(다음 판단 때 얕은 눌림이나 재돌파를 "
            "확인하고 산다), 구간 고가에서 이미 3% 넘게 밀려 식어 가는 중, 고점을 "
            "다시 넘지 못하고 거래량이 줄어드는 중.\n"
            "- 추세 조건에 들지 않는데 VWAP에서 멀리 달아난 종목, 흐름이 꺾여 "
            "내려오는 종목, 변동폭이 너무 작아 목표에 닿기 어려운 종목은 고르지 "
            "않는다.\n")
    else:
        trend_rules = (
            "- VWAP에서 이미 멀리 달아난 종목, 방금 급등해 꼭지일 수 있는 종목, "
            "흐름이 꺾여 내려오는 종목, 변동폭이 너무 작아 목표에 닿기 어려운 "
            "종목은 고르지 않는다.\n")
    return (
        f"{'미국' if market == 'us' else '국내'} 주식 단타 진입 판단을 요청한다. "
        f"{_context_block(ctx)}.\n"
        f"후보는 모두 대형주·주요 ETF이며, 지금 비어 있는 자리는 {num}개다.\n\n"
        f"{view_block}"
        f"{body}\n\n"
        "판단 기준:\n"
        "- [눌림·돌파] 장중 상승 흐름 속의 눌림(VWAP·직전 지지 부근)이나 "
        "거래량이 붙는 돌파처럼, 손절 위치가 분명하고 가까운 자리를 '진입'으로 "
        "고른다. 이때는 mode를 \"pullback\"으로 답한다.\n"
        f"{trend_rules}"
        f"- 손절가는 현재가보다 {min_stop}%~{max_stop}% 아래에서, 최근 저점 등 "
        "가격 구조가 무너지는 자리로 잡는다.\n"
        f"- 목표가는 현재가보다 최소 {min_target}% 위여야 한다(수수료와 호가 "
        "비용을 빼고도 남아야 한다). 보유 한도 안에 그만큼 움직일 여지가 "
        "분봉에서 보이지 않으면 고르지 않는다.\n"
        f"- 목표가까지의 폭은 손절 폭의 {min_rr}배 이상이어야 한다.\n"
        "- 진입 상한가(max_price)는 이 가격을 넘으면 쫓아 사지 않을 가격이다.\n"
        "- 인버스 ETF는 기초지수가 내릴 때 오른다. 판단은 그 ETF 자체의 분봉으로 "
        "똑같이 한다(오르는 흐름의 눌림·돌파). 같은 지수의 정방향과 인버스를 "
        "함께 고르지 않는다.\n"
        "- '갭'이 표시된 종목은 장이 방금 다시 열린 것이다. 열린 뒤의 분봉이 갭을 "
        "지키며 저점·고점을 높이면 진입 후보이고, 갭을 메우며 밀리거나 첫 봉 "
        "고점을 넘지 못하면 고르지 않는다. 개장 초반은 흔들림이 크므로 손절가는 "
        "열린 뒤 저점 아래로 넉넉히 잡는다.\n"
        "- 위 두 유형 가운데 어느 쪽에도 들지 않으면 고르지 않는다. 억지로 "
        "채우지 않되, 조건이 맞는 자리를 지나치게 까다롭게 걸러 놓치지도 않는다."
        "\n\n"
        "출력 형식(JSON 객체 하나만, 다른 글 금지):\n"
        '{"market_view": "지금 장 흐름 한 문장", "decisions": [\n'
        '  {"code": "종목코드", "action": "enter", "mode": "pullback", '
        '"max_price": 숫자, "stop": 숫자, "target": 숫자, '
        '"reason": "근거 한두 문장"},\n'
        '  {"code": "종목코드", "action": "wait", "reason": "기다리는 이유"}\n'
        "]}\n"
        f'action은 enter / wait / skip 중 하나. enter는 최대 {num}개, 좋은 '
        "순서대로. 후보 목록에 있는 코드만 쓴다."
    )


def _review_prompt(market: str, rows: Sequence[dict],
                   context: dict | None) -> str:
    gap = (context or {}).get("tighten_gap_pct", 0.5)
    blocks = []
    for r in rows:
        blocks.append(
            f"[{r.get('code', '?')}] 매입가 {_num(r.get('entry'))} | 현재가 "
            f"{_num(r.get('price'))} ({_signed(r.get('pct'))}) | 보유 "
            f"{r.get('held_min', '?')}분 | 손절가 {_num(r.get('stop'))} | "
            f"목표가 {_num(r.get('target'))} | 보유 중 고가 {_num(r.get('peak'))}"
            f"{' | 추적 익절 중' if r.get('trailing') else ''}"
            f"{'' if r.get('can_tighten', True) else ' | 손절가 상향 아직 불가'}\n"
            f"{_bars_block(r.get('bars'))}")
    body = "\n\n".join(blocks)
    return (
        f"{'미국' if market == 'us' else '국내'} 주식 단타 보유 종목의 청산 "
        f"시점 점검을 요청한다. {_context_block(context)}.\n\n{body}\n\n"
        "종목마다 셋 중 하나를 고른다.\n"
        "- hold: 흐름이 살아 있어 그대로 둔다.\n"
        "- tighten: 손절가를 올린다(stop에 새 손절가). 손절가는 올릴 수만 "
        f"있고, 현재가보다 최소 {gap}% 아래여야 한다. 상승 중 생긴 새 지지 "
        "아래로 잡는다. 수익이 처음 손절 폭만큼 나기 전에는 올릴 수 없다"
        "('손절가 상향 아직 불가'로 표시된 종목) — 그때는 hold나 exit만 고른다.\n"
        "- 목표가에 닿으면 프로그램이 바로 익절하므로, 목표가 근처라는 이유로 "
        "손절가를 바짝 올릴 필요는 없다.\n"
        "- exit: 흐름이 꺾였거나 더 들고 갈 이유가 없어 지금 판다.\n"
        "작은 흔들림에 팔지 말고, 진입 근거가 무너졌을 때만 exit을 고른다.\n\n"
        "출력 형식(JSON 객체 하나만, 다른 글 금지):\n"
        '{"reviews": [{"code": "종목코드", "action": "hold", '
        '"stop": 숫자 또는 null, "reason": "근거 한 문장"}]}'
    )


# ── 응답 해석 ───────────────────────────────────────────────
def extract_json(text: str) -> dict:
    """응답에서 JSON 객체를 꺼낸다. 앞뒤 설명이나 코드 블록 표시는 무시한다."""
    start = text.find("{")
    while start != -1:
        depth, in_str, escape = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
        start = text.find("{", start + 1)
    raise ValueError(f"AI 응답에서 JSON을 찾지 못했다: {text[:200]!r}")


def _to_float(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return None
    number = float(match.group(0))
    return number if number > 0 else None


def _code_map(valid_codes: Sequence[str]) -> dict[str, str]:
    """국내 NXT 통합 표기(005930_AL)와 순수 코드(005930)를 같은 종목으로 본다."""
    valid: dict[str, str] = {}
    for c in valid_codes:
        if not c:
            continue
        code = str(c)
        valid.setdefault(code.upper(), code)
        valid.setdefault(code.upper().split("_", 1)[0], code)
    return valid


def _lookup(valid: dict[str, str], raw) -> str | None:
    key = str(raw or "").strip().upper()
    return valid.get(key) or valid.get(key.split("_", 1)[0])


_ACTIONS_ENTER = {"enter", "buy", "진입", "매수"}
_ACTIONS_EXIT = {"exit", "sell", "청산", "매도"}
_ACTIONS_TIGHTEN = {"tighten", "raise", "손절상향", "상향"}


def parse_entry_plans(text: str, valid_codes: Sequence[str],
                      num: int) -> tuple[list[dict], str]:
    """진입 판단 응답 → (진입 계획 목록, 화면에 보일 근거 요약).

    계획: {"code", "max_price", "stop", "target", "reason"}. 후보에 없는
    코드와 enter가 아닌 판단은 계획에서 뺀다(근거 요약에는 남긴다).
    """
    data = extract_json(text)
    valid = _code_map(valid_codes)
    plans: list[dict] = []
    lines: list[str] = []
    view = str(data.get("market_view") or "").strip()
    if view:
        lines.append(f"장 흐름: {view}")
    decisions = data.get("decisions")
    if not isinstance(decisions, list):
        decisions = []
    for item in decisions:
        if not isinstance(item, dict):
            continue
        code = _lookup(valid, item.get("code"))
        if not code:
            continue
        action = str(item.get("action") or "").strip().lower()
        reason = str(item.get("reason") or "").strip()
        if action in _ACTIONS_ENTER:
            if any(p["code"] == code for p in plans) or len(plans) >= num:
                continue
            mode = ("trend" if str(item.get("mode") or "").strip().lower()
                    in ("trend", "추세", "추세 추종") else "pullback")
            plan = {"code": code, "max_price": _to_float(item.get("max_price")),
                    "stop": _to_float(item.get("stop")),
                    "target": _to_float(item.get("target")), "reason": reason,
                    "mode": mode}
            plans.append(plan)
            levels = " / ".join(
                f"{label} {_num(plan[key])}" for label, key in
                (("상한", "max_price"), ("손절", "stop"), ("목표", "target"))
                if plan[key])
            lines.append(f"진입{'(추세)' if mode == 'trend' else ''} {code}"
                         + (f" ({levels})" if levels else "")
                         + (f" — {reason}" if reason else ""))
        else:
            label = "대기" if action == "wait" else "제외"
            lines.append(f"{label} {code}" + (f" — {reason}" if reason else ""))
    if not plans:
        lines.append("지금은 진입할 자리가 없다고 판단했다.")
    return plans, "\n".join(lines)


def parse_reviews(text: str, valid_codes: Sequence[str]) -> tuple[list[dict], str]:
    """청산 점검 응답 → (판단 목록, 근거 요약).

    판단: {"code", "action": "hold" | "tighten" | "exit", "stop", "reason"}.
    """
    data = extract_json(text)
    valid = _code_map(valid_codes)
    reviews: list[dict] = []
    lines: list[str] = []
    items = data.get("reviews")
    if not isinstance(items, list):
        items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = _lookup(valid, item.get("code"))
        if not code or any(r["code"] == code for r in reviews):
            continue
        raw = str(item.get("action") or "").strip().lower()
        stop = _to_float(item.get("stop"))
        reason = str(item.get("reason") or "").strip()
        if raw in _ACTIONS_EXIT:
            action, label = "exit", "청산"
        elif raw in _ACTIONS_TIGHTEN and stop:
            action, label = "tighten", f"손절 상향 {_num(stop)}"
        else:
            action, label, stop = "hold", "유지", None
        reviews.append({"code": code, "action": action, "stop": stop,
                        "reason": reason})
        lines.append(f"{label} {code}" + (f" — {reason}" if reason else ""))
    return reviews, "\n".join(lines)


# ── 호출 백엔드 ─────────────────────────────────────────────
class _Analyst:
    """진입 판단·청산 점검 공통 로직. 하위 클래스는 _ask만 구현한다."""

    label = "AI"

    def _ask(self, prompt: str) -> str:
        raise NotImplementedError

    def plan_entries(self, market: str, rows: Sequence[dict], num: int,
                     context: dict | None = None) -> tuple[list[dict], str]:
        text = self._ask(_entry_prompt(market, num, rows, context))
        return parse_entry_plans(text, [r.get("code", "") for r in rows], num)

    def review_positions(self, market: str, rows: Sequence[dict],
                         context: dict | None = None) -> tuple[list[dict], str]:
        text = self._ask(_review_prompt(market, rows, context))
        return parse_reviews(text, [r.get("code", "") for r in rows])


class ClaudeCodeAnalyst(_Analyst):
    """API 키 대신 PC에 설치된 Claude Code(구독 로그인)로 판단한다.

    claude.ai Pro/Max 구독 계정으로 `claude` 명령에 로그인되어 있어야 하며,
    건당 API 과금 없이 구독 사용 한도 안에서 동작한다.
    """

    TIMEOUT = 180
    # 구독·한도·로그인 문제는 인자를 바꿔 다시 시도해도 해결되지 않는다
    _FATAL_HINTS = ("limit", "한도", "login", "log in", "로그인", "auth",
                    "credit", "401", "403", "429")
    _tier_cache: dict[tuple, int] = {}

    def __init__(self, runner=None, model: str = MODEL, effort: str = EFFORT):
        self._runner = runner or self._run_cli
        self.model = model or MODEL
        self.effort = effort or EFFORT
        self.label = f"{self.model} · 구독 로그인"

    @staticmethod
    def _find_cli() -> str:
        import os
        import shutil
        for name in ("claude", "claude.cmd", "claude.exe"):
            path = shutil.which(name)
            if path:
                return path
        # PATH에 없어도 설치돼 있을 수 있다 (바로가기로 실행하면 사용자 PATH가
        # 반영되지 않는 경우가 있다) — 알려진 설치 위치를 직접 찾는다
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "claude.exe"),
            os.path.join(home, ".local", "bin", "claude"),
            os.path.join(home, ".claude", "local", "claude.exe"),
            os.path.join(home, ".claude", "local", "claude"),
            os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs",
                         "claude", "claude.exe"),
            "/usr/local/bin/claude",
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return path
        raise RuntimeError(
            "Claude Code가 설치되어 있지 않다. PowerShell에서\n"
            "  irm https://claude.ai/install.ps1 | iex\n"
            "로 설치한 뒤 `claude`를 한 번 실행해 claude.ai 계정(Pro/Max)으로 "
            "로그인해야 한다. 또는 config.ini [claude] api_key를 입력하면 "
            "API 방식으로 동작한다."
        )

    # 여러 줄 프롬프트를 명령 인자로 넘기면 Windows cmd 셔틀에서 잘릴 수
    # 있으므로, 인자는 한 줄 지시만 주고 본문은 표준 입력으로 전달한다.
    _INSTRUCTION = ("표준 입력으로 전달된 지침과 데이터만 근거로 판단하고, "
                    "도구를 쓰지 말고 지시된 JSON만 출력하라.")

    def _arg_tiers(self) -> list[list[str]]:
        """모델·강도 지정 인자. 설치된 Claude Code가 오래돼 인자나 모델 이름을
        모르면 다음 단계(더 단순한 인자)로 내려간다."""
        tiers = [["--model", self.model, "--effort", self.effort],
                 ["--model", self.model]]
        alias = next((a for a in ("opus", "sonnet", "haiku", "fable")
                      if a in self.model), "")
        if alias and alias != self.model:
            tiers.append(["--model", alias])
        tiers.append([])
        return tiers

    def _run_cli(self, prompt: str) -> str:
        import subprocess
        import tempfile
        exe = self._find_cli()
        tiers = self._arg_tiers()
        key = (exe, self.model, self.effort)
        start = min(self._tier_cache.get(key, 0), len(tiers) - 1)
        error = ""
        for index in range(start, len(tiers)):
            proc = subprocess.run(
                [exe, "-p", self._INSTRUCTION, *tiers[index]],
                input=prompt,
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=self.TIMEOUT,
                cwd=tempfile.gettempdir(),  # 코드 저장소를 뒤지지 않게 중립 폴더에서 실행
            )
            if proc.returncode == 0:
                self._tier_cache[key] = index
                return proc.stdout.strip()
            error = (proc.stderr or proc.stdout or "").strip()[:300]
            if any(hint in error.lower() for hint in self._FATAL_HINTS):
                break
        raise RuntimeError(f"Claude Code 실행 실패: {error}")

    def _ask(self, prompt: str) -> str:
        text = self._runner(f"{_SYSTEM}\n\n---\n\n{prompt}")
        if not text:
            raise RuntimeError("Claude Code 응답이 비어 있다.")
        return text


class ClaudeAnalyst(_Analyst):
    """API 키 방식 (anthropic SDK, 건당 과금)."""

    def __init__(self, api_key: str, client=None, model: str = MODEL,
                 effort: str = EFFORT):
        self.model = model or MODEL
        self.effort = effort or EFFORT
        self.label = f"{self.model} · API"
        if client is not None:
            self._client = client
        else:
            if not api_key:
                raise RuntimeError(
                    "Claude API 키가 없다. console.anthropic.com 에서 키를 발급받아 "
                    "config.ini의 [claude] api_key에 입력해야 한다."
                )
            try:
                import anthropic
            except ImportError as e:
                raise RuntimeError(
                    "anthropic 패키지가 필요하다: pip install anthropic"
                ) from e
            self._client = anthropic.Anthropic(api_key=api_key)

    def _ask(self, prompt: str) -> str:
        response = self._client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",   # 안전상 거절 시 서버가 대체 모델로 다시 실행
            output_config={"effort": self.effort},
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("AI 요청이 안전상 거절되었다.")
        text = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        if not text:
            raise RuntimeError("AI 응답이 비어 있다.")
        return text
