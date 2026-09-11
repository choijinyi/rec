"""실전 세션에서 드러난 문제들의 수정 검증:
매도는 주문 한도 예외, 재매수 쿨다운, 미체결 취소, 잔고 동기화, 매수불가 제외.
"""

import unittest
from datetime import datetime, timedelta

from autotrader.broker import PaperBroker
from autotrader.engine import TradingEngine
from autotrader.kiwoom.auto import AutoConfig, MultiLiveTrader
from autotrader.kiwoom_rest import KiwoomRestClient, KiwoomRestError
from autotrader.models import Account, Bar, Side
from autotrader.risk import RiskConfig, RiskManager
from autotrader.strategy import Signal, Strategy


def make_bar(close, day=1):
    return Bar("TEST", datetime(2025, 9, day), close, close, close, close, 100)


class BuyEveryBar(Strategy):
    def on_bar(self, bar):
        return Signal.BUY


class SellCapExemptTest(unittest.TestCase):
    def test_stop_loss_sell_runs_even_when_buy_blocked(self):
        risk = RiskManager(RiskConfig(stop_loss_pct=0.03), 1_000_000)
        engine = TradingEngine(BuyEveryBar(), PaperBroker(0, 0), risk,
                               Account(cash=0))
        pos = engine.account.position("TEST")
        pos.quantity, pos.avg_price = 10, 100.0
        fill = engine.process_bar(make_bar(90.0), allow_buy=False)  # 한도 소진 상황
        self.assertIsNotNone(fill)
        self.assertIs(fill.side, Side.SELL)          # 손절은 그대로 나간다
        self.assertEqual(pos.quantity, 0)

    def test_buy_blocked_when_not_allowed(self):
        risk = RiskManager(RiskConfig(max_position_pct=1.0, order_cash_pct=0.5),
                           1_000_000)
        engine = TradingEngine(BuyEveryBar(), PaperBroker(0, 0), risk,
                               Account(cash=1_000_000))
        self.assertIsNone(engine.process_bar(make_bar(100.0), allow_buy=False))
        self.assertIsNotNone(engine.process_bar(make_bar(100.0), allow_buy=True))


class CooldownTest(unittest.TestCase):
    def test_no_immediate_rebuy_after_sell(self):
        risk = RiskManager(
            RiskConfig(max_position_pct=1.0, order_cash_pct=0.5,
                       stop_loss_pct=0.03, reentry_cooldown_bars=3),
            1_000_000)
        engine = TradingEngine(BuyEveryBar(), PaperBroker(0, 0), risk,
                               Account(cash=1_000_000))
        buy = engine.process_bar(make_bar(100.0, 1))
        self.assertIs(buy.side, Side.BUY)
        stop = engine.process_bar(make_bar(90.0, 2))     # 손절
        self.assertIs(stop.side, Side.SELL)
        # 쿨다운 3봉: 이후 3개의 봉에서는 매수 신호가 있어도 사지 않는다
        self.assertIsNone(engine.process_bar(make_bar(91.0, 3)))
        self.assertIsNone(engine.process_bar(make_bar(92.0, 4)))
        self.assertIsNone(engine.process_bar(make_bar(93.0, 5)))
        rebuy = engine.process_bar(make_bar(94.0, 6))    # 4번째 봉부터 허용
        self.assertIsNotNone(rebuy)
        self.assertIs(rebuy.side, Side.BUY)


