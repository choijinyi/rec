# Windows 설치 안내 — 키움증권 REST API 모의투자 자동매매

키움 REST API 방식이 기본이다. OCX(OpenAPI+)와 달리 32비트 파이썬,
PyQt5, 키움 모듈 설치가 전혀 필요 없고 파이썬 표준 라이브러리만으로 동작한다.

## 가장 쉬운 방법: 원클릭 설치

1. 저장소의 `scripts/AutoTrader-Setup.bat`를 바탕화면에 내려받는다.
2. 더블클릭한다. (SmartScreen 경고가 뜨면 "추가 정보 → 실행")
3. 설치가 끝나면 메모장으로 `config.ini`가 열린다. 아래 "API 키 발급"에서
   받은 두 키를 붙여넣고 저장한다.

설치 프로그램이 자동으로 처리하는 것: Python 3.10+ 확인·설치, 프로그램
다운로드(`%USERPROFILE%\autotrader`), 자체 점검(테스트), `config.ini` 생성,
바탕화면 바로가기("AutoTrader 백테스트", "AutoTrader 모의투자 시작") 생성.

## API 키 발급 (키움증권, 최초 1회)

1. [openapi.kiwoom.com](https://openapi.kiwoom.com) 접속 → 키움 REST API 사용 신청.
2. 모의투자 신청 후 **모의투자용 appkey / secretkey** 발급.
3. `%USERPROFILE%\autotrader\config.ini`에 붙여넣는다:

   ```ini
   [kiwoom]
   appkey = 발급받은키
   secretkey = 발급받은시크릿
   mode = mock
   ```

`mode = mock`이면 모의투자 서버(mockapi.kiwoom.com)로만 접속한다.
`mode = real`(실전, api.kiwoom.com)은 실행 시 `--allow-real` 플래그까지
명시해야 동작한다 — 이중 안전장치다.

## 실행

- **백테스트**: 바탕화면 "AutoTrader 백테스트" 더블클릭. 장이 닫혀 있어도 된다.
- **모의투자 자동매매**: 평일 09:00~15:30 장중에 "AutoTrader 모의투자 시작"
  더블클릭. 종료는 `Ctrl+C` 또는 창 닫기.

명령행에서 직접 실행할 때:

```powershell
cd %USERPROFILE%\autotrader
py -3 -m autotrader live-rest --code 005930 --strategy sma_crossover
py -3 -m autotrader backtest --strategy rsi_reversion --days 250
```

## 문제 해결

| 증상 | 원인/해결 |
|---|---|
| "API 키가 없다" | `config.ini`에 appkey/secretkey 입력 여부 확인 |
| "토큰 발급 실패" | 키가 모의투자용인지, REST API 사용 신청 승인 여부 확인 |
| 주문이 안 나감 | 장중인지, 모의투자 참가 기간인지, 일일 주문 한도(기본 20회) 확인 |
| Python 설치 후에도 인식 안 됨 | 컴퓨터 재시작 후 설치 파일 재실행 |

## 참고: OCX(OpenAPI+) 방식

`python -m autotrader live` 명령으로 기존 OCX 방식도 사용할 수 있으나
Windows 전용 32비트 파이썬 + PyQt5 + 키움 OpenAPI+ 모듈이 필요하다.
특별한 이유가 없으면 REST 방식을 권장한다.

## 유의사항

- 이 프로그램은 수익을 보장하지 않는다. 모의투자에서 최소 수 주간 성과와
  낙폭(MDD)을 관찰한 뒤에 실전 전환을 검토하라.
- API 키는 `config.ini`(내 컴퓨터)에만 두고 절대 저장소·메일 등에 올리지 않는다.
- 자동매매 결과의 책임은 전적으로 사용자 본인에게 있다.
