"""지정가 체결 확인·추격 매도·계좌 보유 편입·상태 복원 (ONCO 사고 재현)."""

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from autotrader.scalper import ScalpConfig, Scalper

US_OPEN_UTC = datetime(2025, 10, 6, 14, 0, tzinfo=timezone.utc)  # 정규장


class FakeUsAPI:
    """미체결 목록을 흉내 내는 미국주식 가짜 API."""

    def __init__(self, prices, ranking=None, balances=None):
        self.prices = dict(prices)
        self.ranking = ranking or []
        self.balances = balances or {}
        self.orders = []             # (code, side, qty, limit_price)
        self.open = {}               # ord_no -> 미체결 주문
        self.cancelled = []
        self.auto_fill = True        # True면 주문 즉시 체결
        self._n = 0

    def current_price(self, code):
        return self.prices[code]

    def send_market_order(self, account_no, code, side, quantity,
                          kr_exchange="KRX", limit_price=None):
        self._n += 1
        no = f"{self._n:09d}"
        self.orders.append((code, side, quantity, limit_price))
        if not self.auto_fill:
            self.open[no] = {"ord_no": no, "code": code, "remaining": quantity,
                             "filled": 0, "side": side}
        return no

    def us_open_orders(self, code=""):
        return [o for o in self.open.values() if not code or o["code"] == code]

    def cancel_order(self, ord_no, code):
        self.cancelled.append(ord_no)
        self.open.pop(ord_no)

    def fill_all(self):
        self.open.clear()

    def top_stocks(self, criteria="volume", limit=10):
        return self.ranking

    def us_balances(self):
        return self.balances


def make(api, state_path=None, **kw):
    clock = {"now": datetime(2025, 10, 6, 23, 0)}
    cfg = dict(market="us", max_positions=1, total_cash=1500,
               scan_interval=99999.0, min_dollar_volume=0,
               target_pct=1.5, stop_pct=3.0, trail_pct=0, open_delay_min=0,
               risk_pct=0)
    cfg.update(kw)
    trader = Scalper(api=api, config=ScalpConfig(**cfg),
                     selector=lambda rows, n: ([rows[0]["code"]], ""),
                     is_simulation=True, clock=lambda: clock["now"],
                     utc_clock=lambda: US_OPEN_UTC, state_path=state_path)
    trader._maybe_scan = lambda *a, **k: None
    trader._clock = clock
    return trader


def advance(trader, seconds):
    trader._clock["now"] += timedelta(seconds=seconds)


ONCO = [{"code": "ONCO", "price": "7.57", "change_pct": "12.0",
         "volume": "900000"}]


class OncoIncidentTest(unittest.TestCase):
    """익절 매도 지정가가 안 받아졌는데 체결로 가정해 장부에서 지운 사고."""

    def _entered(self):
        api = FakeUsAPI({"ONCO": 7.57}, ranking=ONCO)
        trader = make(api)
        trader._scan_worker(1)
        trader.step()                       # 매수 주문 → 즉시 체결
        trader.step()                       # 체결 확인
        self.assertFalse(trader.positions["ONCO"].entry_pending)
        return trader, api

    def test_unfilled_sell_keeps_position_under_watch(self):
        trader, api = self._entered()
        api.auto_fill = False               # 저유동성: 매도가 안 받아짐
        api.prices["ONCO"] = 7.82           # +3.3% → 익절 주문
        trader.step()
        pos = trader.positions.get("ONCO")
        self.assertIsNotNone(pos)           # 유령 보유가 아니라 계속 감시 중
        self.assertTrue(pos.exit_pending)
        self.assertEqual(trader.trades, [])  # 체결 전에는 손익 기록 안 함

    def test_unfilled_sell_is_chased_until_filled(self):
        trader, api = self._entered()
        api.auto_fill = False
        api.prices["ONCO"] = 7.82
        trader.step()                       # 1차 매도 @7.82 (미체결)
        api.prices["ONCO"] = 7.30           # 가격이 빠짐
        advance(trader, 50)                 # 체결 대기 시간 초과
        trader.step()                       # 취소 → 2차 추격 매도
        self.assertEqual(len(api.cancelled), 1)
        code, side, qty, limit = api.orders[-1]
        held = trader.positions["ONCO"].quantity
        self.assertEqual((code, side, qty), ("ONCO", "sell", held))
        self.assertLess(limit, 7.30)        # 현재가보다 낮춰 체결 우선
        self.assertEqual(trader.positions["ONCO"].exit_attempts, 2)
        api.fill_all()                      # 이번엔 체결
        trader.step()
        self.assertNotIn("ONCO", trader.positions)
        self.assertEqual(len(trader.trades), 1)
        self.assertAlmostEqual(trader.trades[0].exit_price, limit)

    def test_partial_fill_then_chase_remainder(self):
        trader, api = self._entered()
        api.auto_fill = False
        api.prices["ONCO"] = 7.82
        trader.step()
        total = trader.positions["ONCO"].quantity
        order = next(iter(api.open.values()))
        order["remaining"], order["filled"] = total - 100, 100   # 100주만 체결
        advance(trader, 50)
        trader.step()
        self.assertEqual(api.orders[-1][2], total - 100)  # 잔량만 재주문
        self.assertEqual(trader.positions["ONCO"].quantity, total - 100)
        api.fill_all()
        trader.step()
        trade = trader.trades[-1]
        self.assertEqual(trade.quantity, total)           # 합산 기록

    def test_unfilled_buy_is_cancelled_and_refunded(self):
        api = FakeUsAPI({"ONCO": 7.57}, ranking=ONCO)
        api.auto_fill = False
        trader = make(api)
        trader._scan_worker(1)
        trader.step()
        self.assertTrue(trader.positions["ONCO"].entry_pending)
        advance(trader, 50)
        trader.step()
        self.assertNotIn("ONCO", trader.positions)
        self.assertAlmostEqual(trader.cash, 1500)


