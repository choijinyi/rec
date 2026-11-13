"""AI 매매 시점 판단(analysis) 테스트: 프롬프트·응답 해석·호출 백엔드."""

import unittest
from types import SimpleNamespace
from unittest import mock

from autotrader.analysis import (
    EFFORT, MODEL, ClaudeAnalyst, ClaudeCodeAnalyst, extract_json,
    parse_entry_plans, parse_reviews,
)

ROWS = [
    {"code": "NVDA", "name": "엔비디아", "kind": "대형주", "price": "201.47",
     "change_pct": "0.71", "volume": "46000000",
     "bars": {"last": 201.47, "day_high": 202.1, "day_low": 198.3,
              "vwap": 200.85, "vs_vwap_pct": 0.31, "ret_5m": 0.05,
              "ret_15m": 0.22, "ret_60m": 0.65, "avg_range_pct": 0.08,
              "closes_5m": [200.1, 200.6, 201.4],
              "closes_1m": [201.2, 201.3, 201.47],
              "volumes_1m": [5000, 6200, 7100]}},
    {"code": "SPY", "name": "SPDR S&P 500", "kind": "ETF", "price": "680.10",
     "change_pct": "0.30", "volume": "30000000"},
]

ENTRY_JSON = (
    '{"market_view": "지수가 완만한 상승 흐름이다", "decisions": ['
    '{"code": "NVDA", "action": "enter", "max_price": 201.8, "stop": 200.3, '
    '"target": 203.6, "reason": "VWAP 눌림 뒤 재상승"}, '
    '{"code": "SPY", "action": "wait", "reason": "변동폭이 작다"}]}')


