"""본전 감시: 손실 보유 종목을 손절 없이 지켜보다 손익분기점에서 매도."""

import os
import tempfile
import unittest
from datetime import timedelta

try:                                   # unittest discover -s tests
    from test_fill_verification import FakeUsAPI, advance, make
except ImportError:                    # python -m unittest tests.test_watch_mode
    from tests.test_fill_verification import FakeUsAPI, advance, make


def holding(qty, avg, now, pl_rt=None):
    if pl_rt is None:
        pl_rt = (now / avg - 1) * 100
    return {"qty": qty, "sellable": qty, "avg_price": avg, "now_price": now,
            "pl_rt": pl_rt, "name": ""}


SOLD = {"ONCO": {"qty": 0, "sellable": 0}}


class WatchAdoptTest(unittest.TestCase):
    def setUp(self):
        # 매입단가 10.00, 현재 9.20(-8%) — 손절선(-2%)을 한참 지난 손실 종목
        self.api = FakeUsAPI({"ONCO": 9.20},
                             balances={"ONCO": holding(100, 10.0, 9.20)})
        self.trader = make(self.api, stop_pct=2.0)

    def watch(self, code="ONCO"):
        self.trader.request(code, "watch")
        self.trader.step()
        return self.trader.positions.get(code)

    def test_losing_holding_is_not_stopped_out(self):
        pos = self.watch()
        self.assertTrue(pos.watch)
        self.assertEqual(pos.entry_price, 10.0)
        self.assertEqual(pos.state(), "본전 감시 중")
        for price in (9.0, 8.0, 7.0):          # 더 떨어져도 팔지 않는다
            self.api.prices["ONCO"] = price
            self.trader.step()
        self.assertEqual(self.api.orders, [])
        self.assertIn("ONCO", self.trader.positions)

    def test_no_timeout_or_close_flatten(self):
        self.watch()
        advance(self.trader, 3 * 3600)         # 시간초과(60분)를 넘겨도 유지
        self.trader.step()
        self.trader._flatten_for_close(10)     # 장 마감 정리 대상도 아니다
        self.assertEqual(self.api.orders, [])
        self.assertIn("ONCO", self.trader.positions)

    def test_sells_at_breakeven_with_buffer(self):
        self.watch()
        self.api.prices["ONCO"] = 10.02        # 매입단가는 넘었지만 여유(0.3%) 미만
        self.trader.step()
        self.assertEqual(self.api.orders, [])
        self.api.prices["ONCO"] = 10.05
        self.trader.step()
        self.assertEqual(self.api.orders, [("ONCO", "sell", 100, 10.05)])
        self.api.balances = SOLD               # 매도 체결이 잔고에 반영됐다
        self.trader.step()                     # 체결 확인
        self.assertNotIn("ONCO", self.trader.positions)
        trade = self.trader.trades[-1]
        self.assertEqual(trade.reason, "본전 매도")
        self.assertGreater(trade.pnl, 0)

    def test_unfilled_breakeven_sell_returns_to_watch(self):
        self.watch()
        self.api.auto_fill = False
        self.api.prices["ONCO"] = 10.05
        self.trader.step()
        self.assertTrue(self.trader.positions["ONCO"].exit_pending)
        self.api.prices["ONCO"] = 9.50         # 주문이 안 받아진 채 다시 하락
        advance(self.trader, 60)
        self.trader.step()
        pos = self.trader.positions["ONCO"]
        self.assertFalse(pos.exit_pending)
        self.assertTrue(pos.watch)
        self.assertEqual(len(self.api.cancelled), 1)
        # 가격을 낮춰 다시 내지 않는다 (본전 아래로는 팔지 않는다)
        self.assertEqual(len(self.api.orders), 1)
        self.api.auto_fill = True
        self.api.prices["ONCO"] = 10.10        # 다시 본전에 닿으면 재주문
        self.trader.step()
        self.assertEqual(self.api.orders[-1], ("ONCO", "sell", 100, 10.10))

    def test_vanished_order_without_fill_returns_to_watch(self):
        # 주문이 미체결 목록에서 사라졌는데 잔고는 그대로 — 팔린 것이 아니다
        self.watch()
        self.api.prices["ONCO"] = 10.05
        self.trader.step()
        self.api.prices["ONCO"] = 9.80
        self.trader.step()
        self.trader.step()                     # 두 번 확인한 뒤 감시로 복귀
        pos = self.trader.positions["ONCO"]
        self.assertFalse(pos.exit_pending)
        self.assertEqual(pos.quantity, 100)
        self.assertEqual(len(self.api.orders), 1)
        self.assertEqual(self.trader.trades, [])

    def test_never_orders_below_breakeven(self):
        # 매수호가가 손익분기점 아래면 손익분기점에 걸어 둔다
        self.api.best_bid_ask = lambda code: (9.98, 10.06)
        self.watch()
        self.api.prices["ONCO"] = 10.05
        self.trader.step()
        self.assertAlmostEqual(self.api.orders[-1][3], 10.03, places=4)

    def test_sells_into_bid_above_breakeven(self):
        self.api.best_bid_ask = lambda code: (10.20, 10.22)
        self.watch()
        self.api.prices["ONCO"] = 10.21
        self.trader.step()
        self.assertEqual(self.api.orders[-1], ("ONCO", "sell", 100, 10.20))

    def test_quote_outage_does_not_dump(self):
        self.watch()

        def broken(code):
            raise RuntimeError("1632")
        self.api.current_price = broken
        advance(self.trader, 20 * 60)          # 시세공백 보호청산(5분)을 넘김
        self.trader.step()
        self.assertEqual(self.api.orders, [])
        self.assertIn("ONCO", self.trader.positions)

    def test_requires_average_price(self):
        self.api.balances["XYZ"] = holding(10, 0, 5.0, pl_rt=-1.0)
        self.api.prices["XYZ"] = 5.0
        self.assertIsNone(self.watch("XYZ"))

    def test_profitable_holding_is_refused(self):
        # 수익 중인 종목을 넣으면 그 자리에서 팔려 버리므로 받지 않는다
        self.api.balances["NVDA"] = holding(3, 100.0, 104.0)
        self.api.prices["NVDA"] = 104.0
        self.assertIsNone(self.watch("NVDA"))
        self.assertEqual(self.api.orders, [])

    def test_watch_does_not_use_a_trading_slot(self):
        self.watch()
        self.assertEqual(self.trader.config.max_positions, 1)
        self.assertEqual(self.trader._open_slots(), 1)
        # 감시 종목은 프로그램 자금으로 산 것이 아니다
        self.assertEqual(self.trader.equity(), self.trader.config.total_cash)

    def test_unwatch_leaves_the_holding_alone(self):
        self.watch()
        self.trader.request("ONCO", "unwatch")
        self.trader.step()
        self.trader.step()
        self.assertNotIn("ONCO", self.trader.positions)
        self.assertEqual(self.api.orders, [])      # 해제는 매도가 아니다
        self.assertEqual(self.trader.trades, [])

    def test_unwatch_never_adopts(self):
        self.trader.request("ONCO", "unwatch")     # 감시 중이 아닌 종목
        self.trader.step()
        self.assertEqual(self.trader.positions, {})

    def test_manage_reapplies_stop(self):
        self.watch()
        self.trader.request("ONCO", "manage")  # 관리 편입 → 손절 규칙 적용
        self.trader.step()
        self.assertEqual(self.api.orders[-1][:3], ("ONCO", "sell", 100))
        self.api.balances = SOLD
        self.trader.step()
        self.assertEqual(self.trader.trades[-1].reason, "손절")

    def test_manual_sell_while_watching(self):
        self.watch()
        self.trader.request("ONCO", "sell")
        self.trader.step()
        self.api.balances = SOLD
        self.trader.step()
        self.assertNotIn("ONCO", self.trader.positions)
        self.assertEqual(self.trader.trades[-1].reason, "수동 청산")


