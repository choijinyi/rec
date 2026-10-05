"""명령행 인터페이스.

사용 예:
    python -m autotrader ui              # 분할매매 대시보드 (브라우저)
    python -m autotrader ui --port 8899 --no-browser
"""

from __future__ import annotations

import argparse
import logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autotrader",
                                     description="분할매매 자동 집행 프로그램")
    sub = parser.add_subparsers(dest="command", required=True)

    ui = sub.add_parser("ui", help="브라우저 대시보드 실행")
    ui.add_argument("--config", default="config.ini", help="설정 파일 경로")
    ui.add_argument("--port", type=int, default=8899, help="웹 UI 포트")
    ui.add_argument("--no-browser", action="store_true",
                    help="브라우저를 자동으로 열지 않는다")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    if args.command == "ui":
        from .webui import run_ui
        run_ui(config_path=args.config, port=args.port,
               open_browser=not args.no_browser)
    return 0
