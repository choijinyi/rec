"""인버스 ETF 포함, 같은 지수 한 종목 규칙, 갭 큰 날의 개장 초반 조기 판단."""

import unittest
from datetime import datetime, timezone

try:
    from test_timing import FakeAPI, make, plan, scan
except ImportError:
    from tests.test_timing import FakeAPI, make, plan, scan

from autotrader.analysis import _entry_prompt
from autotrader.indicators import open_gap, summarize_bars
from autotrader.universe import (
    INVERSE, LEVERAGED, build_universe, group_of,
)


def row(code, price, change):
    return {"code": code, "name": code, "price": str(price),
            "change_pct": str(change), "volume": "50000000"}


class UniverseInverseTest(unittest.TestCase):
    def test_inverse_etfs_are_included_and_labelled(self):
        us = build_universe("us")
        self.assertEqual(us["SOXS"], INVERSE)
        self.assertEqual(us["SQQQ"], INVERSE)
        self.assertEqual(us["SOXL"], LEVERAGED)
        kr = build_universe("kr")
        self.assertEqual(kr["114800"], INVERSE)
        self.assertEqual(kr["122630"], LEVERAGED)

    def test_checkbox_off_removes_both(self):
        plain = build_universe("us", leveraged=False)
        self.assertNotIn("SOXL", plain)
        self.assertNotIn("SOXS", plain)
        only_inverse = build_universe("us", leveraged=False, inverse=True)
        self.assertIn("SOXS", only_inverse)
        self.assertNotIn("SOXL", only_inverse)

    def test_groups(self):
        self.assertEqual(group_of("SOXL"), ("반도체", 1))
        self.assertEqual(group_of("SOXS"), ("반도체", -1))
        self.assertEqual(group_of("SMH")[0], "반도체")
        self.assertEqual(group_of("114800_AL"), ("코스피200", -1))
        self.assertIsNone(group_of("NVDA"))


class InverseTradingTest(unittest.TestCase):
    def test_inverse_is_a_candidate_on_a_down_day(self):
        # 지수가 내리는 날: 정방향은 하락(후보 제외), 인버스는 상승(후보)
        api = FakeAPI({"SOXS": 20.0, "SOXL": 50.0},
                      [row("SOXL", 50, -6.0), row("SOXS", 20, 6.1)])
        trader = make(api, plans=[plan("SOXS", max_price=20.1, stop=19.8,
                                       target=20.4)])
        scan(trader)
        self.assertEqual([r["code"] for r in trader._selector.rows], ["SOXS"])
        self.assertIn("인버스", trader._selector.rows[0]["kind"])
        self.assertEqual([o[:2] for o in api.orders], [("SOXS", "buy")])

    def test_shortlist_keeps_two_of_each_kind(self):
        codes = ["SOXS", "SQQQ", "SPXU", "TZA", "SOXL", "TQQQ", "UPRO"]
        rows = [row(c, 30, 9 - i) for i, c in enumerate(codes)]
        prices = {c: 30.0 for c in codes}
        prices["NVDA"] = 200.0
        api = FakeAPI(prices, rows + [row("NVDA", 200, 0.5)])
        trader = make(api, plans=[], shortlist_size=6)
        scan(trader)
        self.assertEqual([r["code"] for r in trader._selector.rows],
                         ["SOXS", "SQQQ", "SOXL", "TQQQ", "NVDA"])


class OneIndexOnePositionTest(unittest.TestCase):
    def setUp(self):
        self.rows = [row("SOXL", 50, 5.0), row("SOXS", 20, 1.0),
                     row("SMH", 300, 2.0), row("NVDA", 200, 1.0)]
        self.api = FakeAPI({"SOXL": 50.0, "SOXS": 20.0, "SMH": 300.0,
                            "NVDA": 200.0}, self.rows)

    def test_opposite_and_duplicate_are_not_candidates(self):
        trader = make(self.api, plans=[plan("SOXL", max_price=50.2, stop=49.5,
                                            target=51.0)])
        scan(trader)
        self.assertIn("SOXL", trader.positions)
        trader._scan_worker(1)
        # 반도체 지수를 이미 들고 있다: 인버스(SOXS)도, 같은 방향(SMH)도 뺀다
        self.assertEqual([r["code"] for r in trader._selector.rows], ["NVDA"])

    def test_two_plans_for_one_index_buy_only_the_first(self):
        plans = [plan("SOXL", max_price=50.2, stop=49.5, target=51.0),
                 plan("SOXS", max_price=20.1, stop=19.8, target=20.4)]
        trader = make(self.api, plans=plans)
        scan(trader, slots=2)
        self.assertEqual([o[0] for o in self.api.orders], ["SOXL"])
        self.assertTrue(any("같은 지수" in e for e in trader.events))

    def test_watched_holding_does_not_block(self):
        from autotrader.scalper import Position

        trader = make(self.api, plans=[])
        trader.positions["SOXS"] = Position(
            "SOXS", 10, 30.0, trader.clock(), last_price=20.0, watch=True,
            adopted=True)
        self.assertEqual(trader._group_conflict("SOXL"), "")


def gap_bars(before, after, n_after=6, step=0.1):
    """쉬기 전 30개 봉(07:30~07:59) + 다시 열린 뒤 n_after개 봉(10:00~)."""
    out = [{"t": "2026111107%02d00" % m, "o": before, "h": before + 0.05,
            "l": before - 0.05, "c": before, "v": 1000} for m in range(30, 60)]
    out += [{"t": "2026111110%02d00" % m, "o": after + m * step,
             "h": after + m * step + 0.1, "l": after + m * step - 0.1,
             "c": after + m * step, "v": 90000} for m in range(n_after)]
    return out


