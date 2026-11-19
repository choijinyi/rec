"""사용자 매수 중단: 새로 사는 것만 멈추고 보유 종목 관리는 계속한다."""

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

try:
    from test_timing import FakeAPI, NVDA, advance, make, plan, scan
    from test_webui import make_state
except ImportError:
    from tests.test_timing import FakeAPI, NVDA, advance, make, plan, scan
    from tests.test_webui import make_state

from autotrader.webui import Handler


class EnginePauseTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI({"NVDA": 200.0}, [NVDA])
        self.trader = make(self.api, plans=[plan()], trail_pct=0)

    def test_paused_engine_does_not_buy(self):
        self.trader.pause_entries(True)
        scan(self.trader)
        self.assertEqual(self.api.orders, [])
        self.assertEqual(self.trader.halt_reason, "매수 중단(사용자)")

    def test_no_ai_calls_while_paused(self):
        calls = []
        self.trader._maybe_scan = lambda slots: calls.append(slots)
        self.trader.pause_entries(True)
        self.trader.step()
        self.trader.step()
        self.assertEqual(calls, [])          # 판단 요청도 하지 않는다

    def test_decision_received_before_pause_is_dropped(self):
        self.trader._scan_worker(1)          # 진입 판단이 도착해 있다
        self.trader.pause_entries(True)
        self.trader.step()
        self.assertEqual(self.api.orders, [])
        self.trader.pause_entries(False)
        self.trader.step()                   # 재개해도 예전 판단으로 사지 않는다
        self.assertEqual(self.api.orders, [])

    def test_held_positions_are_still_managed(self):
        scan(self.trader)
        self.assertIn("NVDA", self.trader.positions)
        self.trader.pause_entries(True)
        self.api.prices["NVDA"] = 197.0      # 손절가(198) 아래
        self.trader.step()
        self.assertEqual(self.trader.trades[-1].reason, "손절")
        scan(self.trader)                    # 자리가 비어도 다시 사지 않는다
        self.assertEqual([o[1] for o in self.api.orders], ["buy", "sell"])

    def test_take_profit_and_review_continue(self):
        reviews = []
        self.trader.reviewer = lambda rows: (reviews.append(rows) or [], "")
        scan(self.trader)
        self.trader.pause_entries(True)
        advance(self.trader, 300)
        self.trader._review_worker(self.trader._review_rows())
        self.assertEqual(len(reviews), 1)    # AI 청산 점검은 계속한다
        self.api.prices["NVDA"] = 203.5      # 목표가(203) 도달
        self.trader.step()
        self.assertEqual(self.trader.trades[-1].reason, "익절")

    def test_resume_buys_again(self):
        self.trader.pause_entries(True)
        scan(self.trader)
        self.trader.pause_entries(False)
        self.assertTrue(self.trader._rescan_asap)
        scan(self.trader)
        self.assertEqual([o[:2] for o in self.api.orders], [("NVDA", "buy")])
        self.assertEqual(self.trader.halt_reason, "")

    def test_logs_once_per_change(self):
        self.trader.pause_entries(True)
        self.trader.pause_entries(True)
        self.trader.pause_entries(False)
        self.trader.pause_entries(False)
        events = self.trader.events
        self.assertEqual(sum("매수 중단(사용자)" in e for e in events), 1)
        self.assertEqual(sum("매수 재개(사용자)" in e for e in events), 1)


class DashboardPauseTest(unittest.TestCase):
    def _state(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = make_state(tmp.name)
        self.addCleanup(lambda: state.trader and state.trader.stop())
        return state

    def test_toggle_while_running(self):
        state = self._state()
        state.start(market="us", target_pct="2", max_positions="2", cash="1000")
        self.assertFalse(state.status()["buy_paused"])
        self.assertEqual(state.pause_buys(True), {"ok": True, "buy_paused": True})
        self.assertTrue(state.trader.entries_paused)
        self.assertTrue(state.status()["buy_paused"])
        self.assertEqual(state.pause_buys(False),
                         {"ok": True, "buy_paused": False})
        self.assertFalse(state.trader.entries_paused)

    def test_can_start_paused(self):
        # 시작 전에 눌러 두면 매수를 멈춘 채로 시작한다 (보유 종목만 관리)
        state = self._state()
        self.assertEqual(state.pause_buys(True), {"ok": True, "buy_paused": True})
        self.assertTrue(state.status()["buy_paused"])
        state.start(market="us", target_pct="2", max_positions="2", cash="1000")
        self.assertTrue(state.trader.entries_paused)

    def test_pause_survives_stop_and_restart(self):
        state = self._state()
        state.start(market="us", target_pct="2", max_positions="2", cash="1000")
        state.pause_buys(True)
        state.stop()
        state.thread.join(5)
        state.start(market="us", target_pct="2", max_positions="2", cash="1000")
        self.assertTrue(state.trader.entries_paused)


class PauseRouteTest(unittest.TestCase):
    def test_http_route(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        saved = getattr(Handler, "state", None)
        Handler.state = make_state(tmp.name)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(lambda: setattr(Handler, "state", saved))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]

        def post(body):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/pause",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())

        self.assertEqual(post({"paused": True}), {"ok": True, "buy_paused": True})
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status") as r:
            self.assertTrue(json.loads(r.read())["buy_paused"])
        self.assertEqual(post({"paused": False}),
                         {"ok": True, "buy_paused": False})


if __name__ == "__main__":
    unittest.main()