class ExtractJsonTest(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_code_fence_and_surrounding_text(self):
        text = '판단 결과입니다.\n```json\n{"a": {"b": "}"}}\n```\n이상입니다.'
        self.assertEqual(extract_json(text), {"a": {"b": "}"}})

    def test_skips_broken_braces_before_the_real_object(self):
        self.assertEqual(extract_json('{미완성 ... {"ok": true}'), {"ok": True})

    def test_no_json_raises(self):
        with self.assertRaises(ValueError):
            extract_json("살 만한 종목이 없다.")


class ParseEntryPlansTest(unittest.TestCase):
    VALID = ["NVDA", "SPY", "AAPL"]

    def test_enter_decisions_become_plans(self):
        plans, note = parse_entry_plans(ENTRY_JSON, self.VALID, 2)
        self.assertEqual(plans, [{"code": "NVDA", "max_price": 201.8,
                                  "stop": 200.3, "target": 203.6,
                                  "reason": "VWAP 눌림 뒤 재상승",
                                  "mode": "pullback"}])
        self.assertIn("장 흐름", note)
        self.assertIn("진입 NVDA", note)
        self.assertIn("대기 SPY", note)

    def test_wait_only_means_no_plans(self):
        text = '{"decisions": [{"code": "NVDA", "action": "wait", "reason": "과열"}]}'
        plans, note = parse_entry_plans(text, self.VALID, 2)
        self.assertEqual(plans, [])
        self.assertIn("진입할 자리가 없다", note)

    def test_mentioned_codes_in_reasons_are_not_bought(self):
        # 대기·제외 사유에 나온 종목을 진입으로 오해하지 않는다
        text = ('{"decisions": [{"code": "SPY", "action": "skip", '
                '"reason": "NVDA와 AAPL보다 약하다"}]}')
        self.assertEqual(parse_entry_plans(text, self.VALID, 3)[0], [])

    def test_unknown_codes_duplicates_and_cap(self):
        text = ('{"decisions": ['
                '{"code": "MSFT", "action": "enter", "stop": 1, "target": 2},'
                '{"code": "nvda", "action": "enter", "stop": "200.30", "target": 203},'
                '{"code": "NVDA", "action": "enter", "stop": 199, "target": 205},'
                '{"code": "AAPL", "action": "enter", "stop": 250, "target": 256},'
                '{"code": "SPY", "action": "enter", "stop": 670, "target": 690}]}')
        plans, _ = parse_entry_plans(text, self.VALID, 2)
        self.assertEqual([p["code"] for p in plans], ["NVDA", "AAPL"])
        self.assertEqual(plans[0]["stop"], 200.3)       # 문자열 숫자도 읽는다

    def test_missing_levels_are_none(self):
        text = '{"decisions": [{"code": "NVDA", "action": "enter", "stop": null}]}'
        plan = parse_entry_plans(text, self.VALID, 1)[0][0]
        self.assertIsNone(plan["stop"])
        self.assertIsNone(plan["target"])

    def test_korean_codes_with_nxt_suffix(self):
        text = '{"decisions": [{"code": "233740", "action": "enter"}]}'
        plans, _ = parse_entry_plans(text, ["233740_AL", "114800"], 2)
        self.assertEqual(plans[0]["code"], "233740_AL")

    def test_free_text_without_json_is_an_error(self):
        with self.assertRaises(ValueError):
            parse_entry_plans("선정: NVDA", self.VALID, 2)


class ParseReviewsTest(unittest.TestCase):
    def test_actions(self):
        text = ('{"reviews": ['
                '{"code": "NVDA", "action": "tighten", "stop": 200.9, "reason": "새 지지"},'
                '{"code": "SPY", "action": "exit", "reason": "흐름이 꺾였다"},'
                '{"code": "AAPL", "action": "hold", "stop": null, "reason": "유지"}]}')
        reviews, note = parse_reviews(text, ["NVDA", "SPY", "AAPL"])
        self.assertEqual([(r["code"], r["action"], r["stop"]) for r in reviews],
                         [("NVDA", "tighten", 200.9), ("SPY", "exit", None),
                          ("AAPL", "hold", None)])
        self.assertIn("손절 상향", note)
        self.assertIn("청산 SPY", note)

    def test_tighten_without_price_is_hold(self):
        text = '{"reviews": [{"code": "NVDA", "action": "tighten", "stop": null}]}'
        self.assertEqual(parse_reviews(text, ["NVDA"])[0][0]["action"], "hold")

    def test_unknown_action_is_hold(self):
        text = '{"reviews": [{"code": "NVDA", "action": "double_down", "stop": 1}]}'
        review = parse_reviews(text, ["NVDA"])[0][0]
        self.assertEqual((review["action"], review["stop"]), ("hold", None))

    def test_positions_not_held_are_ignored(self):
        text = '{"reviews": [{"code": "TSLA", "action": "exit"}]}'
        self.assertEqual(parse_reviews(text, ["NVDA"])[0], [])


class CliAnalystTest(unittest.TestCase):
    def test_plan_entries_sends_rules_and_data(self):
        prompts = []

        def runner(prompt):
            prompts.append(prompt)
            return ENTRY_JSON

        analyst = ClaudeCodeAnalyst(runner=runner)
        plans, note = analyst.plan_entries(
            "us", ROWS, 2, {"session": "정규장", "minutes_to_close": 200,
                            "max_stop_pct": 2.0, "min_stop_pct": 0.4,
                            "min_rr": 1.2, "min_target_pct": 1.0})
        self.assertEqual([p["code"] for p in plans], ["NVDA"])
        prompt = prompts[0]
        for needle in ("NVDA", "VWAP", "정규장", "0.4%~2.0%", "1.2배",
                       "최소 1.0% 위", "수수료",
                       "max_price", "JSON", "대형주"):
            self.assertIn(needle, prompt)
        self.assertIn("분봉을 받지 못했다", prompt)       # SPY에는 분봉이 없다

    def test_review_positions(self):
        analyst = ClaudeCodeAnalyst(runner=lambda p: (
            '{"reviews": [{"code": "NVDA", "action": "exit", "reason": "꺾임"}]}'))
        reviews, _ = analyst.review_positions(
            "us", [{"code": "NVDA", "entry": 200.0, "price": 201.0, "pct": 0.5,
                    "held_min": 12, "stop": 198.0, "target": 204.0,
                    "peak": 201.5}])
        self.assertEqual(reviews[0]["action"], "exit")

    def test_empty_response_is_an_error(self):
        with self.assertRaises(RuntimeError):
            ClaudeCodeAnalyst(runner=lambda p: "").plan_entries("us", ROWS, 1)

    def test_defaults_to_opus(self):
        analyst = ClaudeCodeAnalyst(runner=lambda p: "")
        self.assertEqual(MODEL, "claude-opus-5-5")
        self.assertEqual(analyst._arg_tiers()[0],
                         ["--model", MODEL, "--effort", EFFORT])
        self.assertIn("구독 로그인", analyst.label)


class CliInvocationTest(unittest.TestCase):
    """설치된 Claude Code가 인자나 모델 이름을 모르면 단순한 인자로 내려간다."""

    def setUp(self):
        ClaudeCodeAnalyst._tier_cache.clear()
        patcher = mock.patch.object(ClaudeCodeAnalyst, "_find_cli",
                                    staticmethod(lambda: "claude"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, results):
        calls = []

        def fake_run(args, **kw):
            calls.append(args)
            code, out, err = results[min(len(calls), len(results)) - 1]
            return SimpleNamespace(returncode=code, stdout=out, stderr=err)

        with mock.patch("subprocess.run", fake_run):
            analyst = ClaudeCodeAnalyst()
            try:
                out = analyst._run_cli("본문")
            except RuntimeError as e:
                out = e
        return calls, out

    def test_first_tier_uses_model_and_effort(self):
        calls, out = self._run([(0, "{}", "")])
        self.assertEqual(out, "{}")
        self.assertEqual(calls[0][3:],
                         ["--model", "claude-opus-5-5", "--effort", "medium"])

    def test_falls_back_when_option_is_unknown(self):
        calls, out = self._run([(1, "", "error: unknown option '--effort'"),
                                (0, "{}", "")])
        self.assertEqual(out, "{}")
        self.assertEqual(calls[1][3:], ["--model", "claude-opus-5-5"])
        # 다음 호출은 통했던 인자부터 시작한다
        with mock.patch("subprocess.run", lambda args, **kw: SimpleNamespace(
                returncode=0, stdout=" ".join(args[3:]), stderr="")):
            self.assertEqual(ClaudeCodeAnalyst()._run_cli("본문"),
                             "--model claude-opus-5-5")

    def test_alias_then_default_model(self):
        calls, out = self._run([(1, "", "unknown option"), (1, "", "invalid model"),
                                (1, "", "invalid model"), (0, "{}", "")])
        self.assertEqual(calls[2][3:], ["--model", "opus"])
        self.assertEqual(calls[3][3:], [])
        self.assertEqual(out, "{}")

    def test_usage_limit_is_not_retried(self):
        calls, out = self._run([(1, "", "Usage limit reached. Resets at 3am")])
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(out, RuntimeError)
        self.assertIn("limit", str(out))


class ApiAnalystTest(unittest.TestCase):
    def _client(self, resp, seen):
        def create(**kw):
            seen.update(kw)
            return resp
        return SimpleNamespace(beta=SimpleNamespace(
            messages=SimpleNamespace(create=create)))

    def test_calls_opus_with_effort_and_fallbacks(self):
        resp = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text=ENTRY_JSON)])
        seen = {}
        analyst = ClaudeAnalyst(api_key="", client=self._client(resp, seen))
        plans, _ = analyst.plan_entries("us", ROWS, 1)
        self.assertEqual(plans[0]["code"], "NVDA")
        self.assertEqual(seen["model"], "claude-opus-5-5")
        self.assertEqual(seen["output_config"], {"effort": "medium"})
        self.assertEqual(seen["fallbacks"], "default")
        self.assertNotIn("thinking", seen)       # 생각은 항상 켜져 있다
        self.assertNotIn("temperature", seen)

    def test_refusal_is_an_error(self):
        resp = SimpleNamespace(stop_reason="refusal", content=[])
        analyst = ClaudeAnalyst(api_key="", client=self._client(resp, {}))
        with self.assertRaises(RuntimeError):
            analyst.plan_entries("us", ROWS, 1)


if __name__ == "__main__":
    unittest.main()
