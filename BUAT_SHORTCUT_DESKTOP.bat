@echo off
title Buat Shortcut Desktop OriontClipper
color 0A

cd /d "%~dp0"

echo ==================================================================
echo             MEMBUAT SHORTCUT DESKTOP ORIONTCLIPPER
echo ==================================================================
echo.

.venv\Scripts\python.exe scripts\bot_manager.py shortcut

echo.
echo ==================================================================
echo Selesai! Sekarang kamu bisa klik dua kali icon 'OriontClipper Bot'
echo langsung dari Desktop kamu kapan saja.
echo ==================================================================
echo.
pause