class WatchConvertTest(unittest.TestCase):
    def test_program_position_can_be_switched_to_watch(self):
        api = FakeUsAPI({"NVDA": 100.0})
        trader = make(api, stop_pct=2.0)
        trader._buy("NVDA")
        trader.step()                          # 매수 체결 확인
        trader.request("NVDA", "watch")
        api.prices["NVDA"] = 95.0              # -5%: 손절선을 넘었지만 감시 중
        trader.step()
        self.assertTrue(trader.positions["NVDA"].watch)
        self.assertEqual([o[1] for o in api.orders], ["buy"])
        self.assertEqual(trader._open_slots(), 1)
        trader.request("NVDA", "unwatch")      # 프로그램이 산 종목은 규칙 복귀
        trader.step()
        self.assertEqual(api.orders[-1][:2], ("NVDA", "sell"))
        self.assertEqual(trader.positions["NVDA"].exit_reason, "손절")


class WatchReconcileTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeUsAPI({"ONCO": 9.20},
                             balances={"ONCO": holding(100, 10.0, 9.20)})
        self.trader = make(self.api, stop_pct=2.0)
        self.trader.request("ONCO", "watch")
        self.trader.step()

    def test_ends_after_two_missing_confirmations(self):
        self.api.balances = {}                 # 사용자가 HTS에서 직접 팔았다
        self.trader.reconcile()
        self.assertIn("ONCO", self.trader.positions)   # 한 번은 지연일 수 있다
        self.trader.reconcile()
        self.assertNotIn("ONCO", self.trader.positions)
        self.assertEqual(self.trader.cash, self.trader.config.total_cash)

    def test_quantity_follows_account(self):
        self.api.balances = {"ONCO": holding(40, 10.0, 9.20)}
        self.trader.reconcile()
        self.trader.reconcile()
        self.assertEqual(self.trader.positions["ONCO"].quantity, 40)

    def test_blip_is_ignored(self):
        saved = self.api.balances
        self.api.balances = {}
        self.trader.reconcile()
        self.api.balances = saved
        self.trader.reconcile()
        self.api.balances = {}
        self.trader.reconcile()
        self.assertIn("ONCO", self.trader.positions)