class CancelBeforeOrderTest(unittest.TestCase):
    def test_us_order_cancels_open_orders_first(self):
        calls = []

        def transport(url, headers, body):
            calls.append((headers.get("api-id"), body))
            api_id = headers.get("api-id")
            if url.endswith("/oauth2/token"):
                return {"token": "T", "expires_dt": ""}
            if api_id == "usa20100":
                return {"return_code": 0, "cur_prc": "10.00"}
            if api_id == "usa10098":
                return {"return_code": 0, "list": [{"stex_tp": "ND"}]}
            if api_id == "ust21050":   # 미체결 2건
                return {"return_code": 0, "result_list": [
                    {"ord_no": "111", "stk_cd": "ACVA"},
                    {"ord_no": "222", "stk_cd": "ACVA"},
                    {"ord_no": "333", "stk_cd": "OTHER"}]}
            if api_id in ("ust20003", "ust20001", "ust20000"):
                return {"return_code": 0, "ord_no": "9"}
            raise AssertionError(api_id)

        client = KiwoomRestClient("A", "S", market="us", transport=transport)
        client.current_price("ACVA")
        client.send_market_order("", "ACVA", "sell", 5)
        ids = [c[0] for c in calls]
        cancels = [c for c in calls if c[0] == "ust20003"]
        self.assertEqual(len(cancels), 2)  # 자기 종목 미체결만 취소
        self.assertEqual({c[1]["orig_ord_no"] for c in cancels}, {"111", "222"})
        self.assertLess(ids.index("ust20003"), ids.index("ust20001"))


class FakeUsAPI:
    """자동매매 루프용: 잔고 조회를 지원하는 가짜 미국주식 API."""

    def __init__(self):
        self.balances = {}

    def top_stocks(self, criteria="volume", limit=10):
        return [{"code": "AAA", "price": "10.00"}]

    def current_price(self, code):
        return 10.0

    def send_market_order(self, account_no, code, side, quantity):
        pass

    def us_balances(self):
        return self.balances


class ReconcileTest(unittest.TestCase):
    def make_trader(self, api):
        return MultiLiveTrader(
            api=api,
            strategy_factory=BuyEveryBar,
            config=AutoConfig(market="us", num_symbols=1, initial_cash=1000),
            risk=RiskManager(RiskConfig(), 1000),
            is_simulation=True,
            utc_clock=lambda: datetime(2025, 7, 10, 14, 0,
                                       tzinfo=__import__("datetime").timezone.utc),
        )

    def test_phantom_position_removed_and_cash_refunded(self):
        api = FakeUsAPI()
        trader = self.make_trader(api)
        trader.step()  # 선정
        pos = trader.account.position("AAA")
        pos.quantity, pos.avg_price = 9, 10.0   # 장부상 9주 (실제 미체결)
        trader.account.cash = 910.0
        api.balances = {}                        # 실제 계좌엔 0주
        trader.reconcile_positions()
        self.assertEqual(pos.quantity, 0)
        self.assertAlmostEqual(trader.account.cash, 1000.0)  # 현금 환급

    def test_matching_balance_untouched(self):
        api = FakeUsAPI()
        trader = self.make_trader(api)
        trader.step()
        pos = trader.account.position("AAA")
        pos.quantity, pos.avg_price = 5, 10.0
        api.balances = {"AAA": {"qty": 5, "sellable": 5}}
        cash_before = trader.account.cash
        trader.reconcile_positions()
        self.assertEqual(pos.quantity, 5)
        self.assertEqual(trader.account.cash, cash_before)


class UnbuyableSymbolTest(unittest.TestCase):
    def test_unbuyable_symbol_dropped(self):
        class RejectingAPI(FakeUsAPI):
            def top_stocks(self, criteria="volume", limit=10):
                return [{"code": "ETHA", "price": "20.00"}]

            def send_market_order(self, account_no, code, side, quantity):
                raise KiwoomRestError("ust20000 실패: [2000](571242:매수 불가한 종목입니다)")

        clock_state = {"now": datetime(2025, 9, 11, 10, 0, 0)}

        def clock():
            now = clock_state["now"]
            clock_state["now"] = now + timedelta(seconds=60)
            return now

        trader = MultiLiveTrader(
            api=RejectingAPI(), strategy_factory=BuyEveryBar,
            config=AutoConfig(market="kr", num_symbols=1, initial_cash=1000),
            risk=RiskManager(RiskConfig(max_position_pct=1.0, order_cash_pct=1.0), 1000),
            is_simulation=True, clock=clock)
        for _ in range(3):
            trader.step()
        self.assertNotIn("ETHA", trader.symbols)


if __name__ == "__main__":
    unittest.main()
