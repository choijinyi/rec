@echo off
setlocal
title AutoTrader 설치 프로그램

rem =====================================================
rem  AutoTrader 원클릭 설치 프로그램 (키움 REST API 버전)
rem  - Python 3.10+ 확인/자동 설치 (64비트 가능)
rem  - 프로그램 다운로드 (github.com/choijinyi/rec)
rem  - 자체 점검, config.ini 생성, 바탕화면 바로가기
rem  - 미국주식/국내주식, 모의투자/실전투자 지원
rem =====================================================

set "INSTALL_DIR=%USERPROFILE%\autotrader"
set "REPO_ZIP=https://github.com/choijinyi/rec/archive/refs/heads/main.zip"
set "PY_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
set "PY_SETUP=%TEMP%\python-3.12.10-amd64.exe"
set "ZIP_FILE=%TEMP%\autotrader-main.zip"
set "SRC_DIR=%TEMP%\autotrader-src"

echo.
echo  =====================================================
echo    AutoTrader 설치를 시작합니다 (키움 REST API 자동매매)
echo    설치 위치: %INSTALL_DIR%
echo  =====================================================
echo.

echo [1/5] Python 확인 중...
set "PYCMD="
py -3 -c "import sys; assert sys.version_info>=(3,10)" >nul 2>&1 && set "PYCMD=py -3"
if not defined PYCMD (
    python -c "import sys; assert sys.version_info>=(3,10)" >nul 2>&1 && set "PYCMD=python"
)
if not defined PYCMD (
    echo    Python이 없어 자동으로 내려받아 설치합니다. 1~2분 걸립니다...
    powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_SETUP%'"
    if errorlevel 1 goto :fail_net
    start /wait "" "%PY_SETUP%" /passive InstallAllUsers=0 PrependPath=1 Include_launcher=1
    py -3 -V >nul 2>&1 && set "PYCMD=py -3"
    if not defined PYCMD if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYCMD="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
)
if not defined PYCMD (
    echo    ! Python 설치를 확인하지 못했습니다. 컴퓨터를 재시작한 뒤 다시 실행해 주세요.
    goto :fail
)
echo    확인 완료

echo [2/5] 프로그램 내려받는 중...
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -Uri '%REPO_ZIP%' -OutFile '%ZIP_FILE%'; Expand-Archive -Force '%ZIP_FILE%' '%SRC_DIR%'"
if errorlevel 1 goto :fail_net
robocopy "%SRC_DIR%\rec-main" "%INSTALL_DIR%" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto :fail
echo    완료

echo [3/5] 자체 점검 실행 중...
pushd "%INSTALL_DIR%"
%PYCMD% -m unittest discover -s tests >nul 2>&1
if errorlevel 1 (
    popd
    echo    ! 자체 점검에 실패했습니다. 설치 상태를 확인해 주세요.
    goto :fail
)
popd
echo    전체 테스트 통과

echo [4/5] 설정 파일 준비 중...
if not exist "%INSTALL_DIR%\config.ini" (
    > "%INSTALL_DIR%\config.ini" (
        echo [kiwoom]
        echo ; openapi.kiwoom.com 에서 발급받은 키를 = 뒤에 붙여넣으세요.
        echo ; 모의투자에는 모의투자용 키, 실전투자에는 실전용 키가 필요합니다.
        echo appkey =
        echo secretkey =
        echo ; mock = 모의투자, real = 실전투자
        echo mode = mock
        echo.
        echo [risk]
        echo ; 숫자는 퍼센트 단위입니다.
        echo ; 종목당 최대 투자 비중
        echo max_position_pct = 20
        echo ; 한 번의 매수에 쓰는 자본 비중
        echo order_cash_pct = 10
        echo ; 자본이 이만큼 줄면 신규 매수를 중단
        echo max_drawdown_pct = 15
    )
)
echo    완료

echo [5/5] 바탕화면 바로가기 만드는 중...
set "DESKTOP="
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "DESKTOP=%%D"
if not defined DESKTOP set "DESKTOP=%USERPROFILE%\Desktop"

> "%DESKTOP%\AutoTrader 백테스트.bat" (
    echo @echo off
    echo title AutoTrader 백테스트
    echo cd /d "%INSTALL_DIR%"
    echo %PYCMD% -m autotrader backtest --strategy sma_crossover --days 250
    echo %PYCMD% -m autotrader backtest --strategy rsi_reversion --days 250
    echo pause
)
> "%DESKTOP%\AutoTrader 미국주식 모의투자.bat" (
    echo @echo off
    echo title AutoTrader 미국주식 모의투자
    echo cd /d "%INSTALL_DIR%"
    echo echo 미국 정규장: 한국시간 밤 10시30분~새벽 5시 ^(겨울철 11시30분~6시^)
    echo %PYCMD% -m autotrader live-rest --market us --code AAPL --strategy sma_crossover
    echo pause
)
> "%DESKTOP%\AutoTrader 미국주식 실전투자.bat" (
    echo @echo off
    echo title AutoTrader 미국주식 실전투자
    echo cd /d "%INSTALL_DIR%"
    echo echo [주의] 실전투자는 config.ini의 mode=real 과 실전용 키가 필요합니다.
    echo %PYCMD% -m autotrader live-rest --market us --code AAPL --strategy sma_crossover --allow-real
    echo pause
)
echo    완료

echo.
echo  =====================================================
echo    설치가 끝났습니다!
echo.
echo    바탕화면 바로가기:
echo      - "AutoTrader 백테스트"          : 지금 바로 실행 가능
echo      - "AutoTrader 미국주식 모의투자" : 모의투자 키 입력 후
echo      - "AutoTrader 미국주식 실전투자" : 실전용 키 + mode=real 필요
echo.
echo    [준비 - openapi.kiwoom.com 에서 최초 1회]
echo      1. 키움 REST API 사용 신청
echo      2. 모의투자용/실전용 appkey, secretkey 발급
echo      3. 잠시 후 열리는 config.ini에 키를 붙여넣고 저장
echo.
echo    다른 종목으로 바꾸려면 바로가기 파일을 메모장으로 열어
echo    --code AAPL 부분을 원하는 종목으로 수정하세요.
echo.
echo    자동매매는 수익을 보장하지 않습니다. 실전투자 전에
echo    모의투자로 충분히 검증하시길 권합니다.
echo  =====================================================
echo.
start "" notepad "%INSTALL_DIR%\config.ini"
pause
exit /b 0

:fail_net
echo.
echo  ! 인터넷 연결 또는 다운로드에 문제가 있습니다. 네트워크 확인 후 다시 실행해 주세요.
:fail
echo.
echo  설치를 완료하지 못했습니다. 위 메시지를 확인해 주세요.
pause
exit /b 1
