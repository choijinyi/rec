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
        self.assertEqual(trader.config.stop_pct, 2.0)   # 기본 손절 -2%
        self.assertTrue(trader.config.require_ai)        # 기본: AI 선정 필수
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


class DashboardScriptSyntaxTest(unittest.TestCase):
    """대시보드 자바스크립트에 문법 오류가 있으면 모든 버튼이 죽는다."""

    def test_page_script_parses(self):
        import shutil
        import subprocess
        import tempfile

        from autotrader.webui import PAGE

        node = shutil.which("node")
        if not node:
            self.skipTest("node가 없어 JS 문법 검사를 건너뛴다")
        script = PAGE.split("<script>")[1].split("</script>")[0]
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(script)
        proc = subprocess.run([node, "--check", f.name],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class DashboardWiringTest(unittest.TestCase):
    def test_every_referenced_element_exists(self):
        import re

        from autotrader.webui import PAGE

        html, script = PAGE.split("<script>")[0], PAGE.split("<script>")[1]
        ids = set(re.findall(r'id="([A-Za-z0-9_]+)"', html))
        for ref in set(re.findall(r'\$\("([A-Za-z0-9_]+)"\)', script)):
            self.assertIn(ref, ids, f"스크립트가 없는 요소 #{ref}를 참조한다")

    def test_start_passes_stop_and_ai_option(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = make_state(tmp.name)
        r = state.start(market="kr", target_pct="2.5", max_positions="2",
                        cash="500000", stop_pct="0.8", require_ai=False)
        self.assertEqual(r, {"ok": True})
        self.assertEqual(state.trader.config.stop_pct, 0.8)
        self.assertEqual(state.trader.config.target_pct, 2.5)
        self.assertFalse(state.trader.config.require_ai)
        state.stop()


class TimingWiringTest(unittest.TestCase):
    """대시보드 → 엔진: 종목군·1회 손실 한도·AI 판단 연결."""

    def _state(self, extra=""):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = make_state(tmp.name, claude=extra)
        self.addCleanup(lambda: state.trader and state.trader.stop())
        return state

    def test_start_passes_risk_and_universe(self):
        state = self._state()
        r = state.start(market="us", target_pct="2", max_positions="2",
                        cash="1000", risk_pct="0.7", leveraged=False)
        self.assertEqual(r, {"ok": True})
        cfg = state.trader.config
        self.assertEqual(cfg.risk_pct, 0.7)
        self.assertIn("NVDA", cfg.universe)
        self.assertNotIn("TQQQ", cfg.universe)          # 레버리지 ETF 제외
        self.assertNotIn("ONCO", cfg.universe)
        self.assertEqual(state.params["universe"], len(cfg.universe))
        self.assertTrue(state.trader.journal_path.endswith(
            "autotrader_journal_us.jsonl"))

    def test_defaults_are_half_percent_risk_with_leveraged(self):
        state = self._state()
        state.start(market="us", target_pct="2", max_positions="2", cash="1000")
        cfg = state.trader.config
        self.assertEqual(cfg.risk_pct, 0.5)
        self.assertIn("TQQQ", cfg.universe)
        self.assertIsNotNone(state.trader.reviewer)

    def test_universe_section_in_config(self):
        state = self._state("[universe]\nadd = COIN\nremove = TSLA\n")
        state.start(market="us", target_pct="2", max_positions="2", cash="1000")
        self.assertIn("COIN", state.trader.config.universe)
        self.assertNotIn("TSLA", state.trader.config.universe)
        state.trader.stop()
        off = self._state("[universe]\nrestrict = false\n")
        off.start(market="us", target_pct="2", max_positions="2", cash="1000")
        self.assertIsNone(off.trader.config.universe)

    def test_invalid_risk_is_reported(self):
        state = self._state()
        r = state.start(market="us", target_pct="2", max_positions="2",
                        cash="1000", risk_pct="x")
        self.assertIn("1회 손실 한도", r["error"])
        r = state.start(market="us", target_pct="2", max_positions="2",
                        cash="1000", risk_pct="9")
        self.assertIn("1회 손실 한도", r["error"])

    def test_subscription_login_is_default_backend(self):
        state = self._state("[claude]\nbackend = auto\napi_key =\n")
        made = []
        state.cli_analyst_factory = lambda **kw: made.append(("cli", kw)) or "CLI"
        state.analyst_factory = lambda key, **kw: made.append(("api", kw)) or "API"
        self.assertEqual(state._make_analyst(), "CLI")
        self.assertEqual(made, [("cli", {})])           # 기본 모델(Opus 5.5)

    def test_model_and_effort_from_config(self):
        state = self._state(
            "[claude]\nmodel = claude-sonnet-5-5\neffort = low\napi_key = K\n")
        made = []
        state.analyst_factory = lambda key, **kw: made.append((key, kw)) or "API"
        self.assertEqual(state._make_analyst(), "API")
        self.assertEqual(made, [("K", {"model": "claude-sonnet-5-5",
                                       "effort": "low"})])

    def test_selector_and_reviewer_call_the_analyst(self):
        state = self._state()
        seen = {}

        class FakeAnalyst:
            label = "claude-opus-5-5 · 구독 로그인"

            def plan_entries(self, market, rows, num, context=None):
                seen["entry"] = (market, num, context)
                return [{"code": rows[0]["code"], "stop": 99.0,
                         "target": 102.0}], "근거"

            def review_positions(self, market, rows, context=None):
                seen["review"] = (market, context)
                return [{"code": rows[0]["code"], "action": "hold"}], "유지"

        state.cli_analyst_factory = lambda **kw: FakeAnalyst()
        state.start(market="us", target_pct="2", max_positions="2",
                    cash="1000", stop_pct="1.5")
        trader = state.trader
        plans, note = trader.selector([{"code": "NVDA", "price": "100"}], 1)
        self.assertEqual(plans[0]["code"], "NVDA")
        market, num, context = seen["entry"]
        self.assertEqual((market, num), ("us", 1))
        self.assertEqual(context["max_stop_pct"], 1.5)
        self.assertIn("session", context)
        self.assertEqual(state.status()["ai"], "claude-opus-5-5 · 구독 로그인")
        reviews, _ = trader.reviewer([{"code": "NVDA"}])
        self.assertEqual(reviews[0]["action"], "hold")
        self.assertEqual(seen["review"][0], "us")

    def test_status_shows_stop_and_target(self):
        from datetime import datetime

        from autotrader.scalper import Position

        state = self._state()
        state.start(market="us", target_pct="2", max_positions="2", cash="1000")
        trader = state.trader
        state.stop()                 # 매매 루프가 표시용 포지션을 건드리지 않게
        state.thread.join(5)
        trader.positions["NVDA"] = Position(
            "NVDA", 2, 200.0, datetime.now(), last_price=201.0,
            stop_price=198.5, target_price=203.0)
        trader.review_note = "유지 NVDA"
        s = state.status()
        self.assertEqual(s["positions"][0]["stop"], 198.5)
        self.assertEqual(s["positions"][0]["target"], 203.0)
        self.assertEqual(s["review_note"], "유지 NVDA")
        trader.positions.clear()

    def test_fee_defaults_and_override(self):
        state = self._state()
        state.start(market="us", target_pct="2", max_positions="2", cash="1000")
        self.assertEqual(state.trader.config.fee_pct, 0.07)   # 미국 편도 기본값
        self.assertEqual(state.trader.config.trail_pct, 0.0)  # 목표가에서 바로 익절
        self.assertEqual(state.status()["fees"], 0.0)
        state.trader.stop()
        kr = self._state()
        kr.start(market="kr", target_pct="2", max_positions="2", cash="1000000")
        self.assertEqual(kr.trader.config.fee_pct, 0.015)
        kr.trader.stop()
        custom = self._state("[strategy]\nfee_pct = 0.25\n")
        custom.start(market="us", target_pct="2", max_positions="2", cash="1000")
        self.assertEqual(custom.trader.config.fee_pct, 0.25)
