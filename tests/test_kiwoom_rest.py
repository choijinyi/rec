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


class KrRankingAndNxtOrderTest(unittest.TestCase):
    def _client(self):
        def transport(url, headers, body):
            self.calls.append((url, headers, body))
            if url.endswith("/oauth2/token"):
                return {"token": "T", "expires_dt": ""}
            if "/api/dostk/rkinfo" in url:
                return {"return_code": 0, "result_list": [
                    {"stk_cd": "114800_AL", "stk_nm": "KODEX 인버스",
                     "cur_prc": "1052", "flu_rt": "0.5", "trde_qty": "1000"},
                    {"stk_cd": "005930", "stk_nm": "삼성전자",
                     "cur_prc": "70000", "flu_rt": "1.0", "trde_qty": "900"},
                ]}
            if "/api/dostk/stkinfo" in url:
                return {"return_code": 0, "cur_prc": "70000"}
            if "/api/dostk/ordr" in url:
                return {"return_code": 0, "ord_no": "1"}
            raise AssertionError(url)

        self.calls = []
        from autotrader.kiwoom_rest import KiwoomRestClient
        return KiwoomRestClient("A", "S", market="kr", transport=transport)

    def test_ranking_strips_nxt_suffix(self):
        rows = self._client().top_stocks("volume", limit=10)
        self.assertEqual([r["code"] for r in rows], ["114800", "005930"])

    def test_krx_order_is_market_type(self):
        client = self._client()
        client.send_market_order("", "005930", "buy", 3, kr_exchange="KRX")
        body = self.calls[-1][2]
        self.assertEqual(body["dmst_stex_tp"], "KRX")
        self.assertEqual(body["trde_tp"], "3")
        self.assertEqual(body["ord_uv"], "")

    def test_nxt_order_uses_limit_at_last_price(self):
        client = self._client()
        client.current_price("005930")   # 직전 시세 캐시
        client.send_market_order("", "005930", "sell", 3, kr_exchange="NXT")
        body = self.calls[-1][2]
        self.assertEqual(body["dmst_stex_tp"], "NXT")
        self.assertEqual(body["trde_tp"], "0")       # 보통(지정가)
        self.assertEqual(body["ord_uv"], "70000")

    def test_nxt_order_without_price_raises(self):
        with self.assertRaises(KiwoomRestError):
            self._client().send_market_order("", "005930", "buy", 1,
                                             kr_exchange="NXT")


