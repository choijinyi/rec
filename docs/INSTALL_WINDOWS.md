# Windows 설치 안내 — 키움증권 모의투자 자동매매

이 문서는 내 컴퓨터(Windows)에 autotrader를 설치해 키움증권 **모의투자**
계좌로 자동매매를 실행하는 절차다. 순서대로 따라 하면 된다.

## 1. 키움증권 사전 준비 (프로그램 설치 전, 1회)

1. **계좌 개설**: 키움증권 계좌가 없으면 영웅문 또는 키움 홈페이지에서 개설한다.
2. **모의투자 신청**: [키움 홈페이지](https://www.kiwoom.com) → 모의투자 →
   *주식 모의투자 참가 신청*. 신청하면 모의투자 서버 로그인이 가능해진다.
3. **OpenAPI+ 사용 신청**: 홈페이지 → 트레이딩 채널 → Open API →
   *키움 Open API+ 사용 신청*.
4. **OpenAPI+ 모듈 설치**: 같은 페이지에서 *키움 Open API+ 모듈* 을 내려받아
   설치한다(OCX 등록). 설치 후 **KOA Studio**(선택)를 받아 두면 TR 명세를
   조회할 때 편하다.

## 2. 32비트 파이썬 설치 (중요)

키움 OpenAPI+는 32비트 OCX라서 **반드시 32비트(x86) 파이썬**이 필요하다.

1. [python.org](https://www.python.org/downloads/windows/)에서
   **Windows installer (32-bit)** — Python 3.10.x 권장 — 을 내려받아 설치한다.
   설치 시 *Add python.exe to PATH* 를 체크한다.
2. PowerShell에서 확인:

   ```powershell
   python -c "import struct; print(struct.calcsize('P') * 8)"
   # 32 가 출력되어야 한다
   ```

   64비트 파이썬이 함께 설치되어 있으면 `py -3.10-32` 로 32비트를 지정한다.

## 3. 프로그램 설치

```powershell
# 원하는 폴더에서
git clone https://github.com/choijinyi/rec.git
cd rec

# 32비트 파이썬으로 가상환경 생성
py -3.10-32 -m venv .venv
.\.venv\Scripts\Activate.ps1

# 의존성 설치 (키움 연동에는 PyQt5만 필요)
pip install PyQt5==5.15.10
```

> PowerShell에서 스크립트 실행이 막히면 관리자 권한으로
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` 를 한 번 실행한다.

## 4. 백테스트로 먼저 검증

실행 환경이 정상인지, 전략이 의도대로 동작하는지 확인한다.

```powershell
python -m unittest discover -s tests        # 전체 테스트
python -m autotrader backtest --strategy sma_crossover --days 250
```

## 5. 모의투자 자동매매 실행

**장중(평일 09:00~15:30)** 에 실행한다.

```powershell
python -m autotrader live --code 005930 --strategy sma_crossover
```

- 키움 로그인 창이 뜨면 **반드시 "모의투자" 서버**를 선택해 로그인한다.
  (버전 처리가 진행될 수 있다 — 완료 후 다시 실행한다.)
- 실전투자 서버로 로그인하면 프로그램이 **즉시 종료**된다. 이것은 의도된
  안전장치다. 실전 전환은 모의투자에서 충분히 검증한 뒤 `--allow-real`
  플래그를 직접 붙였을 때만 가능하다.
- 종료는 `Ctrl+C`.

자주 쓰는 옵션:

```powershell
python -m autotrader live --code 005930 --strategy rsi_reversion `
    --bar-interval 60 --poll-interval 2 --cash 10000000 --max-orders 20
```

## 6. 문제 해결

| 증상 | 원인/해결 |
|---|---|
| `PyQt5가 필요하다` 오류 | 가상환경 활성화 후 `pip install PyQt5==5.15.10` |
| OCX 생성 실패 / 클래스 등록 안 됨 | OpenAPI+ 모듈 재설치, **32비트 파이썬**인지 확인 |
| 로그인 창이 안 뜸 | 백신/방화벽 확인, 관리자 권한 PowerShell로 실행 |
| 버전 처리 후 멈춤 | 프로그램을 완전히 종료하고 다시 실행 |
| 주문이 안 나감 | 장중인지, 모의투자 참가 기간인지, 일일 주문 한도 확인 |

## 7. 유의사항

- 이 프로그램은 수익을 보장하지 않는다. 모의투자에서 최소 수 주간 성과와
  낙폭(MDD)을 관찰한 뒤에 실전 전환을 검토하라.
- 계좌 비밀번호·인증 정보는 코드나 저장소에 절대 저장하지 않는다.
  로그인은 키움 공식 로그인 창에서만 이루어진다.
- 자동매매 결과의 책임은 전적으로 사용자 본인에게 있다.
