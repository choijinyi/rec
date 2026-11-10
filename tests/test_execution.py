"""실전 결과(2026-11-10)에서 고친 것: 청산 매도 가격, 추격 대기 시간, 손익 표시
(수수료·실제 체결가), 청산 사유 이름, AI 호출 간격."""

import unittest
from datetime import datetime, timezone

try:                                   # unittest discover -s tests
    from test_fill_verification import FakeUsAPI, advance, make
except ImportError:                    # python -m unittest tests.test_execution
    from tests.test_fill_verification import FakeUsAPI, advance, make

from autotrader.scalper import ScalpConfig, Scalper

SOXL = [{"code": "SOXL", "price": "54.23", "change_pct": "3.0",
         "volume": "80000000"}]


def entered(api, **kw):
    trader = make(api, **kw)
    trader._scan_worker(1)
    trader.step()            # 매수 주문
    trader.step()            # 매수 체결 확인
    return trader


class ExitLimitTest(unittest.TestCase):
    """내려가는 중의 매도는 매수호가에 맞춰 바로 체결되게 낸다."""

    def setUp(self):
        self.api = FakeUsAPI({"SOXL": 54.23}, SOXL)
        self.api.best_bid_ask = lambda code: self.quote
        self.quote = (54.22, 54.23)
        self.trader = entered(self.api, max_spread_pct=0, stop_pct=1.0,
                              target_pct=1.5)

    def test_stop_sell_goes_to_the_bid(self):
        self.quote = (53.60, 53.66)
        self.api.prices["SOXL"] = 53.65           # 손절가(53.69) 아래
        self.trader.step()
        code, side, qty, limit = self.api.orders[-1]
        self.assertEqual((code, side), ("SOXL", "sell"))
        self.assertEqual(limit, 53.60)            # 직전 체결가가 아니라 매수호가

    def test_bid_far_below_price_is_floored(self):
        self.quote = (52.00, 53.66)               # 호가가 텅 빈 상황
        self.api.prices["SOXL"] = 53.65
        self.trader.step()
        self.assertAlmostEqual(self.api.orders[-1][3], 53.65 * 0.995)

    def test_quote_failure_prices_slightly_below_last(self):
        def broken(code):
            raise RuntimeError("1632")
        self.api.best_bid_ask = broken
        self.api.prices["SOXL"] = 53.65
        self.trader.step()
        self.assertAlmostEqual(self.api.orders[-1][3], 53.65 * 0.999)

    def test_take_profit_also_sells_into_the_bid(self):
        self.quote = (55.08, 55.10)
        self.api.prices["SOXL"] = 55.10           # 목표가(55.04) 도달
        self.trader.step()
        self.assertEqual(self.api.orders[-1][3], 55.08)
        self.assertEqual(self.trader.positions["SOXL"].exit_reason, "익절")

    def test_unfilled_exit_is_chased_after_15_seconds(self):
        self.api.auto_fill = False
        self.api.prices["SOXL"] = 53.65
        self.trader.step()
        sells = [o for o in self.api.orders if o[1] == "sell"]
        self.assertEqual(len(sells), 1)
        advance(self.trader, 10)
        self.trader.step()
        self.assertEqual(len([o for o in self.api.orders if o[1] == "sell"]), 1)
        advance(self.trader, 10)                  # 20초 경과 → 취소하고 추격
        self.trader.step()
        self.assertEqual(len([o for o in self.api.orders if o[1] == "sell"]), 2)


class ExitLabelTest(unittest.TestCase):
    def test_protected_profit_that_ends_in_loss_is_labelled(self):
        # 손절가를 본전 위로 올렸지만 주문이 늦게 받아져 손실로 끝난 경우
        api = FakeUsAPI({"SOXL": 54.23}, SOXL)
        trader = entered(api, stop_pct=0.8, target_pct=3.0)
        api.prices["SOXL"] = 54.70                # +1R → 본전 확보(54.39)
        trader.step()
        self.assertTrue(trader.positions["SOXL"].be_moved)
        api.auto_fill = False
        api.prices["SOXL"] = 54.38
        trader.step()                             # 수익 보전 매도 @54.38 (미체결)
        self.assertEqual(trader.positions["SOXL"].exit_reason, "수익 보전")
        api.prices["SOXL"] = 54.30
        advance(trader, 20)
        trader.step()                             # 2차 추격 매도 @54.14 부근
        api.fill_all()
        api.balances = {"SOXL": {"qty": 0, "sellable": 0}}
        trader.step()
        trade = trader.trades[-1]
        self.assertLess(trade.pnl, 0)
        self.assertEqual(trade.reason, "수익 보전 미체결 손실")


