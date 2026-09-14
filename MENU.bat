@echo off
title OriontClipper - Pusat Kontrol Bot
color 0B

:menu_loop
cls
cd /d "%~dp0"

echo ==================================================================
echo           ORIONTCLIPPER - PUSAT KONTROL BOT TELEGRAM
echo ==================================================================
echo.
echo   [1] Jalankan Bot (Tampilan Layar / Live Log)
echo   [2] Jalankan Bot di Background (Tanpa Jendela CMD)
echo   [3] Cek Status Bot (Apakah bot sedang aktif?)
echo   [4] Matikan Bot
echo   [5] Restart Bot
echo   [6] Buat Shortcut di Desktop
echo   [7] Buka File Log Terakhir (Notepad)
echo   [0] Keluar
echo.
echo ==================================================================
set /p PILIHAN="Pilih nomor menu (0-7): "

if "%PILIHAN%"=="1" goto opt_start
if "%PILIHAN%"=="2" goto opt_bg
if "%PILIHAN%"=="3" goto opt_status
if "%PILIHAN%"=="4" goto opt_stop
if "%PILIHAN%"=="5" goto opt_restart
if "%PILIHAN%"=="6" goto opt_shortcut
if "%PILIHAN%"=="7" goto opt_log
if "%PILIHAN%"=="0" goto opt_exit

echo.
echo [!] Pilihan tidak valid, silakan coba lagi.
ping 127.0.0.1 -n 2 >nul
goto menu_loop

:opt_start
cls
call START_BOT.bat
goto menu_loop

:opt_bg
cls
echo Menjalankan bot di background...
wscript "%~dp0JALANKAN_DI_BACKGROUND.vbs"
ping 127.0.0.1 -n 2 >nul
goto menu_loop

:opt_status
cls
.venv\Scripts\python.exe scripts\bot_manager.py status
echo.
pause
goto menu_loop

:opt_stop
cls
call STOP_BOT.bat
goto menu_loop

:opt_restart
cls
call RESTART_BOT.bat
goto menu_loop

:opt_shortcut
cls
call BUAT_SHORTCUT_DESKTOP.bat
goto menu_loop

:opt_log
cls
if exist "logs\app.log" (
    echo Membuka logs\app.log...
    start notepad "logs\app.log"
) else (
    echo [INFO] File logs\app.log belum ada.
)
ping 127.0.0.1 -n 2 >nul
goto menu_loop

:opt_exit
exit /b 0
