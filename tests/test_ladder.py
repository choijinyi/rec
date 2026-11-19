"""계단식 3분할 매매: 나눠 사고(손절 폭의 1/3·2/3 자리), 나눠 판다(목표의
절반 지점·목표가·고점 추적). 전부 사고 손절돼도 1회 손실 한도 안이어야 한다."""

import os
import tempfile
import unittest

try:
    from test_timing import FakeAPI, NVDA, advance, make, plan, scan
    from test_fill_verification import FakeUsAPI
    from test_fill_verification import advance as advance_us
    from test_fill_verification import make as make_us
except ImportError:
    from tests.test_timing import FakeAPI, NVDA, advance, make, plan, scan
    from tests.test_fill_verification import FakeUsAPI
    from tests.test_fill_verification import advance as advance_us
    from tests.test_fill_verification import make as make_us

from autotrader.analysis import _entry_prompt
from autotrader.scalper import ScalpConfig


def ladder(risk_pct=0.09, **kw):
    """NVDA 200 진입, 손절 198, 목표 203. 총 10,000 · 2종목 → 슬롯 5,000."""
    api = FakeAPI({"NVDA": 200.0}, [NVDA])
    trader = make(api, plans=[plan()], ladder_steps=3, risk_pct=risk_pct, **kw)
    scan(trader)
    return trader, api


def move(trader, api, price, seconds=3):
    api.prices["NVDA"] = price
    advance(trader, seconds)
    trader.step()


class LadderPlanTest(unittest.TestCase):
    def test_default_is_three_steps(self):
        self.assertEqual(ScalpConfig().ladder_steps, 3)

    def test_first_buy_is_one_third_and_rest_wait_below(self):
        trader, api = ladder()
        pos = trader.positions["NVDA"]
        # 1회 손실 한도 9: 한 번에 사면 4주(9÷2)지만 나눠 사면 6주까지 된다
        self.assertEqual(api.orders, [("NVDA", "buy", 2)])
        self.assertEqual(pos.quantity, 2)
        self.assertEqual([q for _p, q in pos.adds], [2, 2])
        self.assertAlmostEqual(pos.adds[0][0], 200 - 2 / 3, places=3)
        self.assertAlmostEqual(pos.adds[1][0], 200 - 4 / 3, places=3)
        self.assertEqual(pos.state(), "보유 (분할 매수 1/3)")
        self.assertAlmostEqual(trader.cash, 10_000 - 400)

    def test_full_ladder_then_stop_stays_within_risk_limit(self):
        trader, api = ladder()
        move(trader, api, 199.3)
        pos = trader.positions["NVDA"]
        self.assertEqual(pos.quantity, 4)
        self.assertAlmostEqual(pos.entry_price, 199.65)
        self.assertAlmostEqual(pos.stop_price, 198.0)       # 손절가는 그대로
        move(trader, api, 198.6)
        self.assertEqual(pos.quantity, 6)
        self.assertEqual(pos.adds, [])
        move(trader, api, 197.99)
        self.assertEqual(api.orders[-1], ("NVDA", "sell", 6))
        trade = trader.trades[-1]
        self.assertEqual((trade.reason, trade.quantity), ("손절", 6))
        self.assertLessEqual(-trade.pnl, 9.0 + 0.1)          # 한도 9 안팎
        self.assertAlmostEqual(trader.cash, 10_000 + trade.pnl)

    def test_remainder_goes_to_the_first_step(self):
        trader, api = ladder(risk_pct=0)                     # 예산만으로: 25주
        pos = trader.positions["NVDA"]
        self.assertEqual(pos.quantity, 9)
        self.assertEqual([q for _p, q in pos.adds], [8, 8])

    def test_gap_through_two_steps_buys_both_at_once(self):
        trader, api = ladder()
        move(trader, api, 198.5)
        self.assertEqual(api.orders[-1], ("NVDA", "buy", 4))
        pos = trader.positions["NVDA"]
        self.assertEqual((pos.quantity, pos.adds), (6, []))
        self.assertAlmostEqual(pos.entry_price, (400 + 4 * 198.5) / 6)

    def test_one_share_is_not_split(self):
        trader, api = ladder(risk_pct=0.02)                  # 한도 2 → 1주
        pos = trader.positions["NVDA"]
        self.assertEqual((pos.quantity, pos.adds, pos.ladder_n), (1, [], 1))
        self.assertEqual(pos.state(), "보유")

    def test_two_shares_split_in_two(self):
        trader, api = ladder(risk_pct=0.031)                 # 한도 3.1 → 2주
        pos = trader.positions["NVDA"]
        self.assertEqual(pos.quantity, 1)
        self.assertEqual(pos.adds, [[199.0, 1]])             # 손절 폭의 절반 자리

    def test_single_step_setting_buys_everything_at_once(self):
        api = FakeAPI({"NVDA": 200.0}, [NVDA])
        trader = make(api, plans=[plan()], ladder_steps=1, risk_pct=0.09)
        scan(trader)
        self.assertEqual(api.orders, [("NVDA", "buy", 4)])
        self.assertEqual(trader.positions["NVDA"].adds, [])


