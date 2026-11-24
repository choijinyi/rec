"""대시보드 연결: 지정 종목 분할 매매(계획 확인·시작·요청)와 매수 가능 금액."""

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from autotrader.kiwoom_rest import KiwoomRestClient, KiwoomRestError
from autotrader.webui import PAGE, AppState, Handler


class FakeClient:
    price = 100.0
    orders = []

    def __init__(self, appkey, secretkey, mock=True, market="kr"):
        self.market = market

    def current_price(self, code):
        if code == "NOPE":
            raise KiwoomRestError("종목을 찾지 못했다")
        return FakeClient.price

    def send_market_order(self, account_no, code, side, quantity,
                          kr_exchange="KRX", limit_price=None):
        FakeClient.orders.append((code, side, quantity))
        return "1"

    def top_stocks(self, criteria="volume", limit=10):
        return []

    def buying_power(self):
        return {"currency": "USD", "orderable": 1234.56, "deposit": 1000.0,
                "krw_deposit": 350000.0}


def make_state(tmp, mode="mock"):
    cfg = Path(tmp) / "config.ini"
    cfg.write_text(f"[kiwoom]\nappkey=A\nsecretkey=B\nmode={mode}\n",
                   encoding="utf-8")
    state = AppState(str(cfg))
    state.client_factory = FakeClient
    FakeClient.price, FakeClient.orders = 100.0, []
    return state


