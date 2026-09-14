"""Claude(Fable) 기반 시장·계좌 분석.

공식 anthropic SDK를 사용한다(`pip install anthropic`). 모델은 클로드
페이블(claude-fable-5-1)이며, 안전상 거절 시 대체 모델로 자동 폴백하는
서버측 fallbacks를 기본 활성화한다. 분석은 참고 자료일 뿐 투자 자문이
아니며, 매매 판단은 전략 엔진과 사용자에게 있다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

MODEL = "claude-fable-5-1"

_SYSTEM = (
    "당신은 주식 자동매매 프로그램에 내장된 시장 분석 보조자다. "
    "제공된 시세·계좌 데이터만 근거로 한국어로 분석한다. "
    "구성: (1) 시세 흐름 요약 (2) 현재 전략 신호 관점의 진단 "
    "(3) 리스크 점검(낙폭·집중도·주문 빈도) (4) 확인이 필요한 불확실성. "
    "과장 없이 서술하고, 수익 보장 표현을 쓰지 않으며, 마지막에 "
    "'이 분석은 참고 자료이며 투자 자문이 아니다'를 한 줄 덧붙인다."
)


@dataclass
class MarketSnapshot:
    """분석에 전달할 현재 상태 요약."""

    market: str                 # kr / us
    code: str
    strategy: str
    mode: str                   # mock / real
    prices: Sequence[tuple[str, float]]   # (시각, 가격) 최근 순서대로
    equity: float
    cash: float
    realized_pnl: float
    position_qty: int
    position_avg_price: float
    orders_today: int
    recent_fills: Sequence[str]           # "10:31 buy 3주 @213.04" 형식

    def to_prompt(self) -> str:
        price_lines = "\n".join(f"  {t}  {p:,.2f}" for t, p in self.prices[-60:])
        fills = "\n".join(f"  {f}" for f in self.recent_fills[-10:]) or "  (없음)"
        return (
            f"시장: {'미국' if self.market == 'us' else '국내'} / 종목: {self.code} / "
            f"전략: {self.strategy} / 모드: {'모의투자' if self.mode == 'mock' else '실전투자'}\n\n"
            f"최근 시세(오래된 것부터):\n{price_lines or '  (수집 전)'}\n\n"
            f"계좌: 평가액 {self.equity:,.2f}, 현금 {self.cash:,.2f}, "
            f"실현손익 {self.realized_pnl:,.2f}\n"
            f"포지션: {self.position_qty}주 (평균단가 {self.position_avg_price:,.2f})\n"
            f"오늘 주문 수: {self.orders_today}\n"
            f"최근 체결:\n{fills}\n\n"
            "위 데이터를 분석해 달라."
        )


def _recommend_prompt(market: str, mode: str, rows: Sequence[dict],
                      criteria_label: str) -> str:
    lines = "\n".join(
        f"  {r.get('code','?')} {r.get('name','')} | 현재가 {r.get('price','?')} | "
        f"등락률 {r.get('change_pct','?')}% | 거래량 {r.get('volume','?')}"
        for r in rows
    )
    return (
        f"오늘 {'미국' if market == 'us' else '국내'} 주식 {criteria_label} 목록이다 "
        f"(운용 모드: {'모의투자' if mode == 'mock' else '실전투자'}).\n\n{lines}\n\n"
        "이 데이터만 근거로, 오늘 관심 있게 볼 후보 3~5개를 골라 "
        "종목별로 선정 이유 1~2문장과 유의점 1문장을 정리해 달라. "
        "제공된 수치 밖의 사실은 추정하지 말고, 추정이 섞이면 표시해 달라."
    )


class ClaudeCodeAnalyst:
    """API 키 대신 PC에 설치된 Claude Code(구독 로그인)로 분석한다.

    claude.ai Pro/Max 구독 계정으로 `claude` 명령에 로그인되어 있어야 하며,
    건당 API 과금 없이 구독 사용 한도 안에서 동작한다.
    """

    TIMEOUT = 240

    def __init__(self, runner=None):
        self._runner = runner or self._run_cli

    @staticmethod
    def _find_cli() -> str:
        import shutil
        for name in ("claude", "claude.cmd", "claude.exe"):
            path = shutil.which(name)
            if path:
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
    _INSTRUCTION = ("표준 입력으로 전달된 지침과 데이터만 근거로 "
                    "한국어 분석 결과 본문만 출력하라.")

    def _run_cli(self, prompt: str) -> str:
        import subprocess
        import tempfile
        exe = self._find_cli()
        proc = subprocess.run(
            [exe, "-p", self._INSTRUCTION],
            input=prompt,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=self.TIMEOUT,
            cwd=tempfile.gettempdir(),  # 코드 저장소를 뒤지지 않게 중립 폴더에서 실행
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Claude Code 실행 실패: {(proc.stderr or proc.stdout)[:300]}")
        return proc.stdout.strip()

    def _ask(self, prompt: str) -> str:
        full = f"{_SYSTEM}\n\n---\n\n{prompt}"
        return self._runner(full) or "분석 결과가 비어 있다."

    def analyze(self, snapshot: MarketSnapshot) -> str:
        return self._ask(snapshot.to_prompt())

    def recommend(self, market: str, mode: str, rows: Sequence[dict],
                  criteria_label: str = "당일 거래량 상위") -> str:
        return self._ask(_recommend_prompt(market, mode, rows, criteria_label))


class ClaudeAnalyst:
    def __init__(self, api_key: str, client=None):
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

    def analyze(self, snapshot: MarketSnapshot) -> str:
        response = self._client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=_SYSTEM,
            messages=[{"role": "user", "content": snapshot.to_prompt()}],
        )
        if response.stop_reason == "refusal":
            detail = ""
            if getattr(response, "stop_details", None):
                detail = f" ({response.stop_details.explanation})"
            return f"분석 요청이 안전상 거절되었다.{detail}"
        return "".join(
            block.text for block in response.content if block.type == "text"
        ).strip() or "분석 결과가 비어 있다."

    def recommend(self, market: str, mode: str, rows: Sequence[dict],
                  criteria_label: str = "당일 거래량 상위") -> str:
        """순위 상위 목록에서 관심 후보를 골라 이유와 함께 정리한다."""
        prompt = _recommend_prompt(market, mode, rows, criteria_label)
        response = self._client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            return "추천 요청이 안전상 거절되었다."
        return "".join(
            block.text for block in response.content if block.type == "text"
        ).strip() or "추천 결과가 비어 있다."