class LadderPromptTest(unittest.TestCase):
    ROWS = [{"code": "NVDA", "name": "엔비디아", "price": "200", "change_pct": "1"}]

    def test_ai_is_told_how_orders_are_split(self):
        text = _entry_prompt("us", 1, self.ROWS, {"ladder": True})
        self.assertIn("1/3씩 더 산다", text)
        self.assertNotIn("목표가에서 절반을 팔고", text)

    def test_single_shot_prompt_is_unchanged(self):
        text = _entry_prompt("us", 1, self.ROWS, {})
        self.assertNotIn("1/3씩 더 산다", text)
        self.assertIn("목표가에서 절반을 팔고", text)


class LadderBuyGuardTest(unittest.TestCase):
    def test_user_pause_blocks_add_on_buys(self):
        trader, api = ladder()
        trader.pause_entries(True)
        move(trader, api, 199.3)
        self.assertEqual(len(api.orders), 1)
        trader.pause_entries(False)
        move(trader, api, 199.3)
        self.assertEqual(api.orders[-1], ("NVDA", "buy", 2))

    def test_no_add_after_daily_loss_limit(self):
        trader, api = ladder(daily_loss_pct=0.05)            # 한도 -5
        from autotrader.scalper import Trade
        trader.trades.append(Trade("SPY", 1, 600, 594, -6.0, -1.0, "손절",
                                   trader.clock(), day=trader._day_key()))
        move(trader, api, 199.3)
        self.assertEqual(len(api.orders), 1)

    def test_no_add_once_profit_taking_started(self):
        trader, api = ladder(risk_pct=0)
        move(trader, api, 201.5)                             # 1차 익절
        pos = trader.positions["NVDA"]
        self.assertEqual(pos.adds, [])
        move(trader, api, 199.3)
        self.assertEqual([o[1] for o in api.orders], ["buy", "sell"])

    def test_watch_mode_drops_the_plan(self):
        trader, api = ladder()
        move(trader, api, 199.9)
        trader.request("NVDA", "watch")
        move(trader, api, 199.3)
        pos = trader.positions["NVDA"]
        self.assertTrue(pos.watch)
        self.assertEqual((pos.adds, pos.quantity), ([], 2))

    def test_stop_wins_over_add(self):
        trader, api = ladder()
        move(trader, api, 197.5)                             # 한 번에 손절선 아래
        self.assertEqual(api.orders[-1], ("NVDA", "sell", 2))
        self.assertNotIn("NVDA", trader.positions)


