#!/data/data/com.termux/files/usr/bin/bash

cd ~/mafioso_bot || exit 1

echo "Starting Mafioso..."

pkill -f '[b]ot.py' 2>/dev/null
pkill -f '[p]ython app.py' 2>/dev/null
pkill -f '[c]loudflared tunnel' 2>/dev/null

sleep 2

# Start Mini App
cd ~/mafioso_bot/miniapp
nohup python app.py > flask.log 2>&1 &

sleep 3

# Start Cloudflare
nohup cloudflared tunnel --protocol http2 \
--url http://127.0.0.1:5000 \
> ~/mafioso_bot/cloudflare.log 2>&1 &

echo "Waiting for Cloudflare URL..."

URL=""
for i in $(seq 1 30); do
    URL=$(grep -o 'https://[-a-zA-Z0-9]*\.trycloudflare\.com' \
    ~/mafioso_bot/cloudflare.log | head -n 1)
    [ -n "$URL" ] && break
    sleep 2
done

if [ -z "$URL" ]; then
    echo "Cloudflare failed. Check cloudflare.log"
    exit 1
fi

echo "Mini App URL: $URL"

# Update bot Mini App URL
cd ~/mafioso_bot

sed -i -E "s#^(WEBAPP_URL[[:space:]]*=[[:space:]]*)['\"][^'\"]*['\"]#\1\"$URL\"#" bot.py

# Start Telegram bot
nohup python bot.py > bot.log 2>&1 &

echo "--------------------------"
echo "MAFIOSO STARTED!"
echo "Mini App: $URL"
echo "Bot and Flask started."
echo "--------------------------"
