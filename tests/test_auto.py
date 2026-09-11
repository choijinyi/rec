"""자동 선정 다중 종목 매매(MultiLiveTrader) 테스트."""

import unittest
from datetime import datetime, timedelta

from autotrader.kiwoom.auto import AutoConfig, MultiLiveTrader
from autotrader.risk import RiskConfig, RiskManager
from autotrader.strategy import SMACrossoverStrategy


class FakeRankedAPI:
    """순위 + 종목별 가격 시퀀스를 제공하고 주문을 기록하는 가짜 API."""

    def __init__(self, ranking, price_seqs):
        self.ranking = ranking
        self.price_seqs = {c: list(seq) for c, seq in price_seqs.items()}
        self.idx = {c: 0 for c in price_seqs}
        self.orders = []
        self.rank_calls = 0

    def top_stocks(self, criteria="volume", limit=10):
        self.rank_calls += 1
        return self.ranking

    def current_price(self, code):
        seq = self.price_seqs[code]
        i = min(self.idx[code], len(seq) - 1)
        self.idx[code] += 1
        return seq[i]

    def send_market_order(self, account_no, code, side, quantity):
        self.orders.append((code, side, quantity))


# 하락 후 반등 → 골든크로스 매수 유도
CROSS_UP = [100, 100, 100, 90, 80, 120, 130, 140, 150, 160]
FLAT = [50] * 10


def make_trader(api, num_symbols=2, max_orders=20, market="kr"):
    clock_state = {"now": datetime(2025, 9, 11, 9, 30, 0)}

    def clock():
        now = clock_state["now"]
        clock_state["now"] = now + timedelta(seconds=60)
        return now

    return MultiLiveTrader(
        api=api,
        strategy_factory=lambda: SMACrossoverStrategy(2, 3),
        config=AutoConfig(market=market, num_symbols=num_symbols,
                          max_orders_per_day=max_orders, initial_cash=1_000_000),
        risk=RiskManager(RiskConfig(max_position_pct=1.0, order_cash_pct=0.3),
                         initial_equity=1_000_000),
        is_simulation=True,
        clock=clock,
    )


class MultiTraderTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeRankedAPI(
            ranking=[{"code": "AAA"}, {"code": "BBB"}, {"code": "CCC"}],
            price_seqs={"AAA": CROSS_UP, "BBB": FLAT, "CCC": FLAT},
        )

    def test_selects_top_n_once(self):
        trader = make_trader(self.api, num_symbols=2)
        for _ in range(3):
            trader.step()
        self.assertEqual(trader.symbols, ["AAA", "BBB"])
        self.assertEqual(self.api.rank_calls, 1)  # 선정은 1회만

    def test_buy_order_on_selected_symbol_only(self):
        trader = make_trader(self.api, num_symbols=2)
        for _ in range(len(CROSS_UP)):
            trader.step()
        buy_codes = {o[0] for o in self.api.orders if o[1] == "buy"}
        self.assertEqual(buy_codes, {"AAA"})       # 횡보 종목은 주문 없음
        self.assertNotIn("CCC", {o[0] for o in self.api.orders})

    def test_shared_cash_and_equity(self):
        trader = make_trader(self.api, num_symbols=2)
        for _ in range(len(CROSS_UP)):
            trader.step()
        self.assertLess(trader.account.cash, 1_000_000)  # 매수로 현금 감소
        self.assertGreater(trader.equity(), 0)
        qty = trader.account.positions["AAA"].quantity
        self.assertGreater(qty, 0)

    def test_daily_order_cap_shared(self):
        trader = make_trader(self.api, num_symbols=2, max_orders=0)
        for _ in range(len(CROSS_UP)):
            trader.step()
        self.assertEqual(self.api.orders, [])

    def test_refuses_real_without_flag(self):
        with self.assertRaises(PermissionError):
            MultiLiveTrader(
                api=self.api,
                strategy_factory=SMACrossoverStrategy,
                config=AutoConfig(allow_real=False),
                is_simulation=False,
            )

    def test_no_selection_outside_market_hours(self):
        night = lambda: datetime(2025, 9, 11, 22, 0, 0)
        trader = MultiLiveTrader(
            api=self.api, strategy_factory=lambda: SMACrossoverStrategy(2, 3),
            config=AutoConfig(market="kr"), is_simulation=True, clock=night)
        self.assertEqual(trader.step(), [])
        self.assertEqual(self.api.rank_calls, 0)


class UsPerCodeLimitPriceTest(unittest.TestCase):
    """미국 다중 종목에서 주문 단가가 해당 종목의 직전 시세를 쓰는지 확인."""

    def test_order_uses_own_symbol_price(self):
        from autotrader.kiwoom_rest import KiwoomRestClient

        calls = []

        def transport(url, headers, body):
            calls.append((url, headers, body))
            if url.endswith("/oauth2/token"):
                return {"token": "T", "expires_dt": ""}
            if "/api/us/stkinfo" in url:  # usa10098 거래소구분 조회
                return {"return_code": 0, "list": [{"stex_tp": "ND"}]}
            if "/api/us/acnt" in url:     # ust21050 미체결 (없음)
                return {"return_code": 0, "result_list": []}
            if "/api/us/mrkcond" in url:
                price = {"NVDA": "200.00", "TSLA": "300.00"}[body["stk_cd"]]
                return {"return_code": 0, "cur_prc": price}
            if "/api/us/ordr" in url:
                return {"return_code": 0, "ord_no": "1"}
            raise AssertionError(url)

        client = KiwoomRestClient("A", "S", market="us", transport=transport)
        client.current_price("NVDA")
        client.current_price("TSLA")   # 다른 종목을 나중에 조회해도
        client.send_market_order("", "NVDA", "buy", 1)
        order_body = calls[-1][2]
        self.assertEqual(order_body["ord_uv"], "200.00")  # NVDA 가격 사용


if __name__ == "__main__":
    unittest.main()
