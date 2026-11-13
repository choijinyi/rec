"""추세 추종 진입: 장 전체 지표, 같은 지수 종목의 방향, 운용자 관점,
목표가에서 절반 익절 + 나머지 추적."""

import os
import tempfile
import unittest

try:
    from test_timing import FakeAPI, advance, bars, make, plan, scan
    from test_fill_verification import FakeUsAPI
    from test_fill_verification import make as make_us
except ImportError:
    from tests.test_timing import FakeAPI, advance, bars, make, plan, scan
    from tests.test_fill_verification import FakeUsAPI
    from tests.test_fill_verification import make as make_us

from autotrader.analysis import _entry_prompt, parse_entry_plans
from autotrader.indicators import summarize_bars
from autotrader.scalper import ScalpConfig

SOXS = {"code": "SOXS", "name": "SOXS", "price": "49.15", "change_pct": "7.5",
        "volume": "90000000"}
SOXL = {"code": "SOXL", "name": "SOXL", "price": "60.00", "change_pct": "-7.4",
        "volume": "90000000"}


def session_bars(n_before=60, n_now=30, before=46.5, start=47.4, step=0.06):
    """직전 장 n_before개 + 이번 장 n_now개 (봉은 끊기지 않고 이어진다)."""
    out = [{"t": "20261113%02d%02d00" % (8 + i // 60, i % 60), "o": before,
            "h": before + 0.1, "l": before - 0.1, "c": before, "v": 1000}
           for i in range(n_before)]
    for i in range(n_now):
        m = n_before + i
        c = start + i * step
        out.append({"t": "20261113%02d%02d00" % (8 + m // 60, m % 60),
                    "o": c - step, "h": c + 0.05, "l": c - 0.1, "c": c,
                    "v": 90000})
    return out


class SessionContextTest(unittest.TestCase):
    """AI가 보는 고가·저가·VWAP은 이번 장 전체여야 한다 (최근 90분이 아니라)."""

    def test_window_is_reported(self):
        summary = summarize_bars(bars(200.0, n=330))
        self.assertEqual(summary["window_min"], 330)
        self.assertGreaterEqual(len(summary["closes_30m"]), 10)
        self.assertGreater(summary["off_high_pct"], -0.2)   # 고가 바로 아래
        self.assertGreater(summary["from_low_pct"], 0)

    def test_session_minutes_split_continuous_bars(self):
        rows = session_bars()
        whole = summarize_bars(rows)
        self.assertEqual(whole["window_min"], 90)
        now = summarize_bars(rows, session_min=29)
        self.assertEqual(now["window_min"], 30)
        self.assertGreater(now["day_low"], 47.0)          # 직전 장의 46.5는 빠진다
        self.assertEqual(now["prior_close"], 46.5)
        self.assertGreater(now["from_prior_pct"], 1.5)

    def test_engine_asks_for_the_whole_session(self):
        api = FakeAPI({"NVDA": 200.0}, [{"code": "NVDA", "price": "200",
                                         "change_pct": "1", "volume": "40000000"}])
        seen = {}

        def minute_bars(code, scope=1, count=90):
            seen["count"] = count
            return bars(200.0, n=count)
        api.minute_bars = minute_bars
        trader = make(api, plans=[])
        scan(trader)
        self.assertEqual(seen["count"], 400)
        # 정규장이 열린 뒤 90분이 지난 시각이므로 그만큼만 '이번 장'으로 본다
        self.assertEqual(trader._selector.rows[0]["bars"]["window_min"], 91)

    def test_prompt_labels_the_window(self):
        rows = [dict(SOXS, bars=summarize_bars(session_bars(), session_min=29))]
        prompt = _entry_prompt("us", 1, rows, {})
        self.assertIn("이번 장 최근 30분", prompt)
        self.assertIn("직전 장 종가 대비", prompt)
        self.assertNotIn("당일 고가", prompt)


class DirectionContextTest(unittest.TestCase):
    def test_peer_changes_reach_the_ai(self):
        # SOXL은 하락이라 후보에서 빠지지만, SOXS를 볼 때 그 등락률은 알려 준다
        api = FakeAPI({"SOXS": 49.15, "SOXL": 60.0}, [SOXL, SOXS])
        trader = make(api, plans=[])
        scan(trader)
        row = trader._selector.rows[0]
        self.assertEqual(row["code"], "SOXS")
        self.assertEqual(row["group"], "반도체")
        self.assertEqual(row["peers"], "SOXL -7.4%")
        prompt = _entry_prompt("us", 1, [row], {})
        self.assertIn("같은 지수(반도체)를 따르는 종목의 당일 등락률: SOXL -7.4%",
                      prompt)

    def test_operator_view(self):
        prompt = _entry_prompt("us", 1, [SOXS], {"view": "반도체 약세 예상"})
        self.assertIn("운용자의 오늘 관점(참고): 반도체 약세 예상", prompt)
        self.assertIn("분봉이 그 방향을 확인해 줄 때만 진입", prompt)
        self.assertNotIn("운용자의 오늘 관점", _entry_prompt("us", 1, [SOXS], {}))

    def test_trend_rules_follow_the_toggle(self):
        on = _entry_prompt("us", 1, [SOXS], {"trend_entries": True})
        self.assertIn("[추세 추종]", on)
        self.assertIn("깊은 눌림이 오지 않는다", on)
        self.assertIn('"mode"', on)
        off = _entry_prompt("us", 1, [SOXS], {"trend_entries": False})
        self.assertNotIn("[추세 추종]", off)
        self.assertIn("VWAP에서 이미 멀리 달아난 종목", off)

    def test_mode_is_parsed(self):
        text = ('{"decisions": ['
                '{"code": "SOXS", "action": "enter", "mode": "trend", "stop": 48.4, "target": 50.3},'
                '{"code": "NVDA", "action": "enter", "stop": 198, "target": 203}]}')
        plans, note = parse_entry_plans(text, ["SOXS", "NVDA"], 2)
        self.assertEqual([p["mode"] for p in plans], ["trend", "pullback"])
        self.assertIn("진입(추세) SOXS", note)


def trend_plan(**kw):
    data = {"code": "SOXS", "mode": "trend", "max_price": 49.4, "stop": 48.4,
            "target": 50.3, "reason": "추세"}
    data.update(kw)
    return data


class ScaleOutTest(unittest.TestCase):
    """추세 진입은 목표가에서 절반만 팔고 나머지는 고점을 따라간다 (국내형: 즉시 체결)."""

    def setUp(self):
        self.api = FakeAPI({"SOXS": 49.15}, [SOXS])
        self.api.minute_bars = lambda code, scope=1, count=90: bars(49.15)
        self.trader = make(self.api, plans=[trend_plan()], total_cash=1000,
                           max_positions=1)
        scan(self.trader)
        self.pos = self.trader.positions["SOXS"]

    def move(self, price):
        self.api.prices["SOXS"] = price
        self.trader.step()

    def test_bought_as_trend(self):
        self.assertEqual(self.pos.mode, "trend")
        self.assertEqual(self.pos.quantity, 6)       # 위험 한도 5 ÷ 0.75
        self.assertTrue(any("매수(추세 추종)" in e for e in self.trader.events))

    def test_half_is_sold_at_target_and_rest_trails(self):
        self.move(50.35)
        pos = self.trader.positions["SOXS"]
        self.assertEqual(self.api.orders[-1], ("SOXS", "sell", 3))
        self.assertEqual(pos.quantity, 3)
        self.assertTrue(pos.trailing and pos.scaled)
        self.assertEqual(pos.state(), "절반 익절 후 추적 중")
        self.assertEqual(self.trader.trades, [])     # 아직 매매가 끝난 것이 아니다
        self.assertAlmostEqual(self.trader.cash, 1000 - 6 * 49.15 + 3 * 50.35)

    def test_rest_rides_the_trend_then_exits_on_pullback(self):
        self.move(50.35)
        trail = self.trader.positions["SOXS"].trail_pct
        for price in (51.5, 52.8, 54.0):             # 추세가 이어지면 계속 보유
            self.move(price)
        self.assertIn("SOXS", self.trader.positions)
        self.move(54.0 * (1 - trail / 100) - 0.01)   # 고점에서 추적 폭만큼 밀림
        self.assertNotIn("SOXS", self.trader.positions)
        trade = self.trader.trades[-1]
        self.assertEqual(trade.reason, "익절")
        self.assertEqual(trade.quantity, 6)
        exit_rest = 54.0 * (1 - trail / 100) - 0.01
        expected = 3 * (50.35 - 49.15) + 3 * (exit_rest - 49.15)
        self.assertAlmostEqual(trade.pnl, expected, places=6)
        # 나눠 판 매매는 평균 매도가로 기록한다
        self.assertAlmostEqual(trade.exit_price, 49.15 + expected / 6, places=6)

    def test_trail_width_follows_volatility(self):
        self.move(50.35)
        pos = self.trader.positions["SOXS"]
        vol = pos.volatility
        cfg = self.trader.config
        self.assertGreater(vol, 0)
        self.assertEqual(pos.trail_pct,
                         round(min(max(cfg.trend_trail_pct, vol * 3), 4.0), 2))

    def test_stop_still_protects_the_rest(self):
        self.move(50.35)                              # 본전 확보 + 절반 익절
        self.assertTrue(self.trader.positions["SOXS"].be_moved)
        self.move(49.2)                               # 급락: 본전 위 손절가에 걸림
        trade = self.trader.trades[-1]
        self.assertEqual(trade.reason, "수익 보전")
        self.assertGreater(trade.pnl, 0)

    def test_pullback_plan_still_sells_everything_at_target(self):
        api = FakeAPI({"SOXS": 49.15}, [SOXS])
        trader = make(api, plans=[trend_plan(mode="pullback")], total_cash=1000,
                      max_positions=1)
        scan(trader)
        api.prices["SOXS"] = 50.35
        trader.step()
        self.assertEqual(api.orders[-1], ("SOXS", "sell", 6))
        self.assertEqual(trader.trades[-1].reason, "익절")

    def test_toggle_off_forces_pullback(self):
        api = FakeAPI({"SOXS": 49.15}, [SOXS])
        trader = make(api, plans=[trend_plan()], total_cash=1000,
                      max_positions=1, trend_entries=False)
        scan(trader)
        self.assertEqual(trader.positions["SOXS"].mode, "pullback")

    def test_single_share_trails_whole(self):
        api = FakeAPI({"SOXS": 49.15}, [SOXS])
        trader = make(api, plans=[trend_plan()], total_cash=160,
                      max_positions=1)               # 위험 한도 0.8 → 1주
        scan(trader)
        self.assertEqual(trader.positions["SOXS"].quantity, 1)
        api.prices["SOXS"] = 50.35
        trader.step()
        pos = trader.positions["SOXS"]
        self.assertEqual(pos.quantity, 1)
        self.assertTrue(pos.trailing)
        self.assertEqual([o[1] for o in api.orders], ["buy"])

    def test_ai_cannot_tighten_inside_the_trail(self):
        self.move(50.35)
        self.move(52.0)
        pos = self.trader.positions["SOXS"]
        self.trader._review_result = (
            [{"code": "SOXS", "action": "tighten", "stop": 51.9}], "",
            self.trader.clock())
        self.trader.step()
        self.assertLessEqual(pos.stop_price,
                             52.0 * (1 - pos.trail_pct / 100) + 1e-9)


class ScaleOutVerifiedTest(unittest.TestCase):
    """미국형: 절반 익절 주문도 체결을 확인한 뒤에만 장부에 반영한다."""

    ROWS = [{"code": "SOXS", "price": "49.15", "change_pct": "7.5",
             "volume": "90000000"}]

    def _entered(self, state_path=None):
        api = FakeUsAPI({"SOXS": 49.15}, self.ROWS)
        trader = make_us(api, state_path=state_path, stop_pct=2.0,
                         target_pct=2.0, total_cash=1000)
        trader.selector = lambda rows, n: ([trend_plan()], "")
        trader._scan_worker(1)
        trader.step()
        trader.step()                                 # 매수 체결 확인
        qty = trader.positions["SOXS"].quantity
        api.balances = {"SOXS": {"qty": qty, "sellable": qty}}
        trader.positions["SOXS"].baseline_qty = 0
        return trader, api, qty

    def test_filled_half(self):
        trader, api, qty = self._entered()
        api.prices["SOXS"] = 50.35
        trader.step()
        pos = trader.positions["SOXS"]
        self.assertTrue(pos.scale_pending)
        self.assertEqual(pos.state(), "절반 익절 체결 대기")
        self.assertEqual(api.orders[-1][:3], ("SOXS", "sell", qty // 2))
        api.balances = {"SOXS": {"qty": qty - qty // 2, "sellable": qty - qty // 2}}
        trader.step()
        self.assertFalse(pos.scale_pending)
        self.assertEqual(pos.quantity, qty - qty // 2)
        self.assertTrue(pos.trailing)

    def test_unfilled_half_is_not_chased(self):
        trader, api, qty = self._entered()
        api.auto_fill = False
        api.prices["SOXS"] = 50.35
        trader.step()
        sells = [o for o in api.orders if o[1] == "sell"]
        self.assertEqual(len(sells), 1)
        advance_us = trader._clock
        advance_us["now"] = advance_us["now"].replace(second=30)   # 30초 경과
        trader.step()
        pos = trader.positions["SOXS"]
        self.assertFalse(pos.scale_pending)
        self.assertEqual(pos.quantity, qty)           # 안 팔린 몫은 그대로 보유
        self.assertEqual(len(api.cancelled), 1)
        self.assertEqual(len([o for o in api.orders if o[1] == "sell"]), 1)
        self.assertTrue(pos.trailing)                 # 전부 추적 익절로 넘어간다

    def test_vanished_order_is_checked_against_the_account(self):
        trader, api, qty = self._entered()
        api.prices["SOXS"] = 50.35
        trader.step()                                 # 주문은 사라졌지만 잔고는 그대로
        trader.step()
        pos = trader.positions["SOXS"]
        self.assertTrue(pos.scale_pending)            # 한 번은 반영 지연일 수 있다
        trader.step()
        self.assertFalse(pos.scale_pending)
        self.assertEqual(pos.quantity, qty)

    def test_scaled_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            trader, api, qty = self._entered(state_path=path)
            api.prices["SOXS"] = 50.35
            trader.step()
            rest = qty - qty // 2
            api.balances = {"SOXS": {"qty": rest, "sellable": rest,
                                     "now_price": 50.4}}
            trader.step()
            api2 = FakeUsAPI({"SOXS": 50.4}, self.ROWS, balances=api.balances)
            second = make_us(api2, state_path=path, stop_pct=2.0,
                             target_pct=2.0, total_cash=1000)
            second.restore_state()
            pos = second.positions["SOXS"]
            self.assertTrue(pos.scaled and pos.trailing)
            self.assertEqual(pos.mode, "trend")
            self.assertEqual(pos.quantity, rest)
            self.assertEqual(pos.orig_qty, qty)
            second.step()                              # 다시 절반을 팔지 않는다
            self.assertEqual(api2.orders, [])


class DefaultsTest(unittest.TestCase):
    def test_trend_defaults(self):
        cfg = ScalpConfig()
        self.assertTrue(cfg.trend_entries)
        self.assertEqual(cfg.trend_trail_pct, 1.0)
        self.assertEqual(cfg.trend_trail_max_pct, 4.0)


if __name__ == "__main__":
    unittest.main()
