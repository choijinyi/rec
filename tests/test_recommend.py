"""오늘의 추천(거래량 상위 + Claude 후보 선정) 테스트."""

import unittest
from types import SimpleNamespace

from autotrader.analysis import ClaudeAnalyst
from autotrader.kiwoom_rest import KiwoomRestClient
from autotrader.webui import AppState


RANK_ROWS = [
    {"stk_cd": "005930", "stk_nm": "삼성전자", "cur_prc": "-70300",
     "flu_rt": "-1.20", "trde_qty": "12345678"},
    {"stk_cd": "000660", "stk_nm": "SK하이닉스", "cur_prc": "+250000",
     "flu_rt": "2.10", "trde_qty": "8888888"},
]


class FakeTransport:
    def __init__(self, market="kr"):
        self.market = market
        self.calls = []

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        if url.endswith("/oauth2/token"):
            return {"token": "T", "expires_dt": ""}
        if "rkinfo" in url:
            return {"return_code": 0, "rank_list": RANK_ROWS}
        raise AssertionError(url)


class TopVolumeTest(unittest.TestCase):
    def test_kr_ranking_request_and_parse(self):
        t = FakeTransport()
        client = KiwoomRestClient("A", "S", transport=t, market="kr")
        rows = client.top_volume_stocks(limit=5)
        url, headers, body = t.calls[-1]
        self.assertIn("/api/dostk/rkinfo", url)
        self.assertEqual(headers["api-id"], "ka10030")
        self.assertEqual(rows[0]["code"], "005930")
        self.assertEqual(rows[0]["name"], "삼성전자")
        self.assertEqual(rows[1]["change_pct"], "2.10")

    def test_us_ranking_uses_us_api(self):
        t = FakeTransport("us")
        client = KiwoomRestClient("A", "S", transport=t, market="us")
        client.top_volume_stocks()
        url, headers, _ = t.calls[-1]
        self.assertIn("/api/us/rkinfo", url)
        self.assertEqual(headers["api-id"], "usa20530")

    def test_change_criteria_api_ids(self):
        t = FakeTransport()
        KiwoomRestClient("A", "S", transport=t, market="kr").top_stocks("change")
        self.assertEqual(t.calls[-1][1]["api-id"], "ka10027")
        t2 = FakeTransport("us")
        KiwoomRestClient("A", "S", transport=t2, market="us").top_stocks("change")
        self.assertEqual(t2.calls[-1][1]["api-id"], "usa20910")

    def test_unknown_criteria_rejected(self):
        from autotrader.kiwoom_rest import KiwoomRestError
        client = KiwoomRestClient("A", "S", transport=FakeTransport())
        with self.assertRaises(KiwoomRestError):
            client.top_stocks("moon_phase")

    def test_token_mismatch_hint(self):
        from autotrader.kiwoom_rest import KiwoomRestError

        def mismatch(url, headers, body):
            return {"return_msg": "입력 값 오류입니다[8030:투자구분(실전/모의)이 "
                                  "달라서 Appkey를 사용할수가 없습니다]",
                    "return_code": 2}

        client = KiwoomRestClient("A", "S", transport=mismatch)
        with self.assertRaises(KiwoomRestError) as ctx:
            client.current_price("005930")
        self.assertIn("mode", str(ctx.exception))
        self.assertIn("어긋났다", str(ctx.exception))


class FakeClaudeClient:
    def __init__(self):
        self.kwargs = None
        outer = self

        def create(**kwargs):
            outer.kwargs = kwargs
            return SimpleNamespace(
                stop_reason="end_turn", stop_details=None,
                content=[SimpleNamespace(type="text", text="1) 005930 삼성전자 ...")])

        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


class RecommendPromptTest(unittest.TestCase):
    def test_prompt_contains_candidates(self):
        fake = FakeClaudeClient()
        analyst = ClaudeAnalyst(api_key="", client=fake)
        rows = [{"code": "005930", "name": "삼성전자", "price": "70300",
                 "change_pct": "-1.2", "volume": "123"}]
        text = analyst.recommend("kr", "mock", rows)
        self.assertIn("005930", fake.kwargs["messages"][0]["content"])
        self.assertIn("삼성전자", text)


class AppStateRecommendTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "config.ini"

    def tearDown(self):
        self.tmp.cleanup()

    def _state(self, with_claude_key: bool):
        content = "[kiwoom]\nappkey=A\nsecretkey=S\nmode=mock\n"
        if with_claude_key:
            content += "[claude]\napi_key=sk-test\n"
        self.cfg.write_text(content, encoding="utf-8")
        state = AppState(str(self.cfg))
        state.client_factory = (
            lambda ak, sk, mock=True, market="kr", **kw:
            KiwoomRestClient(ak, sk, mock=mock, market=market,
                             transport=FakeTransport(market)))
        return state

    def test_without_claude_key_returns_plain_listing(self):
        state = self._state(with_claude_key=False)
        out = state.recommend("kr")
        self.assertIn("목록만 표시", out["text"])
        self.assertIn("005930", out["text"])

    def test_with_claude_key_returns_ai_text(self):
        state = self._state(with_claude_key=True)
        state.analyst_factory = lambda key: ClaudeAnalyst(
            api_key="", client=FakeClaudeClient())
        out = state.recommend("kr")
        self.assertIn("관심 후보", out["text"])
        self.assertIn("삼성전자", out["text"])


if __name__ == "__main__":
    unittest.main()