class OpenGapTest(unittest.TestCase):
    def test_gap_is_measured_across_the_pause(self):
        gap = open_gap(gap_bars(56.0, 58.5))
        self.assertEqual(gap, {"gap_pct": 4.46, "bars_since": 6})
        summary = summarize_bars(gap_bars(56.0, 58.5))
        self.assertEqual(summary["gap_pct"], 4.46)
        self.assertEqual(summary["bars_since_gap"], 6)

    def test_no_pause_means_no_gap(self):
        self.assertIsNone(open_gap(gap_bars(56.0, 58.5)[:30]))
        self.assertIsNone(summarize_bars(gap_bars(56.0, 58.5)[:30])["gap_pct"])

    def test_bad_timestamps_are_ignored(self):
        bars = [{"t": "", "c": 10.0, "h": 10, "l": 10, "v": 1} for _ in range(6)]
        self.assertIsNone(open_gap(bars))

    def test_prompt_shows_gap_and_inverse_guidance(self):
        rows = [{"code": "SOXL", "name": "SOXL", "kind": "레버리지 ETF",
                 "price": "59.0", "change_pct": "5.0", "volume": "9",
                 "bars": summarize_bars(gap_bars(56.0, 58.5))}]
        prompt = _entry_prompt("us", 1, rows, {})
        self.assertIn("갭 +4.46%", prompt)
        self.assertIn("열린 뒤 6분 경과", prompt)
        self.assertIn("갭을 메우며", prompt)
        self.assertIn("인버스 ETF는 기초지수가 내릴 때 오른다", prompt)


class EarlyGapEntryTest(unittest.TestCase):
    """개장 5~15분: 갭이 2% 이상 뜬 종목만 AI가 먼저 판단한다."""

    # 2025-10-06은 미국 서머타임: 정규장 시작 09:30 ET = 13:30 UTC
    def at(self, minutes_after_open):
        return lambda: datetime(2025, 10, 6, 13, 30 + minutes_after_open,
                                tzinfo=timezone.utc)

    def _trader(self, bars_by_code, plans, minutes=6, **kw):
        rows = [row("SOXL", 59.0, 5.0), row("NVDA", 200, 1.0)]
        api = FakeAPI({"SOXL": 59.0, "NVDA": 200.0}, rows)
        api.minute_bars = lambda code, scope=1, count=90: bars_by_code[code]
        trader = make(api, plans=plans, open_delay_min=15, **kw)
        trader.utc_clock = self.at(minutes)
        return trader, api

    BARS = {"SOXL": gap_bars(56.0, 58.5), "NVDA": gap_bars(200.0, 200.2)}
    SOXL_PLAN = [{"code": "SOXL", "max_price": 59.3, "stop": 58.2,
                  "target": 60.2, "reason": "갭을 지키며 고점을 높임"}]

    def test_gapped_symbol_is_judged_early(self):
        trader, api = self._trader(self.BARS, self.SOXL_PLAN)
        scan(trader)
        # AI에는 갭이 뜬 SOXL만 넘어간다 (NVDA 갭 +0.1%는 15분을 기다린다)
        self.assertEqual([r["code"] for r in trader._selector.rows], ["SOXL"])
        self.assertEqual([o[:2] for o in api.orders], [("SOXL", "buy")])
        self.assertTrue(any("조기 판단" in e for e in trader.events))

    def test_no_gap_means_no_ai_call(self):
        flat = {"SOXL": gap_bars(58.9, 59.0), "NVDA": gap_bars(200.0, 200.2)}
        trader, api = self._trader(flat, self.SOXL_PLAN)
        scan(trader)
        self.assertFalse(hasattr(trader._selector, "rows"))   # AI를 부르지 않았다
        self.assertEqual(api.orders, [])

    def test_gap_down_is_not_chased(self):
        down = {"SOXL": gap_bars(61.0, 58.5), "NVDA": gap_bars(200.0, 200.2)}
        trader, api = self._trader(down, self.SOXL_PLAN)
        scan(trader)
        self.assertEqual(api.orders, [])

    def test_first_five_minutes_stay_blocked(self):
        trader, api = self._trader(self.BARS, self.SOXL_PLAN, minutes=3)
        scan(trader)
        self.assertEqual(api.orders, [])
        self.assertEqual(trader.halt_reason, "개장 직후 대기")

    def test_ai_can_still_decline(self):
        trader, api = self._trader(self.BARS, [])
        scan(trader)
        self.assertEqual([r["code"] for r in trader._selector.rows], ["SOXL"])
        self.assertEqual(api.orders, [])

    def test_no_early_entry_without_ai(self):
        trader, api = self._trader(self.BARS, self.SOXL_PLAN, require_ai=False)
        scan(trader)
        self.assertEqual(api.orders, [])

    def test_can_be_disabled(self):
        trader, api = self._trader(self.BARS, self.SOXL_PLAN, early_gap_pct=0)
        scan(trader)
        self.assertEqual(api.orders, [])
        self.assertEqual(trader.halt_reason, "개장 직후 대기")

    def test_normal_rules_after_the_delay(self):
        trader, api = self._trader(self.BARS, self.SOXL_PLAN, minutes=20)
        scan(trader)
        # 15분이 지나면 갭과 무관하게 모든 후보를 본다
        self.assertEqual(sorted(r["code"] for r in trader._selector.rows),
                         ["NVDA", "SOXL"])


if __name__ == "__main__":
    unittest.main()
