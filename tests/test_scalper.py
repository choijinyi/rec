"""AI 단타 엔진(Scalper) 테스트."""

import time
import unittest
from datetime import datetime, timedelta, timezone

from autotrader.scalper import ScalpConfig, Scalper, kr_session, us_session


class FakeAPI:
    """시세 시퀀스·순위를 재생하고 주문을 기록하는 가짜 API."""

    def __init__(self, price_seqs=None, ranking=None):
        self.price_seqs = {c: list(seq) for c, seq in (price_seqs or {}).items()}
        self.idx = {c: 0 for c in self.price_seqs}
        self.ranking = ranking or []
        self.orders = []          # (code, side, qty, kr_exchange)
        self.rank_calls = 0

    def current_price(self, code):
        seq = self.price_seqs.get(code, [10_000])
        i = min(self.idx.get(code, 0), len(seq) - 1)
        self.idx[code] = self.idx.get(code, 0) + 1
        return seq[i]

    def send_market_order(self, account_no, code, side, quantity,
                          kr_exchange="KRX"):
        self.orders.append((code, side, quantity, kr_exchange))

    def top_stocks(self, criteria="volume", limit=10):
        self.rank_calls += 1
        return self.ranking


ROWS = [
    {"code": "AAA", "price": "10000", "change_pct": "5.0", "volume": "900"},
    {"code": "BBB", "price": "20000", "change_pct": "3.0", "volume": "800"},
    {"code": "CCC", "price": "30000", "change_pct": "-1.0", "volume": "700"},
]


def make_trader(price_seqs=None, ranking=None, selector=None, **cfg_kw):
    clock_state = {"now": datetime(2025, 10, 6, 10, 0, 0)}  # 월요일 정규장

    def clock():
        return clock_state["now"]

    defaults = dict(market="kr", target_pct=1.5, stop_pct=3.0,
                    max_positions=2, total_cash=2_000_000,
                    scan_interval=99999.0)
    defaults.update(cfg_kw)
    api = FakeAPI(price_seqs, ranking if ranking is not None else list(ROWS))
    trader = Scalper(api=api, config=ScalpConfig(**defaults),
                     selector=selector, is_simulation=True, clock=clock)
    trader._last_scan = time.monotonic()   # 테스트에서는 스캔을 수동으로 돌린다
    trader._clock_state = clock_state
    return trader, api


def scan_now(trader, slots):
    """백그라운드 스레드 없이 탐색을 동기로 실행한다."""
    trader._scan_worker(slots)
    trader.step()   # 결과 소비(매수)


class ConfigGuardTest(unittest.TestCase):
    def test_refuses_real_without_flag(self):
        with self.assertRaises(PermissionError):
            Scalper(api=FakeAPI(), config=ScalpConfig(allow_real=False),
                    is_simulation=False)

    def test_invalid_config_rejected(self):
        for kw in ({"target_pct": 0}, {"target_pct": 11}, {"stop_pct": 0},
                   {"max_positions": 0}, {"total_cash": 0}):
            with self.assertRaises(ValueError):
                make_trader(**kw)


class SelectionTest(unittest.TestCase):
    def test_selector_choice_is_bought(self):
        trader, api = make_trader(
            selector=lambda rows, num: (["BBB", "AAA"], "근거"))
        scan_now(trader, 2)
        buys = [o[0] for o in api.orders if o[1] == "buy"]
        self.assertEqual(buys, ["BBB", "AAA"])
        self.assertEqual(trader.selection_note, "근거")
        self.assertEqual(set(trader.positions), {"AAA", "BBB"})

    def test_selector_failure_falls_back_to_momentum(self):
        def boom(rows, num):
            raise RuntimeError("백엔드 없음")

        trader, api = make_trader(selector=boom)
        scan_now(trader, 2)
        buys = [o[0] for o in api.orders if o[1] == "buy"]
        self.assertEqual(buys, ["AAA", "BBB"])   # 등락률 상위 순
        self.assertIn("대체", trader.selection_note)

    def test_falling_stocks_excluded(self):
        trader, api = make_trader()              # selector 없음 → 모멘텀
        scan_now(trader, 3)
        self.assertNotIn("CCC", trader.positions)  # 등락률 음수 제외

    def test_cooldown_blocks_rebuy(self):
        trader, api = make_trader()
        trader._cooldown["AAA"] = trader.clock() + timedelta(minutes=10)
        scan_now(trader, 2)
        self.assertNotIn("AAA", trader.positions)
        self.assertIn("BBB", trader.positions)

    def test_cheap_stocks_excluded(self):
        rows = [{"code": "PNY", "price": "500", "change_pct": "9.0"}] + ROWS
        trader, api = make_trader(ranking=rows)
        scan_now(trader, 3)
        self.assertNotIn("PNY", trader.positions)

    def test_slot_budget_sizing(self):
        trader, api = make_trader(               # 200만 / 2슬롯 = 슬롯당 100만
            price_seqs={"AAA": [10_000], "BBB": [20_000]})
        scan_now(trader, 2)
        qty = {o[0]: o[2] for o in api.orders if o[1] == "buy"}
        self.assertEqual(qty["AAA"], 100)        # 100만 // 10,000
        self.assertEqual(qty["BBB"], 50)         # 100만 // 20,000


