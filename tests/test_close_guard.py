"""장 마감 가드(마감 1시간 전 매수 중단·보유 정리)와 유동성 하한 테스트."""

import time
import unittest
from datetime import datetime, timedelta, timezone

from autotrader.scalper import (
    ScalpConfig, Scalper, kr_session_end, us_session_end,
)

UTC = timezone.utc


class SessionEndTest(unittest.TestCase):
    def test_kr_session_ends(self):
        mon = datetime(2026, 11, 9, 0, 0)
        self.assertEqual(kr_session_end(mon.replace(hour=8, minute=30)),
                         mon.replace(hour=9))
        self.assertEqual(kr_session_end(mon.replace(hour=10)),
                         mon.replace(hour=15, minute=30))
        self.assertEqual(kr_session_end(mon.replace(hour=16)),
                         mon.replace(hour=20))
        self.assertIsNone(kr_session_end(mon.replace(hour=21)))

    def test_us_session_ends_winter(self):
        # 2026-11-06(금)은 겨울 시간: ET = UTC-5
        self.assertEqual(us_session_end(datetime(2026, 11, 6, 15, 0, tzinfo=UTC)),
                         datetime(2026, 11, 6, 21, 0, tzinfo=UTC))   # 정규장 16:00 ET
        self.assertEqual(us_session_end(datetime(2026, 11, 6, 12, 53, tzinfo=UTC)),
                         datetime(2026, 11, 6, 14, 30, tzinfo=UTC))  # 프리 09:30 ET
        self.assertEqual(us_session_end(datetime(2026, 11, 6, 22, 0, tzinfo=UTC)),
                         datetime(2026, 11, 7, 1, 0, tzinfo=UTC))    # 애프터 20:00 ET
        # 월요일 KST 12:00 — 주간거래, 겨울엔 KST 18:00 종료
        self.assertEqual(us_session_end(datetime(2026, 11, 9, 3, 0, tzinfo=UTC)),
                         datetime(2026, 11, 9, 9, 0, tzinfo=UTC))

    def test_us_session_end_summer(self):
        # 2025-10-06(월)은 서머타임: 정규장 종료 16:00 EDT = 20:00 UTC
        self.assertEqual(us_session_end(datetime(2025, 10, 6, 14, 0, tzinfo=UTC)),
                         datetime(2025, 10, 6, 20, 0, tzinfo=UTC))


class FakeAPI:
    def __init__(self, prices, ranking):
        self.prices = dict(prices)
        self.ranking = ranking
        self.orders = []

    def current_price(self, code):
        return self.prices[code]

    def send_market_order(self, account_no, code, side, quantity,
                          kr_exchange="KRX"):
        self.orders.append((code, side, quantity))

    def top_stocks(self, criteria="volume", limit=10):
        return self.ranking


ROWS = [{"code": "AAA", "price": "10000", "change_pct": "5",
         "volume": "5000000"}]


def make(hour, minute=0, **kw):
    clock = {"now": datetime(2026, 11, 9, hour, minute)}   # 월요일
    cfg = dict(market="kr", max_positions=1, total_cash=1_000_000,
               scan_interval=99999.0)
    cfg.update(kw)
    api = FakeAPI({"AAA": 10_000}, ROWS)
    trader = Scalper(api=api, config=ScalpConfig(**cfg),
                     selector=lambda r, n: (["AAA"], ""),
                     is_simulation=True, clock=lambda: clock["now"])
    trader._maybe_scan = lambda *a, **k: None
    trader._clock = clock
    return trader, api


class CloseGuardTest(unittest.TestCase):
    def test_entries_allowed_before_cutoff(self):
        trader, api = make(14, 20)                 # 정규장 마감 70분 전
        self.assertTrue(trader._entries_allowed())

    def test_no_new_buys_in_last_hour(self):
        trader, api = make(14, 45)                 # 마감 45분 전
        trader._scan_worker(1)
        trader.step()
        self.assertEqual(api.orders, [])
        self.assertEqual(trader.halt_reason, "마감 1시간 전 매수 중단")
        self.assertIsNone(trader._scan_result)     # 받아 둔 선정도 버림

    def test_held_position_flattened_at_cutoff(self):
        trader, api = make(14, 0)                  # 마감 90분 전 매수
        trader._scan_worker(1)
        trader.step()
        self.assertIn("AAA", trader.positions)
        api.prices["AAA"] = 9_900                  # -1%: 익절·손절 미도달
        trader._clock["now"] = trader._clock["now"].replace(hour=14, minute=31)
        trader.step()                              # 마감 59분 전 → 강제 정리
        self.assertNotIn("AAA", trader.positions)
        self.assertEqual(trader.trades[-1].reason, "장마감 정리")
        self.assertEqual(api.orders[-1][1], "sell")

    def test_resumes_in_next_session(self):
        trader, api = make(14, 45)
        trader._entries_allowed()                  # 정규장 마감 가드 진입
        trader._clock["now"] = trader._clock["now"].replace(hour=15, minute=40)
        self.assertTrue(trader._entries_allowed()) # 장후(~20:00) 열림 → 재개
        self.assertEqual(trader.halt_reason, "")

    def test_premarket_kr_is_entirely_guarded(self):
        trader, api = make(8, 10)                  # 장전은 09:00 종료 — 1시간 미만
        self.assertFalse(trader._entries_allowed())

    def test_us_last_hour_of_regular_session(self):
        clock = {"now": datetime(2026, 11, 9, 5, 30)}
        api = FakeAPI({"AAA": 10.0}, [])
        trader = Scalper(api=api, config=ScalpConfig(market="us"),
                         is_simulation=True, clock=lambda: clock["now"],
                         utc_clock=lambda: datetime(2026, 11, 6, 20, 30,
                                                    tzinfo=UTC))  # 15:30 ET
        self.assertAlmostEqual(trader.minutes_to_close(), 30)
        self.assertFalse(trader._entries_allowed())


class LiquidityDefaultsTest(unittest.TestCase):
    def _candidates(self, rows, market="us"):
        api = FakeAPI({}, rows)
        trader = Scalper(api=api, config=ScalpConfig(market=market),
                         is_simulation=True,
                         clock=lambda: datetime(2026, 11, 9, 10, 0))
        return [r["code"] for r in trader._candidates()]

    def test_us_thresholds(self):
        rows = [
            {"code": "THIN", "price": "7.57", "change_pct": "12", "volume": "300000"},
            {"code": "SMALLDV", "price": "8", "change_pct": "9", "volume": "900000"},
            {"code": "FEWSHR", "price": "900", "change_pct": "3", "volume": "40000"},
            {"code": "NVDA", "price": "190", "change_pct": "2", "volume": "30000000"},
        ]
        self.assertEqual(self._candidates(rows), ["NVDA"])

    def test_kr_thresholds(self):
        rows = [
            {"code": "000001", "price": "5000", "change_pct": "8", "volume": "200000"},
            {"code": "005930", "price": "70000", "change_pct": "1", "volume": "15000000"},
        ]
        self.assertEqual(self._candidates(rows, market="kr"), ["005930"])


class SelectPromptTest(unittest.TestCase):
    def test_prompt_forbids_illiquid(self):
        from autotrader.analysis import _select_prompt

        prompt = _select_prompt("us", 2, [{"code": "NVDA"}], "거래량 상위")
        self.assertIn("거래대금", prompt)


if __name__ == "__main__":
    unittest.main()
