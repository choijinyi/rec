"""지정 종목 분할 매매: 지정가에서 간격만큼 내려갈 때마다 나눠 사고, 회차별
(+간격) 또는 매도 지정가부터 나눠 판다. 정한 금액을 넘겨 사지 않는다."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from autotrader.splitter import SplitTrader, build_plan

US_OPEN_UTC = datetime(2025, 10, 6, 15, 0, tzinfo=timezone.utc)     # 정규장
US_CLOSED_UTC = datetime(2025, 10, 4, 15, 0, tzinfo=timezone.utc)   # 토요일


class SimAPI:
    """즉시 체결되는 가짜 API (국내형 경로)."""

    def __init__(self, price):
        self.price = price
        self.orders = []

    def current_price(self, code):
        return self.price

    def send_market_order(self, account_no, code, side, quantity,
                          kr_exchange="KRX", limit_price=None):
        self.orders.append((side, quantity, self.price))
        return "1"


class UsAPI:
    """미체결·잔고를 흉내 내는 미국주식 가짜 API."""

    def __init__(self, price, holdings=None):
        self.price = price
        self.holdings = dict(holdings or {})     # code -> (qty, avg)
        self.orders = []
        self.open = {}
        self.cancelled = []
        self.auto_fill = True
        self.lag = False             # True면 체결돼도 잔고에 안 보인다
        self._n = 0

    def current_price(self, code):
        return self.price

    def best_bid_ask(self, code):
        return self.price - 0.01, self.price

    def _apply(self, code, side, qty):
        held, avg = self.holdings.get(code, (0, 0.0))
        self.holdings[code] = (held + (qty if side == "buy" else -qty), avg)

    def send_market_order(self, account_no, code, side, quantity,
                          kr_exchange="KRX", limit_price=None):
        self._n += 1
        no = f"{self._n:07d}"
        self.orders.append((side, quantity, limit_price))
        if self.auto_fill:
            if not self.lag:
                self._apply(code, side, quantity)
        else:
            self.open[no] = {"ord_no": no, "code": code, "side": side,
                             "remaining": quantity, "filled": 0}
        return no

    def fill(self, qty=None):
        for no, order in list(self.open.items()):
            done = order["remaining"] if qty is None else qty
            self._apply(order["code"], order["side"], done)
            order["remaining"] -= done
            order["filled"] += done
            if order["remaining"] == 0:
                del self.open[no]

    def us_open_orders(self, code=""):
        return list(self.open.values())

    def cancel_order(self, ord_no, code):
        self.cancelled.append(ord_no)
        self.open.pop(ord_no, None)

    def us_balances(self):
        return {code: {"qty": qty, "avg_price": avg}
                for code, (qty, avg) in self.holdings.items() if qty > 0}


def engine(api, **kw):
    clock = {"now": datetime(2025, 10, 7, 0, 0)}
    trader = SplitTrader(api=api, market="us", is_simulation=True,
                         clock=lambda: clock["now"],
                         utc_clock=kw.pop("utc_clock", lambda: US_OPEN_UTC),
                         **kw)
    trader._clock = clock
    return trader


def move(trader, api, price, seconds=3):
    api.price = price
    trader._clock["now"] += timedelta(seconds=seconds)
    trader.step()


class PlanTest(unittest.TestCase):
    def test_five_steps_one_percent_apart(self):
        levels, sells = build_plan(100.0, 1000.0)
        self.assertEqual([lv.buy for lv in levels], [100, 99, 98, 97, 96])
        self.assertEqual([lv.qty for lv in levels], [2, 2, 2, 2, 2])
        self.assertEqual([lv.sell for lv in levels],
                         [101.0, 99.99, 98.98, 97.97, 96.96])
        self.assertEqual(sells, [])
        self.assertLessEqual(sum(lv.buy * lv.qty for lv in levels), 1000.0)

    def test_cost_never_exceeds_the_budget(self):
        for budget in (333.0, 777.7, 1234.5, 50_000.0):
            levels, _ = build_plan(54.37, budget)
            self.assertLessEqual(sum(lv.buy * lv.qty for lv in levels), budget)

    def test_rejects_a_budget_too_small_for_one_share(self):
        with self.assertRaisesRegex(ValueError, "1주도 살 수 없다"):
            build_plan(200.0, 500.0)

    def test_sell_ladder_starts_at_the_sell_price(self):
        levels, sells = build_plan(100.0, 1000.0, mode="ladder", sell_start=105)
        self.assertEqual(sells, [105.0, 106.05, 107.1, 108.15, 109.2])
        self.assertTrue(all(lv.sell == 0 for lv in levels))

    def test_sell_start_must_be_above_the_buy_price(self):
        with self.assertRaisesRegex(ValueError, "높아야"):
            build_plan(100.0, 1000.0, mode="ladder", sell_start=100)
        with self.assertRaisesRegex(ValueError, "매도 지정가"):
            build_plan(100.0, 1000.0, mode="ladder")

    def test_sell_only_plan_has_no_buy_levels(self):
        levels, sells = build_plan(0, 0, mode="ladder", sell_start=10,
                                   hold_qty=7)
        self.assertEqual(levels, [])
        self.assertEqual(len(sells), 5)

    def test_bad_inputs(self):
        for kw in ({"steps": 0}, {"steps": 11}, {"gap_pct": 0}, {"gap_pct": 30},
                   {"mode": "x"}, {"hold_qty": 3}):
            with self.assertRaises(ValueError):
                build_plan(100.0, 1000.0, **kw)
        with self.assertRaises(ValueError):
            build_plan(100.0, 0.0)

    def test_korean_prices_are_whole_won(self):
        levels, _ = build_plan(71_250, 1_000_000, market="kr")
        self.assertTrue(all(lv.buy == int(lv.buy) and lv.sell == int(lv.sell)
                            for lv in levels))


class EachModeTest(unittest.TestCase):
    def start(self, price=100.5, **kw):
        api = SimAPI(price)
        trader = engine(api, **kw.pop("engine", {}))
        job = trader.add_job("soxl", 100.0, 1000.0, **kw)
        trader.step()
        return trader, api, job

    def test_buys_one_step_each_time_price_drops_a_percent(self):
        trader, api, job = self.start()
        self.assertEqual(api.orders, [])                 # 지정가보다 위
        move(trader, api, 100.0)
        self.assertEqual(api.orders, [("buy", 2, 100.0)])
        move(trader, api, 99.5)
        self.assertEqual(len(api.orders), 1)             # 다음 회차 전
        move(trader, api, 99.0)
        move(trader, api, 98.0)
        self.assertEqual([o[:2] for o in api.orders], [("buy", 2)] * 3)
        self.assertEqual((job.held, job.code), (6, "SOXL"))
        self.assertAlmostEqual(job.cost, 2 * (100 + 99 + 98))

    def test_each_step_sells_one_gap_above_its_own_buy_price(self):
        trader, api, job = self.start()
        for price in (100.0, 99.0, 98.0):
            move(trader, api, price)
        move(trader, api, 98.97)
        self.assertEqual(job.held, 6)                    # 3회차 매도가 98.98 전
        move(trader, api, 98.98)
        self.assertEqual(api.orders[-1], ("sell", 2, 98.98))
        self.assertEqual([lv.held for lv in job.levels], [2, 2, 0, 0, 0])
        move(trader, api, 101.0)                         # 2·1회차 매도가 통과
        self.assertEqual(api.orders[-1], ("sell", 4, 101.0))
        self.assertEqual(job.held, 0)
        self.assertAlmostEqual(job.realized,
                               2 * 0.98 + 2 * (101 - 99) + 2 * (101 - 100))

    def test_without_repeat_a_sold_step_is_not_bought_again(self):
        trader, api, job = self.start()
        move(trader, api, 100.0)
        move(trader, api, 101.0)
        self.assertTrue(job.levels[0].done)
        move(trader, api, 100.0)
        self.assertEqual([o[0] for o in api.orders], ["buy", "sell"])
        self.assertEqual(job.status, "진행")              # 남은 회차가 있다

    def test_finishes_after_every_step_made_one_round_trip(self):
        trader, api, job = self.start()
        for price in (100, 99, 98, 97, 96, 102):
            move(trader, api, float(price))
        self.assertEqual(job.status, "완료")
        self.assertEqual(job.held, 0)
        self.assertEqual(trader.codes(), set())

    def test_repeat_buys_the_same_step_again(self):
        trader, api, job = self.start(repeat=True)
        for price in (100.0, 101.0, 100.0, 101.0):
            move(trader, api, price)
        self.assertEqual([o[0] for o in api.orders],
                         ["buy", "sell", "buy", "sell"])
        self.assertEqual(job.levels[0].rounds, 2)
        self.assertEqual(job.status, "진행")
        self.assertAlmostEqual(job.realized, 4.0)

    def test_starting_below_the_price_buys_the_passed_steps_at_once(self):
        trader, api, job = self.start(price=97.5)
        self.assertEqual(api.orders, [("buy", 6, 97.5)])
        self.assertEqual([lv.held for lv in job.levels], [2, 2, 2, 0, 0])
        move(trader, api, 98.98)                         # 매도가는 계획 그대로
        self.assertEqual(api.orders[-1], ("sell", 2, 98.98))

    def test_spending_stays_within_the_budget(self):
        trader, api, job = self.start(price=90.0)        # 다섯 회차 모두 즉시
        spent = sum(q * p for side, q, p in api.orders if side == "buy")
        self.assertEqual(job.held, 10)
        self.assertLessEqual(spent, 1000.0)
        move(trader, api, 80.0)
        self.assertEqual(len(api.orders), 1)             # 더 사지 않는다

    def test_fees_are_taken_out_of_the_result(self):
        trader, api, job = self.start(engine={"fee_pct": 0.1})
        move(trader, api, 100.0)
        move(trader, api, 101.0)
        fee = (200 + 202) * 0.1 / 100
        self.assertAlmostEqual(job.fees, fee)
        self.assertAlmostEqual(job.realized, 2.0 - fee)

    def test_buy_pause_stops_buys_but_not_sells(self):
        trader, api, job = self.start()
        move(trader, api, 100.0)
        trader.buy_paused = True
        move(trader, api, 99.0)
        self.assertEqual(len(api.orders), 1)
        move(trader, api, 101.0)
        self.assertEqual(api.orders[-1][0], "sell")

    def test_no_stop_by_default_keeps_holding(self):
        trader, api, job = self.start(price=96.0)
        move(trader, api, 70.0)
        self.assertEqual((job.held, job.status), (10, "진행"))
        self.assertEqual([o[0] for o in api.orders], ["buy"])

    def test_optional_stop_sells_everything_and_ends(self):
        trader, api, job = self.start(stop_pct=2.0)
        for price in (100.0, 99.0):
            move(trader, api, price)
        move(trader, api, 94.09)                         # 96 × 0.98 = 94.08
        self.assertEqual(job.held, 10)                   # 남은 회차까지 샀다
        move(trader, api, 94.0)
        self.assertEqual(api.orders[-1][:2], ("sell", 10))
        self.assertEqual((job.status, job.held), ("손절", 0))
        move(trader, api, 93.0)
        self.assertEqual(api.orders[-1][0], "sell")      # 끝난 뒤에는 안 산다

    def test_no_orders_while_the_market_is_closed(self):
        api = SimAPI(99.0)
        trader = engine(api, utc_clock=lambda: US_CLOSED_UTC)
        trader.add_job("SOXL", 100.0, 1000.0)
        trader.step()
        self.assertEqual(api.orders, [])


class RequestTest(unittest.TestCase):
    def setUp(self):
        self.api = SimAPI(100.0)
        self.trader = engine(self.api)
        self.job = self.trader.add_job("SOXL", 100.0, 1000.0)
        self.trader.step()

    def test_pause_and_resume(self):
        self.trader.request("SOXL", "pause")
        move(self.trader, self.api, 99.0)
        self.assertEqual((self.job.status, len(self.api.orders)), ("멈춤", 1))
        move(self.trader, self.api, 101.0)
        self.assertEqual(len(self.api.orders), 1)        # 멈춘 동안은 팔지도 않는다
        self.trader.request("SOXL", "resume")
        move(self.trader, self.api, 101.0)
        self.assertEqual(self.api.orders[-1][0], "sell")

    def test_stop_keeps_the_shares(self):
        self.trader.request("SOXL", "stop")
        move(self.trader, self.api, 99.0)
        self.assertEqual((self.job.status, self.job.held), ("종료", 2))
        self.assertEqual(len(self.api.orders), 1)

    def test_liquidate_sells_everything_then_ends(self):
        move(self.trader, self.api, 99.0)
        self.trader.request("SOXL", "liquidate")
        move(self.trader, self.api, 99.2)
        self.assertEqual(self.api.orders[-1][:2], ("sell", 4))
        self.assertEqual((self.job.status, self.job.held), ("종료", 0))

    def test_remove_only_drops_finished_jobs(self):
        self.trader.request("SOXL", "remove")
        self.trader.step()
        self.assertIn("SOXL", self.trader.jobs)
        self.trader.request("SOXL", "stop")
        self.trader.request("SOXL", "remove")
        self.trader.step()
        self.assertNotIn("SOXL", self.trader.jobs)

    def test_one_job_per_code_and_a_job_limit(self):
        with self.assertRaisesRegex(ValueError, "이미 분할 매매 중"):
            self.trader.add_job("SOXL", 100.0, 1000.0)
        for code in ("A", "B", "C", "D"):
            self.trader.add_job(code, 100.0, 1000.0)
        with self.assertRaisesRegex(ValueError, "5종목"):
            self.trader.add_job("E", 100.0, 1000.0)


class LadderModeTest(unittest.TestCase):
    def start(self, price=100.0, **kw):
        api = SimAPI(price)
        trader = engine(api)
        job = trader.add_job("NVDA", 100.0, 1000.0, mode="ladder",
                             sell_start=105.0, **kw)
        trader.step()
        return trader, api, job

    def test_sells_a_share_of_the_holdings_at_each_step_up(self):
        trader, api, job = self.start()
        move(trader, api, 99.0)                          # 4주 보유
        sold = []
        for price in (105.0, 106.05, 107.1, 108.15):
            move(trader, api, price)
            sold.append(api.orders[-1][:2])
        self.assertEqual(sold, [("sell", 1)] * 4)        # 4주를 남은 회차로 나눔
        self.assertEqual((job.held, job.status), (0, "완료"))

    def test_eight_shares_split_two_two_two_one_one(self):
        trader, api, job = self.start(price=96.5)        # 네 회차 8주 매수
        self.assertEqual(job.held, 8)
        qtys = []
        for price in (105.0, 106.05, 107.1, 108.15, 109.2):
            move(trader, api, price)
            qtys.append(api.orders[-1][1])
        self.assertEqual(qtys, [2, 2, 2, 1, 1])
        self.assertEqual(job.held, 0)

    def test_jumping_over_steps_sells_their_shares_together(self):
        trader, api, job = self.start(price=96.0)        # 10주
        move(trader, api, 107.2)                         # 1~3회차 매도가 통과
        self.assertEqual(api.orders[-1][:2], ("sell", 6))
        self.assertEqual(job.sell_done, 3)

    def test_below_the_first_sell_price_nothing_is_sold(self):
        trader, api, job = self.start()
        move(trader, api, 104.99)
        self.assertEqual([o[0] for o in api.orders], ["buy"])

    def test_repeat_starts_over_after_everything_is_sold(self):
        trader, api, job = self.start(repeat=True)
        for price in (105.0, 106.05):
            move(trader, api, price)
        self.assertEqual((job.held, job.status, job.rounds), (0, "진행", 1))
        move(trader, api, 100.0)
        self.assertEqual(api.orders[-1][:2], ("buy", 2))
        self.assertEqual(job.sell_done, 0)


class SellOnlyTest(unittest.TestCase):
    """이미 가진 주식에 분할 매도만 건다 — 넣지 않은 수량은 건드리지 않는다."""

    def setUp(self):
        self.api = UsAPI(180.0, holdings={"NVDA": (7, 188.2)})
        self.trader = engine(self.api)

    def test_sells_only_the_shares_handed_over(self):
        job = self.trader.add_job("NVDA", 0, 0, mode="ladder", sell_start=190.0,
                                  hold_qty=5)
        self.assertEqual((job.held, job.baseline_qty), (5, 2))
        for price in (190.01, 191.91, 193.81, 195.71, 197.61):
            move(self.trader, self.api, price)
            move(self.trader, self.api, price)           # 체결 확인
        self.assertEqual(self.api.holdings["NVDA"][0], 2)    # 2주는 그대로
        self.assertEqual((job.held, job.status), (0, "완료"))
        self.assertEqual(sum(q for s, q, _ in self.api.orders if s == "sell"), 5)
        self.assertTrue(all(side == "sell" for side, _q, _p in self.api.orders))

    def test_cannot_hand_over_more_than_the_account_holds(self):
        with self.assertRaisesRegex(ValueError, "계좌 보유는 7주"):
            self.trader.add_job("NVDA", 0, 0, mode="ladder", sell_start=190.0,
                                hold_qty=8)

    def test_never_sells_shares_that_left_the_account(self):
        job = self.trader.add_job("NVDA", 0, 0, mode="ladder", sell_start=190.0,
                                  hold_qty=5)
        self.api.holdings["NVDA"] = (3, 188.2)           # 사용자가 4주를 직접 팔았다
        move(self.trader, self.api, 197.7)               # 마지막 매도가까지 통과
        self.assertEqual(self.api.orders, [("sell", 1, 197.69)])
        for _ in range(4):                               # 잔고 동기화 두 번
            move(self.trader, self.api, 197.7, seconds=61)
        self.assertEqual(len(self.api.orders), 1)        # 남의 2주는 팔지 않았다
        self.assertEqual(self.api.holdings["NVDA"][0], 2)
        self.assertEqual((job.held, job.status), (0, "완료"))


class VerifiedOrderTest(unittest.TestCase):
    """미국 지정가: 체결을 확인한 뒤에만 장부에 넣는다."""

    def start(self, price=100.0):
        api = UsAPI(price)
        api.auto_fill = False
        trader = engine(api)
        job = trader.add_job("SOXL", 100.0, 1000.0)
        trader.step()
        return trader, api, job

    def test_buy_is_booked_after_the_fill_is_seen(self):
        trader, api, job = self.start()
        self.assertEqual(api.orders, [("buy", 2, 100.0)])    # 매도호가 지정가
        self.assertEqual(job.held, 0)
        self.assertIsNotNone(job.pending)
        api.fill()
        move(trader, api, 100.0)
        self.assertEqual((job.held, job.pending), (2, None))

    def test_never_pays_more_than_the_step_price(self):
        api = UsAPI(100.0)
        api.best_bid_ask = lambda code: (99.99, 100.03)      # 매도호가가 더 높다
        trader = engine(api)
        trader.add_job("SOXL", 100.0, 1000.0)
        trader.step()
        self.assertEqual(api.orders, [])

    def test_never_sells_below_the_step_sell_price(self):
        trader, api, job = self.start()
        api.fill()
        move(trader, api, 100.0)
        move(trader, api, 101.0)                             # 매수호가 100.99
        self.assertEqual(len(api.orders), 1)
        move(trader, api, 101.01)
        self.assertEqual(api.orders[-1], ("sell", 2, 101.0))

    def test_unfilled_order_is_cancelled_and_retried_when_price_returns(self):
        trader, api, job = self.start()
        move(trader, api, 100.5, seconds=21)
        self.assertEqual(len(api.cancelled), 1)
        self.assertEqual((job.held, job.pending), (0, None))
        move(trader, api, 100.5)
        self.assertEqual(len(api.orders), 1)                 # 가격이 위에 있다
        move(trader, api, 100.0)
        self.assertEqual(len(api.orders), 2)

    def test_partial_fill_keeps_what_was_bought(self):
        trader, api, job = self.start()
        api.fill(1)
        move(trader, api, 100.0, seconds=21)
        self.assertEqual(job.held, 1)
        self.assertEqual(job.levels[0].bought, 1)
        move(trader, api, 100.0)
        self.assertEqual(api.orders[-1], ("buy", 1, 100.0))  # 모자란 1주만 다시

    def test_late_balance_is_absorbed_instead_of_left_unwatched(self):
        api = UsAPI(100.0)
        api.lag = True                    # 주문은 사라졌는데 잔고에 안 보인다
        trader = engine(api)
        job = trader.add_job("SOXL", 100.0, 1000.0)
        trader.step()
        for _ in range(3):
            move(trader, api, 100.5)
        self.assertEqual((job.held, job.missed_buy, job.pending), (0, 2, None))
        api.holdings["SOXL"] = (2, 100.0)                    # 뒤늦게 반영
        move(trader, api, 100.5, seconds=61)
        self.assertEqual((job.held, job.missed_buy), (2, 0))
        self.assertEqual(len(api.orders), 1)                 # 또 사지 않았다

    def test_user_holdings_of_the_same_code_are_left_alone(self):
        api = UsAPI(100.0, holdings={"SOXL": (30, 55.0)})
        trader = engine(api)
        job = trader.add_job("SOXL", 100.0, 1000.0)
        self.assertEqual(job.baseline_qty, 30)
        trader.step()
        move(trader, api, 100.0)
        self.assertEqual(job.held, 2)
        move(trader, api, 101.01)
        move(trader, api, 101.01)
        self.assertEqual(api.holdings["SOXL"][0], 30)
        self.assertEqual(job.held, 0)


class RestartTest(unittest.TestCase):
    def test_jobs_come_back_paused_and_resume_on_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "split.json")
            api = UsAPI(100.0)
            first = engine(api, state_path=path)
            first.add_job("SOXL", 100.0, 1000.0, repeat=True)
            first.step()
            first.step()
            self.assertEqual(first.jobs["SOXL"].held, 2)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["jobs"][0]["code"], "SOXL")

            second = engine(api, state_path=path)
            self.assertEqual(second.restore_state(), 1)
            job = second.jobs["SOXL"]
            self.assertEqual((job.status, job.held, job.repeat), ("멈춤", 2, True))
            self.assertEqual(job.levels[0].held, 2)
            self.assertEqual(second.codes(), {"SOXL"})
            move(second, api, 99.0)
            self.assertEqual(len(api.orders), 1)             # 재개 전에는 주문 없음
            second.request("SOXL", "resume")
            move(second, api, 99.0)
            self.assertEqual(api.orders[-1][:2], ("buy", 2))

    def test_buy_in_flight_at_shutdown_is_picked_up_from_the_balance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "split.json")
            api = UsAPI(100.0)
            api.auto_fill = False
            first = engine(api, state_path=path)
            first.add_job("SOXL", 100.0, 1000.0)
            first.step()                                     # 주문만 나간 채 종료
            api.fill()
            second = engine(api, state_path=path)
            second.restore_state()
            job = second.jobs["SOXL"]
            self.assertEqual((job.held, job.missed_buy), (0, 2))
            second.request("SOXL", "resume")
            move(second, api, 100.5)
            self.assertEqual((job.held, job.missed_buy), (2, 0))

    def test_real_mode_needs_explicit_permission(self):
        with self.assertRaises(PermissionError):
            SplitTrader(api=SimAPI(1.0), is_simulation=False)


if __name__ == "__main__":
    unittest.main()
