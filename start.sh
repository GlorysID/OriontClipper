#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

# Kill old processes
pkill -f "cloudflared tunnel --url" 2>/dev/null || true
pkill -f "contentclipper-bot" 2>/dev/null || true
screen -S contentclipper-bot -X quit 2>/dev/null || true
sleep 2

# Start Cloudflare Tunnel
nohup cloudflared tunnel --url http://localhost:8765 --no-autoupdate &>/tmp/cf_tunnel.log &
CF_PID=$!
echo "Cloudflare tunnel starting (PID $CF_PID)..."

# Wait for tunnel URL
URL=""
for i in $(seq 1 15); do
    URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/cf_tunnel.log 2>/dev/null | head -1)
    if [ -n "$URL" ]; then
        echo "$URL" > .public_url
        echo "Tunnel URL: $URL"
        break
    fi
    sleep 2
done

if [ -z "$URL" ]; then
    echo "WARNING: Tunnel URL not found, check /tmp/cf_tunnel.log"
fi

# Start bot
screen -dmS contentclipper-bot .venv/bin/python bot.py
echo "Bot started in screen 'contentclipper-bot'"