class FeeTest(unittest.TestCase):
    def test_fees_are_subtracted_from_pnl_and_cash(self):
        api = FakeUsAPI({"SOXL": 50.0}, SOXL)
        trader = entered(api, fee_pct=0.07, total_cash=1000)
        qty = trader.positions["SOXL"].quantity          # 20주
        api.prices["SOXL"] = 51.0                        # 목표(+1.5%) 도달
        trader.step()
        api.balances = {"SOXL": {"qty": 0, "sellable": 0}}
        trader.step()
        trade = trader.trades[-1]
        fee = (50.0 + 51.0) * qty * 0.0007
        self.assertAlmostEqual(trade.fee, fee)
        self.assertAlmostEqual(trade.pnl, 1.0 * qty - fee)
        self.assertAlmostEqual(trader.cash, 1000 + 1.0 * qty - fee)
        self.assertAlmostEqual(trader.realized_pnl, trade.pnl)

    def test_scratch_trade_is_a_loss_after_fees(self):
        api = FakeUsAPI({"SOXL": 50.0}, SOXL)
        trader = entered(api, fee_pct=0.07, total_cash=1000, stale_min=1)
        api.prices["SOXL"] = 50.01
        advance(trader, 120)                             # 시간초과로 본전 청산
        trader.step()
        api.balances = {"SOXL": {"qty": 0, "sellable": 0}}
        trader.step()
        self.assertLess(trader.trades[-1].pnl, 0)
        self.assertEqual(trader.losses, 1)

    def test_sell_tax(self):
        api = FakeUsAPI({"SOXL": 50.0}, SOXL)
        trader = entered(api, fee_pct=0.0, sell_tax_pct=0.2, total_cash=1000)
        qty = trader.positions["SOXL"].quantity
        api.prices["SOXL"] = 51.0
        trader.step()
        api.balances = {"SOXL": {"qty": 0, "sellable": 0}}
        trader.step()
        self.assertAlmostEqual(trader.trades[-1].fee, 51.0 * qty * 0.002)

    def test_no_fee_by_default_in_engine(self):
        self.assertEqual(ScalpConfig().fee_pct, 0.0)


class FillPriceTest(unittest.TestCase):
    """손익을 주문가가 아니라 실제 체결가로 기록한다."""

    def _api(self, fills):
        api = FakeUsAPI({"SOXL": 50.0}, SOXL)
        api.fills = fills

        def order_fill(ord_no):
            value = api.fills.get(int(ord_no))
            if isinstance(value, Exception):
                raise value
            return value
        api.order_fill = order_fill
        return api

    def test_entry_and_exit_use_actual_fills(self):
        api = self._api({1: (20, 49.96), 2: (20, 51.05)})
        trader = entered(api, total_cash=1000)
        pos = trader.positions["SOXL"]
        self.assertAlmostEqual(pos.entry_price, 49.96)
        self.assertAlmostEqual(trader.cash, 1000 - 20 * 49.96)
        self.assertAlmostEqual(pos.stop_price, 48.5)      # 계획한 손절가 그대로
        api.prices["SOXL"] = 51.0
        trader.step()
        api.balances = {"SOXL": {"qty": 0, "sellable": 0}}
        trader.step()
        trade = trader.trades[-1]
        self.assertAlmostEqual(trade.exit_price, 51.05)
        self.assertAlmostEqual(trade.pnl, (51.05 - 49.96) * 20)

    def test_lookup_failure_falls_back_to_order_price(self):
        api = self._api({1: RuntimeError("1632"), 2: None})
        trader = entered(api, total_cash=1000)
        self.assertAlmostEqual(trader.positions["SOXL"].entry_price, 50.0)
        api.prices["SOXL"] = 51.0
        trader.step()
        api.balances = {"SOXL": {"qty": 0, "sellable": 0}}
        trader.step()
        self.assertAlmostEqual(trader.trades[-1].exit_price, 51.0)

    def test_implausible_fill_price_is_ignored(self):
        api = self._api({1: (20, 5.0)})                  # 주문가와 동떨어진 값
        trader = entered(api, total_cash=1000)
        self.assertAlmostEqual(trader.positions["SOXL"].entry_price, 50.0)


class ScanBackoffTest(unittest.TestCase):
    """AI가 '살 자리 없음'을 이어서 답하면 묻는 간격을 늘린다."""

    def _trader(self, answer):
        api = FakeUsAPI({"SOXL": 50.0}, SOXL)
        clock = datetime(2025, 10, 6, 23, 0)
        trader = Scalper(
            api=api, config=ScalpConfig(market="us", total_cash=1000,
                                        min_dollar_volume=0, scan_interval=120.0,
                                        open_delay_min=0),
            selector=lambda rows, n: answer(), is_simulation=True,
            clock=lambda: clock,
            utc_clock=lambda: datetime(2025, 10, 6, 14, 0, tzinfo=timezone.utc))
        delays = []
        trader._retry_scan_soon = lambda seconds=30.0: delays.append(seconds)
        return trader, delays

    def test_interval_grows_then_caps(self):
        trader, delays = self._trader(lambda: ([], "자리 없음"))
        for _ in range(6):
            trader._scan_worker(1)
        self.assertEqual(delays, [180.0, 240.0, 300.0, 360.0, 360.0, 360.0])

    def test_ai_failure_also_backs_off(self):
        def boom():
            raise RuntimeError("usage limit")
        trader, delays = self._trader(boom)
        trader._scan_worker(1)
        trader._scan_worker(1)
        self.assertEqual(delays, [180.0, 240.0])

    def test_a_plan_resets_the_interval(self):
        answers = [([], ""), ([], ""), (["SOXL"], ""), ([], "")]
        trader, delays = self._trader(lambda: answers.pop(0))
        for _ in range(4):
            trader._scan_worker(1)
        self.assertEqual(delays, [180.0, 240.0, 180.0])


if __name__ == "__main__":
    unittest.main()