class ExitTest(unittest.TestCase):
    def _enter(self, prices, **cfg_kw):
        trader, api = make_trader(price_seqs={"AAA": prices},
                                  selector=lambda r, n: (["AAA"], ""),
                                  max_positions=1, total_cash=1_000_000,
                                  **cfg_kw)
        scan_now(trader, 1)     # 첫 가격으로 진입
        return trader, api

    def test_take_profit_at_target(self):
        trader, api = self._enter([10_000, 10_100, 10_160])
        trader.step()           # +1.0% — 유지
        self.assertIn("AAA", trader.positions)
        trader.step()           # +1.6% ≥ 1.5% — 익절
        self.assertNotIn("AAA", trader.positions)
        self.assertEqual(trader.trades[-1].reason, "익절")
        self.assertEqual(trader.wins, 1)
        self.assertAlmostEqual(trader.realized_pnl, 160 * 100)

    def test_hard_stop_at_minus_three(self):
        trader, api = self._enter([10_000, 9_750, 9_690])
        trader.step()           # -2.5% — 유지
        trader.step()           # -3.1% — 무조건 손절
        self.assertEqual(trader.trades[-1].reason, "손절")
        self.assertEqual(trader.losses, 1)
        self.assertLess(trader.realized_pnl, 0)

    def test_stale_position_rotated(self):
        trader, api = self._enter([10_000, 10_010], stale_min=30)
        trader._clock_state["now"] += timedelta(minutes=31)
        trader.step()
        self.assertEqual(trader.trades[-1].reason, "시간초과")

    def test_closed_symbol_gets_cooldown(self):
        trader, api = self._enter([10_000, 10_200])
        trader.step()           # 익절
        self.assertIn("AAA", trader._cooldown)


class DailyGuardTest(unittest.TestCase):
    def test_daily_loss_halts_new_entries(self):
        trader, api = make_trader(daily_loss_pct=5.0, total_cash=2_000_000,
                                  price_seqs={"AAA": [10_000, 9_000]},
                                  selector=lambda r, n: (["AAA"], ""),
                                  max_positions=1)
        scan_now(trader, 1)
        trader.step()            # -10% → 손절, 누적 -10만 (한도 -10만 도달)
        self.assertFalse(trader._entries_allowed())
        self.assertEqual(trader.halt_reason, "손실 한도")

    def test_trade_count_halts_new_entries(self):
        trader, api = make_trader(max_trades_per_day=1,
                                  price_seqs={"AAA": [10_000, 10_200]},
                                  selector=lambda r, n: (["AAA"], ""),
                                  max_positions=1)
        scan_now(trader, 1)
        trader.step()            # 익절 1회 = 한도
        self.assertFalse(trader._entries_allowed())
        self.assertEqual(trader.halt_reason, "매매 횟수 한도")


