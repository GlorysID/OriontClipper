@echo off
title OriontClipper Bot - Restart
color 0A

cd /d "%~dp0"

echo ==================================================================
echo                 ME-RESTART BOT ORIONTCLIPPER
echo ==================================================================
echo.

echo [1/2] Menghentikan bot yang sedang berjalan...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$procs = Get-CimInstance Win32_Process | Where-Object { $_.Name -like '*python*' -and $_.CommandLine -like '*bot.py*' }; if ($procs) { $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }"

echo [2/2] Memulai kembali bot...
timeout /t 2 /nobreak >nul

call START_BOT.bat
