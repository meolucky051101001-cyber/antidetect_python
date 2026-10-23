@echo off
setlocal
title Tat Antidetect Python
cd /d "%~dp0"

echo Dang dung cac profile dang chay...
python cli.py stop-all
if errorlevel 1 (
    echo.
    echo Khong the dung tat ca profile. Server duoc giu lai de tranh bo lai Chrome mo coi.
    if not defined ANTIDETECT_NO_PAUSE pause
    exit /b 1
)

echo Dang dung may chu an toan...
python -m antidetect.server_control stop
if errorlevel 1 (
    echo.
    echo May chu chua tat duoc. Khong force-kill de tranh mat du lieu profile.
    if not defined ANTIDETECT_NO_PAUSE pause
    exit /b 1
)

echo Da tat Antidetect Python.
if not defined ANTIDETECT_NO_PAUSE pause
endlocal
