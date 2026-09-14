"""OriontClipper Bot Process Manager.

Provides robust process management: start, stop, status, restart, and shortcut.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def get_bot_processes() -> list[dict[str, Any]]:
    """Return list of running bot.py and START_BOT runner processes."""
    ps_cmd = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { "
        "  ($_.Name -like '*python*' -and $_.CommandLine -match '\\bbot\\.py' -and $_.CommandLine -notlike '*bot_manager*') -or "
        "  ($_.Name -like '*cmd*' -and $_.CommandLine -like '*START_BOT.bat*') -or "
        "  ($_.Name -like '*wscript*' -and $_.CommandLine -like '*JALANKAN_DI_BACKGROUND*') "
        "} | "
        "Select-Object ProcessId, Name, CommandLine | "
        "ConvertTo-Json"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            check=True,
        )
        stdout = res.stdout.strip()
        if not stdout:
            return []
        import json
        data = json.loads(stdout)
        if isinstance(data, dict):
            return [data]
        elif isinstance(data, list):
            return data
        return []
    except Exception:
        return []


def stop_bot() -> int:
    """Safely terminate all running bot.py and runner processes."""
    procs = get_bot_processes()
    if not procs:
        print("[INFO] Tidak ada proses bot.py yang sedang berjalan.")
        return 0

    print(f"[!] Ditemukan {len(procs)} proses bot/runner aktif. Menghentikan...")
    for p in procs:
        pid = p.get("ProcessId")
        name = p.get("Name", "Process")
        if pid:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    text=True,
                )
                print(f"[OK] Berhasil menghentikan {name} (PID: {pid})")
            except Exception as e:
                print(f"[ERROR] Gagal menghentikan PID {pid}: {e}")
    return 0


def status_bot() -> int:
    """Display current bot running status."""
    procs = get_bot_processes()
    print("==================================================================")
    print("                    STATUS BOT ORIONTCLIPPER                      ")
    print("==================================================================")
    if procs:
        print("[STATUS]  AKTIF / ONLINE")
        print(f"[INSTANSI] {len(procs)} proses sedang berjalan:")
        for p in procs:
            print(f"  - PID: {p.get('ProcessId')}")
    else:
        print("[STATUS]  TIDAK AKTIF / OFFLINE")
        print("Bot sedang mati atau belum dijalankan.")
    print("==================================================================")
    return 0 if procs else 1


def create_desktop_shortcut() -> int:
    """Create Windows desktop shortcuts with custom icon."""
    try:
        icon_path = ROOT_DIR / "assets" / "bot_icon.ico"
        icon_str = str(icon_path) if icon_path.exists() else ""

        ps_script = f"""
$ws = New-Object -ComObject WScript.Shell
$desk = [Environment]::GetFolderPath('Desktop')

# 1. Main Shortcut (Jalankan Bot dengan Terminal / Live Log)
$sc1 = $ws.CreateShortcut("$desk\\OriontClipper Bot.lnk")
$sc1.TargetPath = '{ROOT_DIR / "START_BOT.bat"}'
$sc1.WorkingDirectory = '{ROOT_DIR}'
$sc1.Description = 'Aktifkan OriontClipper Bot (Live Console)'
if ('{icon_str}' -ne '') {{ $sc1.IconLocation = '{icon_str}' }}
$sc1.Save()

# 2. Background Runner Shortcut (Jalankan di Background tanpa Jendela)
$sc2 = $ws.CreateShortcut("$desk\\OriontClipper (Background).lnk")
$sc2.TargetPath = 'wscript.exe'
$sc2.Arguments = '"{ROOT_DIR / "JALANKAN_DI_BACKGROUND.vbs"}"'
$sc2.WorkingDirectory = '{ROOT_DIR}'
$sc2.Description = 'Aktifkan OriontClipper Bot di Background (Tanpa Jendela)'
if ('{icon_str}' -ne '') {{ $sc2.IconLocation = '{icon_str}' }}
$sc2.Save()

# 3. Control Center Shortcut (Pusat Kontrol Menu)
$sc3 = $ws.CreateShortcut("$desk\\Pusat Kontrol OriontClipper.lnk")
$sc3.TargetPath = '{ROOT_DIR / "MENU.bat"}'
$sc3.WorkingDirectory = '{ROOT_DIR}'
$sc3.Description = 'Pusat Kontrol Bot OriontClipper (Menu Lengkap)'
if ('{icon_str}' -ne '') {{ $sc3.IconLocation = '{icon_str}' }}
$sc3.Save()

# 4. Stop Bot Shortcut (Matikan Bot 1-Klik)
$sc4 = $ws.CreateShortcut("$desk\\Matikan Bot OriontClipper.lnk")
$sc4.TargetPath = '{ROOT_DIR / "STOP_BOT.bat"}'
$sc4.WorkingDirectory = '{ROOT_DIR}'
$sc4.Description = 'Hentikan Bot OriontClipper yang sedang berjalan'
if ('{icon_str}' -ne '') {{ $sc4.IconLocation = '{icon_str}' }}
$sc4.Save()

Write-Host '[OK] Shortcut Desktop berhasil diperbarui (termasuk Matikan Bot)!' -ForegroundColor Green
"""
        res = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True,
            text=True,
            check=True,
        )
        print(res.stdout.strip())
        return 0
    except Exception as e:
        print(f"[ERROR] Gagal membuat shortcut: {e}")
        return 1


def start_bot() -> int:
    """Start the bot in background if not already running."""
    procs = get_bot_processes()
    if procs:
        print(f"[INFO] Bot sudah berjalan dengan PID: {procs[0].get('ProcessId')}")
        return 0
    py_exec = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
    if not py_exec.exists():
        py_exec = Path(sys.executable)
    print(f"[*] Memulai OriontClipper bot dengan {py_exec}...")
    DETACHED_FLAG = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [str(py_exec), "bot.py"],
        cwd=str(ROOT_DIR),
        creationflags=DETACHED_FLAG,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    time.sleep(2)
    return status_bot()


def restart_bot() -> int:
    """Stop all running instances and start a single fresh instance."""
    stop_bot()
    time.sleep(2)
    return start_bot()


if __name__ == "__main__":
    action = sys.argv[1].lower() if len(sys.argv) > 1 else "status"
    if action == "start":
        sys.exit(start_bot())
    elif action == "stop":
        sys.exit(stop_bot())
    elif action == "restart":
        sys.exit(restart_bot())
    elif action == "status":
        sys.exit(status_bot())
    elif action == "shortcut":
        sys.exit(create_desktop_shortcut())
    else:
        print(f"Usage: {sys.argv[0]} [start|stop|restart|status|shortcut]")
        sys.exit(1)
