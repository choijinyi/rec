"""분할매매 웹 UI 테스트 (키움 API는 가짜로 대체)."""

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

    def send_market_order(self, account_no, code, side, quantity):
        pass


def make_state(tmpdir, mode="mock"):
    from pathlib import Path

    cfg = Path(tmpdir) / "config.ini"
    cfg.write_text(f"[kiwoom]\nappkey=A\nsecretkey=B\nmode={mode}\n",
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

    def test_start_builds_split_trader(self):
        r = self.state.start(market="kr", code="005930", buy_price="68000", sell_price="72000",
                             step_pct="3", buy_splits="4", sell_splits="2",
                             cash="1000000")
        self.assertEqual(r, {"ok": True})
        trader = self.state.trader
        self.assertEqual(trader.config.code, "005930")
        self.assertEqual(trader.config.buy_start_price, 68000.0)
        self.assertEqual(trader.config.sell_start_price, 72000.0)
        self.assertEqual(len(trader.buy_levels), 4)
        self.assertEqual(len(trader.sell_levels), 2)
        self.state.stop()

    def test_invalid_number_returns_error(self):
        r = self.state.start(market="kr", code="005930", buy_price="abc", sell_price="72000",
                             step_pct="3", buy_splits="3", sell_splits="3",
                             cash="1000000")
        self.assertIn("매수 시작 금액", r["error"])
        self.assertIsNone(self.state.trader)

    def test_status_exposes_levels(self):
        self.state.start(market="kr", code="005930", buy_price="10000", sell_price="10500",
                         step_pct="5", buy_splits="2", sell_splits="1",
                         cash="1000000")
        s = self.state.status()
        self.assertEqual(len(s["levels"]), 3)
        self.assertEqual(s["levels"][0]["price"], 10000.0)
        self.assertEqual(s["levels"][0]["side"], "buy")
        self.state.stop()

    def test_stop_without_start(self):
        self.assertIn("error", self.state.stop())


class RealModeConfirmTest(unittest.TestCase):
    def test_real_requires_yes(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = make_state(tmp.name, mode="real")
        r = state.start(market="kr", code="005930", buy_price="10000", sell_price="10500",
                        step_pct="5", buy_splits="2", sell_splits="2",
                        cash="1000000", confirm="")
        self.assertTrue(r.get("need_confirm"))
        self.assertIsNone(state.trader)
        r = state.start(market="kr", code="005930", buy_price="10000", sell_price="10500",
                        step_pct="5", buy_splits="2", sell_splits="2",
                        cash="1000000", confirm="YES")
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
        self.assertIn("분할매매".encode(), body)

    def test_status_endpoint(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/status") as r:
            data = json.loads(r.read())
        self.assertEqual(data["config_mode"], "mock")

    def test_start_and_stop_roundtrip(self):
        r = self._post("/api/start", {
            "market": "kr", "code": "005930", "buy_price": "10000", "sell_price": "10500",
            "step_pct": "5", "buy_splits": "2", "sell_splits": "2",
            "cash": "1000000"})
        self.assertEqual(r, {"ok": True})
        self.assertEqual(self._post("/api/stop", {}), {"ok": True})


if __name__ == "__main__":
    unittest.main()