class WatchPersistenceTest(unittest.TestCase):
    def test_watch_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            balances = {"ONCO": holding(100, 10.0, 9.20)}
            api = FakeUsAPI({"ONCO": 9.20}, balances=balances)
            first = make(api, state_path=path, stop_pct=2.0)
            first.request("ONCO", "watch")
            first.step()

            api2 = FakeUsAPI({"ONCO": 9.00}, balances=balances)
            second = make(api2, state_path=path, stop_pct=2.0)
            second.restore_state()
            pos = second.positions["ONCO"]
            self.assertTrue(pos.watch and pos.adopted)
            second.step()                      # -10%지만 손절하지 않는다
            self.assertEqual(api2.orders, [])
            self.assertEqual(second.cash, second.config.total_cash)


    def _saved_program_position(self, path):
        api = FakeUsAPI({"ONCO": 10.0})
        first = make(api, state_path=path, stop_pct=2.0)
        first._buy("ONCO")
        first.step()                           # 매수 체결 확인 → 상태 저장
        return first.positions["ONCO"].quantity

    def test_underwater_position_is_not_dumped_on_restart(self):
        # 꺼져 있는 동안 -8%가 된 종목: 재시작하자마자 손절하지 않는다
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            qty = self._saved_program_position(path)
            api = FakeUsAPI({"ONCO": 9.20},
                            balances={"ONCO": holding(qty, 10.0, 9.20)})
            second = make(api, state_path=path, stop_pct=2.0)
            second.restore_state()
            self.assertTrue(second.positions["ONCO"].watch)
            second.step()
            self.assertEqual(api.orders, [])

    def test_position_within_stop_keeps_normal_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            qty = self._saved_program_position(path)
            api = FakeUsAPI({"ONCO": 9.90},
                            balances={"ONCO": holding(qty, 10.0, 9.90)})
            second = make(api, state_path=path, stop_pct=2.0)
            second.restore_state()
            self.assertFalse(second.positions["ONCO"].watch)
            api.prices["ONCO"] = 9.70          # 실행 중에 손절선을 넘으면 손절
            second.step()
            self.assertEqual(api.orders[-1][:2], ("ONCO", "sell"))
            self.assertEqual(second.positions["ONCO"].exit_reason, "손절")


class WatchLosersEndpointTest(unittest.TestCase):
    def test_queues_only_losing_holdings(self):
        try:
            from test_webui import make_state
        except ImportError:
            from tests.test_webui import make_state

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = make_state(tmp.name)
        state.start(market="us", target_pct="2", max_positions="2", cash="1000")
        self.addCleanup(state.stop)
        self.assertEqual(state.trader.config.stop_pct, 2.0)
        self.assertIn("error", state.adopt("*LOSERS", "watch"))   # 보유 없음
        state.trader.holdings = {
            "ONCO": holding(100, 10.0, 9.2), "SOXL": holding(5, 30.0, 27.0),
            "NVDA": holding(3, 100.0, 104.0)}
        r = state.adopt("*LOSERS", "watch")
        self.assertEqual(sorted(r["codes"]), ["ONCO", "SOXL"])
        self.assertIn("error", state.adopt("*LOSERS", "sell"))
        self.assertEqual(state.adopt("onco", "watch"), {"ok": True})
        self.assertEqual(state.adopt("onco", "unwatch"), {"ok": True})


if __name__ == "__main__":
    unittest.main()
