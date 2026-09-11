"""실시간 매매 경로 테스트 (키움 API는 가짜 구현으로 대체)."""

import unittest
from datetime import datetime, timedelta

from autotrader.bars import BarAggregator
from autotrader.kiwoom.live import LiveConfig, LiveTrader, in_market_hours
from autotrader.strategy import SMACrossoverStrategy


class FakeAPI:
    """정해진 가격 시퀀스를 돌려주고, 전송된 주문을 기록하는 가짜 키움 API."""

    def __init__(self, prices):
        self.prices = list(prices)
        self.idx = 0
        self.orders = []

    def current_price(self, code):
        price = self.prices[min(self.idx, len(self.prices) - 1)]
        self.idx += 1
        return price

    def send_market_order(self, account_no, code, side, quantity):
        self.orders.append((account_no, code, side, quantity))


class BarAggregatorTest(unittest.TestCase):
    def test_completes_bar_on_interval_boundary(self):
        agg = BarAggregator("005930", interval_seconds=60)
        t0 = datetime(2025, 9, 11, 9, 0, 0)
        self.assertIsNone(agg.add_tick(t0, 100.0))
        self.assertIsNone(agg.add_tick(t0 + timedelta(seconds=30), 110.0))
        bar = agg.add_tick(t0 + timedelta(seconds=61), 105.0)
        self.assertIsNotNone(bar)
        self.assertEqual((bar.open, bar.high, bar.low, bar.close),
                         (100.0, 110.0, 100.0, 110.0))

    def test_flush_returns_partial_bar(self):
        agg = BarAggregator("005930", interval_seconds=60)
        agg.add_tick(datetime(2025, 9, 11, 9, 0, 0), 100.0)
        bar = agg.flush()
        self.assertEqual(bar.close, 100.0)
        self.assertIsNone(agg.flush())


class MarketHoursTest(unittest.TestCase):
    def test_weekday_session(self):
        self.assertTrue(in_market_hours(datetime(2025, 9, 11, 10, 0)))   # 목요일 장중
        self.assertFalse(in_market_hours(datetime(2025, 9, 11, 8, 59)))  # 장전
        self.assertFalse(in_market_hours(datetime(2025, 9, 11, 15, 31)))  # 장후
        self.assertFalse(in_market_hours(datetime(2025, 9, 13, 10, 0)))  # 토요일


class LiveTraderTest(unittest.TestCase):
    def make_trader(self, prices, **config_kwargs):
        api = FakeAPI(prices)
        config = LiveConfig(account_no="8012345611", code="005930",
                            bar_interval=60, **config_kwargs)
        clock_state = {"now": datetime(2025, 9, 11, 9, 0, 0)}

        def clock():
            now = clock_state["now"]
            clock_state["now"] = now + timedelta(seconds=60)  # 틱마다 봉 하나 완성
            return now

        trader = LiveTrader(api=api, strategy=SMACrossoverStrategy(2, 3),
                            config=config, is_simulation=True, clock=clock)
        return api, trader

    def test_refuses_real_server_without_flag(self):
        api = FakeAPI([100.0])
        config = LiveConfig(account_no="123", code="005930", allow_real=False)
        with self.assertRaises(PermissionError):
            LiveTrader(api=api, strategy=SMACrossoverStrategy(2, 3),
                       config=config, is_simulation=False)

    def test_golden_cross_sends_buy_order(self):
        # 하락 후 반등 → 단기선이 장기선을 상향 돌파 → 매수 주문
        prices = [100, 100, 100, 90, 80, 120, 130, 140, 150, 160]
        api, trader = self.make_trader(prices)
        for _ in range(len(prices)):
            trader.step()
        buy_orders = [o for o in api.orders if o[2] == "buy"]
        self.assertGreaterEqual(len(buy_orders), 1)
        self.assertEqual(buy_orders[0][1], "005930")
        self.assertGreater(buy_orders[0][3], 0)

    def test_daily_order_limit(self):
        prices = [100, 100, 100, 90, 80, 120, 130, 140, 150, 160]
        api, trader = self.make_trader(prices, max_orders_per_day=0)
        for _ in range(len(prices)):
            trader.step()
        self.assertEqual(api.orders, [])

    def test_no_trading_outside_market_hours(self):
        api = FakeAPI([100.0] * 5)
        config = LiveConfig(account_no="123", code="005930")
        night = lambda: datetime(2025, 9, 11, 22, 0, 0)
        trader = LiveTrader(api=api, strategy=SMACrossoverStrategy(2, 3),
                            config=config, is_simulation=True, clock=night)
        self.assertIsNone(trader.step())
        self.assertEqual(api.idx, 0)  # 시세 조회조차 하지 않음


if __name__ == "__main__":
    unittest.main()
