# autotrader — 주식 자동매매 프레임워크

전략 신호 → 리스크 검증 → 주문 체결의 파이프라인으로 구성된 파이썬 자동매매
프레임워크다. 기본 동작은 모의투자(페이퍼 트레이딩)와 백테스트이며, 외부
패키지 없이 파이썬 표준 라이브러리만으로 실행된다.

## 빠른 시작

```bash
# 가상 시세 250일로 이동평균 교차 전략 백테스트
python -m autotrader backtest --strategy sma_crossover --days 250

# RSI 평균회귀 전략
python -m autotrader backtest --strategy rsi_reversion --days 250

# 실제 시세 CSV(timestamp,open,high,low,close,volume)로 백테스트
python -m autotrader backtest --strategy sma_crossover --csv data/005930.csv --symbol 005930
```

테스트 실행:

```bash
python -m unittest discover -s tests -v
```

## 구조

| 모듈 | 역할 |
|---|---|
| `autotrader/models.py` | Bar·Order·Fill·Position·Account 도메인 모델 |
| `autotrader/data.py` | CSV 데이터 피드, 데모용 가상 시세 생성기 |
| `autotrader/strategy.py` | 전략 인터페이스와 SMA 교차·RSI 평균회귀 전략 |
| `autotrader/risk.py` | 종목당 비중 제한, 낙폭 한도, 포지션 사이징 |
| `autotrader/broker.py` | 수수료·슬리피지를 반영한 모의 브로커(PaperBroker) |
| `autotrader/engine.py` | 백테스트/실시간 공용 매매 엔진과 성과 리포트 |
| `autotrader/cli.py` | 명령행 인터페이스 |

## 설계 원칙

- **전략과 집행의 분리**: 전략은 신호(BUY/SELL/HOLD)만 내고, 수량 결정과
  한도 검증은 `RiskManager`가, 체결은 `Broker`가 담당한다.
- **백테스트 = 실시간 경로**: `TradingEngine.process_bar`를 백테스트와
  실시간 루프가 공유하므로, 검증한 로직이 그대로 운용된다.
- **공매도 미지원, 낙폭 한도 기본 탑재**: 계좌 평가액이 초기 자본 대비
  15% 이상 하락하면 신규 매수를 중단한다.

## 키움증권 모의투자 실시간 매매

키움 **REST API**(`autotrader/kiwoom_rest.py`)가 기본이다. OCX와 달리
32비트 파이썬·PyQt5가 필요 없고 표준 라이브러리만으로 동작한다.
설치와 API 키 발급은 [docs/INSTALL_WINDOWS.md](docs/INSTALL_WINDOWS.md) 참고.

**원클릭 설치**: [scripts/AutoTrader-Setup.bat](scripts/AutoTrader-Setup.bat)를
바탕화면에 내려받아 더블클릭하면 Python 확인·설치, 프로그램 다운로드,
자체 점검, `config.ini` 생성, 바탕화면 실행 바로가기 생성까지 자동 진행된다.
(키움 REST API 사용 신청과 모의투자용 키 발급은 openapi.kiwoom.com 에서 1회 수동)

```powershell
python -m autotrader live-rest --code 005930 --strategy sma_crossover
```

- 현재가 폴링 → 1분봉 집계 → 백테스트와 동일한 엔진 경로로 신호 평가 →
  시장가 주문 전송.
- **안전장치**: `mode=mock`(모의투자 서버)이 기본, 실전은 `mode=real` +
  `--allow-real` 이중 명시 필요. 장중(평일 09:00~15:30)에만 매매,
  일일 주문 횟수 제한(기본 20회), 낙폭 한도 도달 시 신규 매수 중단.

| 추가 모듈 | 역할 |
|---|---|
| `autotrader/bars.py` | 폴링 시세를 봉(Bar)으로 집계 |
| `autotrader/kiwoom_rest.py` | 키움 REST API 클라이언트 (토큰·시세·주문) |
| `autotrader/kiwoom/api.py` | (레거시) 키움 OpenAPI+ OCX 래퍼 — `live` 명령 |
| `autotrader/kiwoom/live.py` | 실시간 매매 루프와 안전장치 (REST/OCX 공용) |

## 유의사항

자동매매는 수익을 보장하지 않으며 원금 손실 위험이 있다. 반드시 모의투자
계좌에서 충분히 검증한 뒤 실전 전환을 검토해야 하고, 투자 결과의 책임은
사용자에게 있다. 계좌 비밀번호 등 인증 정보는 코드와 저장소에 저장하지
않는다(로그인은 키움 공식 로그인 창에서만 수행).
