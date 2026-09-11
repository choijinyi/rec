import unittest
from datetime import datetime

from autotrader.broker import PaperBroker
from autotrader.data import generate_synthetic_bars
from autotrader.engine import run_backtest
from autotrader.models import Account, Bar, Fill, Order, Position, Side
from autotrader.risk import RiskConfig, RiskManager
from autotrader.strategy import RSIReversionStrategy, Signal, SMACrossoverStrategy


def make_bar(close: float, symbol: str = "TEST", day: int = 1) -> Bar:
    return Bar(symbol, datetime(2025, 1, day), close, close * 1.01, close * 0.99, close, 1000)


class PositionTest(unittest.TestCase):
    def test_buy_updates_average_price(self):
        pos = Position("TEST")
        ts = datetime(2025, 1, 1)
        pos.apply_fill(Fill("TEST", Side.BUY, 10, 100.0, 0.0, ts))
        pos.apply_fill(Fill("TEST", Side.BUY, 10, 200.0, 0.0, ts))
        self.assertEqual(pos.quantity, 20)
        self.assertAlmostEqual(pos.avg_price, 150.0)

    def test_sell_realizes_pnl_and_blocks_short(self):
        pos = Position("TEST")
        ts = datetime(2025, 1, 1)
        pos.apply_fill(Fill("TEST", Side.BUY, 10, 100.0, 0.0, ts))
        realized = pos.apply_fill(Fill("TEST", Side.SELL, 10, 120.0, 0.0, ts))
        self.assertAlmostEqual(realized, 200.0)
        self.assertEqual(pos.quantity, 0)
        with self.assertRaises(ValueError):
            pos.apply_fill(Fill("TEST", Side.SELL, 1, 120.0, 0.0, ts))


class StrategyTest(unittest.TestCase):
    def test_sma_crossover_emits_buy_then_sell(self):
        strategy = SMACrossoverStrategy(short_window=2, long_window=3)
        prices = [100, 100, 100, 90, 80, 120, 130, 80, 70]
        signals = [strategy.on_bar(make_bar(p)) for p in prices]
        self.assertIn(Signal.BUY, signals)
        self.assertIn(Signal.SELL, signals)
        buy_idx = signals.index(Signal.BUY)
        sell_idx = len(signals) - 1 - signals[::-1].index(Signal.SELL)
        self.assertLess(buy_idx, sell_idx)

    def test_rsi_signals(self):
        strategy = RSIReversionStrategy(period=3)
        for p in [100, 90, 80, 70]:  # 연속 하락 → 과매도
            signal = strategy.on_bar(make_bar(p))
        self.assertIs(signal, Signal.BUY)
        strategy2 = RSIReversionStrategy(period=3)
        for p in [100, 110, 120, 130]:  # 연속 상승 → 과매수
            signal = strategy2.on_bar(make_bar(p))
        self.assertIs(signal, Signal.SELL)


class RiskManagerTest(unittest.TestCase):
    def test_position_size_respects_cash_budget(self):
        risk = RiskManager(RiskConfig(order_cash_pct=0.1, max_position_pct=1.0), 1_000_000)
        account = Account(cash=1_000_000)
        qty = risk.size_order(account, "TEST", Side.BUY, 10_000, equity=1_000_000)
        self.assertEqual(qty, 10)  # 100만 * 10% / 1만

    def test_drawdown_blocks_new_buys(self):
        risk = RiskManager(RiskConfig(max_drawdown_pct=0.15), 1_000_000)
        account = Account(cash=800_000)
        qty = risk.size_order(account, "TEST", Side.BUY, 10_000, equity=800_000)
        self.assertEqual(qty, 0)

    def test_sell_returns_full_position(self):
        risk = RiskManager(RiskConfig(), 1_000_000)
        account = Account(cash=0)
        account.position("TEST").quantity = 7
        qty = risk.size_order(account, "TEST", Side.SELL, 10_000, equity=70_000)
        self.assertEqual(qty, 7)


class PaperBrokerTest(unittest.TestCase):
    def test_market_buy_applies_slippage_and_commission(self):
        broker = PaperBroker(commission_rate=0.001, slippage_rate=0.01)
        fill = broker.execute(Order("TEST", Side.BUY, 10), make_bar(100.0))
        self.assertIsNotNone(fill)
        self.assertAlmostEqual(fill.price, 101.0)
        self.assertAlmostEqual(fill.commission, 1.01)


class BacktestTest(unittest.TestCase):
    def test_backtest_runs_and_accounts_balance(self):
        bars = generate_synthetic_bars("TEST", days=300, seed=7)
        risk = RiskManager(RiskConfig(), initial_equity=10_000_000)
        result = run_backtest(
            SMACrossoverStrategy(), PaperBroker(), risk, bars, initial_cash=10_000_000
        )
        self.assertEqual(len(result.equity_curve), 300)
        self.assertGreater(result.num_trades, 0)
        self.assertGreater(result.final_equity, 0)
        self.assertGreaterEqual(result.max_drawdown_pct, 0)

    def test_deterministic_with_same_seed(self):
        def run():
            bars = generate_synthetic_bars("TEST", days=200, seed=1)
            risk = RiskManager(RiskConfig(), initial_equity=1_000_000)
            return run_backtest(
                SMACrossoverStrategy(), PaperBroker(), risk, bars, initial_cash=1_000_000
            ).final_equity

        self.assertEqual(run(), run())


if __name__ == "__main__":
    unittest.main()
