@echo off
chcp 65001 >nul
setlocal
title AutoTrader 설치 프로그램

rem =====================================================
rem  AutoTrader 원클릭 설치 프로그램 (Windows)
rem  - 32비트 Python 3.10 확인/자동 설치
rem  - 프로그램 다운로드 (github.com/choijinyi/rec)
rem  - 가상환경 + PyQt5 설치, 자체 점검
rem  - 바탕화면에 실행 바로가기 생성
rem =====================================================

set "INSTALL_DIR=%USERPROFILE%\autotrader"
set "REPO_ZIP=https://github.com/choijinyi/rec/archive/refs/heads/main.zip"
set "PY_URL=https://www.python.org/ftp/python/3.10.11/python-3.10.11.exe"
set "PY_SETUP=%TEMP%\python-3.10.11-32bit.exe"
set "ZIP_FILE=%TEMP%\autotrader-main.zip"
set "SRC_DIR=%TEMP%\autotrader-src"
set "VENV_PY=%INSTALL_DIR%\.venv\Scripts\python.exe"

echo.
echo  =====================================================
echo    AutoTrader 설치를 시작합니다 (키움 모의투자 자동매매)
echo    설치 위치: %INSTALL_DIR%
echo  =====================================================
echo.

echo [1/6] 32비트 Python 3.10 확인 중...
py -3.10-32 -V >nul 2>&1
if errorlevel 1 (
    echo    설치되어 있지 않아 자동으로 내려받아 설치합니다. 1~2분 걸립니다...
    powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_SETUP%'"
    if errorlevel 1 goto :fail_net
    start /wait "" "%PY_SETUP%" /passive InstallAllUsers=0 PrependPath=1 Include_launcher=1
)
echo    확인 완료

echo [2/6] 프로그램 내려받는 중...
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -Uri '%REPO_ZIP%' -OutFile '%ZIP_FILE%'; Expand-Archive -Force '%ZIP_FILE%' '%SRC_DIR%'"
if errorlevel 1 goto :fail_net
robocopy "%SRC_DIR%\rec-main" "%INSTALL_DIR%" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto :fail
echo    완료

echo [3/6] 파이썬 가상환경 준비 중...
if not exist "%VENV_PY%" py -3.10-32 -m venv "%INSTALL_DIR%\.venv" 2>nul
if not exist "%VENV_PY%" if exist "%LOCALAPPDATA%\Programs\Python\Python310-32\python.exe" "%LOCALAPPDATA%\Programs\Python\Python310-32\python.exe" -m venv "%INSTALL_DIR%\.venv"
if not exist "%VENV_PY%" (
    echo    ! 32비트 파이썬을 찾지 못했습니다. 컴퓨터를 재시작한 뒤 이 파일을 다시 실행해 주세요.
    goto :fail
)
echo    완료

echo [4/6] 필수 패키지 PyQt5 설치 중...
"%VENV_PY%" -m pip install --quiet --upgrade pip
"%VENV_PY%" -m pip install --quiet PyQt5==5.15.10
if errorlevel 1 goto :fail_net
echo    완료

echo [5/6] 자체 점검 실행 중...
pushd "%INSTALL_DIR%"
"%VENV_PY%" -m unittest discover -s tests >nul 2>&1
if errorlevel 1 (
    popd
    echo    ! 자체 점검에 실패했습니다. 설치 상태를 확인해 주세요.
    goto :fail
)
popd
echo    전체 테스트 통과

echo [6/6] 바탕화면 바로가기 만드는 중...
set "DESKTOP="
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "DESKTOP=%%D"
if not defined DESKTOP set "DESKTOP=%USERPROFILE%\Desktop"

> "%DESKTOP%\AutoTrader 모의투자 시작.bat" (
    echo @echo off
    echo chcp 65001 ^>nul
    echo title AutoTrader 모의투자
    echo cd /d "%INSTALL_DIR%"
    echo echo 키움 로그인 창이 뜨면 반드시 "모의투자" 서버를 선택하세요.
    echo "%VENV_PY%" -m autotrader live --code 005930 --strategy sma_crossover
    echo pause
)
> "%DESKTOP%\AutoTrader 백테스트.bat" (
    echo @echo off
    echo chcp 65001 ^>nul
    echo title AutoTrader 백테스트
    echo cd /d "%INSTALL_DIR%"
    echo "%VENV_PY%" -m autotrader backtest --strategy sma_crossover --days 250
    echo "%VENV_PY%" -m autotrader backtest --strategy rsi_reversion --days 250
    echo pause
)
echo    완료

echo.
echo  =====================================================
echo    설치가 끝났습니다!
echo.
echo    바탕화면에 두 개의 바로가기가 생겼습니다.
echo      - "AutoTrader 백테스트"     : 지금 바로 실행해 볼 수 있습니다.
echo      - "AutoTrader 모의투자 시작" : 아래 준비 후 장중에 실행하세요.
echo.
echo    [모의투자 실행 전 준비 - 키움증권, 최초 1회]
echo      1. 키움증권 홈페이지에서 "모의투자 참가 신청"
echo      2. "키움 Open API+ 사용 신청" 후 OpenAPI+ 모듈 설치
echo         (이 두 가지는 키움 홈페이지에서만 가능해 자동화할 수 없습니다)
echo.
echo    자동매매는 수익을 보장하지 않습니다. 모의투자로 충분히
echo    검증한 뒤에만 실전 전환을 검토하세요.
echo  =====================================================
echo.
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
