"""autotrader: AI 단타 자동매매 프로그램.

거래량·등락률 상위 후보를 AI(Claude Fable)가 분석해 매매 종목을 자동
선정하고, 키움 REST API로 목표 수익(+1~2%) 익절 / -3% 손절을 반복한다.
"""

__version__ = "0.3.0"
