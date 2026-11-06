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
    trader._maybe_scan = lambda *a, **k: None   # 테스트는 scan_now로 수동 구동
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
        trader._maybe_scan = lambda *a, **k: None
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


class RejectedBuyTest(unittest.TestCase):
    """매수 거부(레버리지 ETP 예탁금 등) 시 제외하고 다음 후보로 교체."""

    def _trader_with_rejection(self, message):
        trader, api = make_trader(selector=lambda r, n: (["AAA"], "AI 선정"),
                                  max_positions=1)
        orig = api.send_market_order

        def send(account_no, code, side, qty, kr_exchange="KRX"):
            if code == "AAA" and side == "buy":
                raise RuntimeError(message)
            orig(account_no, code, side, qty, kr_exchange)

        api.send_market_order = send
        return trader, api

    def test_deposit_requirement_excludes_for_day_and_rotates(self):
        trader, api = self._trader_with_rejection(
            "ust20000 실패: [2000](573501:단일 해외레버리지ETP 기본예탁금 미만입니다)")
        scan_now(trader, 1)
        self.assertNotIn("AAA", trader.positions)
        self.assertIn("BBB", trader.positions)         # 예비 후보로 교체 매수
        until = trader._cooldown["AAA"]
        self.assertGreaterEqual((until - trader.clock()).total_seconds(),
                                23 * 3600)             # 당일 제외
        self.assertTrue(any("매수 실패" in e for e in trader.events))

    def test_transient_failure_gets_short_cooldown(self):
        trader, api = self._trader_with_rejection("일시 오류")
        scan_now(trader, 1)
        self.assertIn("BBB", trader.positions)
        until = trader._cooldown["AAA"]
        secs = (until - trader.clock()).total_seconds()
        self.assertLessEqual(secs, 2 * 3600)           # 1시간 제외


class PreexistingHoldingsTest(unittest.TestCase):
    """계좌에 원래 있던 주식(장부보다 많은 실보유)은 흡수하지 않는다."""

    def test_reconcile_never_grows_position(self):
        class UsAPI(FakeAPI):
            def us_balances(self):
                # 프로그램은 14주만 샀는데 계좌에는 기존 보유 포함 75주
                return {"SOXL": {"qty": 75, "sellable": 75}}

        api = UsAPI(price_seqs={"SOXL": [48.0]},
                    ranking=[{"code": "SOXL", "price": "48",
                              "change_pct": "2.0"}])
        trader = Scalper(
            api=api,
            config=ScalpConfig(market="us", max_positions=1, total_cash=700,
                               scan_interval=99999.0),
            selector=lambda r, n: (["SOXL"], ""),
            is_simulation=True,
            utc_clock=lambda: datetime(2025, 10, 6, 14, 0, tzinfo=timezone.utc),
        )
        trader._maybe_scan = lambda *a, **k: None
        trader._scan_worker(1)
        trader.step()
        self.assertEqual(trader.positions["SOXL"].quantity, 14)  # 700//48
        cash_before = trader.cash
        trader.reconcile()
        self.assertEqual(trader.positions["SOXL"].quantity, 14)  # 늘지 않음
        self.assertEqual(trader.cash, cash_before)
        # 익절 시에도 프로그램이 산 14주만 판다
        api.price_seqs["SOXL"] = [48.8]
        api.idx["SOXL"] = 0
        trader.step()
        sells = [o for o in api.orders if o[1] == "sell"]
        self.assertEqual(sells[-1][2], 14)


