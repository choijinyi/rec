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

## 실계좌 연동에 대하여

이 저장소는 실주문 코드를 포함하지 않는다. 실계좌 연동이 필요하면
`broker.Broker` 프로토콜을 구현한 어댑터(예: 한국투자증권 KIS Developers
OpenAPI)를 작성해 엔진에 주입하면 되고, 반드시 모의투자 계좌에서 충분히
검증한 뒤 사용해야 한다. 자동매매는 원금 손실 위험이 있으며 투자 결과의
책임은 사용자에게 있다.
