"""웹 UI와 Claude 분석 모듈 테스트 (외부 API는 가짜로 대체)."""

import json
import unittest
import urllib.request
from types import SimpleNamespace

from autotrader.analysis import MODEL, ClaudeAnalyst, MarketSnapshot
from autotrader.webui import AppState, Handler
from http.server import ThreadingHTTPServer
import threading


def make_snapshot(**kw):
    base = dict(market="us", code="AAPL", strategy="sma_crossover", mode="mock",
                prices=[("10:00:00", 213.0), ("10:01:00", 214.0)],
                equity=10_000.0, cash=9_000.0, realized_pnl=12.5,
                position_qty=4, position_avg_price=212.0, orders_today=2,
                recent_fills=["10:01 매수 4주 @213.00"])
    base.update(kw)
    return MarketSnapshot(**base)


class FakeClaudeClient:
    """anthropic 클라이언트의 beta.messages.create 표면만 흉내낸다."""

    def __init__(self, stop_reason="end_turn", text="분석 결과입니다."):
        self.kwargs = None
        self._resp = SimpleNamespace(
            stop_reason=stop_reason,
            stop_details=None,
            content=[SimpleNamespace(type="text", text=text)],
        )
        outer = self

        def create(**kwargs):
            outer.kwargs = kwargs
            return outer._resp

        self.beta = SimpleNamespace(
            messages=SimpleNamespace(create=create))


class ClaudeAnalystTest(unittest.TestCase):
    def test_uses_fable_model_with_fallbacks(self):
        fake = FakeClaudeClient()
        analyst = ClaudeAnalyst(api_key="", client=fake)
        text = analyst.analyze(make_snapshot())
        self.assertEqual(text, "분석 결과입니다.")
        self.assertEqual(fake.kwargs["model"], MODEL)
        self.assertEqual(fake.kwargs["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", fake.kwargs["betas"])
        self.assertNotIn("thinking", fake.kwargs)  # Fable은 thinking 상시 활성

    def test_prompt_contains_market_state(self):
        fake = FakeClaudeClient()
        ClaudeAnalyst(api_key="", client=fake).analyze(make_snapshot())
        prompt = fake.kwargs["messages"][0]["content"]
        self.assertIn("AAPL", prompt)
        self.assertIn("모의투자", prompt)
        self.assertIn("213", prompt)

    def test_refusal_is_reported_not_crashed(self):
        fake = FakeClaudeClient(stop_reason="refusal")
        text = ClaudeAnalyst(api_key="", client=fake).analyze(make_snapshot())
        self.assertIn("거절", text)

    def test_missing_key_raises(self):
        with self.assertRaises(RuntimeError):
            ClaudeAnalyst(api_key="")


class WebUiHttpTest(unittest.TestCase):
    """실제 HTTP 서버를 임시 포트에 띄워 라우팅을 검증한다."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        from pathlib import Path

        cls.tmp = tempfile.TemporaryDirectory()
        cfg = Path(cls.tmp.name) / "config.ini"
        cfg.write_text("[kiwoom]\nappkey=A\nsecretkey=B\nmode=mock\n",
                       encoding="utf-8")
        Handler.state = AppState(str(cfg))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
            return r.status, r.read()

    def _post(self, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def test_index_serves_dashboard(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("AutoTrader".encode(), body)

    def test_status_endpoint(self):
        status, body = self._get("/api/status")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertFalse(data["running"])
        self.assertEqual(data["config_mode"], "mock")

    def test_backtest_endpoint(self):
        data = self._post("/api/backtest", {"strategy": "sma_crossover"})
        self.assertIn("return_pct", data)
        self.assertGreater(data["trades"], 0)

    def test_stop_without_start(self):
        data = self._post("/api/stop", {})
        self.assertIn("error", data)

    def test_analyze_without_key_returns_error(self):
        data = self._post("/api/analyze", {})
        self.assertIn("error", data)
        self.assertIn("API 키", data["error"])


if __name__ == "__main__":
    unittest.main()


class ResolveCashTest(unittest.TestCase):
    def test_valid_values(self):
        self.assertEqual(AppState.resolve_cash("us", "1000"), 1000.0)
        self.assertEqual(AppState.resolve_cash("us", "2,500"), 2500.0)
        self.assertEqual(AppState.resolve_cash("kr", 500000), 500000.0)

    def test_invalid_falls_back_to_market_default(self):
        self.assertEqual(AppState.resolve_cash("us", ""), 1000.0)
        self.assertEqual(AppState.resolve_cash("us", None), 1000.0)
        self.assertEqual(AppState.resolve_cash("us", "abc"), 1000.0)
        self.assertEqual(AppState.resolve_cash("us", "-50"), 1000.0)
        self.assertEqual(AppState.resolve_cash("kr", ""), 1000000.0)
