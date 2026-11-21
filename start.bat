@echo off
title Antidetect Python
cd /d "%~dp0"
set "PYEXE=python"
if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
) else (
    echo [Canh bao] Chua co .venv, dang dung Python toan cuc. Chay setup.bat de cai moi truong rieng.
)
"%PYEXE%" run.py
pause
