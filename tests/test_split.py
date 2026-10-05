"""분할매매(SplitTrader) 핵심 로직 테스트."""

import unittest
from datetime import datetime, timedelta, timezone

from autotrader.split import (
    SplitConfig, SplitTrader, in_kr_market_hours, in_us_day_session,
    in_us_market_hours,
)


class FakeAPI:
    """가격 시퀀스를 재생하고 주문을 기록하는 가짜 API."""

    def __init__(self, prices):
        self.prices = list(prices)
        self.idx = 0
        self.orders = []

    def current_price(self, code):
        price = self.prices[min(self.idx, len(self.prices) - 1)]
        self.idx += 1
        return price

    def send_market_order(self, account_no, code, side, quantity):
        self.orders.append((code, side, quantity))


def make_trader(prices, buy_start=10_000, sell_start=10_500, step=5.0,
                nbuy=3, nsell=2, cash=3_000_000, market="kr"):
    clock_state = {"now": datetime(2025, 10, 6, 10, 0, 0)}  # 월요일 장중

    def clock():
        now = clock_state["now"]
        clock_state["now"] = now + timedelta(seconds=10)
        return now

    api = FakeAPI(prices)
    trader = SplitTrader(
        api=api,
        config=SplitConfig(code="005930", buy_start_price=buy_start,
                           sell_start_price=sell_start, step_pct=step,
                           buy_splits=nbuy, sell_splits=nsell,
                           total_cash=cash, market=market),
        is_simulation=True,
        clock=clock,
    )
    return trader, api


class LevelPlanTest(unittest.TestCase):
    def test_levels_built_from_start_prices(self):
        trader, _ = make_trader([], buy_start=10_000, sell_start=10_500,
                                step=5.0, nbuy=3, nsell=2)
        self.assertEqual([l.price for l in trader.buy_levels],
                         [10_000, 9_500, 9_000])      # 매수 시작가, -5%, -10%
        self.assertEqual([l.price for l in trader.sell_levels],
                         [10_500, 11_025])            # 매도 시작가, +5%

    def test_invalid_config_rejected(self):
        for kwargs in ({"buy_start": 0}, {"sell_start": 0},
                       {"sell_start": 9_000},  # 매도 시작가 <= 매수 시작가
                       {"step": 0}, {"step": 100},
                       {"nbuy": 0}, {"nsell": 0}, {"cash": 0}):
            with self.assertRaises(ValueError):
                make_trader([], **kwargs)

    def test_refuses_real_without_flag(self):
        with self.assertRaises(PermissionError):
            SplitTrader(
                api=FakeAPI([]),
                config=SplitConfig(code="005930", buy_start_price=1000,
                                   sell_start_price=1100, step_pct=5,
                                   allow_real=False),
                is_simulation=False,
            )


class BuyLadderTest(unittest.TestCase):
    def test_first_buy_at_base_price(self):
        trader, api = make_trader([10_000])
        trader.step()
        self.assertEqual(api.orders, [("005930", "buy", 100)])  # 100만/10000
        self.assertTrue(trader.buy_levels[0].done)
        self.assertEqual(trader.position_qty, 100)

    def test_no_buy_above_base(self):
        trader, api = make_trader([10_100])
        trader.step()
        self.assertEqual(api.orders, [])

    def test_second_buy_after_step_down(self):
        trader, api = make_trader([10_000, 9_700, 9_500])
        trader.step()                     # 1차 매수
        trader.step()                     # 9,700: 레벨 사이, 매수 없음
        self.assertEqual(len(api.orders), 1)
        trader.step()                     # 9,500: 2차 매수
        self.assertEqual(api.orders[-1], ("005930", "buy", 105))  # 100만/9500
        self.assertEqual([l.done for l in trader.buy_levels],
                         [True, True, False])

    def test_gap_down_fills_multiple_levels(self):
        trader, api = make_trader([8_900])  # 3개 레벨 모두 아래
        trader.step()
        self.assertEqual(len(api.orders), 3)
        self.assertTrue(all(l.done for l in trader.buy_levels))
        # 각 분할 몫을 더 낮은 가격에 샀으므로 수량은 몫당 100만/8900 = 112
        self.assertEqual(api.orders[0][2], 112)

    def test_each_level_fires_once(self):
        trader, api = make_trader([10_000, 10_000, 9_999])
        for _ in range(3):
            trader.step()
        self.assertEqual(len(api.orders), 1)

    def test_skip_level_when_cash_per_split_too_small(self):
        trader, api = make_trader([10_000], cash=15_000, nbuy=3)  # 몫 5,000
        trader.step()
        self.assertEqual(api.orders, [])
        self.assertTrue(trader.buy_levels[0].done)
        self.assertIn("건너뜀", trader.buy_levels[0].note)