class LadderSellTest(unittest.TestCase):
    def test_third_at_halfway_third_at_target_rest_trails(self):
        trader, api = ladder(risk_pct=0)                     # 9주 보유
        move(trader, api, 201.4)
        self.assertEqual(len(api.orders), 1)                 # 아직 절반 지점 전
        move(trader, api, 201.5)
        pos = trader.positions["NVDA"]
        self.assertEqual(api.orders[-1], ("NVDA", "sell", 3))
        self.assertEqual((pos.quantity, pos.tp_stage), (6, 1))
        self.assertEqual(pos.state(), "1차 익절 후 보유")
        self.assertFalse(pos.trailing)
        move(trader, api, 203.0)
        self.assertEqual(api.orders[-1], ("NVDA", "sell", 3))
        self.assertEqual((pos.quantity, pos.tp_stage), (3, 2))
        self.assertTrue(pos.trailing and pos.scaled)
        self.assertEqual(pos.state(), "분할 익절 후 추적 중")
        move(trader, api, 204.0)
        self.assertEqual(len(api.orders), 3)                 # 오르는 동안은 보유
        move(trader, api, 204.0 * (1 - pos.trail_pct / 100) - 0.01)
        self.assertEqual(api.orders[-1], ("NVDA", "sell", 3))
        trade = trader.trades[-1]
        self.assertEqual((trade.reason, trade.quantity), ("익절", 9))
        expected = 3 * 1.5 + 3 * 3.0 + 3 * (trade_price(api) - 200)
        self.assertAlmostEqual(trade.pnl, expected, places=2)
        self.assertAlmostEqual(trader.cash, 10_000 + trade.pnl, places=2)

    def test_jump_straight_to_target_sells_two_thirds(self):
        trader, api = ladder(risk_pct=0)
        move(trader, api, 203.2)
        pos = trader.positions["NVDA"]
        self.assertEqual(api.orders[-1], ("NVDA", "sell", 6))
        self.assertEqual((pos.quantity, pos.tp_stage), (3, 2))
        self.assertTrue(pos.trailing)

    def test_halfway_uses_the_averaged_entry(self):
        trader, api = ladder(risk_pct=0)                     # 9 + 8 + 8
        move(trader, api, 199.3)
        pos = trader.positions["NVDA"]
        self.assertEqual(pos.quantity, 17)
        halfway = (pos.entry_price + 203.0) / 2
        self.assertLess(halfway, 201.5)
        move(trader, api, round(halfway + 0.01, 2))
        self.assertEqual(api.orders[-1], ("NVDA", "sell", 5))   # 17 // 3
        self.assertEqual(pos.adds, [])

    def test_two_shares_sell_one_then_trail_one(self):
        trader, api = ladder()                               # 첫 몫 2주
        move(trader, api, 201.5)
        pos = trader.positions["NVDA"]
        self.assertEqual(api.orders[-1], ("NVDA", "sell", 1))
        move(trader, api, 203.0)
        self.assertEqual(len(api.orders), 2)                 # 남은 1주는 추적
        self.assertTrue(pos.trailing)
        self.assertEqual(pos.quantity, 1)

    def test_stop_after_first_take_profit_books_blended_result(self):
        trader, api = ladder(risk_pct=0)
        move(trader, api, 201.5)                             # 3주 +1.5
        move(trader, api, 197.9)                             # 6주 손절
        trade = trader.trades[-1]
        self.assertEqual(trade.quantity, 9)
        self.assertAlmostEqual(trade.pnl, 3 * 1.5 + 6 * (197.9 - 200), places=2)


def trade_price(api):
    return api.prices["NVDA"]


AAA = [{"code": "AAA", "price": "100", "change_pct": "2.0",
        "volume": "900000"}]


def entered_us(state_path=None):
    """체결 확인 경로(미국 지정가): 100에 5주, 99·98에 5주씩 대기."""
    api = FakeUsAPI({"AAA": 100.0}, ranking=AAA)
    trader = make_us(api, state_path=state_path, ladder_steps=3)
    trader._scan_worker(1)
    trader.step()
    api.balances = {"AAA": {"qty": 5}}
    trader.step()
    pos = trader.positions["AAA"]
    assert not pos.entry_pending and pos.quantity == 5
    return trader, api, pos


