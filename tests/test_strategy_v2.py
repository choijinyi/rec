"""실전 결과 분석 뒤 바꾼 규칙: 손익비·추적 익절, 호가 간격, 개장 대기,
추격 재매수 제한, AI 필수, 당일 한도 초기화, 선정 해석."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from autotrader.analysis import ClaudeCodeAnalyst, parse_selection
from autotrader.scalper import (
    ScalpConfig, Scalper, kr_session_start, us_session_start,
)

UTC = timezone.utc


class FakeAPI:
    def __init__(self, prices, ranking=None, quotes=None):
        self.prices = dict(prices)
        self.ranking = ranking or []
        self.quotes = quotes            # code -> (bid, ask) | Exception
        self.orders = []

    def current_price(self, code):
        return self.prices[code]

    def send_market_order(self, account_no, code, side, quantity,
                          kr_exchange="KRX"):
        self.orders.append((code, side, quantity))

    def top_stocks(self, criteria="volume", limit=10):
        return self.ranking


class QuoteAPI(FakeAPI):
    def best_bid_ask(self, code):
        value = self.quotes[code]
        if isinstance(value, Exception):
            raise value
        return value


ROWS = [{"code": "AAA", "price": "100", "change_pct": "5", "volume": "9000000"},
        {"code": "BBB", "price": "50", "change_pct": "3", "volume": "9000000"}]


def make(api, hour=10, minute=0, selector="AAA", **kw):
    clock = {"now": datetime(2026, 11, 9, hour, minute)}      # 월요일, 국내
    cfg = dict(market="kr", max_positions=1, total_cash=100_000,
               scan_interval=99999.0, min_dollar_volume=0, min_price=1)
    cfg.update(kw)
    if isinstance(selector, str):
        code = selector
        selector = lambda rows, n: ([code], "AI 근거")
    trader = Scalper(api=api, config=ScalpConfig(**cfg), selector=selector,
                     is_simulation=True, clock=lambda: clock["now"])
    trader._maybe_scan = lambda *a, **k: None
    trader._clock = clock
    return trader


def scan(trader, slots=1):
    trader._scan_worker(slots)
    trader.step()


class DefaultsTest(unittest.TestCase):
    def test_stop_is_not_wider_than_target(self):
        cfg = ScalpConfig()
        self.assertLessEqual(cfg.stop_pct, cfg.target_pct)
        self.assertEqual((cfg.target_pct, cfg.stop_pct), (2.0, 2.0))
        self.assertTrue(cfg.require_ai)


class TrailingExitTest(unittest.TestCase):
    def _entered(self):
        api = FakeAPI({"AAA": 100.0}, ROWS)
        trader = make(api)
        scan(trader)
        self.assertIn("AAA", trader.positions)
        return trader, api

    def test_target_starts_trailing_instead_of_selling(self):
        trader, api = self._entered()
        api.prices["AAA"] = 102.1               # +2.1% — 목표 도달
        trader.step()
        self.assertIn("AAA", trader.positions)   # 바로 팔지 않는다
        self.assertTrue(trader.positions["AAA"].trailing)

    def test_rides_trend_and_sells_on_pullback(self):
        trader, api = self._entered()
        for price in (102.1, 104.0, 106.0):      # 계속 상승 — 보유 유지
            api.prices["AAA"] = price
            trader.step()
        self.assertIn("AAA", trader.positions)
        api.prices["AAA"] = 105.2                # 고점 106에서 -0.75% 밀림
        trader.step()
        self.assertNotIn("AAA", trader.positions)
        trade = trader.trades[-1]
        self.assertEqual(trade.reason, "익절")
        self.assertAlmostEqual(trade.pct, 5.2, places=1)   # +1.5%가 아니라 +5.2%

    def test_small_pullback_keeps_position(self):
        trader, api = self._entered()
        for price in (102.1, 104.0, 103.5):      # 고점 대비 -0.48%
            api.prices["AAA"] = price
            trader.step()
        self.assertIn("AAA", trader.positions)

    def test_stop_at_minus_two_percent(self):
        trader, api = self._entered()
        api.prices["AAA"] = 98.5                 # -1.5% — 유지
        trader.step()
        self.assertIn("AAA", trader.positions)
        api.prices["AAA"] = 97.9                 # -2.1% — 손절
        trader.step()
        self.assertEqual(trader.trades[-1].reason, "손절")

    def test_loss_labelled_take_profit_is_corrected(self):
        trader, api = self._entered()
        pos = trader.positions["AAA"]
        trader._finalize(pos, 98.0, "익절")      # 익절 주문 미체결 → 손해 매도
        self.assertEqual(trader.trades[-1].reason, "익절 미체결 손실")


class SpreadFilterTest(unittest.TestCase):
    def test_wide_spread_is_skipped(self):
        api = QuoteAPI({"AAA": 100.0}, ROWS, quotes={"AAA": (97.0, 100.5)})
        trader = make(api)
        scan(trader)
        self.assertEqual(api.orders, [])
        self.assertIn("AAA", trader._cooldown)
        self.assertTrue(any("호가 간격" in e for e in trader.events))

    def test_tight_spread_buys_at_ask(self):
        api = QuoteAPI({"AAA": 100.0}, ROWS, quotes={"AAA": (99.95, 100.10)})
        trader = make(api)
        scan(trader)
        self.assertEqual(api.orders[0][:2], ("AAA", "buy"))
        self.assertAlmostEqual(trader.positions["AAA"].entry_price, 100.10)

    def test_unknown_spread_means_no_buy_and_no_blacklist(self):
        api = QuoteAPI({"AAA": 100.0}, ROWS,
                       quotes={"AAA": RuntimeError("usa20101 실패 [1632]")})
        trader = make(api)
        scan(trader)
        self.assertEqual(api.orders, [])
        self.assertNotIn("AAA", trader._cooldown)

    def test_filter_can_be_disabled(self):
        api = QuoteAPI({"AAA": 100.0}, ROWS, quotes={"AAA": (90.0, 100.0)})
        trader = make(api, max_spread_pct=0)
        scan(trader)
        self.assertEqual(len(api.orders), 1)


class OpenDelayTest(unittest.TestCase):
    def test_no_buy_right_after_open(self):
        api = FakeAPI({"AAA": 100.0}, ROWS)
        trader = make(api, hour=9, minute=5)     # 정규장 시작 5분 뒤
        scan(trader)
        self.assertEqual(api.orders, [])
        self.assertEqual(trader.halt_reason, "개장 직후 대기")

    def test_buy_after_delay(self):
        api = FakeAPI({"AAA": 100.0}, ROWS)
        trader = make(api, hour=9, minute=20)
        scan(trader)
        self.assertEqual(len(api.orders), 1)
        self.assertEqual(trader.halt_reason, "")

    def test_session_starts(self):
        mon = datetime(2026, 11, 9, 0, 0)
        self.assertEqual(kr_session_start(mon.replace(hour=10)),
                         mon.replace(hour=9))
        self.assertEqual(kr_session_start(mon.replace(hour=16)),
                         mon.replace(hour=15, minute=30))
        # 미국 겨울: 정규장 09:30 ET = 14:30 UTC
        self.assertEqual(
            us_session_start(datetime(2026, 11, 9, 14, 35, tzinfo=UTC)),
            datetime(2026, 11, 9, 14, 30, tzinfo=UTC))

    def test_us_open_minutes(self):
        trader = Scalper(api=FakeAPI({}), config=ScalpConfig(market="us"),
                         is_simulation=True,
                         utc_clock=lambda: datetime(2026, 11, 9, 14, 32,
                                                    tzinfo=UTC))
        self.assertAlmostEqual(trader.minutes_since_open(), 2)
        self.assertFalse(trader._entries_allowed())   # 23:30 개장 직후 매수 금지


class ChaseLimitTest(unittest.TestCase):
    def _cycle(self, trader, api, price):
        """price에 매수를 시도하고, 샀으면 곧바로 정리한다. 매수 여부 반환."""
        api.prices["AAA"] = price
        trader._cooldown.clear()
        before = len([o for o in api.orders if o[1] == "buy"])
        scan(trader)
        bought = len([o for o in api.orders if o[1] == "buy"]) > before
        if bought:
            trader._finalize(trader.positions["AAA"], price, "중지")
        return bought

    def test_second_higher_rebuy_is_blocked(self):
        api = FakeAPI({"AAA": 100.0}, ROWS)
        trader = make(api, max_trades_per_day=99)
        self.assertTrue(self._cycle(trader, api, 100.0))    # 첫 매수
        self.assertTrue(self._cycle(trader, api, 104.0))    # 올려 사기 1회 허용
        self.assertFalse(self._cycle(trader, api, 108.0))   # 2회째는 차단
        self.assertTrue(self._cycle(trader, api, 103.0))    # 더 싸면 허용

    def test_limit_resets_next_day(self):
        api = FakeAPI({"AAA": 100.0}, ROWS)
        trader = make(api, max_trades_per_day=99)
        self._cycle(trader, api, 100.0)
        self._cycle(trader, api, 104.0)
        self.assertFalse(self._cycle(trader, api, 108.0))
        trader._clock["now"] += timedelta(days=1)
        self.assertTrue(self._cycle(trader, api, 108.0))


class RequireAiTest(unittest.TestCase):
    def test_ai_failure_means_no_buy(self):
        def boom(rows, n):
            raise RuntimeError("Claude Code가 설치되어 있지 않다")

        api = FakeAPI({"AAA": 100.0, "BBB": 50.0}, ROWS)
        trader = make(api, selector=boom)
        scan(trader)
        self.assertEqual(api.orders, [])           # 등락률 상위로 대신 사지 않는다
        self.assertIn("AI 선정 불가", trader.selection_note)
        self.assertEqual(sum("AI 선정 불가" in e for e in trader.events), 1)

    def test_ai_says_none_means_no_buy(self):
        api = FakeAPI({"AAA": 100.0, "BBB": 50.0}, ROWS)
        trader = make(api, selector=lambda rows, n: ([], "선정: 없음"))
        scan(trader)
        self.assertEqual(api.orders, [])

    def test_backups_come_only_from_ai_picks(self):
        api = FakeAPI({"AAA": 100.0, "BBB": 50.0}, ROWS)
        trader = make(api, selector=lambda rows, n: (["BBB"], ""))

        def reject(account_no, code, side, qty, kr_exchange="KRX"):
            raise RuntimeError("매수 불가")

        api.send_market_order = reject
        api.attempts = []
        orig_price = api.current_price
        api.current_price = lambda c: (api.attempts.append(c), orig_price(c))[1]
        scan(trader)
        self.assertEqual(set(api.attempts), {"BBB"})   # AI가 안 고른 AAA는 시도 안 함

    def test_fallback_still_available_when_not_required(self):
        def boom(rows, n):
            raise RuntimeError("없음")

        api = FakeAPI({"AAA": 100.0, "BBB": 50.0}, ROWS)
        trader = make(api, selector=boom, require_ai=False)
        scan(trader)
        self.assertEqual(api.orders[0][0], "AAA")


class DailyLimitResetTest(unittest.TestCase):
    def test_loss_limit_applies_per_trading_day(self):
        api = FakeAPI({"AAA": 100.0}, ROWS)
        trader = make(api, daily_loss_pct=5.0)
        scan(trader)
        trader._finalize(trader.positions["AAA"], 90.0, "손절")   # -10%
        self.assertFalse(trader._entries_allowed())
        self.assertEqual(trader.halt_reason, "손실 한도")
        trader._clock["now"] += timedelta(days=1)                 # 다음 거래일
        self.assertTrue(trader._entries_allowed())
        self.assertEqual(trader.halt_reason, "")

    def test_us_day_key_rolls_at_day_session(self):
        t = Scalper(api=FakeAPI({}), config=ScalpConfig(market="us"),
                    is_simulation=True,
                    utc_clock=lambda: datetime(2026, 11, 10, 0, 30, tzinfo=UTC))
        # ET 11/9 19:30(애프터마켓) → 아직 11/9 거래일
        self.assertEqual(t._day_key(), "2026-11-09")
        t.utc_clock = lambda: datetime(2026, 11, 10, 1, 30, tzinfo=UTC)
        # ET 11/9 20:30(주간거래 시작 뒤) → 11/10 거래일
        self.assertEqual(t._day_key(), "2026-11-10")


class SelectionParsingTest(unittest.TestCase):
    V = ["NETG", "SOXL", "NVDA"]

    def test_none_answer_returns_empty(self):
        text = "선정: 없음\n\nNVDA는 과열, SOXL은 변동성 과다로 제외한다."
        self.assertEqual(parse_selection(text, self.V, 2), [])

    def test_bold_line_with_trailing_explanation(self):
        text = "**선정: SOXL** — 3개는 채울 수 없다. NETG는 유동성 미달로 제외했다."
        self.assertEqual(parse_selection(text, self.V, 3), ["SOXL"])

    def test_heading_then_selection_line(self):
        text = "## 선정 결과\n\n선정: SOXL, NVDA\n- SOXL: 거래량 최대"
        self.assertEqual(parse_selection(text, self.V, 3), ["SOXL", "NVDA"])


class FindCliTest(unittest.TestCase):
    def test_finds_install_outside_path(self):
        with tempfile.TemporaryDirectory() as home:
            bin_dir = os.path.join(home, ".local", "bin")
            os.makedirs(bin_dir)
            exe = os.path.join(bin_dir, "claude.exe")
            open(exe, "w").close()
            with mock.patch("shutil.which", return_value=None), \
                    mock.patch("os.path.expanduser", return_value=home):
                self.assertEqual(ClaudeCodeAnalyst._find_cli(), exe)

    def test_missing_everywhere_raises(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch("shutil.which", return_value=None), \
                    mock.patch("os.path.expanduser", return_value=home), \
                    mock.patch.dict(os.environ, {"APPDATA": home,
                                                 "LOCALAPPDATA": home}), \
                    mock.patch("os.path.isfile", return_value=False):
                with self.assertRaises(RuntimeError):
                    ClaudeCodeAnalyst._find_cli()


class BidAskClientTest(unittest.TestCase):
    def _client(self, market, payload):
        def transport(url, headers, body):
            if url.endswith("/oauth2/token"):
                return {"token": "T", "expires_dt": ""}
            if headers.get("api-id") == "usa10098":
                return {"return_code": 0, "list": [{"stex_tp": "ND"}]}
            self.api_id = headers.get("api-id")
            return dict(payload, return_code=0)

        from autotrader.kiwoom_rest import KiwoomRestClient
        return KiwoomRestClient("A", "S", market=market, transport=transport)

    def test_us_bid_ask(self):
        client = self._client("us", {"fpr_buy_bid": "47.30", "fpr_sel_bid": "47.34"})
        self.assertEqual(client.best_bid_ask("SOXL"), (47.30, 47.34))
        self.assertEqual(self.api_id, "usa20101")

    def test_kr_bid_ask(self):
        client = self._client("kr", {"buy_fpr_bid": "+70000", "sel_fpr_bid": "+70100"})
        self.assertEqual(client.best_bid_ask("005930"), (70000.0, 70100.0))
        self.assertEqual(self.api_id, "ka10004")

    def test_empty_quote_raises(self):
        from autotrader.kiwoom_rest import KiwoomRestError
        client = self._client("us", {"fpr_buy_bid": "0", "fpr_sel_bid": "0"})
        with self.assertRaises(KiwoomRestError):
            client.best_bid_ask("SOXL")


if __name__ == "__main__":
    unittest.main()
