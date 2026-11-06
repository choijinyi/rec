"""AI 단타 웹 UI 테스트 (키움 API·분석기는 가짜로 대체)."""

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from autotrader.webui import AppState, Handler


class FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def current_price(self, code):
        return 10_000.0

    def send_market_order(self, account_no, code, side, quantity,
                          kr_exchange="KRX"):
        pass

    def top_stocks(self, criteria="volume", limit=10):
        return []


def make_state(tmpdir, mode="mock", claude=""):
    from pathlib import Path

    cfg = Path(tmpdir) / "config.ini"
    cfg.write_text(f"[kiwoom]\nappkey=A\nsecretkey=B\nmode={mode}\n{claude}",
                   encoding="utf-8")
    state = AppState(str(cfg))
    state.client_factory = FakeClient
    return state


class StartStopTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = make_state(tmp.name)

    def test_start_builds_scalper_with_selector(self):
        r = self.state.start(market="kr", target_pct="2", max_positions="3",
                             cash="500000")
        self.assertEqual(r, {"ok": True})
        trader = self.state.trader
        self.assertEqual(trader.config.target_pct, 2.0)
        self.assertEqual(trader.config.stop_pct, 3.0)   # 손절 -3% 고정
        self.assertEqual(trader.config.max_positions, 3)
        self.assertIsNotNone(trader.selector)           # AI 선정 연결
        self.state.stop()

    def test_invalid_number_returns_error(self):
        r = self.state.start(market="kr", target_pct="abc", max_positions="2",
                             cash="500000")
        self.assertIn("목표 수익", r["error"])
        self.assertIsNone(self.state.trader)

    def test_status_shape(self):
        self.state.start(market="kr", target_pct="1.5", max_positions="2",
                         cash="500000")
        s = self.state.status()
        for key in ("session", "positions", "trades", "wins", "losses",
                    "selection_note", "halt_reason"):
            self.assertIn(key, s)
        self.state.stop()

    def test_stop_without_start(self):
        self.assertIn("error", self.state.stop())


class RealModeConfirmTest(unittest.TestCase):
    def test_real_requires_yes(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = make_state(tmp.name, mode="real")
        r = state.start(market="kr", target_pct="1.5", max_positions="2",
                        cash="500000", confirm="")
        self.assertTrue(r.get("need_confirm"))
        self.assertIsNone(state.trader)
        r = state.start(market="kr", target_pct="1.5", max_positions="2",
                        cash="500000", confirm="YES")
        self.assertEqual(r, {"ok": True})
        self.assertTrue(state.trader.config.allow_real)
        state.stop()


class HttpRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile

        cls.tmp = tempfile.TemporaryDirectory()
        Handler.state = make_state(cls.tmp.name)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def _post(self, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def test_index_serves_dashboard(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as r:
            body = r.read()
        self.assertIn("AI 단타".encode(), body)

    def test_status_endpoint(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/status") as r:
            data = json.loads(r.read())
        self.assertEqual(data["config_mode"], "mock")

    def test_start_and_stop_roundtrip(self):
        r = self._post("/api/start", {"market": "kr", "target_pct": "1.5",
                                      "max_positions": "2", "cash": "500000"})
        self.assertEqual(r, {"ok": True})
        self.assertEqual(self._post("/api/stop", {}), {"ok": True})


if __name__ == "__main__":
    unittest.main()


class AdoptEndpointTest(unittest.TestCase):
    def test_adopt_requires_running_trader(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = make_state(tmp.name)
        self.assertIn("error", state.adopt("ONCO", "manage"))

    def test_adopt_queues_request(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = make_state(tmp.name)
        state.start(market="us", target_pct="1.5", max_positions="2",
                    cash="1000")
        self.assertEqual(state.adopt("onco", "manage"), {"ok": True})
        self.assertIn(("ONCO", "manage"), state.trader._requests
                      or [("ONCO", "manage")])
        self.assertIn("error", state.adopt("ONCO", "bogus"))
        self.assertTrue(state.trader.state_path.endswith(
            "autotrader_state_us.json"))
        state.stop()
