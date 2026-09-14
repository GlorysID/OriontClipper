@echo off
title OriontClipper Bot - Stop
color 0E

cd /d "%~dp0"

echo ==================================================================
echo                 MENGHENTIKAN BOT ORIONTCLIPPER
echo ==================================================================
echo.

.venv\Scripts\python.exe scripts\bot_manager.py stop

echo.
echo ==================================================================
echo [OK] Selesai.
echo ==================================================================
echo.
pause