class UsQuoteFallbackTest(unittest.TestCase):
    """usa20100 서버 오류 시 거래소 재확인 → usa20101(10호가)로 대체."""

    def _client(self, fail_20100=True, bad_cached_exchange=None):
        self.calls = []

        def transport(url, headers, body):
            api_id = headers.get("api-id", "")
            self.calls.append((api_id, dict(body)))
            if url.endswith("/oauth2/token"):
                return {"token": "T", "expires_dt": ""}
            if api_id == "usa10098":
                return {"return_code": 0, "list": [{"stex_tp": "ND"}]}
            if api_id == "usa20100":
                if fail_20100:
                    return {"return_code": 7,
                            "return_msg": "서비스를 처리하는 중에 오류 [1632]"}
                return {"return_code": 0, "cur_prc": "48.53"}
            if api_id == "usa20101":
                return {"return_code": 0, "cur_prc": "48.60"}
            raise AssertionError(api_id)

        from autotrader.kiwoom_rest import KiwoomRestClient
        client = KiwoomRestClient("A", "S", market="us", transport=transport)
        if bad_cached_exchange:
            client._exchanges["SOXL"] = bad_cached_exchange
        return client

    def test_falls_back_to_bid_quote(self):
        client = self._client(fail_20100=True)
        self.assertEqual(client.current_price("SOXL"), 48.60)
        ids = [c[0] for c in self.calls]
        self.assertIn("usa20101", ids)

    def test_prefers_bid_quote_after_fallback(self):
        client = self._client(fail_20100=True)
        client.current_price("SOXL")
        n_20100 = sum(1 for c in self.calls if c[0] == "usa20100")
        client.current_price("SOXL")      # 2분간 usa20101 우선
        self.assertEqual(sum(1 for c in self.calls if c[0] == "usa20100"),
                         n_20100)         # usa20100 추가 호출 없음
        self.assertEqual(self.calls[-1][0], "usa20101")

    def test_bad_cached_exchange_is_refreshed(self):
        client = self._client(fail_20100=True, bad_cached_exchange="XX")
        client.current_price("SOXL")
        resolves = [c for c in self.calls if c[0] == "usa10098"]
        self.assertTrue(resolves)                       # 거래소 재확인
        self.assertEqual(client._exchanges["SOXL"], "ND")

    def test_no_fallback_when_primary_works(self):
        client = self._client(fail_20100=False)
        self.assertEqual(client.current_price("SOXL"), 48.53)
        ids = [c[0] for c in self.calls]
        self.assertNotIn("usa20101", ids)

    def test_ranking_ignores_unknown_exchange_format(self):
        def transport(url, headers, body):
            if url.endswith("/oauth2/token"):
                return {"token": "T", "expires_dt": ""}
            if "/api/us/rkinfo" in url:
                return {"return_code": 0, "result_list": [
                    {"stk_cd": "SOXL", "cur_prc": "48", "flu_rt": "2",
                     "trde_qty": "100", "stex_tp": "2"},      # 비표준 형식
                    {"stk_cd": "NVDA", "cur_prc": "180", "flu_rt": "1",
                     "trde_qty": "90", "stex_tp": "ND"},
                ]}
            raise AssertionError(url)

        from autotrader.kiwoom_rest import KiwoomRestClient
        client = KiwoomRestClient("A", "S", market="us", transport=transport)
        client.top_stocks("volume")
        self.assertNotIn("SOXL", client._exchanges)     # 오염 안 됨
        self.assertEqual(client._exchanges["NVDA"], "ND")


class RateLimitTest(unittest.TestCase):
    def test_consecutive_requests_are_spaced(self):
        import time as _time

        from autotrader import kiwoom_rest as kr

        kr._last_request_at = 0.0
        start = _time.monotonic()
        kr._rate_limit_wait()
        kr._rate_limit_wait()
        kr._rate_limit_wait()
        elapsed = _time.monotonic() - start
        self.assertGreaterEqual(elapsed, 2 * kr._MIN_REQUEST_INTERVAL * 0.9)


class UsTickFallbackTest(unittest.TestCase):
    """mrkcond(20100·20101)가 통째로 죽어도 틱차트(usa06010)로 시세를 받는다."""

    def _client(self):
        self.calls = []

        def transport(url, headers, body):
            api_id = headers.get("api-id", "")
            self.calls.append(api_id)
            if url.endswith("/oauth2/token"):
                return {"token": "T", "expires_dt": ""}
            if api_id == "usa10098":
                return {"return_code": 0, "list": [{"stex_tp": "ND"}]}
            if api_id in ("usa20100", "usa20101"):
                return {"return_code": 7, "return_msg": "오류 [1632]"}
            if api_id == "usa06010":
                return {"return_code": 0, "result_list": [
                    {"cur_prc": "48.70", "cntr_tm": "235900"}]}
            raise AssertionError(api_id)

        from autotrader.kiwoom_rest import KiwoomRestClient
        return KiwoomRestClient("A", "S", market="us", transport=transport)

    def test_tick_chart_serves_price(self):
        client = self._client()
        self.assertEqual(client.current_price("SOXL"), 48.70)
        self.assertIn("usa06010", self.calls)

    def test_tick_preferred_after_success(self):
        client = self._client()
        client.current_price("SOXL")
        before = sum(1 for c in self.calls if c in ("usa20100", "usa20101"))
        client.current_price("SOXL")        # 2분간 틱차트 우선
        after = sum(1 for c in self.calls if c in ("usa20100", "usa20101"))
        self.assertEqual(before, after)     # mrkcond 재시도 없음
        self.assertEqual(self.calls[-1], "usa06010")
