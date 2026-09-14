"""최소 보유 시간(min_hold_bars) 테스트: 매수 직후 왕복 매매 방지."""

import unittest
from datetime import datetime

from autotrader.broker import PaperBroker
from autotrader.engine import TradingEngine
from autotrader.kiwoom_rest import load_risk_config
from autotrader.models import Account, Bar, Side
from autotrader.risk import RiskConfig, RiskManager
from autotrader.strategy import Signal, Strategy


class BuyThenSell(Strategy):
    """첫 봉에 매수, 이후 매 봉마다 매도 신호(왕복 매매 재현)."""

    def __init__(self):
        self.first = True

    def on_bar(self, bar):
        if self.first:
            self.first = False
            return Signal.BUY
        return Signal.SELL


def make_bar(close, day=1):
    return Bar("TEST", datetime(2025, 9, day), close, close, close, close, 100)


def make_engine(min_hold=3, stop_loss=0.03, cash=1_000_000):
    risk = RiskManager(
        RiskConfig(max_position_pct=1.0, order_cash_pct=0.5,
                   stop_loss_pct=stop_loss, min_hold_bars=min_hold,
                   reentry_cooldown_bars=0),
        initial_equity=cash)
    return TradingEngine(BuyThenSell(), PaperBroker(0, 0), risk,
                         Account(cash=cash))


class MinHoldTest(unittest.TestCase):
    def test_strategy_sell_blocked_during_hold(self):
        engine = make_engine(min_hold=3)
        buy = engine.process_bar(make_bar(100.0, 1))
        self.assertIs(buy.side, Side.BUY)
        # 매수 후 3봉 동안은 전략 매도 신호를 무시한다
        self.assertIsNone(engine.process_bar(make_bar(101.0, 2)))
        self.assertIsNone(engine.process_bar(make_bar(102.0, 3)))
        self.assertIsNone(engine.process_bar(make_bar(103.0, 4)))
        sell = engine.process_bar(make_bar(104.0, 5))  # 4번째 봉부터 허용
        self.assertIsNotNone(sell)
        self.assertIs(sell.side, Side.SELL)

    def test_stop_loss_exempt_from_min_hold(self):
        engine = make_engine(min_hold=10, stop_loss=0.03)
        engine.process_bar(make_bar(100.0, 1))          # 매수
        fill = engine.process_bar(make_bar(96.0, 2))    # -4%: 보유 시간 무관 손절
        self.assertIsNotNone(fill)
        self.assertIs(fill.side, Side.SELL)
        self.assertEqual(engine.account.position("TEST").quantity, 0)

    def test_zero_disables_min_hold(self):
        engine = make_engine(min_hold=0)
        engine.process_bar(make_bar(100.0, 1))
        sell = engine.process_bar(make_bar(101.0, 2))   # 즉시 매도 허용
        self.assertIsNotNone(sell)
        self.assertIs(sell.side, Side.SELL)

    def test_config_parses_min_hold(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.ini"
            p.write_text("[kiwoom]\nappkey=A\nsecretkey=B\n"
                         "[risk]\nmin_hold_bars = 7\n", encoding="utf-8")
            self.assertEqual(load_risk_config(p).min_hold_bars, 7)
            p.write_text("[kiwoom]\nappkey=A\nsecretkey=B\n"
                         "[risk]\nstop_loss_pct = 3\n", encoding="utf-8")
            self.assertEqual(load_risk_config(p).min_hold_bars, 3)  # 기본값


if __name__ == "__main__":
    unittest.main()
