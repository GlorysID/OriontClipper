@echo off
title OriontClipper Bot - AI Video Curator
color 0B

cd /d "%~dp0"

echo ==================================================================
echo             ORIONTCLIPPER - TELEGRAM AI BOT RUNNER
echo ==================================================================
echo.

:: 1. Cek Virtual Environment
if not exist ".venv\Scripts\python.exe" (
    color 0C
    echo [ERROR] Virtual Environment .venv tidak ditemukan!
    echo Silakan jalankan instalasi environment terlebih dahulu.
    pause
    exit /b 1
)

:: 2. Cek File .env
if not exist ".env" (
    color 0C
    echo [WARNING] File .env tidak ditemukan!
    echo Pastikan file .env sudah berisi TELEGRAM_BOT_TOKEN.
    echo.
)

:: 3. Bersihkan proses bot.py lama yang mungkin macet
echo [1/3] Memeriksa dan membersihkan proses bot lama...
.venv\Scripts\python.exe scripts\bot_manager.py stop >nul 2>nul

:: 4. Cek FFmpeg
echo [2/3] Memeriksa FFmpeg...
where ffmpeg >nul 2>nul
if %errorlevel% neq 0 (
    echo [WARNING] FFmpeg tidak ditemukan di PATH sistem! Render video mungkin gagal.
) else (
    echo [OK] FFmpeg siap digunakan.
)

echo [3/3] Memulai Telegram Bot...
echo.
echo ==================================================================
echo  Bot OriontClipper sedang AKTIF dan siap menerima pesan!
echo  (Biarkan jendela ini tetap terbuka. Tekan Ctrl+C untuk berhenti)
echo ==================================================================
echo.

:run_loop
.venv\Scripts\python.exe bot.py
set EXIT_CODE=%errorlevel%

if %EXIT_CODE% neq 0 (
    echo.
    echo ==================================================================
    echo [PERINGATAN] Bot terhenti dengan kode error: %EXIT_CODE%
    echo Me-restart otomatis dalam 5 detik... Tekan CTRL+C untuk membatalkan.
    echo ==================================================================
    timeout /t 5 /nobreak >nul
    goto run_loop
)

echo.
echo [INFO] Bot berhenti secara normal.
pause
