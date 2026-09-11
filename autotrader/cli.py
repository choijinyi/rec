"""명령행 인터페이스.

사용 예:
    python -m autotrader backtest --strategy sma_crossover --days 250
    python -m autotrader backtest --strategy rsi_reversion --csv data/005930.csv --symbol 005930
"""

from __future__ import annotations

import argparse

from .broker import PaperBroker
from .data import CSVDataFeed, generate_synthetic_bars
from .engine import run_backtest
from .risk import RiskConfig, RiskManager
from .strategy import STRATEGIES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autotrader", description="주식 자동매매 백테스터")
    sub = parser.add_subparsers(dest="command", required=True)

    bt = sub.add_parser("backtest", help="전략 백테스트 실행")
    bt.add_argument("--strategy", choices=sorted(STRATEGIES), default="sma_crossover")
    bt.add_argument("--symbol", default="005930", help="종목 코드 (기본: 005930 삼성전자)")
    bt.add_argument("--csv", help="OHLCV CSV 경로. 생략하면 가상 시세를 생성한다")
    bt.add_argument("--days", type=int, default=250, help="가상 시세 일수")
    bt.add_argument("--cash", type=float, default=10_000_000, help="초기 자본")
    bt.add_argument("--seed", type=int, default=42, help="가상 시세 난수 시드")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "backtest":
        bars = (
            CSVDataFeed(args.symbol, args.csv)
            if args.csv
            else generate_synthetic_bars(args.symbol, days=args.days, seed=args.seed)
        )
        strategy = STRATEGIES[args.strategy]()
        risk = RiskManager(RiskConfig(), initial_equity=args.cash)
        result = run_backtest(strategy, PaperBroker(), risk, bars, initial_cash=args.cash)
        print(f"[{args.symbol}] 전략: {args.strategy}")
        print(result.summary())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