class ContinuousTradingTest(unittest.TestCase):
    """청산 후 매매가 끊기지 않고 이어지는지."""

    def _enter_and_close(self, prices, **cfg_kw):
        trader, api = make_trader(price_seqs={"AAA": prices},
                                  selector=lambda r, n: (["AAA"], ""),
                                  max_positions=1, total_cash=1_000_000,
                                  **cfg_kw)
        scan_now(trader, 1)
        trader.step()            # 청산
        return trader, api

    def test_win_gets_short_cooldown_and_immediate_rescan(self):
        trader, api = self._enter_and_close([10_000, 10_200])   # 익절
        self.assertTrue(trader._rescan_asap)                     # 즉시 재탐색
        wait = (trader._cooldown["AAA"] - trader.clock()).total_seconds()
        self.assertLessEqual(wait, 10 * 60)                      # 익절은 짧은 쿨다운
        self.assertGreater(wait, 0)

    def test_loss_keeps_long_cooldown(self):
        trader, api = self._enter_and_close([10_000, 9_600])     # 손절
        wait = (trader._cooldown["AAA"] - trader.clock()).total_seconds()
        self.assertGreaterEqual(wait, 25 * 60)

    def test_rescan_asap_bypasses_interval(self):
        trader, api = make_trader()
        # 원래 _maybe_scan 로직으로 검사하되 스레드 대신 기록만 한다
        spawned = []
        trader._maybe_scan = Scalper._maybe_scan.__get__(trader)
        import threading as _th
        orig = _th.Thread

        class Rec(orig):
            def start(self2):
                spawned.append(True)

        _th.Thread = Rec
        try:
            trader._last_scan = time.monotonic()   # 주기 미도달
            trader._maybe_scan(1)
            self.assertEqual(spawned, [])
            trader._rescan_asap = True
            trader._maybe_scan(1)
            self.assertEqual(spawned, [True])      # 플래그로 즉시 탐색
        finally:
            _th.Thread = orig

    def test_no_candidates_logged_once_and_retried_soon(self):
        falling = [{"code": "AAA", "price": "10000", "change_pct": "-2.0"}]
        trader, api = make_trader(ranking=falling, scan_interval=120.0)
        trader._scan_worker(1)
        trader._scan_worker(1)
        logs = [e for e in trader.events if "후보가 없다" in e]
        self.assertEqual(len(logs), 1)             # 상태 전환 시 1회만 기록
        # 다음 탐색이 30초 안에 가능하도록 당겨졌는지
        self.assertGreaterEqual(time.monotonic() - trader._last_scan,
                                trader.config.scan_interval - 31)

    def test_all_rejected_retries_soon(self):
        trader, api = make_trader(selector=lambda r, n: (["AAA"], ""),
                                  max_positions=1, scan_interval=120.0)
        trader._last_scan = time.monotonic()

        def reject(account_no, code, side, qty, kr_exchange="KRX"):
            raise RuntimeError("매수 불가")

        api.send_market_order = reject
        scan_now(trader, 1)
        self.assertEqual(trader.positions, {})
        self.assertGreaterEqual(time.monotonic() - trader._last_scan,
                                trader.config.scan_interval - 31)


class BlindQuoteProtectionTest(unittest.TestCase):
    """시세 조회가 장시간 막히면 마지막 시세로 보호 청산한다."""

    def _blind_trader(self):
        trader, api = make_trader(price_seqs={"AAA": [10_000]},
                                  selector=lambda r, n: (["AAA"], ""),
                                  max_positions=1, total_cash=1_000_000)
        scan_now(trader, 1)      # 10,000에 진입

        def boom(code):
            raise RuntimeError("usa20100 실패 (return_code=7) [1632]")

        api.current_price = boom
        return trader, api

    def test_short_outage_keeps_position(self):
        trader, api = self._blind_trader()
        trader._clock_state["now"] += timedelta(minutes=2)
        trader.step()
        self.assertIn("AAA", trader.positions)   # 5분 미만 공백은 유지

    def test_long_outage_exits_at_last_price(self):
        trader, api = self._blind_trader()
        trader._clock_state["now"] += timedelta(minutes=6)
        trader.step()
        self.assertNotIn("AAA", trader.positions)
        self.assertEqual(trader.trades[-1].reason, "시세공백 보호청산")
        self.assertEqual(api.orders[-1][1], "sell")

    def test_quote_recovery_resets_blind_timer(self):
        trader, api = self._blind_trader()
        # 시세가 복구되면 타이머가 리셋된다
        api.current_price = lambda code: 10_050.0
        trader._clock_state["now"] += timedelta(minutes=4)
        trader.step()            # 성공 → last_quote_time 갱신
        def boom(code):
            raise RuntimeError("다시 실패")
        api.current_price = boom
        trader._clock_state["now"] += timedelta(minutes=4)
        trader.step()            # 공백 4분 — 아직 유지
        self.assertIn("AAA", trader.positions)


class QuoteFailureOnBuyTest(unittest.TestCase):
    """매수 직전 시세 오류는 종목을 제외하지 않고 다음 후보로 넘어간다."""

    def _trader_with_quote_failure(self):
        trader, api = make_trader(price_seqs={"BBB": [20_000]},
                                  selector=lambda r, n: (["AAA"], ""),
                                  max_positions=1)
        orig = api.current_price

        def flaky(code):
            if code == "AAA":
                raise RuntimeError("usa20100 실패 (return_code=7) [1632]")
            return orig(code)

        api.current_price = flaky
        return trader, api

    def test_no_cooldown_and_rotates_to_backup(self):
        trader, api = self._trader_with_quote_failure()
        scan_now(trader, 1)
        self.assertNotIn("AAA", trader._cooldown)    # 억울한 제외 없음
        self.assertIn("BBB", trader.positions)       # 예비 후보로 교체
        self.assertTrue(any("매수 보류" in e for e in trader.events))

    def test_retried_on_next_scan(self):
        trader, api = self._trader_with_quote_failure()
        scan_now(trader, 1)
        trader._close(trader.positions["BBB"], 20_000, "중지")  # 슬롯 비움
        # 시세가 복구되면 같은 종목을 다시 살 수 있다
        api.current_price = lambda code: 10_000.0
        scan_now(trader, 1)
        self.assertIn("AAA", trader.positions)