class AdoptHoldingTest(unittest.TestCase):
    def test_adopt_losing_holding_triggers_stop(self):
        api = FakeUsAPI({"ONCO": 7.02}, balances={
            "ONCO": {"qty": 193, "sellable": 193, "avg_price": 7.57,
                     "now_price": 7.02, "pl_rt": -7.23, "name": "ONCO"}})
        trader = make(api)
        cash_before = trader.cash
        trader.request("ONCO", "manage")
        trader.step()                       # 편입 → 같은 주기에 -7.3% 손절 주문
        sells = [o for o in api.orders if o[1] == "sell"]
        self.assertEqual(sells[-1][:3], ("ONCO", "sell", 193))
        api.balances = {"ONCO": {"qty": 0, "sellable": 0}}   # 매도 체결 반영
        trader.step()                       # 체결 확인
        self.assertEqual(trader.trades[-1].reason, "손절")
        self.assertLess(trader.trades[-1].pnl, 0)
        self.assertAlmostEqual(trader.cash, cash_before)   # 편입분은 현금 무관

    def test_manual_sell_request(self):
        api = FakeUsAPI({"AAPL": 200.0}, balances={
            "AAPL": {"qty": 3, "sellable": 3, "avg_price": 190.0,
                     "now_price": 200.0}})
        trader = make(api)
        trader.request("AAPL", "sell")
        trader.step()
        api.balances = {"AAPL": {"qty": 0, "sellable": 0}}
        trader.step()
        self.assertEqual(trader.trades[-1].reason, "수동 청산")

    def test_reconcile_ignores_adopted(self):
        api = FakeUsAPI({"ONCO": 7.6}, balances={
            "ONCO": {"qty": 193, "sellable": 193, "avg_price": 7.57}})
        trader = make(api)
        trader.request("ONCO", "manage")
        trader.step()
        api.balances = {"ONCO": {"qty": 0, "sellable": 0}}
        trader.reconcile()
        self.assertIn("ONCO", trader.positions)


class LiquidityFilterTest(unittest.TestCase):
    def test_low_dollar_volume_excluded_by_default(self):
        rows = [{"code": "THIN", "price": "7.57", "change_pct": "20",
                 "volume": "23134"},                    # 약 $17만 — 제외
                {"code": "SOXL", "price": "48", "change_pct": "5",
                 "volume": "56000000"}]                 # 약 $27억 — 통과
        api = FakeUsAPI({"THIN": 7.57, "SOXL": 48.0}, ranking=rows)
        trader = make(api, min_dollar_volume=None)      # 기본값($3M)
        codes = [r["code"] for r in trader._candidates()]
        self.assertEqual(codes, ["SOXL"])

    def test_unknown_volume_excluded(self):
        rows = [{"code": "X", "price": "10", "change_pct": "3"}]
        api = FakeUsAPI({"X": 10.0}, ranking=rows)
        trader = make(api, min_dollar_volume=None)
        self.assertEqual(trader._candidates(), [])


