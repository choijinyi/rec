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

    def recommend(self, market: str, mode: str, rows: Sequence[dict]) -> str:
        """당일 거래량 상위 목록에서 관심 후보를 골라 이유와 함께 정리한다."""
        lines = "\n".join(
            f"  {r.get('code','?')} {r.get('name','')} | 현재가 {r.get('price','?')} | "
            f"등락률 {r.get('change_pct','?')}% | 거래량 {r.get('volume','?')}"
            for r in rows
        )
        prompt = (
            f"오늘 {'미국' if market == 'us' else '국내'} 주식 당일 거래량 상위 목록이다 "
            f"(운용 모드: {'모의투자' if mode == 'mock' else '실전투자'}).\n\n{lines}\n\n"
            "이 데이터만 근거로, 오늘 관심 있게 볼 후보 3~5개를 골라 "
            "종목별로 선정 이유 1~2문장과 유의점 1문장을 정리해 달라. "
            "제공된 수치 밖의 사실은 추정하지 말고, 추정이 섞이면 표시해 달라."
        )
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
