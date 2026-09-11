@echo off
setlocal
title AutoTrader 설치 프로그램

rem =====================================================
rem  AutoTrader 원클릭 설치 프로그램 (대시보드 UI 버전)
rem  - Python 3.10+ 확인/자동 설치
rem  - 프로그램 다운로드 + anthropic(Claude) 패키지 설치
rem  - 자체 점검, config.ini 생성, 바탕화면 바로가기
rem =====================================================

set "INSTALL_DIR=%USERPROFILE%\autotrader"
set "REPO_ZIP=https://github.com/choijinyi/rec/archive/refs/heads/main.zip"
set "PY_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
set "PY_SETUP=%TEMP%\python-3.12.10-amd64.exe"
set "ZIP_FILE=%TEMP%\autotrader-main.zip"
set "SRC_DIR=%TEMP%\autotrader-src"

echo.
echo  =====================================================
echo    AutoTrader 설치를 시작합니다 (대시보드 UI + AI 분석)
echo    설치 위치: %INSTALL_DIR%
echo  =====================================================
echo.

echo [1/6] Python 확인 중...
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

echo [2/6] 프로그램 내려받는 중...
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -Uri '%REPO_ZIP%' -OutFile '%ZIP_FILE%'; Expand-Archive -Force '%ZIP_FILE%' '%SRC_DIR%'"
if errorlevel 1 goto :fail_net
robocopy "%SRC_DIR%\rec-main" "%INSTALL_DIR%" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto :fail
echo    완료

echo [3/6] Claude 분석 패키지(anthropic) 설치 중...
%PYCMD% -m pip install --quiet --upgrade anthropic
if errorlevel 1 (
    echo    ! anthropic 설치에 실패했습니다. AI 분석 기능만 제한되고 매매는 정상 동작합니다.
) else (
    echo    완료
)

echo [4/6] 자체 점검 실행 중...
pushd "%INSTALL_DIR%"
%PYCMD% -m unittest discover -s tests >nul 2>&1
if errorlevel 1 (
    popd
    echo    ! 자체 점검에 실패했습니다. 설치 상태를 확인해 주세요.
    goto :fail
)
popd
echo    전체 테스트 통과

echo [5/6] 설정 파일 준비 중...
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
        echo [claude]
        echo ; AI 분석 방식. auto = 키가 있으면 API, 없으면 Claude Code 구독 로그인
        echo backend = auto
        echo ; API 방식을 쓸 때만 입력. 구독 로그인 사용 시 비워 두세요.
        echo api_key =
        echo.
        echo [risk]
        echo ; 숫자는 퍼센트 단위입니다.
        echo max_position_pct = 20
        echo order_cash_pct = 10
        echo max_drawdown_pct = 15
        echo ; 평균단가 대비 이만큼 하락하면 전량 손절 매도 ^(0이면 끔^)
        echo stop_loss_pct = 3
        echo ; 매도 후 같은 종목 재매수까지 기다릴 봉 수 ^(과매매 완화^)
        echo reentry_cooldown_bars = 5
    )
)
echo    완료

echo [6/6] 바탕화면 바로가기 만드는 중...
set "DESKTOP="
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "DESKTOP=%%D"
if not defined DESKTOP set "DESKTOP=%USERPROFILE%\Desktop"

> "%DESKTOP%\AutoTrader 실행.bat" (
    echo @echo off
    echo title AutoTrader
    echo cd /d "%INSTALL_DIR%"
    echo echo 브라우저에 대시보드가 열립니다. 이 창은 닫지 마세요.
    echo %PYCMD% -m autotrader ui
    echo pause
)
echo    완료

echo.
echo  =====================================================
echo    설치가 끝났습니다!
echo.
echo    바탕화면의 "AutoTrader 실행"을 더블클릭하면 브라우저에
echo    대시보드가 열립니다. 시작/중지, 시세 차트, 백테스트,
echo    Claude Fable AI 분석을 모두 화면에서 사용합니다.
echo.
echo    [준비 - 최초 1회, 잠시 후 열리는 config.ini에 입력]
echo      1. openapi.kiwoom.com : 키움 REST API 신청 후
echo         appkey / secretkey 발급 (모의투자용)
echo      2. console.anthropic.com : Claude API 키 발급 (AI 분석용)
echo.
echo    실전투자는 config.ini에서 mode=real + 실전용 키로 바꾼 뒤
echo    대시보드 확인란에 YES를 입력해야만 시작됩니다.
echo.
echo    자동매매는 수익을 보장하지 않습니다. 모의투자로 충분히
echo    검증한 뒤에만 실전 전환을 검토하세요.
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