def wait_for(check, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


class SplitBase(unittest.TestCase):
    mode = "mock"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.state = make_state(self.tmp, self.mode)
        self.addCleanup(self._stop)

    def _stop(self):
        for engine in self.state.splitters.values():
            engine.stop()
        if self.state.trader:
            self.state.trader.stop()

    BODY = dict(market="us", code="soxl", price="100", budget="1000",
                steps="5", gap_pct="1", mode="each")


class PreviewTest(SplitBase):
    def test_preview_lists_the_plan_without_ordering(self):
        r = self.state.split_preview(**self.BODY)
        self.assertTrue(r["ok"])
        self.assertIn("SOXL 분할 매매 계획 [회차별 매도]", r["text"])
        self.assertIn("100.00×2주, 99.00×2주", r["text"])
        self.assertIn("101.00, 99.99", r["text"])
        self.assertIn("손절 없음", r["text"])
        self.assertIn("1~1회차 2주", r["text"])          # 현재가 = 지정가
        self.assertEqual(FakeClient.orders, [])
        self.assertEqual(self.state.splitters, {})       # 엔진도 만들지 않는다

    def test_preview_reports_bad_input(self):
        for change, text in (({"price": "abc"}, "숫자가 아니다"),
                             ({"budget": "100"}, "1주도 살 수 없다"),
                             ({"code": ""}, "종목코드"),
                             ({"mode": "ladder"}, "매도 시작가")):
            r = self.state.split_preview(**{**self.BODY, **change})
            self.assertIn(text, r["error"])

    def test_preview_warns_when_the_quote_fails(self):
        r = self.state.split_preview(**{**self.BODY, "code": "NOPE"})
        self.assertIn("현재가를 확인하지 못했다", r["text"])

    def test_preview_warns_when_budget_exceeds_buying_power(self):
        self.state.buying_power("us")
        self.assertTrue(wait_for(
            lambda: self.state.buying_power("us")["data"] is not None))
        r = self.state.split_preview(**{**self.BODY, "budget": "5000"})
        self.assertIn("매수 가능 금액: $1,234.56", r["text"])
        self.assertIn("매수 가능 금액보다 많다", r["text"])

    def test_sell_ladder_preview(self):
        r = self.state.split_preview(**{**self.BODY, "mode": "ladder",
                                        "sell_start": "105", "stop_pct": "3"})
        self.assertIn("105.00, 106.05", r["text"])
        self.assertIn("손절 -3%", r["text"])


class StartTest(SplitBase):
    def test_start_registers_a_job_and_shows_it_in_status(self):
        self.assertEqual(self.state.split_start(**self.BODY), {"ok": True})
        engine = self.state.splitters["us"]
        self.assertEqual(engine.codes(), {"SOXL"})
        job = self.state.status()["split"]["jobs"][0]
        self.assertEqual((job["code"], job["status"], job["mode"]),
                         ("SOXL", "진행", "each"))
        self.assertEqual([lv["buy"] for lv in job["levels"]],
                         [100, 99, 98, 97, 96])
        self.assertTrue(Path(self.tmp, "autotrader_state_split_us.json").exists())

    def test_duplicate_code_is_refused(self):
        self.state.split_start(**self.BODY)
        self.assertIn("이미 분할 매매 중",
                      self.state.split_start(**self.BODY)["error"])

    def test_actions_are_validated(self):
        self.state.split_start(**self.BODY)
        self.assertIn("알 수 없는",
                      self.state.split_action("us", "SOXL", "explode")["error"])
        self.assertIn("없다",
                      self.state.split_action("us", "NVDA", "stop")["error"])
        self.assertEqual(self.state.split_action("us", "soxl", "pause"),
                         {"ok": True})

    def test_buy_pause_reaches_the_split_engine(self):
        self.state.pause_buys(True)
        self.state.split_start(**self.BODY)
        self.assertTrue(self.state.splitters["us"].buy_paused)
        self.state.pause_buys(False)
        self.assertFalse(self.state.splitters["us"].buy_paused)

    def test_ai_trader_keeps_away_from_split_codes(self):
        self.state.split_start(**self.BODY)
        self.state.start(market="us", target_pct="2", max_positions="2",
                         cash="1000")
        trader = self.state.trader
        self.assertEqual(trader.blocked_codes(), {"SOXL"})
        self.assertFalse(trader._buy("SOXL"))
        self.assertEqual(self.state.params["market"], "us")

    def test_holdings_buttons_refuse_split_codes(self):
        self.state.split_start(**self.BODY)
        self.state.start(market="us", target_pct="2", max_positions="2",
                         cash="1000")
        self.assertIn("분할 매매 중",
                      self.state.adopt("SOXL", "sell")["error"])

    def test_split_refuses_a_code_the_ai_trader_holds(self):
        from autotrader.scalper import Position

        self.state.start(market="us", target_pct="2", max_positions="2",
                         cash="1000")
        self.state.trader.stop()
        self.state.thread.join(timeout=5)
        self.state.start(market="us", target_pct="2", max_positions="2",
                         cash="1000")
        self.state.trader.positions["SOXL"] = Position(
            "SOXL", 3, 100.0, self.state.trader.clock(), watch=True)
        r = self.state.split_start(**self.BODY)
        self.assertIn("AI 단타가 보유", r["error"])
        self.assertIn("AI 단타가 보유",
                      self.state.split_preview(**self.BODY)["error"])

    def test_saved_jobs_reappear_paused_after_restart(self):
        self.state.split_start(**self.BODY)
        self._stop()
        again = make_state(self.tmp)
        self.addCleanup(lambda: [e.stop() for e in again.splitters.values()])
        again.load_saved_splits()
        job = again.status()["split"]["jobs"][0]
        self.assertEqual((job["code"], job["status"]), ("SOXL", "멈춤"))


class RealModeTest(SplitBase):
    mode = "real"

    def test_real_mode_needs_yes_to_start_and_to_resume(self):
        r = self.state.split_start(**self.BODY)
        self.assertTrue(r["need_confirm"])
        self.assertEqual(self.state.splitters, {})
        self.assertEqual(self.state.split_start(confirm="YES", **self.BODY),
                         {"ok": True})
        self.state.split_action("us", "SOXL", "pause")
        self.assertTrue(
            self.state.split_action("us", "SOXL", "resume")["need_confirm"])
        self.assertEqual(
            self.state.split_action("us", "SOXL", "resume", confirm="YES"),
            {"ok": True})

    def test_preview_says_real(self):
        self.assertIn("실전투자", self.state.split_preview(**self.BODY)["text"])


class BuyingPowerTest(SplitBase):
    def test_status_reports_buying_power_for_the_chosen_market(self):
        self.assertIsNone(self.state.status()["buying_power"])   # 시장 없이는 안 읽는다
        self.state.status("us")
        self.assertTrue(wait_for(
            lambda: self.state.status("us")["buying_power"]["data"]))
        data = self.state.status("us")["buying_power"]["data"]
        self.assertEqual((data["currency"], data["orderable"]),
                         ("USD", 1234.56))
        self.assertIn("time", data)

    def test_failure_is_shown_not_raised(self):
        def broken(self):
            raise KiwoomRestError("ust21110 실패")
        original = FakeClient.buying_power
        FakeClient.buying_power = broken
        self.addCleanup(setattr, FakeClient, "buying_power", original)
        self.state.status("us")
        self.assertTrue(wait_for(
            lambda: self.state.status("us")["buying_power"]["error"]))
        self.assertIn("ust21110", self.state.status("us")["buying_power"]["error"])

    def test_us_parses_the_dollar_row(self):
        def transport(url, headers, body):
            if url.endswith("/oauth2/token"):
                return {"token": "T", "expires_dt": ""}
            assert headers["api-id"] == "ust21110", headers
            return {"return_code": 0, "krw_entra": "000000000350000",
                    "result_list": [
                        {"crnc_code": "GBP", "fc_ord_alowa": "9.00"},
                        {"crnc_code": "USD", "fc_entra": "1000.00",
                         "fc_ord_alowa": "1234.56"}]}
        client = KiwoomRestClient("A", "B", market="us", transport=transport)
        self.assertEqual(client.buying_power(),
                         {"currency": "USD", "orderable": 1234.56,
                          "deposit": 1000.0, "krw_deposit": 350000.0})

    def test_kr_reads_orderable_cash(self):
        def transport(url, headers, body):
            if url.endswith("/oauth2/token"):
                return {"token": "T", "expires_dt": ""}
            assert (headers["api-id"], body) == ("kt00001", {"qry_tp": "3"})
            return {"return_code": 0, "entr": "000000000017534",
                    "ord_alow_amt": "000000000085341"}
        client = KiwoomRestClient("A", "B", market="kr", transport=transport)
        power = client.buying_power()
        self.assertEqual((power["currency"], power["orderable"],
                          power["deposit"]), ("KRW", 85341.0, 17534.0))

    def test_missing_fields_raise(self):
        def transport(url, headers, body):
            if url.endswith("/oauth2/token"):
                return {"token": "T", "expires_dt": ""}
            return {"return_code": 0, "result_list": []}
        for market in ("us", "kr"):
            with self.assertRaises(KiwoomRestError):
                KiwoomRestClient("A", "B", market=market,
                                 transport=transport).buying_power()


class RouteTest(SplitBase):
    def setUp(self):
        super().setUp()
        Handler.state = self.state
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def post(self, path, body):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_preview_start_action_and_status_over_http(self):
        body = dict(self.BODY, repeat=True, confirm="", sell_start="",
                    hold_qty="", stop_pct="0")
        self.assertIn("반복", self.post("/api/split/preview", body)["text"])
        self.assertEqual(self.post("/api/split/start", body), {"ok": True})
        status = self.get("/api/status?market=us")
        self.assertEqual(status["split"]["jobs"][0]["code"], "SOXL")
        self.assertIn("buying_power", status)
        self.assertEqual(self.post("/api/split/action",
                                   {"market": "us", "code": "SOXL",
                                    "action": "stop"}), {"ok": True})
        self.assertEqual(self.get("/api/status")["buying_power"], None)

    def test_page_has_the_panel_and_the_buying_power_card(self):
        for needle in ('id="spCode"', 'id="spJobs"', 'id="stBP"',
                       "/api/split/preview", "매수 가능 금액",
                       "지정 종목 분할 매매"):
            self.assertIn(needle, PAGE)


if __name__ == "__main__":
    unittest.main()
