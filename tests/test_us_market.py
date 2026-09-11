"""미국주식(REST)과 미국 장 시간, [risk] 설정 테스트."""

import unittest
from datetime import datetime, timezone

from autotrader.kiwoom.live import in_us_market_hours
from autotrader.kiwoom_rest import KiwoomRestClient, KiwoomRestError, load_risk_config


class FakeTransport:
    def __init__(self, price_payload=None):
        self.calls = []
        self.price_payload = price_payload or {"return_code": 0, "cur_prc": "213.04"}

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        if url.endswith("/oauth2/token"):
            return {"token": "T", "expires_dt": ""}
        if "/api/us/stkinfo" in url:  # usa10098 거래소구분 조회
            return {"return_code": 0,
                    "list": [{"stex_tp": "ND", "stk_cd": body.get("stk_cd", "")}]}
        if "/api/us/acnt" in url:     # ust21050 미체결 (없음)
            return {"return_code": 0, "result_list": []}
        if "/api/us/mrkcond" in url:
            return self.price_payload
        if "/api/us/ordr" in url:
            return {"return_code": 0, "ord_no": "000000050"}
        raise AssertionError(f"unexpected url {url}")


class UsClientTest(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.client = KiwoomRestClient("AK", "SK", mock=True, market="us",
                                       exchange="ND", transport=self.transport)

    def test_us_price_request(self):
        price = self.client.current_price("NVDA")
        self.assertEqual(price, 213.04)
        url, headers, body = self.transport.calls[-1]
        self.assertIn("/api/us/mrkcond", url)
        self.assertEqual(headers["api-id"], "usa20100")
        self.assertEqual(body, {"stex_tp": "ND", "stk_cd": "NVDA"})

    def test_us_price_fallback_key(self):
        self.transport.price_payload = {"return_code": 0, "last_pric": "100.5"}
        self.assertEqual(self.client.current_price("NVDA"), 100.5)

    def test_us_order_uses_last_price_as_limit(self):
        self.client.current_price("NVDA")
        self.client.send_market_order("", "NVDA", "buy", 3)
        url, headers, body = self.transport.calls[-1]
        self.assertIn("/api/us/ordr", url)
        self.assertEqual(headers["api-id"], "ust20000")
        self.assertEqual(body["ord_uv"], "213.04")
        self.assertEqual(body["trde_tp"], "00")
        self.assertEqual(body["ord_qty"], "3")

        self.client.send_market_order("", "NVDA", "sell", 3)
        _, headers, body = self.transport.calls[-1]
        self.assertEqual(headers["api-id"], "ust20001")
        self.assertIn("stop_pric", body)

    def test_us_order_without_price_fails(self):
        with self.assertRaises(KiwoomRestError):
            self.client.send_market_order("", "NVDA", "buy", 1)

    def test_invalid_market_rejected(self):
        with self.assertRaises(KiwoomRestError):
            KiwoomRestClient("AK", "SK", market="jp")


class UsMarketHoursTest(unittest.TestCase):
    def test_summer_session_edt(self):
        # 2025-07-10(목) 14:00 UTC = 10:00 EDT → 장중
        self.assertTrue(in_us_market_hours(
            datetime(2025, 7, 10, 14, 0, tzinfo=timezone.utc)))
        # 13:00 UTC = 09:00 EDT → 장전
        self.assertFalse(in_us_market_hours(
            datetime(2025, 7, 10, 13, 0, tzinfo=timezone.utc)))

    def test_winter_session_est(self):
        # 2025-12-10(수) 15:00 UTC = 10:00 EST → 장중
        self.assertTrue(in_us_market_hours(
            datetime(2025, 12, 10, 15, 0, tzinfo=timezone.utc)))
        # 14:00 UTC = 09:00 EST → 장전
        self.assertFalse(in_us_market_hours(
            datetime(2025, 12, 10, 14, 0, tzinfo=timezone.utc)))

    def test_weekend_closed(self):
        # 2025-07-12(토)
        self.assertFalse(in_us_market_hours(
            datetime(2025, 7, 12, 14, 0, tzinfo=timezone.utc)))


class RiskConfigFileTest(unittest.TestCase):
    def test_reads_risk_section(self):
        import tempfile
        from pathlib import Path

        content = ("[kiwoom]\nappkey=A\nsecretkey=B\nmode=mock\n"
                   "[risk]\nmax_position_pct = 10\norder_cash_pct = 5\n"
                   "max_drawdown_pct = 8\n")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.ini"
            p.write_text(content, encoding="utf-8")
            rc = load_risk_config(p)
            self.assertAlmostEqual(rc.max_position_pct, 0.10)
            self.assertAlmostEqual(rc.order_cash_pct, 0.05)
            self.assertAlmostEqual(rc.max_drawdown_pct, 0.08)

    def test_absent_section_returns_none(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.ini"
            p.write_text("[kiwoom]\nappkey=A\nsecretkey=B\n", encoding="utf-8")
            self.assertIsNone(load_risk_config(p))


if __name__ == "__main__":
    unittest.main()
