"""AI 매매 시점 판단을 엔진이 검증·집행하는 부분: 종목군 제한, 진입 계획
검증, 1회 손실 한도 수량, 종목별 손절가·목표가, 본전 확보, AI 청산 점검."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from autotrader.scalper import EntryPlan, ScalpConfig, Scalper
from autotrader.universe import build_universe

US_OPEN_UTC = datetime(2025, 10, 6, 15, 0, tzinfo=timezone.utc)   # 정규장 중반

NVDA = {"code": "NVDA", "name": "엔비디아", "price": "200.00",
        "change_pct": "1.2", "volume": "40000000"}
SPY = {"code": "SPY", "name": "SPDR S&P 500", "price": "600.00",
       "change_pct": "0.4", "volume": "30000000"}
ONCO = {"code": "ONCO", "name": "온코네틱스", "price": "7.57",
        "change_pct": "35.0", "volume": "9000000"}


def bars(last=200.0, n=90):
    return [{"t": "20251006%02d%02d00" % (10 + i // 60, i % 60),
             "o": last - 1 + i / n, "h": last - 0.9 + i / n,
             "l": last - 1.1 + i / n, "c": last - 1 + (i + 1) / n,
             "v": 5000 + i} for i in range(n)]


class FakeAPI:
    def __init__(self, prices, ranking):
        self.prices = dict(prices)
        self.ranking = list(ranking)
        self.orders = []
        self.rank_calls = []
        self.bar_calls = []

    def current_price(self, code):
        return self.prices[code]

    def send_market_order(self, account_no, code, side, quantity,
                          kr_exchange="KRX", limit_price=None):
        self.orders.append((code, side, quantity))
        return "1"

    def top_stocks(self, criteria="volume", limit=10):
        self.rank_calls.append((criteria, limit))
        return self.ranking

    def minute_bars(self, code, scope=1, count=90):
        self.bar_calls.append(code)
        return bars(self.prices[code])


def make(api, plans=None, reviewer=None, journal_path=None, state_path=None,
         **kw):
    clock = {"now": datetime(2025, 10, 7, 0, 0)}
    cfg = dict(market="us", max_positions=2, total_cash=10_000,
               scan_interval=99999.0, open_delay_min=0, max_spread_pct=0,
               universe=build_universe("us"))
    cfg.update(kw)

    def selector(rows, n):
        selector.rows = rows
        if callable(plans):
            return plans(rows, n)
        return list(plans or []), "AI 근거"

    trader = Scalper(api=api, config=ScalpConfig(**cfg), selector=selector,
                     reviewer=reviewer, is_simulation=True,
                     clock=lambda: clock["now"], utc_clock=lambda: US_OPEN_UTC,
                     state_path=state_path, journal_path=journal_path)
    trader._maybe_scan = lambda *a, **k: None
    trader._clock = clock
    trader._selector = selector
    return trader


def scan(trader, slots=1):
    trader._scan_worker(slots)
    trader.step()


def advance(trader, seconds):
    trader._clock["now"] += timedelta(seconds=seconds)


def plan(code="NVDA", **kw):
    data = {"code": code, "max_price": 200.5, "stop": 198.0, "target": 203.0,
            "reason": "VWAP 눌림"}
    data.update(kw)
    return data


class UniverseTest(unittest.TestCase):
    def test_small_caps_never_reach_the_ai(self):
        api = FakeAPI({"NVDA": 200.0, "SPY": 600.0, "ONCO": 7.57},
                      [ONCO, NVDA, SPY])
        trader = make(api, plans=[])
        scan(trader)
        seen = [r["code"] for r in trader._selector.rows]
        self.assertEqual(seen, ["NVDA", "SPY"])      # 등락률 1위 ONCO는 빠진다
        self.assertNotIn("ONCO", api.bar_calls)

    def test_scans_dollar_volume_ranking_widely(self):
        api = FakeAPI({"NVDA": 200.0}, [NVDA])
        scan(make(api, plans=[]))
        self.assertEqual(api.rank_calls,
                         [("value", 100), ("volume", 100), ("change", 100)])

    def test_no_restriction_keeps_legacy_ranking(self):
        api = FakeAPI({"ONCO": 7.57}, [ONCO])
        trader = make(api, plans=[], universe=None)
        scan(trader)
        self.assertEqual(api.rank_calls, [("change", 10), ("volume", 10)])
        self.assertEqual([r["code"] for r in trader._selector.rows], ["ONCO"])

    def test_ai_gets_bars_and_kind(self):
        api = FakeAPI({"NVDA": 200.0, "SPY": 600.0}, [NVDA, SPY])
        trader = make(api, plans=[])
        scan(trader)
        row = trader._selector.rows[0]
        self.assertEqual(row["kind"], "대형주")
        self.assertEqual(trader._selector.rows[1]["kind"], "ETF")
        self.assertAlmostEqual(row["bars"]["last"], 200.0)
        self.assertIn("vwap", row["bars"])

    def test_bar_failure_does_not_block_the_decision(self):
        api = FakeAPI({"NVDA": 200.0}, [NVDA])

        def broken(code, scope=1, count=90):
            raise RuntimeError("1632")
        api.minute_bars = broken
        trader = make(api, plans=[plan()])
        scan(trader)
        self.assertNotIn("bars", trader._selector.rows[0])
        self.assertIn("NVDA", trader.positions)

    def test_leveraged_etfs_do_not_crowd_out_the_shortlist(self):
        lev = [{"code": c, "price": "50", "change_pct": str(9 - i),
                "volume": "20000000"}
               for i, c in enumerate(["SOXL", "TQQQ", "TECL", "UPRO", "SPXL"])]
        prices = {r["code"]: 50.0 for r in lev}
        prices.update({"NVDA": 200.0, "SPY": 600.0})
        api = FakeAPI(prices, lev + [NVDA, SPY])
        trader = make(api, plans=[], shortlist_size=4)
        scan(trader)
        self.assertEqual([r["code"] for r in trader._selector.rows],
                         ["SOXL", "TQQQ", "NVDA", "SPY"])

    def test_build_universe_options(self):
        self.assertNotIn("ONCO", build_universe("us"))
        self.assertNotIn("TQQQ", build_universe("us", leveraged=False))
        self.assertEqual(build_universe("us")["TQQQ"], "lev")
        custom = build_universe("us", add="coin, hood", remove="TSLA")
        self.assertIn("COIN", custom)
        self.assertNotIn("TSLA", custom)
        self.assertIn("005930", build_universe("kr"))


class EntryPlanTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI({"NVDA": 200.0, "SPY": 600.0}, [NVDA, SPY])

    def test_ai_levels_are_used(self):
        trader = make(self.api, plans=[plan()])
        scan(trader)
        pos = trader.positions["NVDA"]
        self.assertAlmostEqual(pos.stop_price, 198.0)
        self.assertAlmostEqual(pos.target_price, 203.0)
        self.assertAlmostEqual(pos.init_risk, 2.0)

    def test_quantity_follows_risk_limit(self):
        # 총 10,000의 0.5% = 50 위험. 1주당 위험 2.00 → 25주 (슬롯 예산은 25주까지)
        trader = make(self.api, plans=[plan()])
        scan(trader)
        self.assertEqual(self.api.orders, [("NVDA", "buy", 25)])
        # 손절이 가까우면 더 많이 사되 슬롯 예산(5,000 → 25주)을 넘지 않는다
        api2 = FakeAPI({"NVDA": 200.0}, [NVDA])
        scan(make(api2, plans=[plan(stop=199.0, target=202.0)]))
        self.assertEqual(api2.orders, [("NVDA", "buy", 25)])
        # 손절이 멀면 적게 산다: 1주당 위험 4.00 → 12주
        api3 = FakeAPI({"NVDA": 200.0}, [NVDA])
        scan(make(api3, plans=[plan(stop=196.0, target=206.0)]))
        self.assertEqual(api3.orders, [("NVDA", "buy", 12)])

    def test_skips_when_one_share_exceeds_risk(self):
        trader = make(self.api, plans=[plan()], total_cash=300,
                      max_positions=1)            # 위험 한도 1.5 < 1주당 2.00
        scan(trader)
        self.assertEqual(self.api.orders, [])
        self.assertTrue(any("1회 손실 한도" in e for e in trader.events))

    def test_does_not_chase_above_max_price(self):
        trader = make(self.api, plans=[plan(max_price=199.5)])
        scan(trader)
        self.assertEqual(self.api.orders, [])
        self.assertTrue(any("진입 상한" in e for e in trader.events))

    def test_discards_plan_when_price_drifted(self):
        trader = make(self.api, plans=[plan(max_price=205.0)])
        trader._scan_worker(1)
        self.api.prices["NVDA"] = 201.2            # 판단 이후 +0.6%
        trader.step()
        self.assertEqual(self.api.orders, [])
        self.assertTrue(any("판단을 버린다" in e for e in trader.events))

    def test_discards_stale_plan(self):
        trader = make(self.api, plans=[plan()])
        trader._scan_worker(1)
        advance(trader, 120)                       # 유효 시간 90초 초과
        trader.step()
        self.assertEqual(self.api.orders, [])
        self.assertTrue(any("오래됐다" in e for e in trader.events))

    def test_rejects_poor_reward_to_risk(self):
        trader = make(self.api, plans=[plan(stop=198.0, target=202.0)])  # 1.0배
        scan(trader)
        self.assertEqual(self.api.orders, [])
        self.assertTrue(any("손익비 부족" in e for e in trader.events))

    def test_rejects_target_too_small_to_cover_fees(self):
        # 손익비는 맞아도 목표가 +0.5%뿐이면 수수료를 빼고 남는 것이 없다
        trader = make(self.api, plans=[plan(stop=199.2, target=201.0)])
        scan(trader)
        self.assertEqual(self.api.orders, [])
        self.assertTrue(any("수수료" in e for e in trader.events))

    def test_stop_is_clamped_to_allowed_range(self):
        # 너무 먼 손절(-5%)은 최대 손절 2%로, 너무 가까운 손절은 0.4%로 맞춘다
        trader = make(self.api, plans=[plan(stop=190.0, target=215.0)])
        scan(trader)
        self.assertAlmostEqual(trader.positions["NVDA"].stop_price, 196.0)
        api2 = FakeAPI({"NVDA": 200.0}, [NVDA])
        trader2 = make(api2, plans=[plan(stop=199.9, target=202.0)])
        scan(trader2)
        self.assertAlmostEqual(trader2.positions["NVDA"].stop_price, 199.2)

    def test_missing_levels_fall_back_to_settings(self):
        trader = make(self.api, plans=["NVDA"], stop_pct=1.5, target_pct=2.5)
        scan(trader)
        pos = trader.positions["NVDA"]
        self.assertAlmostEqual(pos.stop_price, 197.0)
        self.assertAlmostEqual(pos.target_price, 205.0)

    def test_code_outside_candidates_is_ignored(self):
        trader = make(self.api, plans=[plan(code="TSLA"), plan()])
        scan(trader)
        self.assertEqual([o[0] for o in self.api.orders], ["NVDA"])

    def test_plan_objects_are_accepted(self):
        trader = make(self.api, plans=[EntryPlan("NVDA", stop=198.0, target=203.0)])
        scan(trader)
        self.assertIn("NVDA", trader.positions)


class ExitLevelsTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI({"NVDA": 200.0}, [NVDA])
        self.trader = make(self.api, plans=[plan()], trail_pct=0)
        scan(self.trader)

    def move(self, price):
        self.api.prices["NVDA"] = price
        self.trader.step()

    def test_stop_at_ai_level(self):
        self.move(198.5)
        self.assertIn("NVDA", self.trader.positions)      # -0.75%: 아직 손절가 위
        self.move(197.9)
        self.assertEqual(self.trader.trades[-1].reason, "손절")
        # 25주 × 2.10 = 52.5 — 1회 손실 한도(50) 근처에서 끝난다
        self.assertAlmostEqual(self.trader.trades[-1].pnl, -52.5)

    def test_target_at_ai_level(self):
        self.move(202.9)
        self.assertIn("NVDA", self.trader.positions)
        self.move(203.1)
        self.assertEqual(self.trader.trades[-1].reason, "익절")

    def test_stop_moves_to_breakeven_after_one_r(self):
        self.move(202.1)                                  # +1R(2.00) 도달
        pos = self.trader.positions["NVDA"]
        self.assertTrue(pos.be_moved)
        self.assertAlmostEqual(pos.stop_price, 200.6)     # 매입가 + 0.3%
        self.move(200.5)                                  # 되밀려도 손실이 아니다
        trade = self.trader.trades[-1]
        self.assertEqual(trade.reason, "수익 보전")
        self.assertGreater(trade.pnl, 0)

    def test_breakeven_move_can_be_disabled(self):
        api = FakeAPI({"NVDA": 200.0}, [NVDA])
        trader = make(api, plans=[plan()], trail_pct=0, breakeven_at_r=0)
        scan(trader)
        api.prices["NVDA"] = 202.1
        trader.step()
        self.assertAlmostEqual(trader.positions["NVDA"].stop_price, 198.0)


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI({"NVDA": 200.0}, [NVDA])
        self.calls = []
        self.answer = ([], "")

        def reviewer(rows):
            self.calls.append(rows)
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

        self.trader = make(self.api, plans=[plan()], reviewer=reviewer,
                           trail_pct=0)
        scan(self.trader)

    def review(self):
        rows = self.trader._review_rows()
        if rows:
            self.trader._review_worker(rows)
        self.trader.step()
        return rows

    def test_not_reviewed_before_interval(self):
        advance(self.trader, 60)
        self.assertEqual(self.review(), [])
        advance(self.trader, 200)
        rows = self.review()
        self.assertEqual(rows[0]["code"], "NVDA")
        self.assertEqual(rows[0]["stop"], 198.0)
        self.assertIn("bars", self.calls[0][0])           # 분봉을 붙여 넘긴다

    def test_tighten_ignored_before_one_r(self):
        # 수익이 손절 폭(2.00)만큼 나기 전에는 AI가 손절가를 올리지 못한다
        self.api.prices["NVDA"] = 201.0
        self.trader.step()
        advance(self.trader, 300)
        self.answer = ([{"code": "NVDA", "action": "tighten", "stop": 200.2,
                         "reason": "새 지지"}], "손절 상향")
        rows = self.review()
        self.assertFalse(rows[0]["can_tighten"])
        self.assertAlmostEqual(self.trader.positions["NVDA"].stop_price, 198.0)
        self.assertEqual(self.trader.review_note, "손절 상향")   # 근거는 표시

    def test_tighten_raises_stop_after_one_r(self):
        self.api.prices["NVDA"] = 202.4          # +1R 도달 → 본전 확보(200.6)
        self.trader.step()
        advance(self.trader, 300)
        self.answer = ([{"code": "NVDA", "action": "tighten", "stop": 201.2,
                         "reason": "새 지지"}], "")
        rows = self.review()
        self.assertTrue(rows[0]["can_tighten"])
        self.assertAlmostEqual(self.trader.positions["NVDA"].stop_price, 201.2)

    def test_tighten_keeps_distance_from_price(self):
        # 현재가에 바짝 붙인 손절가는 0.5% 아래로 물린다
        self.api.prices["NVDA"] = 202.4
        self.trader.step()
        advance(self.trader, 300)
        self.answer = ([{"code": "NVDA", "action": "tighten", "stop": 202.3}], "")
        self.review()
        self.assertAlmostEqual(self.trader.positions["NVDA"].stop_price,
                               202.4 * 0.995)

    def test_ai_cannot_lower_the_stop(self):
        advance(self.trader, 300)
        self.answer = ([{"code": "NVDA", "action": "tighten", "stop": 195.0}], "")
        self.review()
        self.assertAlmostEqual(self.trader.positions["NVDA"].stop_price, 198.0)

    def test_stop_above_price_never_sells(self):
        self.api.prices["NVDA"] = 202.4
        self.trader.step()
        advance(self.trader, 300)
        self.answer = ([{"code": "NVDA", "action": "tighten", "stop": 205.0}], "")
        self.review()
        self.assertLess(self.trader.positions["NVDA"].stop_price, 202.4)
        self.assertEqual(len(self.api.orders), 1)

    def test_exit_sells_now(self):
        self.api.prices["NVDA"] = 200.8
        self.trader.step()
        advance(self.trader, 300)
        self.answer = ([{"code": "NVDA", "action": "exit", "reason": "꺾임"}], "")
        self.review()
        self.assertEqual(self.api.orders[-1], ("NVDA", "sell", 25))
        self.assertEqual(self.trader.trades[-1].reason, "AI 청산")

    def test_late_answer_is_dropped(self):
        advance(self.trader, 300)
        self.answer = ([{"code": "NVDA", "action": "exit"}], "")
        self.trader._review_worker(self.trader._review_rows())
        advance(self.trader, 200)                         # 답이 너무 늦게 도착
        self.trader.step()
        self.assertEqual(len(self.api.orders), 1)

    def test_reviewer_failure_keeps_code_rules(self):
        advance(self.trader, 300)
        self.answer = RuntimeError("usage limit")
        self.review()
        advance(self.trader, 300)
        self.review()
        self.assertEqual(
            sum("AI 청산 점검 불가" in e for e in self.trader.events), 1)
        self.api.prices["NVDA"] = 197.5                   # 손절은 그대로 작동
        self.trader.step()
        self.assertEqual(self.trader.trades[-1].reason, "손절")

    def test_maybe_review_marks_positions(self):
        advance(self.trader, 300)
        self.trader._maybe_review()
        thread = self.trader._review_thread
        if thread is not None:
            thread.join(2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.trader._review_rows(), [])  # 다음 주기까지 쉰다


class PersistenceAndJournalTest(unittest.TestCase):
    def test_levels_survive_restart(self):
        class UsAPI(FakeAPI):
            def us_balances(self):
                return {"NVDA": {"qty": 25, "sellable": 25, "avg_price": 200.0,
                                 "now_price": 200.4, "pl_rt": 0.2}}

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            api = UsAPI({"NVDA": 200.0}, [NVDA])
            first = make(api, plans=[plan()], state_path=path)
            scan(first)
            second = make(UsAPI({"NVDA": 200.4}, [NVDA]), plans=[],
                          state_path=path)
            second.restore_state()
            pos = second.positions["NVDA"]
            self.assertAlmostEqual(pos.stop_price, 198.0)
            self.assertAlmostEqual(pos.target_price, 203.0)

    def test_journal_records_decisions_and_trades(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "journal.jsonl")
            api = FakeAPI({"NVDA": 200.0}, [NVDA])
            trader = make(api, plans=[plan()], journal_path=path, trail_pct=0)
            scan(trader)
            api.prices["NVDA"] = 197.0
            trader.step()
            with open(path, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f]
        kinds = [r["kind"] for r in rows]
        self.assertEqual(kinds, ["entry_decision", "buy", "trade"])
        self.assertEqual(rows[0]["plans"][0]["stop"], 198.0)
        self.assertEqual(rows[2]["reason"], "손절")


class DefaultsTest(unittest.TestCase):
    def test_risk_defaults(self):
        cfg = ScalpConfig()
        self.assertEqual(cfg.risk_pct, 0.5)
        self.assertEqual(cfg.daily_loss_pct, 2.0)
        self.assertEqual(cfg.max_trades_per_day, 20)
        self.assertIsNone(cfg.universe)

    def test_invalid_risk_rejected(self):
        with self.assertRaises(ValueError):
            Scalper(api=FakeAPI({}, []), config=ScalpConfig(risk_pct=9))


if __name__ == "__main__":
    unittest.main()
