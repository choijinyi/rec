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

    live = sub.add_parser("live", help="키움 모의투자 실시간 자동매매 (Windows 전용)")
    live.add_argument("--strategy", choices=sorted(STRATEGIES), default="sma_crossover")
    live.add_argument("--code", default="005930", help="종목코드 (기본: 005930 삼성전자)")
    live.add_argument("--bar-interval", type=int, default=60, help="봉 주기(초)")
    live.add_argument("--poll-interval", type=float, default=2.0, help="시세 폴링 주기(초)")
    live.add_argument("--cash", type=float, default=10_000_000, help="운용 기준 자본")
    live.add_argument("--max-orders", type=int, default=20, help="일일 최대 주문 횟수")
    live.add_argument(
        "--allow-real", action="store_true",
        help="실전투자 서버 접속 허용. 지정하지 않으면 모의투자 서버가 아닐 때 즉시 종료한다",
    )

    rest = sub.add_parser("live-rest", help="키움 REST API 실시간 자동매매 (권장, OS 무관)")
    rest.add_argument("--strategy", choices=sorted(STRATEGIES), default="sma_crossover")
    rest.add_argument("--code", default="005930", help="종목코드 (기본: 005930 삼성전자)")
    rest.add_argument("--config", default="config.ini", help="API 키 설정 파일 경로")
    rest.add_argument("--bar-interval", type=int, default=60, help="봉 주기(초)")
    rest.add_argument("--poll-interval", type=float, default=3.0, help="시세 폴링 주기(초)")
    rest.add_argument("--cash", type=float, default=10_000_000, help="운용 기준 자본")
    rest.add_argument("--max-orders", type=int, default=20, help="일일 최대 주문 횟수")
    rest.add_argument(
        "--allow-real", action="store_true",
        help="실전투자(mode=real) 허용. 지정하지 않으면 모의투자(mock)만 실행된다",
    )
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

    elif args.command == "live":
        import logging

        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        # PyQt5/OCX 의존성은 여기서만 로드한다 (Windows 전용)
        from .kiwoom.api import KiwoomOpenAPI
        from .kiwoom.live import LiveConfig, LiveTrader

        api = KiwoomOpenAPI()
        info = api.connect()
        config = LiveConfig(
            account_no=info.account_no,
            code=args.code,
            bar_interval=args.bar_interval,
            poll_interval=args.poll_interval,
            initial_cash=args.cash,
            max_orders_per_day=args.max_orders,
            allow_real=args.allow_real,
        )
        trader = LiveTrader(
            api=api,
            strategy=STRATEGIES[args.strategy](),
            config=config,
            is_simulation=info.is_simulation,
        )
        print(f"[{args.code} {api.stock_name(args.code)}] 전략 {args.strategy}, "
              f"{'모의투자' if info.is_simulation else '실전투자'} 계좌 {info.account_no}")
        trader.run()

    elif args.command == "live-rest":
        import logging

        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        from .kiwoom.live import LiveConfig, LiveTrader
        from .kiwoom_rest import KiwoomRestClient, KiwoomRestError, load_config

        try:
            cfg = load_config(args.config)
            mock = cfg["mode"] != "real"
            client = KiwoomRestClient(cfg["appkey"], cfg["secretkey"], mock=mock)
            config = LiveConfig(
                account_no="",  # REST API는 키에 계좌가 연결되어 있다
                code=args.code,
                bar_interval=args.bar_interval,
                poll_interval=args.poll_interval,
                initial_cash=args.cash,
                max_orders_per_day=args.max_orders,
                allow_real=args.allow_real,
            )
            trader = LiveTrader(
                api=client,
                strategy=STRATEGIES[args.strategy](),
                config=config,
                is_simulation=mock,
            )
        except (KiwoomRestError, PermissionError) as e:
            print(f"\n[중단] {e}")
            return 1
        print(f"[{args.code}] 전략 {args.strategy}, "
              f"{'모의투자(mockapi)' if mock else '실전투자(api)'} 서버")
        trader.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
