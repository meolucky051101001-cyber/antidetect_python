@echo off
setlocal
title Cai dat Antidetect Python
cd /d "%~dp0"

rem Tao moi truong .venv rieng bang CPython 3.10 x64 va cai dependency khoa hash.
rem Co the chi dinh interpreter: set ANTIDETECT_PYTHON=C:\Python310\python.exe
set "CHECK=import sys,struct; raise SystemExit(0 if sys.version_info[:2]==(3,10) and struct.calcsize('P')==8 else 1)"
set "VENV_PY=.venv\Scripts\python.exe"

if exist "%VENV_PY%" (
    "%VENV_PY%" -c "%CHECK%" >nul 2>nul
    if errorlevel 1 (
        echo .venv hien tai khong phai CPython 3.10 x64. Xoa thu muc .venv roi chay lai setup.bat.
        if not defined ANTIDETECT_NO_PAUSE pause
        exit /b 1
    )
    goto install
)

set "BASE_PY="
if defined ANTIDETECT_PYTHON (
    "%ANTIDETECT_PYTHON%" -c "%CHECK%" >nul 2>nul && set "BASE_PY=%ANTIDETECT_PYTHON%"
)
if not defined BASE_PY (
    py -3.10-64 -c "%CHECK%" >nul 2>nul && for /f "delims=" %%I in ('py -3.10-64 -c "import sys; print(sys.executable)"') do set "BASE_PY=%%I"
)
if not defined BASE_PY (
    python -c "%CHECK%" >nul 2>nul && for /f "delims=" %%I in ('python -c "import sys; print(sys.executable)"') do set "BASE_PY=%%I"
)
if not defined BASE_PY (
    echo Khong tim thay CPython 3.10 x64.
    echo Cai Python 3.10 x64 tu python.org hoac dat ANTIDETECT_PYTHON tro toi python.exe 3.10 x64.
    if not defined ANTIDETECT_NO_PAUSE pause
    exit /b 1
)

echo Tao .venv bang %BASE_PY%
"%BASE_PY%" -m venv .venv
if errorlevel 1 goto fail

:install
echo Cai dependency khoa hash vao .venv...
"%VENV_PY%" -m pip install --require-hashes --only-binary=:all: -r requirements-win-py310.lock.txt
if errorlevel 1 goto fail
"%VENV_PY%" -m pip check
if errorlevel 1 goto fail

echo.
echo Cai dat xong. Chay start.bat de mo dashboard.
if not defined ANTIDETECT_NO_PAUSE pause
exit /b 0

:fail
echo.
echo Cai dat that bai. Xem loi o tren.
if not defined ANTIDETECT_NO_PAUSE pause
exit /b 1