class LadderVerifiedTest(unittest.TestCase):
    def test_add_is_booked_only_after_fill_is_confirmed(self):
        trader, api, pos = entered_us()
        self.assertEqual(pos.adds, [[99.0, 5], [98.0, 5]])
        api.auto_fill = False
        api.prices["AAA"] = 99.0
        trader.step()
        self.assertTrue(pos.add_pending)
        self.assertEqual(pos.quantity, 5)                    # 아직 장부에 없다
        self.assertEqual(pos.state(), "추가 매수 체결 확인 중")
        api.fill_all()
        api.balances = {"AAA": {"qty": 10}}
        trader.step()
        self.assertFalse(pos.add_pending)
        self.assertEqual(pos.quantity, 10)
        self.assertAlmostEqual(pos.entry_price, 99.5)
        self.assertAlmostEqual(trader.cash, 1500 - 500 - 495)

    def test_unfilled_add_is_cancelled_and_retried_later(self):
        trader, api, pos = entered_us()
        api.auto_fill = False
        api.prices["AAA"] = 99.0
        trader.step()
        advance_us(trader, 16)
        trader.step()
        self.assertEqual(len(api.cancelled), 1)
        self.assertFalse(pos.add_pending)
        self.assertEqual((pos.quantity, len(pos.adds)), (5, 2))
        self.assertAlmostEqual(trader.cash, 1000)            # 묶었던 현금 복구
        buys = sum(1 for o in api.orders if o[1] == "buy")
        trader.step()
        self.assertEqual(sum(1 for o in api.orders if o[1] == "buy"), buys)
        advance_us(trader, 61)
        trader.step()
        self.assertEqual(sum(1 for o in api.orders if o[1] == "buy"), buys + 1)

    def test_partial_add_fill_keeps_what_was_bought(self):
        trader, api, pos = entered_us()
        api.auto_fill = False
        api.prices["AAA"] = 99.0
        trader.step()
        order = next(iter(api.open.values()))
        order["remaining"], order["filled"] = 3, 2
        advance_us(trader, 16)
        trader.step()
        self.assertEqual(pos.quantity, 7)
        self.assertEqual(pos.adds, [[98.0, 5]])
        self.assertAlmostEqual(trader.cash, 1500 - 500 - 2 * 99)

    def test_late_balance_is_absorbed_not_left_unwatched(self):
        trader, api, pos = entered_us()
        api.prices["AAA"] = 99.0
        trader.step()                       # 주문은 곧바로 미체결 목록에서 사라진다
        trader.step()                       # 잔고에는 아직 5주
        self.assertTrue(pos.add_pending)
        trader.step()
        self.assertFalse(pos.add_pending)
        self.assertEqual((pos.quantity, pos.add_missed), (5, 5))
        api.balances = {"AAA": {"qty": 10}}     # 뒤늦게 반영
        trader.reconcile()
        self.assertEqual((pos.quantity, pos.add_missed), (10, 0))
        self.assertAlmostEqual(pos.entry_price, 99.5)
        self.assertAlmostEqual(trader.cash, 1500 - 500 - 495)

    def test_reconcile_never_absorbs_shares_the_program_did_not_order(self):
        trader, api, pos = entered_us()
        api.balances = {"AAA": {"qty": 30}}     # 사용자가 따로 산 25주
        trader.reconcile()
        self.assertEqual(pos.quantity, 5)

    def test_first_take_profit_waits_for_fill(self):
        trader, api, pos = entered_us()
        api.auto_fill = False
        api.prices["AAA"] = 100.8           # 목표 101.5의 절반 지점(100.75) 통과
        trader.step()
        self.assertEqual(pos.state(), "1차 분할 익절 체결 대기")
        self.assertEqual(api.orders[-1][:3], ("AAA", "sell", 1))    # 5 // 3
        self.assertEqual(pos.quantity, 5)
        api.fill_all()
        api.balances = {"AAA": {"qty": 4}}
        trader.step()
        self.assertEqual((pos.quantity, pos.tp_stage), (4, 1))
        self.assertEqual(pos.adds, [])

    def test_plan_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            trader, api, pos = entered_us(state_path=path)
            api.balances = {"AAA": {"qty": 5, "now_price": 99.6}}
            again = make_us(api, state_path=path, ladder_steps=3)
            again.restore_state()
            restored = again.positions["AAA"]
            self.assertEqual(restored.adds, [[99.0, 5], [98.0, 5]])
            self.assertEqual(restored.ladder_n, 3)
            api.prices["AAA"] = 99.0
            api.balances = {"AAA": {"qty": 10}}
            again.step()
            again.step()
            self.assertEqual(restored.quantity, 10)


if __name__ == "__main__":
    unittest.main()
