#!/data/data/com.termux/files/usr/bin/bash

cd ~/mafioso_bot || exit 1

echo "در حال متوقف کردن نسخه‌های قبلی..."

pkill -f '[p]ython.*bot.py' 2>/dev/null || true
pkill -f '[p]ython.*miniapp/app.py' 2>/dev/null || true
pkill -x cloudflared 2>/dev/null || true

sleep 2

echo "در حال اجرای Mini App..."
nohup python miniapp/app.py > miniapp.log 2>&1 &
echo $! > miniapp.pid

sleep 3

echo "در حال اجرای Cloudflared..."
nohup cloudflared tunnel --url http://127.0.0.1:5000 --no-autoupdate > cloudflared.log 2>&1 &
echo $! > cloudflared.pid

echo "در انتظار دریافت آدرس..."

URL=""
for i in $(seq 1 30); do
    URL=$(grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' cloudflared.log | head -n 1)
    [ -n "$URL" ] && break
    sleep 2
done

if [ -z "$URL" ]; then
    echo "خطا: آدرس Cloudflared دریافت نشد."
    echo "لاگ را بررسی کن: cloudflared.log"
    exit 1
fi

echo "آدرس جدید: $URL"

cp bot.py bot.py.before_start

python - "$URL" <<'PY'
import sys
import re
from pathlib import Path

path = Path("bot.py")
code = path.read_text()
url = sys.argv[1]

code, count = re.subn(
    r'^WEBAPP_URL\s*=\s*["\'].*?["\']',
    f'WEBAPP_URL = "{url}"',
    code,
    count=1,
    flags=re.M
)

if count != 1:
    print("خطا: WEBAPP_URL در bot.py پیدا نشد.")
    sys.exit(1)

path.write_text(code)
PY

if [ $? -ne 0 ]; then
    echo "ربات اجرا نشد؛ آدرس را بررسی کن."
    exit 1
fi

echo "در حال اجرای ربات..."
nohup python bot.py > bot.log 2>&1 &
echo $! > bot.pid

sleep 3

echo "================================"
echo "Mafioso در حال اجراست!"
echo "Mini App: $URL"
echo "لاگ ربات: bot.log"
echo "لاگ مینی‌اپ: miniapp.log"
echo "================================"
