"""Claude 분석 백엔드 선택(API 키 vs Claude Code 구독 로그인) 테스트."""

import tempfile
import unittest
from pathlib import Path

from autotrader.analysis import ClaudeCodeAnalyst
from autotrader.webui import AppState
from tests.test_webui import make_snapshot


class ClaudeCodeAnalystTest(unittest.TestCase):
    def test_analyze_via_injected_runner(self):
        prompts = []

        def runner(prompt):
            prompts.append(prompt)
            return "구독 분석 결과"

        analyst = ClaudeCodeAnalyst(runner=runner)
        text = analyst.analyze(make_snapshot())
        self.assertEqual(text, "구독 분석 결과")
        self.assertIn("AAPL", prompts[0])          # 시장 데이터 포함
        self.assertIn("투자 자문이 아니다", prompts[0])  # 시스템 지침 포함

    def test_recommend_via_injected_runner(self):
        analyst = ClaudeCodeAnalyst(runner=lambda p: "후보: 005930")
        rows = [{"code": "005930", "name": "삼성전자", "price": "70300",
                 "change_pct": "1.0", "volume": "1"}]
        self.assertIn("005930", analyst.recommend("kr", "real", rows))

    def test_empty_output_is_reported(self):
        analyst = ClaudeCodeAnalyst(runner=lambda p: "")
        self.assertIn("비어", analyst.analyze(make_snapshot()))


class BackendSelectionTest(unittest.TestCase):
    def _state(self, claude_section: str) -> AppState:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = Path(self.tmp.name) / "config.ini"
        cfg.write_text("[kiwoom]\nappkey=A\nsecretkey=B\nmode=mock\n"
                       + claude_section, encoding="utf-8")
        state = AppState(str(cfg))
        state.analyst_factory = lambda key: ("api", key)
        state.cli_analyst_factory = lambda: ("cli",)
        return state

    def test_auto_with_key_uses_api(self):
        state = self._state("[claude]\napi_key = sk-test\n")
        self.assertEqual(state._make_analyst(), ("api", "sk-test"))

    def test_auto_without_key_uses_cli(self):
        state = self._state("")
        self.assertEqual(state._make_analyst(), ("cli",))

    def test_explicit_cli_backend_ignores_key(self):
        state = self._state("[claude]\napi_key = sk-test\nbackend = cli\n")
        self.assertEqual(state._make_analyst(), ("cli",))

    def test_explicit_api_backend(self):
        state = self._state("[claude]\napi_key = sk-test\nbackend = api\n")
        self.assertEqual(state._make_analyst(), ("api", "sk-test"))


if __name__ == "__main__":
    unittest.main()