class SessionTest(unittest.TestCase):
    def test_kr_sessions(self):
        day = datetime(2025, 10, 6, 0, 0)   # 월요일
        self.assertEqual(kr_session(day.replace(hour=8, minute=30)), "장전")
        self.assertEqual(kr_session(day.replace(hour=10)), "정규장")
        self.assertEqual(kr_session(day.replace(hour=16)), "장후")
        self.assertEqual(kr_session(day.replace(hour=21)), "장외")
        self.assertEqual(kr_session(day.replace(hour=8, minute=30),
                                    extended=False), "장외")
        sunday = datetime(2025, 10, 5, 10, 0)
        self.assertEqual(kr_session(sunday), "장외")

    def test_us_sessions(self):
        utc = timezone.utc
        self.assertEqual(us_session(datetime(2025, 10, 6, 14, 0, tzinfo=utc)),
                         "정규장")
        self.assertEqual(us_session(datetime(2025, 10, 6, 3, 0, tzinfo=utc)),
                         "주간거래")
        self.assertEqual(us_session(datetime(2025, 10, 6, 3, 0, tzinfo=utc),
                                    day_session=False), "장외")

    def test_no_trade_when_closed(self):
        trader, api = make_trader()
        trader._clock_state["now"] = datetime(2025, 10, 6, 22, 0)
        trader.step()
        self.assertEqual(api.orders, [])
        self.assertEqual(api.rank_calls, 0)

    def test_order_routes_krx_in_regular_nxt_after_hours(self):
        trader, api = make_trader(selector=lambda r, n: (["AAA"], ""),
                                  max_positions=1)
        scan_now(trader, 1)      # 정규장(10시) 매수
        self.assertEqual(api.orders[-1][3], "KRX")
        trader._clock_state["now"] = trader.clock().replace(hour=16)  # 장후
        trader.positions["AAA"].entry_price = 10_000
        api.price_seqs["AAA"] = [10_200]
        api.idx["AAA"] = 0
        trader.step()            # 장후 익절 → NXT 경로
        self.assertEqual(api.orders[-1][1], "sell")
        self.assertEqual(api.orders[-1][3], "NXT")


class ReconcileTest(unittest.TestCase):
    def test_us_balance_overrides_book(self):
        class UsAPI(FakeAPI):
            def us_balances(self):
                return {"AAPL": {"qty": 2, "sellable": 2}}

        api = UsAPI(price_seqs={"AAPL": [100.0]},
                    ranking=[{"code": "AAPL", "price": "100",
                              "change_pct": "2.0"}])
        trader = Scalper(
            api=api,
            config=ScalpConfig(market="us", max_positions=1, total_cash=1000,
                               scan_interval=99999.0),
            selector=lambda r, n: (["AAPL"], ""),
            is_simulation=True,
            utc_clock=lambda: datetime(2025, 10, 6, 14, 0, tzinfo=timezone.utc),
        )
        trader._last_scan = time.monotonic()
        trader._scan_worker(1)
        trader.step()
        self.assertEqual(trader.positions["AAPL"].quantity, 10)
        trader.reconcile()       # 실제는 2주만 체결
        self.assertEqual(trader.positions["AAPL"].quantity, 2)
        self.assertAlmostEqual(trader.cash, 1000 - 2 * 100)


if __name__ == "__main__":
    unittest.main()


class UsExtendedSessionTest(unittest.TestCase):
    """미국 프리마켓/애프터마켓 (2026-11-06은 겨울 시간: ET = UTC-5)."""

    def test_premarket_and_afterhours(self):
        utc = timezone.utc
        # ET 07:53 (프리마켓 04:00~09:30)
        self.assertEqual(us_session(datetime(2026, 11, 6, 12, 53, tzinfo=utc)),
                         "프리마켓")
        # ET 17:00 (애프터마켓 16:00~20:00)
        self.assertEqual(us_session(datetime(2026, 11, 6, 22, 0, tzinfo=utc)),
                         "애프터마켓")
        # ET 금요일 19:00 = 한국 토요일 새벽 — ET 요일 기준으로 매매 가능
        self.assertEqual(us_session(datetime(2026, 11, 7, 0, 0, tzinfo=utc)),
                         "애프터마켓")
        # ET 21:00 — 애프터마켓 종료 후
        self.assertEqual(us_session(datetime(2026, 11, 7, 2, 0, tzinfo=utc)),
                         "장외")

    def test_extended_off_reads_closed(self):
        utc = timezone.utc
        self.assertEqual(
            us_session(datetime(2026, 11, 6, 12, 53, tzinfo=utc),
                       extended=False), "장외")

    def test_regular_session_takes_priority(self):
        utc = timezone.utc
        self.assertEqual(us_session(datetime(2026, 11, 6, 15, 0, tzinfo=utc)),
                         "정규장")   # ET 10:00
