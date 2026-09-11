"""손절 규칙 테스트."""

import unittest
from datetime import datetime

from autotrader.broker import PaperBroker
from autotrader.engine import TradingEngine
from autotrader.kiwoom_rest import load_risk_config
from autotrader.models import Account, Bar, Side
from autotrader.risk import RiskConfig, RiskManager
from autotrader.strategy import Signal, Strategy


class AlwaysHold(Strategy):
    """지표만 쌓고 신호는 내지 않는 전략(손절 단독 검증용)."""

    def __init__(self):
        self.bars = 0

    def on_bar(self, bar):
        self.bars += 1
        return Signal.HOLD


class BuyOnce(Strategy):
    def __init__(self):
        self.bought = False

    def on_bar(self, bar):
        if not self.bought:
            self.bought = True
            return Signal.BUY
        return Signal.HOLD


def make_bar(close, day=1):
    return Bar("TEST", datetime(2025, 9, day), close, close, close, close, 1000)


def make_engine(strategy, stop_loss_pct=0.03, cash=1_000_000):
    risk = RiskManager(
        RiskConfig(max_position_pct=1.0, order_cash_pct=0.5,
                   stop_loss_pct=stop_loss_pct),
        initial_equity=cash)
    return TradingEngine(strategy, PaperBroker(commission_rate=0, slippage_rate=0),
                         risk, Account(cash=cash))


class StopLossTest(unittest.TestCase):
    def test_stop_out_sells_entire_position(self):
        engine = make_engine(BuyOnce())
        fill = engine.process_bar(make_bar(100.0, day=1))   # 매수
        self.assertIs(fill.side, Side.BUY)
        qty = engine.account.position("TEST").quantity
        self.assertGreater(qty, 0)

        self.assertIsNone(engine.process_bar(make_bar(98.0, day=2)))  # -2%: 유지
        fill = engine.process_bar(make_bar(96.9, day=3))              # -3.1%: 손절
        self.assertIsNotNone(fill)
        self.assertIs(fill.side, Side.SELL)
        self.assertEqual(fill.quantity, qty)
        self.assertEqual(engine.account.position("TEST").quantity, 0)
        self.assertLess(engine.account.realized_pnl, 0)

    def test_stop_loss_disabled_at_zero(self):
        engine = make_engine(BuyOnce(), stop_loss_pct=0.0)
        engine.process_bar(make_bar(100.0, day=1))
        self.assertIsNone(engine.process_bar(make_bar(50.0, day=2)))  # 폭락해도 유지
        self.assertGreater(engine.account.position("TEST").quantity, 0)

    def test_indicators_updated_on_stop_bar(self):
        strategy = AlwaysHold()
        engine = make_engine(strategy)
        pos = engine.account.position("TEST")
        pos.quantity, pos.avg_price = 10, 100.0
        engine.process_bar(make_bar(90.0))
        self.assertEqual(strategy.bars, 1)  # 손절 봉에서도 지표 갱신
        self.assertEqual(pos.quantity, 0)

    def test_no_stop_without_position(self):
        strategy = AlwaysHold()
        engine = make_engine(strategy)
        self.assertIsNone(engine.process_bar(make_bar(10.0)))

    def test_config_parses_stop_loss(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.ini"
            p.write_text("[kiwoom]\nappkey=A\nsecretkey=B\n"
                         "[risk]\nstop_loss_pct = 5\n", encoding="utf-8")
            rc = load_risk_config(p)
            self.assertAlmostEqual(rc.stop_loss_pct, 0.05)

            p.write_text("[kiwoom]\nappkey=A\nsecretkey=B\n"
                         "[risk]\nstop_loss_pct = 0\n", encoding="utf-8")
            self.assertAlmostEqual(load_risk_config(p).stop_loss_pct, 0.0)


if __name__ == "__main__":
    unittest.main()