class MarginShortageTest(unittest.TestCase):
    def test_margin_error_pauses_without_blacklisting(self):
        api = FakeUsAPI({"ONCO": 7.57}, ranking=ONCO)

        def reject(*a, **k):
            raise RuntimeError("[2000](855056:매수증거금이 부족합니다. 매수가능(0)주)")

        api.send_market_order = reject
        trader = make(api)
        trader._scan_worker(1)
        trader.step()
        self.assertNotIn("ONCO", trader._cooldown)      # 종목 탓 아님
        self.assertFalse(trader._entries_allowed())     # 잠시 신규 매수 멈춤
        advance(trader, 200)
        self.assertTrue(trader._entries_allowed())


class StateRestoreTest(unittest.TestCase):
    def test_positions_survive_restart(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "state.json")
        api = FakeUsAPI({"ONCO": 7.57}, ranking=ONCO,
                        balances={"ONCO": {"qty": 193, "sellable": 193}})
        trader = make(api, state_path=path)
        trader._scan_worker(1)
        trader.step()
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["positions"][0]["code"], "ONCO")

        # 재시작: 새 프로세스가 상태 파일과 실계좌를 대조해 복원
        restarted = make(FakeUsAPI({"ONCO": 7.0},
                                   balances={"ONCO": {"qty": 193,
                                                      "sellable": 193}}),
                         state_path=path)
        restarted.restore_state()
        self.assertEqual(restarted.positions["ONCO"].quantity, 193)
        self.assertAlmostEqual(restarted.cash, 1500 - 193 * 7.57)

    def test_restore_caps_to_actual_and_skips_sold(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "state.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"market": "us", "positions": [
                {"code": "ONCO", "quantity": 193, "entry_price": 7.57,
                 "entry_time": "2025-10-06T23:00:00", "adopted": False},
                {"code": "GONE", "quantity": 5, "entry_price": 10.0,
                 "entry_time": "2025-10-06T23:00:00", "adopted": False}]}, f)
        api = FakeUsAPI({}, balances={"ONCO": {"qty": 100, "sellable": 100}})
        trader = make(api, state_path=path)
        trader.restore_state()
        self.assertEqual(trader.positions["ONCO"].quantity, 100)
        self.assertNotIn("GONE", trader.positions)


if __name__ == "__main__":
    unittest.main()


class ExpiredOrderTest(unittest.TestCase):
    """매도 주문이 체결 없이 소멸(장 마감 등)해도 유령 보유가 생기지 않는다."""

    def test_vanished_unfilled_sell_is_resent(self):
        api = FakeUsAPI({"ONCO": 7.57}, ranking=ONCO,
                        balances={"ONCO": {"qty": 0, "sellable": 0}})
        trader = make(api)
        trader._scan_worker(1)
        trader.step()
        held = trader.positions["ONCO"].quantity
        api.balances = {"ONCO": {"qty": held + 61, "sellable": held + 61}}
        trader.step()                       # 체결 확인 (기존 보유 61주 기준선)
        self.assertEqual(trader.positions["ONCO"].baseline_qty, 61)
        api.auto_fill = False
        api.prices["ONCO"] = 7.82
        trader.step()                       # 익절 매도 주문
        api.open.clear()                    # 체결 없이 소멸 — 잔고 그대로
        trader.step()                       # 1차 확인: 아직 판단 보류
        self.assertIn("ONCO", trader.positions)
        trader.step()                       # 2차 확인: 미매도 → 재주문
        self.assertEqual(api.orders[-1][:3], ("ONCO", "sell", held))  # 61주는 안 건드림
        self.assertEqual(trader.trades, [])

    def test_vanished_filled_sell_is_finalized(self):
        api = FakeUsAPI({"ONCO": 7.57}, ranking=ONCO,
                        balances={"ONCO": {"qty": 0, "sellable": 0}})
        trader = make(api)
        trader._scan_worker(1)
        trader.step()
        held = trader.positions["ONCO"].quantity
        api.balances = {"ONCO": {"qty": held, "sellable": held}}
        trader.step()
        api.auto_fill = False
        api.prices["ONCO"] = 7.82
        trader.step()
        api.open.clear()
        api.balances = {"ONCO": {"qty": 0, "sellable": 0}}   # 실제로 팔림
        trader.step()
        self.assertNotIn("ONCO", trader.positions)
        self.assertEqual(trader.trades[-1].reason, "익절")
