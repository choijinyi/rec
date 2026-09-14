"""키움 미국주식 주간거래 시간 판정과 옵션 테스트."""

import unittest
from datetime import datetime, timezone

from autotrader.kiwoom.auto import AutoConfig, MultiLiveTrader
from autotrader.kiwoom.live import in_us_day_session
from autotrader.kiwoom_rest import load_config
from autotrader.risk import RiskConfig, RiskManager
from autotrader.strategy import SMACrossoverStrategy


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class DaySessionHoursTest(unittest.TestCase):
    def test_dst_window_kst_9_to_17(self):
        # 2025-07-10(목) 01:00 UTC = KST 10:00 → 주간거래 중
        self.assertTrue(in_us_day_session(utc(2025, 7, 10, 1, 0)))
        # 08:30 UTC = KST 17:30 → 서머타임 창(9~17시) 종료 후
        self.assertFalse(in_us_day_session(utc(2025, 7, 10, 8, 30)))

    def test_winter_window_kst_10_to_18(self):
        # 2025-12-10(수) 02:00 UTC = KST 11:00 → 주간거래 중
        self.assertTrue(in_us_day_session(utc(2025, 12, 10, 2, 0)))
        # 00:30 UTC = KST 09:30 → 겨울 창(10~18시) 시작 전
        self.assertFalse(in_us_day_session(utc(2025, 12, 10, 0, 30)))

    def test_weekend_closed(self):
        # 2025-07-12(토) KST 낮
        self.assertFalse(in_us_day_session(utc(2025, 7, 12, 2, 0)))


class FakeAPI:
    def top_stocks(self, criteria="volume", limit=10):
        return [{"code": "AAA", "price": "10.00"}]

    def current_price(self, code):
        return 10.0

    def send_market_order(self, *a):
        pass


class DaySessionOptionTest(unittest.TestCase):
    def make_trader(self, day_session):
        # KST 11:00 (미국 정규장은 닫힘, 주간거래는 열림)인 시각으로 고정
        daytime = lambda: utc(2025, 7, 10, 2, 0)
        return MultiLiveTrader(
            api=FakeAPI(), strategy_factory=lambda: SMACrossoverStrategy(2, 3),
            config=AutoConfig(market="us", num_symbols=1,
                              us_day_session=day_session),
            risk=RiskManager(RiskConfig(), 1000),
            is_simulation=True, utc_clock=daytime)

    def test_daytime_trading_enabled_by_option(self):
        self.assertTrue(self.make_trader(True)._market_open())
        self.assertFalse(self.make_trader(False)._market_open())

    def test_config_parses_flag(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.ini"
            p.write_text("[kiwoom]\nappkey=A\nsecretkey=B\n", encoding="utf-8")
            self.assertTrue(load_config(p)["us_day_session"])  # 기본 켬
            p.write_text("[kiwoom]\nappkey=A\nsecretkey=B\n"
                         "us_day_session = false\n", encoding="utf-8")
            self.assertFalse(load_config(p)["us_day_session"])


if __name__ == "__main__":
    unittest.main()
