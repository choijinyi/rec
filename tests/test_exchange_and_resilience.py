"""거래소구분 자동 인식·저가 필터·오류 생존성 테스트."""

import unittest
from datetime import datetime, timedelta

from autotrader.kiwoom.auto import AutoConfig, MultiLiveTrader
from autotrader.kiwoom_rest import KiwoomRestClient, KiwoomRestError
from autotrader.risk import RiskConfig, RiskManager
from autotrader.strategy import SMACrossoverStrategy


class ExchangeTransport:
    """순위(NY 종목 포함)·거래소조회·시세·주문을 흉내낸다."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        if url.endswith("/oauth2/token"):
            return {"token": "T", "expires_dt": ""}
        if "/api/us/rkinfo" in url:
            return {"return_code": 0, "result_list": [
                {"rank": "1", "stex_tp": "NY", "stk_cd": "ACVA",
                 "stk_nm": "아큐바", "cur_prc": "12.50", "flu_rt": "3.1",
                 "acc_trde_qty": "999"},
            ]}
        if "/api/us/stkinfo" in url:  # usa10098
            return {"return_code": 0,
                    "list": [{"stex_tp": "NA", "stk_cd": body.get("stk_cd")}]}
        if "/api/us/acnt" in url:     # ust21050 미체결 (없음)
            return {"return_code": 0, "result_list": []}
        if "/api/us/mrkcond" in url:
            return {"return_code": 0, "cur_prc": "12.50"}
        if "/api/us/ordr" in url:
            return {"return_code": 0, "ord_no": "1"}
        raise AssertionError(url)


class ExchangeResolutionTest(unittest.TestCase):
    def test_ranking_exchange_is_cached_and_used(self):
        t = ExchangeTransport()
        client = KiwoomRestClient("A", "S", market="us", transport=t)
        rows = client.top_stocks("volume")
        self.assertEqual(rows[0]["exchange"], "NY")
        client.current_price("ACVA")
        price_call = [c for c in t.calls if "mrkcond" in c[0]][-1]
        self.assertEqual(price_call[2]["stex_tp"], "NY")   # ND 고정이 아니라 NY
        client.send_market_order("", "ACVA", "buy", 1)
        order_call = [c for c in t.calls if "ordr" in c[0]][-1]
        self.assertEqual(order_call[2]["stex_tp"], "NY")

    def test_manual_code_resolves_via_usa10098(self):
        t = ExchangeTransport()
        client = KiwoomRestClient("A", "S", market="us", transport=t)
        self.assertEqual(client.resolve_exchange("SOFI"), "NA")  # 조회 결과 사용
        client.current_price("SOFI")
        price_call = [c for c in t.calls if "mrkcond" in c[0]][-1]
        self.assertEqual(price_call[2]["stex_tp"], "NA")


class FailingAPI:
    """AAA는 정상, BAD는 항상 시세 오류를 내는 API."""

    def __init__(self):
        self.orders = []
        self.calls = {"AAA": 0, "BAD": 0}

    def top_stocks(self, criteria="volume", limit=10):
        return [{"code": "BAD", "price": "10000"},
                {"code": "AAA", "price": "10000"}]

    def current_price(self, code):
        self.calls[code] += 1
        if code == "BAD":
            raise KiwoomRestError("usa20100 실패 (1903: 종목 정보가 없습니다)")
        return 100.0 + self.calls["AAA"]

    def send_market_order(self, account_no, code, side, quantity):
        self.orders.append((code, side, quantity))


class ResilienceTest(unittest.TestCase):
    def make_trader(self, api, **cfg):
        clock_state = {"now": datetime(2025, 9, 11, 10, 0, 0)}

        def clock():
            now = clock_state["now"]
            clock_state["now"] = now + timedelta(seconds=60)
            return now

        return MultiLiveTrader(
            api=api, strategy_factory=lambda: SMACrossoverStrategy(2, 3),
            config=AutoConfig(market="kr", num_symbols=2, **cfg),
            risk=RiskManager(RiskConfig(max_position_pct=1.0), 1_000_000),
            is_simulation=True, clock=clock)

    def test_bad_symbol_removed_after_3_failures_others_continue(self):
        api = FailingAPI()
        trader = self.make_trader(api)
        for _ in range(6):
            trader.step()
        self.assertNotIn("BAD", trader.symbols)   # 3회 실패 후 제외
        self.assertIn("AAA", trader.symbols)
        self.assertEqual(api.calls["BAD"], 3)
        self.assertGreaterEqual(api.calls["AAA"], 6)  # 계속 폴링됨
        self.assertIn("BAD", trader.last_error)

    def test_min_price_filters_penny_stocks(self):
        class PennyAPI(FailingAPI):
            def top_stocks(self, criteria="volume", limit=10):
                return [{"code": "PNY", "price": "0.577"},
                        {"code": "AAA", "price": "150.00"}]

        # 필터 로직은 시장 무관이므로 국내 장 시간 기준으로 검증한다
        api = PennyAPI()
        trader = self.make_trader(api, min_price=5.0)
        trader.step()
        self.assertEqual(trader.symbols, ["AAA"])   # 0.577짜리는 제외

    def test_default_min_price_kr(self):
        class CheapKrAPI(FailingAPI):
            def top_stocks(self, criteria="volume", limit=10):
                return [{"code": "CHEAP", "price": "500"},
                        {"code": "AAA", "price": "70300"}]

        api = CheapKrAPI()
        trader = self.make_trader(api)  # 기본 국내 하한 1,000원
        trader.step()
        self.assertEqual(trader.symbols, ["AAA"])


if __name__ == "__main__":
    unittest.main()
