"""AI 종목 선정(analysis) 테스트."""

import unittest

from autotrader.analysis import ClaudeCodeAnalyst, parse_selection


class ParseSelectionTest(unittest.TestCase):
    VALID = ["NVDA", "TSLA", "AAPL", "SOXL"]

    def test_reads_selection_line(self):
        text = "선정: NVDA, SOXL\n\nNVDA는 거래량이 가장 많다."
        self.assertEqual(parse_selection(text, self.VALID, 2), ["NVDA", "SOXL"])

    def test_drops_codes_not_in_candidates(self):
        self.assertEqual(parse_selection("선정: NVDA, MSFT, TSLA", self.VALID, 3),
                         ["NVDA", "TSLA"])

    def test_falls_back_to_body_scan(self):
        text = "오늘은 TSLA와 AAPL이 좋아 보인다."
        self.assertEqual(parse_selection(text, self.VALID, 2), ["TSLA", "AAPL"])

    def test_caps_and_dedupes(self):
        self.assertEqual(
            parse_selection("선정: NVDA, NVDA, TSLA, AAPL", self.VALID, 2),
            ["NVDA", "TSLA"])

    def test_korean_codes_with_nxt_suffix(self):
        valid = ["233740_AL", "114800"]
        self.assertEqual(parse_selection("선정: 233740, 114800_AL", valid, 2),
                         ["233740_AL", "114800"])

    def test_empty_when_nothing_matches(self):
        self.assertEqual(parse_selection("후보가 마땅치 않다.", self.VALID, 3), [])


class SelectSymbolsTest(unittest.TestCase):
    ROWS = [
        {"code": "NVDA", "name": "NVIDIA", "price": "180.0",
         "change_pct": "2.1", "volume": "9000000"},
        {"code": "SOXL", "name": "SOXL", "price": "28.0",
         "change_pct": "4.2", "volume": "6000000"},
    ]

    def test_cli_analyst_selects_and_returns_note(self):
        prompts = []

        def runner(prompt):
            prompts.append(prompt)
            return "선정: SOXL, NVDA\nSOXL은 모멘텀이 강하다."

        analyst = ClaudeCodeAnalyst(runner=runner)
        codes, note = analyst.select_symbols("us", self.ROWS, 2,
                                             criteria_label="당일 거래량 상위")
        self.assertEqual(codes, ["SOXL", "NVDA"])
        self.assertIn("모멘텀", note)
        # 프롬프트에 단타 규칙과 후보 데이터, 출력 형식이 들어가야 한다
        self.assertIn("NVDA", prompts[0])
        self.assertIn("단타", prompts[0])
        self.assertIn("-2%", prompts[0])
        self.assertIn("없음", prompts[0])
        self.assertIn("선정:", prompts[0])

    def test_api_analyst_selects(self):
        from types import SimpleNamespace

        from autotrader.analysis import ClaudeAnalyst

        resp = SimpleNamespace(
            stop_reason="end_turn", stop_details=None,
            content=[SimpleNamespace(type="text", text="선정: NVDA\n이유.")])
        client = SimpleNamespace(beta=SimpleNamespace(
            messages=SimpleNamespace(create=lambda **kw: resp)))
        codes, note = ClaudeAnalyst(api_key="", client=client).select_symbols(
            "us", self.ROWS, 1)
        self.assertEqual(codes, ["NVDA"])
        self.assertIn("이유", note)


if __name__ == "__main__":
    unittest.main()
