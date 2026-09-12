"""키움 REST API 클라이언트 테스트 (HTTP는 가짜 transport로 대체)."""

import unittest

from autotrader.kiwoom_rest import (
    MOCK_HOST, REAL_HOST, KiwoomRestClient, KiwoomRestError, load_config,
)


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.token_count = 0

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        if url.endswith("/oauth2/token"):
            self.token_count += 1
            return {"token": f"TOKEN{self.token_count}", "expires_dt": ""}
        if "/api/dostk/stkinfo" in url:
            return {"return_code": 0, "cur_prc": "-70300"}
        if "/api/dostk/ordr" in url:
            return {"return_code": 0, "ord_no": "0000138"}
        raise AssertionError(f"unexpected url {url}")


class KiwoomRestClientTest(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.client = KiwoomRestClient("AK", "SK", mock=True,
                                       transport=self.transport)

    def test_requires_keys(self):
        with self.assertRaises(KiwoomRestError):
            KiwoomRestClient("", "", mock=True)

    def test_mock_and_real_hosts(self):
        self.assertEqual(self.client.host, MOCK_HOST)
        real = KiwoomRestClient("AK", "SK", mock=False, transport=self.transport)
        self.assertEqual(real.host, REAL_HOST)

    def test_token_issued_once_and_attached(self):
        self.client.current_price("005930")
        self.client.current_price("005930")
        self.assertEqual(self.transport.token_count, 1)  # 토큰 재사용
        url, headers, body = self.transport.calls[-1]
        self.assertEqual(headers["authorization"], "Bearer TOKEN1")
        self.assertEqual(headers["api-id"], "ka10001")
        self.assertEqual(body, {"stk_cd": "005930"})

    def test_current_price_parses_signed_value(self):
        self.assertEqual(self.client.current_price("005930"), 70300.0)

    def test_buy_and_sell_orders(self):
        self.client.send_market_order("", "005930", "buy", 10)
        url, headers, body = self.transport.calls[-1]
        self.assertIn("/api/dostk/ordr", url)
        self.assertEqual(headers["api-id"], "kt10000")
        self.assertEqual(body["stk_cd"], "005930")
        self.assertEqual(body["ord_qty"], "10")
        self.assertEqual(body["trde_tp"], "3")  # 시장가

        self.client.send_market_order("", "005930", "sell", 5)
        _, headers, body = self.transport.calls[-1]
        self.assertEqual(headers["api-id"], "kt10001")
        self.assertEqual(body["ord_qty"], "5")

    def test_error_return_code_raises(self):
        def failing(url, headers, body):
            if url.endswith("/oauth2/token"):
                return {"token": "T", "expires_dt": ""}
            return {"return_code": 8005, "return_msg": "주문가능금액 부족"}

        client = KiwoomRestClient("AK", "SK", transport=failing)
        with self.assertRaises(KiwoomRestError):
            client.send_market_order("", "005930", "buy", 10)


class LoadConfigTest(unittest.TestCase):
    def test_reads_utf8_and_cp949(self):
        import tempfile
        from pathlib import Path

        content = "[kiwoom]\nappkey = AK\nsecretkey = SK\nmode = mock\n"
        for enc in ("utf-8", "cp949"):
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "config.ini"
                p.write_bytes(content.encode(enc))
                cfg = load_config(p)
                self.assertEqual(cfg["appkey"], "AK")
                self.assertEqual(cfg["mode"], "mock")

    def test_missing_file_raises(self):
        with self.assertRaises(KiwoomRestError):
            load_config("/no/such/config.ini")


if __name__ == "__main__":
    unittest.main()


class DecodeJsonTest(unittest.TestCase):
    def test_empty_response_gives_korean_guidance(self):
        from autotrader.kiwoom_rest import _decode_json
        with self.assertRaises(KiwoomRestError) as ctx:
            _decode_json(b"", "https://mockapi.kiwoom.com/oauth2/token")
        self.assertIn("점검", str(ctx.exception))

    def test_non_json_response_shows_snippet(self):
        from autotrader.kiwoom_rest import _decode_json
        with self.assertRaises(KiwoomRestError) as ctx:
            _decode_json(b"<html>Service Unavailable</html>", "https://x/y")
        self.assertIn("JSON이 아니다", str(ctx.exception))
        self.assertIn("Service Unavailable", str(ctx.exception))

    def test_valid_json_passes(self):
        from autotrader.kiwoom_rest import _decode_json
        self.assertEqual(_decode_json(b'{"token": "T"}', "u"), {"token": "T"})
