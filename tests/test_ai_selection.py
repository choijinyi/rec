"""Fable AI 종목 선정(selector) 테스트."""

import unittest
from datetime import datetime, timedelta

from autotrader.analysis import ClaudeCodeAnalyst, parse_selection
from autotrader.kiwoom.auto import AutoConfig, MultiLiveTrader
from autotrader.risk import RiskConfig, RiskManager
from autotrader.strategy import SMACrossoverStrategy


class ParseSelectionTest(unittest.TestCase):
    VALID = ["NVDA", "TSLA", "AAPL", "SOXL"]

    def test_reads_selection_line(self):
        text = "선정: NVDA, SOXL\n\nNVDA는 거래량이 가장 많다.\nAAPL 언급."
        self.assertEqual(parse_selection(text, self.VALID, 2), ["NVDA", "SOXL"])

    def test_drops_codes_not_in_candidates(self):
        text = "선정: NVDA, MSFT, TSLA"
        self.assertEqual(parse_selection(text, self.VALID, 3), ["NVDA", "TSLA"])

    def test_falls_back_to_body_scan_without_selection_line(self):
        text = "오늘은 TSLA와 AAPL이 좋아 보인다. NVDA는 제외한다."
        self.assertEqual(parse_selection(text, self.VALID, 2), ["TSLA", "AAPL"])

    def test_caps_at_num_and_dedupes(self):
        text = "선정: NVDA, NVDA, TSLA, AAPL, SOXL"
        self.assertEqual(parse_selection(text, self.VALID, 2), ["NVDA", "TSLA"])

    def test_korean_numeric_codes(self):
        text = "선정: 005930, 000660"
        self.assertEqual(parse_selection(text, ["005930", "000660", "035720"], 2),
                         ["005930", "000660"])

    def test_empty_when_nothing_matches(self):
        self.assertEqual(parse_selection("후보가 마땅치 않다.", self.VALID, 3), [])

    def test_bare_code_matches_nxt_suffixed_candidate(self):
        # 순위 응답이 233740_AL로 와도 AI가 233740으로 답하면 인정한다
        valid = ["233740_AL", "114800_AL", "462330_AL"]
        text = "선정: 233740, 114800, 462330"
        self.assertEqual(parse_selection(text, valid, 3),
                         ["233740_AL", "114800_AL", "462330_AL"])

    def test_suffixed_answer_matches_bare_candidate(self):
        valid = ["233740", "114800"]
        self.assertEqual(parse_selection("선정: 114800_AL", valid, 1), ["114800"])


class SelectSymbolsTest(unittest.TestCase):
    ROWS = [
        {"code": "NVDA", "name": "NVIDIA", "price": "180.0",
         "change_pct": "2.1", "volume": "9000000"},
        {"code": "TSLA", "name": "Tesla", "price": "410.0",
         "change_pct": "-1.0", "volume": "7000000"},
        {"code": "SOXL", "name": "SOXL", "price": "28.0",
         "change_pct": "4.2", "volume": "6000000"},
    ]

    def test_cli_analyst_selects_and_returns_note(self):
        prompts = []

        def runner(prompt):
            prompts.append(prompt)
            return "선정: SOXL, NVDA\nSOXL은 등락률이 높다. NVDA는 거래량 1위."

        analyst = ClaudeCodeAnalyst(runner=runner)
        codes, note = analyst.select_symbols("us", self.ROWS, 2,
                                             criteria_label="당일 거래량 상위")
        self.assertEqual(codes, ["SOXL", "NVDA"])
        self.assertIn("SOXL은 등락률이 높다", note)
        # 프롬프트에 후보 데이터와 출력 형식 지시가 들어가야 한다
        self.assertIn("NVDA", prompts[0])
        self.assertIn("당일 거래량 상위", prompts[0])
        self.assertIn("선정:", prompts[0])
        self.assertIn("2개", prompts[0])

    def test_api_analyst_selects(self):
        from types import SimpleNamespace

        resp = SimpleNamespace(
            stop_reason="end_turn", stop_details=None,
            content=[SimpleNamespace(type="text", text="선정: TSLA, SOXL\n이유.")])
        client = SimpleNamespace(beta=SimpleNamespace(
            messages=SimpleNamespace(create=lambda **kw: resp)))
        from autotrader.analysis import ClaudeAnalyst

        codes, note = ClaudeAnalyst(api_key="", client=client).select_symbols(
            "us", self.ROWS, 2)
        self.assertEqual(codes, ["TSLA", "SOXL"])
        self.assertIn("이유", note)


class FakeRankedAPI:
    def __init__(self, ranking):
        self.ranking = ranking

    def top_stocks(self, criteria="volume", limit=10):
        return self.ranking

    def current_price(self, code):
        return 100.0

    def send_market_order(self, account_no, code, side, quantity):
        pass