class SellLadderTest(unittest.TestCase):
    def test_sell_after_step_up(self):
        trader, api = make_trader([10_000, 10_500], nsell=2)
        trader.step()                     # 1차 매수 100주
        trader.step()                     # +5%: 1차 매도 (100/2=50주)
        self.assertEqual(api.orders[-1], ("005930", "sell", 50))
        self.assertEqual(trader.position_qty, 50)
        self.assertAlmostEqual(trader.realized_pnl, 50 * 500)

    def test_last_sell_clears_position(self):
        trader, api = make_trader([10_000, 10_500, 11_100], nsell=2)
        for _ in range(3):
            trader.step()
        self.assertEqual(trader.position_qty, 0)
        self.assertEqual(api.orders[-1], ("005930", "sell", 50))
        self.assertAlmostEqual(trader.realized_pnl, 50 * 500 + 50 * 1100)

    def test_no_sell_without_position(self):
        trader, api = make_trader([10_600])   # 매수 없이 상승
        trader.step()
        self.assertEqual(api.orders, [])

    def test_gap_up_fires_multiple_sell_levels(self):
        trader, api = make_trader([10_000, 11_200], nsell=2)
        trader.step()
        trader.step()                     # 두 매도 레벨 동시 통과
        sells = [o for o in api.orders if o[1] == "sell"]
        self.assertEqual([q for _, _, q in sells], [50, 50])
        self.assertEqual(trader.position_qty, 0)


class CompletionTest(unittest.TestCase):
    def test_completes_when_all_bought_and_sold(self):
        # 모든 매수 후 모든 매도까지 끝나면 완료로 멈춘다
        trader, api = make_trader([8_900, 11_200], nbuy=3, nsell=2)
        trader.step()                     # 매수 3분할 전부
        trader.step()                     # 매도 2분할 전부
        self.assertTrue(trader.completed)
        self.assertEqual(trader.position_qty, 0)
        trader.step()                     # 완료 후에는 더 집행하지 않는다
        self.assertEqual(len(api.orders), 5)

    def test_remaining_sells_closed_when_no_position(self):
        trader, api = make_trader([10_000, 10_500, 11_100], nsell=2)
        for _ in range(3):
            trader.step()
        self.assertTrue(all(l.done for l in trader.sell_levels))
        self.assertTrue(trader.completed)


class MarketHoursTest(unittest.TestCase):
    def test_kr_hours(self):
        self.assertTrue(in_kr_market_hours(datetime(2025, 10, 6, 10, 0)))
        self.assertFalse(in_kr_market_hours(datetime(2025, 10, 6, 16, 0)))
        self.assertFalse(in_kr_market_hours(datetime(2025, 10, 5, 10, 0)))  # 일요일

    def test_us_hours_and_day_session(self):
        # 2025-10-06(월)은 서머타임: 정규장 13:30~20:00 UTC
        utc = timezone.utc
        self.assertTrue(in_us_market_hours(datetime(2025, 10, 6, 14, 0, tzinfo=utc)))
        self.assertFalse(in_us_market_hours(datetime(2025, 10, 6, 12, 0, tzinfo=utc)))
        # 주간거래(서머타임): KST 09~17 = UTC 00~08
        self.assertTrue(in_us_day_session(datetime(2025, 10, 6, 3, 0, tzinfo=utc)))
        self.assertFalse(in_us_day_session(datetime(2025, 10, 6, 9, 0, tzinfo=utc)))

    def test_no_trade_outside_market_hours(self):
        api = FakeAPI([10_000])
        trader = SplitTrader(
            api=api,
            config=SplitConfig(code="005930", buy_start_price=10_000,
                               sell_start_price=10_500, step_pct=5),
            is_simulation=True,
            clock=lambda: datetime(2025, 10, 6, 22, 0, 0),  # 장 마감 후
        )
        trader.step()
        self.assertEqual(api.orders, [])
        self.assertEqual(api.idx, 0)  # 시세 조회도 하지 않는다


class ReconcileTest(unittest.TestCase):
    def test_us_balance_overrides_book(self):
        class UsAPI(FakeAPI):
            def us_balances(self):
                return {"AAPL": {"qty": 2, "sellable": 2}}

        api = UsAPI([100.0])
        trader = SplitTrader(
            api=api,
            config=SplitConfig(code="AAPL", buy_start_price=100,
                               sell_start_price=110, step_pct=5,
                               buy_splits=1, sell_splits=1, total_cash=1000,
                               market="us"),
            is_simulation=True,
            utc_clock=lambda: datetime(2025, 10, 6, 14, 0, tzinfo=timezone.utc),
        )
        trader.step()                       # 10주 매수 (장부)
        self.assertEqual(trader.position_qty, 10)
        trader.reconcile()                  # 실제는 2주만 체결
        self.assertEqual(trader.position_qty, 2)
        self.assertAlmostEqual(trader.cash, 1000 - 2 * 100)


if __name__ == "__main__":
    unittest.main()