def make_trader(selector, num_symbols=2):
    clock_state = {"now": datetime(2025, 9, 11, 9, 30, 0)}

    def clock():
        now = clock_state["now"]
        clock_state["now"] = now + timedelta(seconds=60)
        return now

    api = FakeRankedAPI(
        ranking=[{"code": "AAA", "price": "5000"},
                 {"code": "BBB", "price": "5000"},
                 {"code": "CCC", "price": "5000"}])
    return MultiLiveTrader(
        api=api,
        strategy_factory=lambda: SMACrossoverStrategy(2, 3),
        config=AutoConfig(market="kr", num_symbols=num_symbols,
                          initial_cash=1_000_000),
        risk=RiskManager(RiskConfig(), initial_equity=1_000_000),
        is_simulation=True,
        clock=clock,
        selector=selector,
    )


class MultiTraderAiSelectionTest(unittest.TestCase):
    def test_selector_choice_overrides_rank_order(self):
        trader = make_trader(lambda rows, num: (["CCC", "BBB"], "근거 본문"))
        trader.step()
        self.assertEqual(trader.symbols, ["CCC", "BBB"])
        self.assertEqual(trader.selection_note, "근거 본문")
        self.assertIsNone(trader.selector)  # 1회 사용 후 해제

    def test_selector_receives_eligible_rows_and_num(self):
        seen = {}

        def selector(rows, num):
            seen["codes"] = [r["code"] for r in rows]
            seen["num"] = num
            return (["AAA"], "")

        make_trader(selector, num_symbols=1).step()
        self.assertEqual(seen["codes"], ["AAA", "BBB", "CCC"])
        self.assertEqual(seen["num"], 1)

    def test_selector_failure_falls_back_to_rank(self):
        def boom(rows, num):
            raise RuntimeError("백엔드 없음")

        trader = make_trader(boom)
        trader.step()
        self.assertEqual(trader.symbols, ["AAA", "BBB"])  # 순위 상위로 대체
        self.assertIn("AI 종목 선정 실패", trader.last_error)
        self.assertEqual(trader.selection_note, "")

    def test_invalid_codes_fall_back_to_rank(self):
        trader = make_trader(lambda rows, num: (["ZZZ"], "후보 밖 코드"))
        trader.step()
        self.assertEqual(trader.symbols, ["AAA", "BBB"])
        self.assertIn("유효한 종목을 찾지 못해", trader.last_error)

    def test_extra_codes_capped_to_num(self):
        trader = make_trader(
            lambda rows, num: (["BBB", "AAA", "CCC"], ""), num_symbols=2)
        trader.step()
        self.assertEqual(trader.symbols, ["BBB", "AAA"])


class KrRankingCodeSuffixTest(unittest.TestCase):
    """국내 순위 응답의 NXT 통합 표기(005930_AL)가 순수 코드로 정규화되는지."""

    def test_strips_al_suffix_for_kr(self):
        from autotrader.kiwoom_rest import KiwoomRestClient

        def transport(url, headers, body):
            if url.endswith("/oauth2/token"):
                return {"token": "T", "expires_dt": ""}
            if "/api/dostk/rkinfo" in url:
                return {"return_code": 0, "result_list": [
                    {"stk_cd": "114800_AL", "stk_nm": "KODEX 인버스",
                     "cur_prc": "1052", "flu_rt": "0.5", "trde_qty": "1000"},
                    {"stk_cd": "005930", "stk_nm": "삼성전자",
                     "cur_prc": "70000", "flu_rt": "1.0", "trde_qty": "900"},
                ]}
            raise AssertionError(url)

        client = KiwoomRestClient("A", "S", market="kr", transport=transport)
        rows = client.top_stocks("volume", limit=10)
        self.assertEqual([r["code"] for r in rows], ["114800", "005930"])


class WebUiWiringTest(unittest.TestCase):
    """UI start()가 ai_select 여부에 따라 selector를 붙이는지 확인."""

    def _make_state(self):
        import tempfile
        from pathlib import Path

        from autotrader.webui import AppState

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name) / "config.ini"
        cfg.write_text("[kiwoom]\nappkey=A\nsecretkey=B\nmode=mock\n",
                       encoding="utf-8")
        state = AppState(str(cfg))
        state.client_factory = lambda *a, **k: FakeRankedAPI([])
        return state

    def test_auto_with_ai_select_attaches_selector(self):
        state = self._make_state()
        r = state.start(market="kr", code="", strategy="sma_crossover",
                        confirm="", auto=True, ai_select=True)
        self.assertEqual(r, {"ok": True})
        self.assertIsNotNone(state.trader.selector)
        self.assertTrue(state.params["ai_select"])
        state.stop()

    def test_auto_without_ai_select_has_no_selector(self):
        state = self._make_state()
        state.start(market="kr", code="", strategy="sma_crossover",
                    confirm="", auto=True, ai_select=False)
        self.assertIsNone(state.trader.selector)
        state.stop()


if __name__ == "__main__":
    unittest.main()
