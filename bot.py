from pathlib import Path
import sqlite3
import json
import re
import asyncio
import time
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from telegram.error import TelegramError

ADMIN_ID = 9184824727
COIN_NAME = "Mafia Coin"
REWARD = 250
REFERRAL_REWARD = 1000
WEBAPP_URL = "https://teaching-mpg-overhead-brave.trycloudflare.com"

BASE_DIR = Path.home() / "mafioso_bot"
DB_PATH = BASE_DIR / "mafioso.db"


def get_db():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT NOT NULL,
            qtype TEXT NOT NULL,
            options TEXT,
            correct_answer TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            answer TEXT NOT NULL,
            correct INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(question_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS required_channels (
            channel_id TEXT PRIMARY KEY,
            channel_title TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS referrals (
            referred_id INTEGER PRIMARY KEY,
            referrer_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS godfather_claims (
                user_id INTEGER PRIMARY KEY,
                last_claim INTEGER NOT NULL
            )
        """)

        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(users)")
        }
        if "first_name" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN first_name TEXT DEFAULT ''")
        if "last_name" not in columns:
            db.execute("ALTER TABLE users ADD COLUMN last_name TEXT DEFAULT ''")


def register_user(user, referral_payload=None):
    with get_db() as db:
        existing = db.execute(
            "SELECT user_id FROM users WHERE user_id=?",
            (user.id,)
        ).fetchone()

        db.execute(
            """INSERT INTO users(user_id, username, first_name, last_name)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id)
               DO UPDATE SET username=excluded.username,
                             first_name=excluded.first_name,
                             last_name=excluded.last_name""",
            (user.id, user.username or "", user.first_name or "", user.last_name or "")
        )

        if existing is None and referral_payload:
            match = re.fullmatch(r"ref_(\d+)", referral_payload)
            if match:
                referrer_id = int(match.group(1))
                if referrer_id != user.id:
                    referrer = db.execute(
                        "SELECT user_id, banned FROM users WHERE user_id=?",
                        (referrer_id,)
                    ).fetchone()

                    if referrer and not referrer["banned"]:
                        result = db.execute(
                            """INSERT OR IGNORE INTO referrals
                               (referred_id, referrer_id, created_at)
                               VALUES (?, ?, CAST(strftime('%s','now') AS INTEGER))""",
                            (user.id, referrer_id)
                        )
                        if result.rowcount:
                            db.execute(
                                "UPDATE users SET balance=balance+? WHERE user_id=?",
                                (REFERRAL_REWARD, referrer_id)
                            )


def banned(user_id):
    with get_db() as db:
        row = db.execute(
            "SELECT banned FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
    return bool(row and row["banned"])


def normalize(value):
    value = str(value).strip().lower()
    value = value.replace("ي", "ی").replace("ك", "ک")
    return re.sub(r"\s+", " ", value)


def is_admin(user_id):
    return user_id == ADMIN_ID


async def missing_channels(user_id, context):
    with get_db() as db:
        channels = db.execute(
            "SELECT channel_id, channel_title FROM required_channels"
        ).fetchall()

    missing = []
    for channel in channels:
        try:
            member = await context.bot.get_chat_member(
                chat_id=channel["channel_id"],
                user_id=user_id
            )
            if member.status in ("left", "kicked"):
                missing.append(channel)
        except TelegramError:
            missing.append(channel)

    return missing


async def show_join(update, context, channels):
    buttons = []
    for channel in channels:
        title = channel["channel_title"]
        if title.startswith("@"):
            url = "https://t.me/" + title[1:]
            buttons.append([
                InlineKeyboardButton("عضویت در " + title, url=url)
            ])

    buttons.append([
        InlineKeyboardButton("بررسی عضویت ✅", callback_data="check_join")
    ])

    text = "برای استفاده از ربات، ابتدا در کانال‌های زیر عضو شو:"
    if update.callback_query:
        await update.callback_query.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )


def main_keyboard(user_id):
    buttons = [
        [InlineKeyboardButton(
            "🎮 ورود به Mini App",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )],
        [
            InlineKeyboardButton("🏆 مسابقات", callback_data="competitions"),
            InlineKeyboardButton("🥇 رتبه‌بندی", callback_data="ranking")
        ],
        [InlineKeyboardButton("🎁 دعوت دوستان", callback_data="referral")]
    ]
    if is_admin(user_id):
        buttons.append([
            InlineKeyboardButton("👑 پنل مدیریت", callback_data="admin")
        ])
    return InlineKeyboardMarkup(buttons)



async def referral_menu(update, context):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    if banned(user_id):
        await query.message.reply_text("⛔ دسترسی شما مسدود است.")
        return

    bot_info = await context.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"

    with get_db() as db:
        invited = db.execute(
            """SELECT u.user_id, u.username, u.first_name, u.last_name
               FROM referrals r
               JOIN users u ON u.user_id = r.referred_id
               WHERE r.referrer_id=?
               ORDER BY r.created_at DESC""",
            (user_id,)
        ).fetchall()

    total = len(invited)
    earned = total * REFERRAL_REWARD

    names = []
    for person in invited[:20]:
        full_name = " ".join(
            part for part in (person["first_name"], person["last_name"]) if part
        ).strip()
        display_name = full_name or person["username"] or "بدون نام"
        username = f" (@{person['username']})" if person["username"] else ""
        names.append(f"• {display_name}{username} — ID: {person['user_id']}")

    invited_text = "\\n".join(names) if names else "هنوز کسی با لینک تو وارد نشده."
    if total > 20:
        invited_text += f"\\n... و {total - 20} دعوت دیگر"

    text = (
        "🎁 سیستم دعوت دوستان Mafioso\\n\\n"
        f"👥 تعداد دعوت‌های موفق: {total}\\n"
        f"💰 مجموع پاداش دریافتی: {earned} Mafia Coin\\n"
        f"🪙 پاداش هر دعوت: {REFERRAL_REWARD} Mafia Coin\\n\\n"
        "👤 افراد دعوت‌شده:\\n"
        f"{invited_text}\\n\\n"
        "🔗 لینک اختصاصی دعوت شما:\\n"
        f"{link}\\n\\n"
        "با هر دعوت موفق، ۱۰۰۰ Mafia Coin دریافت می‌کنی."
    )

    buttons = [[InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu")]]
    await query.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(buttons)
    )

async def show_menu(update, context):
    user = update.effective_user
    text = (
        "🕶 به خانواده Mafioso خوش آمدی!\n\n"
        "🪙 واحد پول: Mafia Coin\n"
        "🎮 از Mini App سکه جمع کن.\n"
        "🏆 در مسابقات شرکت کن و جایزه بگیر."
    )

    if update.callback_query:
        await update.callback_query.message.reply_text(
            text, reply_markup=main_keyboard(user.id)
        )
    else:
        await update.message.reply_text(
            text, reply_markup=main_keyboard(user.id)
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    payload = context.args[0] if context.args else None
    register_user(user, payload)

    if banned(user.id):
        await update.message.reply_text("⛔ دسترسی شما به ربات مسدود شده است.")
        return

    if not is_admin(user.id):
        missing = await missing_channels(user.id, context)
        if missing:
            await show_join(update, context, missing)
            return

    await show_menu(update, context)


async def check_join(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if is_admin(user_id):
        await show_menu(update, context)
        return

    missing = await missing_channels(user_id, context)
    if missing:
        await show_join(update, context, missing)
    else:
        await show_menu(update, context)


async def main_menu(update, context):
    query = update.callback_query
    await query.answer()

    if banned(query.from_user.id):
        await query.message.reply_text("⛔ دسترسی شما مسدود است.")
        return

    await show_menu(update, context)


async def competitions(update, context):
    query = update.callback_query
    await query.answer()

    if banned(query.from_user.id):
        await query.message.reply_text("⛔ دسترسی شما مسدود است.")
        return

    with get_db() as db:
        questions = db.execute(
            "SELECT id, question FROM questions WHERE active=1 ORDER BY id DESC"
        ).fetchall()

    if not questions:
        await query.message.reply_text("فعلاً مسابقه فعالی وجود ندارد.")
        return

    buttons = []
    for q in questions:
        buttons.append([
            InlineKeyboardButton(
                "❓ " + q["question"][:45],
                callback_data=f"q:{q['id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton("🔙 بازگشت", callback_data="menu")
    ])

    await query.message.reply_text(
        "🏆 مسابقات Mafioso\nهر پاسخ صحیح = ۲۵۰ Mafia Coin",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def open_question(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    qid = int(query.data.split(":")[1])

    if banned(user_id):
        await query.message.reply_text("⛔ دسترسی شما مسدود است.")
        return

    with get_db() as db:
        q = db.execute(
            "SELECT * FROM questions WHERE id=? AND active=1",
            (qid,)
        ).fetchone()
        old = db.execute(
            "SELECT id FROM answers WHERE question_id=? AND user_id=?",
            (qid, user_id)
        ).fetchone()

    if not q:
        await query.message.reply_text("این سؤال دیگر فعال نیست.")
        return

    if old:
        await query.message.reply_text("شما قبلاً به این سؤال پاسخ داده‌ای.")
        return

    if q["qtype"] == "mc":
        options = json.loads(q["options"])
        buttons = []
        for index, option in enumerate(options):
            buttons.append([
                InlineKeyboardButton(
                    option, callback_data=f"a:{qid}:{index}"
                )
            ])
        await query.message.reply_text(
            "❓ " + q["question"],
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        context.user_data["answering_question"] = qid
        await query.message.reply_text(
            "❓ " + q["question"] + "\n\nپاسخت را در یک پیام بنویس:"
        )


def save_answer(user_id, qid, answer):
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")

        q = db.execute(
            "SELECT * FROM questions WHERE id=? AND active=1",
            (qid,)
        ).fetchone()

        if not q:
            db.rollback()
            return None

        old = db.execute(
            "SELECT id FROM answers WHERE question_id=? AND user_id=?",
            (qid, user_id)
        ).fetchone()

        if old:
            db.rollback()
            return "already"

        correct = normalize(answer) == normalize(q["correct_answer"])

        db.execute(
            """INSERT INTO answers
               (question_id, user_id, answer, correct, created_at)
               VALUES (?, ?, ?, ?, strftime('%s','now'))""",
            (qid, user_id, str(answer), int(correct))
        )

        if correct:
            db.execute(
                "UPDATE users SET balance=balance+? WHERE user_id=?",
                (REWARD, user_id)
            )

        db.commit()
        return correct


async def answer_choice(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if banned(user_id):
        await query.message.reply_text("⛔ دسترسی شما مسدود است.")
        return

    _, qid, index = query.data.split(":")
    qid, index = int(qid), int(index)

    with get_db() as db:
        q = db.execute(
            "SELECT options FROM questions WHERE id=? AND active=1",
            (qid,)
        ).fetchone()

    if not q:
        await query.message.reply_text("سؤال دیگر فعال نیست.")
        return

    options = json.loads(q["options"])
    if index < 0 or index >= len(options):
        return

    result = save_answer(user_id, qid, options[index])
    await send_answer_result(query.message, result)


async def send_answer_result(message, result):
    if result is True:
        await message.reply_text(
            "🎉 آفرین! پاسخ صحیح بود.\n"
            "🪙 ۲۵۰ Mafia Coin به موجودی تو اضافه شد."
        )
    elif result is False:
        await message.reply_text("❌ پاسخ اشتباه بود. موفق باشی!")
    elif result == "already":
        await message.reply_text("شما قبلاً به این سؤال پاسخ داده‌ای.")
    else:
        await message.reply_text("این سؤال در دسترس نیست.")


async def ranking(update, context):
    query = update.callback_query
    await query.answer()

    with get_db() as db:
        rows = db.execute(
            """SELECT user_id, username, balance FROM users
               WHERE banned=0
               ORDER BY balance DESC, user_id ASC LIMIT 10"""
        ).fetchall()

    if not rows:
        await query.message.reply_text("هنوز کاربری در رتبه‌بندی نیست.")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 رتبه‌بندی Mafioso\n"]
    for i, row in enumerate(rows, 1):
        name = row["username"] or f"کاربر {row['user_id']}"
        medal = medals[i-1] if i <= 3 else f"{i}."
        lines.append(
            f"{medal} {name} — {row['balance']} Mafia Coin"
        )

    await query.message.reply_text("\n".join(lines))


def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ ساخت سؤال", callback_data="admin:make"),
            InlineKeyboardButton("📝 مدیریت سؤال‌ها", callback_data="admin:questions")
        ],
        [
            InlineKeyboardButton("💰 مدیریت سکه", callback_data="admin:points"),
            InlineKeyboardButton("🚫 بن کاربر", callback_data="admin:ban")
        ],
        [
            InlineKeyboardButton("🔓 آنبن کاربر", callback_data="admin:unban"),
            InlineKeyboardButton("📢 پیام همگانی", callback_data="admin:broadcast")
        ],
        [
            InlineKeyboardButton("➕ افزودن کانال", callback_data="admin:addch"),
            InlineKeyboardButton("➖ حذف کانال", callback_data="admin:delch")
        ],
        [InlineKeyboardButton("🔙 منوی اصلی", callback_data="menu")]
    ])


async def admin_panel(update, context):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    await query.message.reply_text(
        "👑 پنل مدیریت Mafioso",
        reply_markup=admin_keyboard()
    )


async def admin_command(update, context):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "👑 پنل مدیریت Mafioso",
        reply_markup=admin_keyboard()
    )


async def admin_callbacks(update, context):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.message.reply_text("⛔ دسترسی غیرمجاز.")
        return

    parts = query.data.split(":")
    action = parts[1]

    if action == "make":
        context.user_data["admin_state"] = "q_type"
        await query.message.reply_text(
            "نوع سؤال را انتخاب کن:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("چهارگزینه‌ای", callback_data="admin:type:mc")],
                [InlineKeyboardButton("تشریحی", callback_data="admin:type:text")]
            ])
        )

    elif action == "type":
        context.user_data["q_type"] = parts[2]
        context.user_data["admin_state"] = "q_text"
        await query.message.reply_text("متن سؤال را بفرست:")

    elif action == "questions":
        with get_db() as db:
            rows = db.execute(
                "SELECT id, question FROM questions WHERE active=1 ORDER BY id DESC"
            ).fetchall()

        if not rows:
            await query.message.reply_text("سؤال فعالی وجود ندارد.")
            return

        buttons = [
            [InlineKeyboardButton(
                "❌ غیرفعال کردن: " + row["question"][:35],
                callback_data=f"admin:off:{row['id']}"
            )]
            for row in rows
        ]
        await query.message.reply_text(
            "مدیریت سؤال‌ها:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif action == "off":
        qid = int(parts[2])
        with get_db() as db:
            db.execute(
                "UPDATE questions SET active=0 WHERE id=?",
                (qid,)
            )
        await query.message.reply_text("سؤال غیرفعال شد.")

    elif action == "points":
        context.user_data["admin_state"] = "points_user"
        await query.message.reply_text("شناسه عددی تلگرام کاربر را بفرست:")

    elif action in ("ban", "unban"):
        context.user_data["admin_state"] = action + "_user"
        await query.message.reply_text("شناسه عددی تلگرام کاربر را بفرست:")

    elif action == "broadcast":
        context.user_data["admin_state"] = "broadcast"
        await query.message.reply_text("متن پیام همگانی را بفرست:")

    elif action == "addch":
        context.user_data["admin_state"] = "channel_add"
        await query.message.reply_text(
            "آیدی عمومی کانال را بفرست؛ مثلاً @MyChannel\n"
            "ربات باید در کانال ادمین باشد."
        )

    elif action == "delch":
        with get_db() as db:
            rows = db.execute(
                "SELECT channel_id, channel_title FROM required_channels"
            ).fetchall()

        if not rows:
            await query.message.reply_text("کانال اجباری ثبت نشده.")
            return

        buttons = [
            [InlineKeyboardButton(
                "حذف " + row["channel_title"],
                callback_data="admin:delchannel:" + row["channel_id"]
            )]
            for row in rows
        ]
        await query.message.reply_text(
            "کانال موردنظر را انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif action == "delchannel":
        with get_db() as db:
            db.execute(
                "DELETE FROM required_channels WHERE channel_id=?",
                (parts[2],)
            )
        await query.message.reply_text("کانال از جوین اجباری حذف شد.")


async def admin_text(update, context):
    state = context.user_data.get("admin_state")
    if not state or not is_admin(update.effective_user.id):
        return False

    text = update.message.text.strip()

    if state == "q_text":
        context.user_data["q_text"] = text
        if context.user_data.get("q_type") == "mc":
            context.user_data["admin_state"] = "q_options"
            await update.message.reply_text(
                "چهار گزینه را با علامت | از هم جدا کن.\n"
                "مثال:\nگزینه اول | گزینه دوم | گزینه سوم | گزینه چهارم"
            )
        else:
            context.user_data["admin_state"] = "q_correct_text"
            await update.message.reply_text("پاسخ صحیح را بنویس:")
        return True

    if state == "q_options":
        options = [x.strip() for x in text.split("|")]
        if len(options) != 4 or any(not x for x in options):
            await update.message.reply_text(
                "باید دقیقاً چهار گزینه را با | جدا کنی."
            )
            return True

        context.user_data["q_options"] = options
        context.user_data["admin_state"] = "q_correct_index"
        await update.message.reply_text(
            "شماره گزینه صحیح را بفرست: ۱، ۲، ۳ یا ۴"
        )
        return True

    if state == "q_correct_index":
        digits = text.translate(str.maketrans("۱۲۳۴", "1234"))
        if digits not in ("1", "2", "3", "4"):
            await update.message.reply_text("فقط عدد ۱ تا ۴ را بفرست.")
            return True

        options = context.user_data["q_options"]
        correct = options[int(digits)-1]
        save_question(context, "mc", json.dumps(options, ensure_ascii=False), correct)
        await update.message.reply_text("✅ سؤال چهارگزینه‌ای ساخته شد.")
        context.user_data.clear()
        return True

    if state == "q_correct_text":
        save_question(context, "text", None, text)
        await update.message.reply_text("✅ سؤال تشریحی ساخته شد.")
        context.user_data.clear()
        return True

    if state == "points_user":
        if not text.isdigit():
            await update.message.reply_text("شناسه باید عددی باشد.")
            return True
        context.user_data["target_user"] = int(text)
        context.user_data["admin_state"] = "points_amount"
        await update.message.reply_text(
            "مقدار تغییر سکه را با علامت بفرست.\n"
            "مثال: 500 برای افزایش یا -250 برای کاهش"
        )
        return True

    if state == "points_amount":
        try:
            amount = int(text)
        except ValueError:
            await update.message.reply_text("یک عدد صحیح بفرست.")
            return True

        target = context.user_data["target_user"]
        with get_db() as db:
            db.execute(
                "INSERT OR IGNORE INTO users(user_id, username) VALUES (?, '')",
                (target,)
            )
            db.execute(
                "UPDATE users SET balance=MAX(0, balance+?) WHERE user_id=?",
                (amount, target)
            )
            row = db.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (target,)
            ).fetchone()

        await update.message.reply_text(
            f"✅ موجودی کاربر: {row['balance']} Mafia Coin"
        )
        context.user_data.clear()
        return True

    if state in ("ban_user", "unban_user"):
        if not text.isdigit():
            await update.message.reply_text("شناسه باید عددی باشد.")
            return True

        target = int(text)
        with get_db() as db:
            db.execute(
                "INSERT OR IGNORE INTO users(user_id, username) VALUES (?, '')",
                (target,)
            )
            db.execute(
                "UPDATE users SET banned=? WHERE user_id=?",
                (1 if state == "ban_user" else 0, target)
            )

        await update.message.reply_text(
            "✅ کاربر مسدود شد." if state == "ban_user"
            else "✅ مسدودیت کاربر برداشته شد."
        )
        context.user_data.clear()
        return True

    if state == "broadcast":
        with get_db() as db:
            ids = db.execute(
                "SELECT user_id FROM users WHERE banned=0"
            ).fetchall()

        sent = 0
        failed = 0
        for row in ids:
            try:
                await context.bot.send_message(
                    chat_id=row["user_id"], text=text
                )
                sent += 1
                await asyncio.sleep(0.04)
            except TelegramError:
                failed += 1

        await update.message.reply_text(
            f"📢 پیام همگانی تمام شد.\n"
            f"ارسال موفق: {sent}\nناموفق: {failed}"
        )
        context.user_data.clear()
        return True

    if state == "channel_add":
        if not text.startswith("@"):
            await update.message.reply_text(
                "لطفاً آیدی عمومی کانال را با @ بفرست."
            )
            return True

        try:
            chat = await context.bot.get_chat(text)
            me = await context.bot.get_me()
            bot_member = await context.bot.get_chat_member(chat.id, me.id)

            if bot_member.status not in ("administrator", "creator"):
                await update.message.reply_text(
                    "ابتدا ربات را در کانال ادمین کن."
                )
                return True

            with get_db() as db:
                db.execute(
                    """INSERT OR REPLACE INTO required_channels
                       (channel_id, channel_title) VALUES (?, ?)""",
                    (str(chat.id), "@" + chat.username)
                )

            await update.message.reply_text("✅ کانال به جوین اجباری اضافه شد.")
            context.user_data.clear()

        except TelegramError:
            await update.message.reply_text(
                "کانال پیدا نشد یا دسترسی ربات کافی نیست."
            )
        return True

    return False


def save_question(context, qtype, options, correct):
    import time
    with get_db() as db:
        db.execute(
            """INSERT INTO questions
               (question, qtype, options, correct_answer, active, created_at)
               VALUES (?, ?, ?, ?, 1, ?)""",
            (
                context.user_data["q_text"],
                qtype,
                options,
                correct,
                int(time.time())
            )
        )



async def godfather_handler(update, context):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    if chat.type not in ("group", "supergroup"):
        return

    if normalize(message.text or "") != normalize("پسرخوانده"):
        return

    register_user(user)

    if banned(user.id):
        await message.reply_text("⛔ دسترسی شما مسدود است.")
        return

    now = int(time.time())
    claimed = False
    remaining = 0

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT last_claim FROM godfather_claims WHERE user_id=?",
            (user.id,)
        ).fetchone()

        if row:
            remaining = max(0, 120 - (now - row["last_claim"]))

        if remaining == 0:
            db.execute(
                """
                INSERT INTO godfather_claims(user_id, last_claim)
                VALUES (?, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET last_claim=excluded.last_claim
                """,
                (user.id, now)
            )
            db.execute(
                "UPDATE users SET balance=balance+150 WHERE user_id=?",
                (user.id,)
            )
            claimed = True

    if claimed:
        await message.reply_text(
            "🕶 پسرخوانده!\n"
            "🎉 ۱۵۰ Mafia Coin دریافت کردی.\n"
            "⏳ جایزه بعدی ۲ دقیقه دیگر"
        )
    else:
        await message.reply_text(
            f"⏳ {remaining} ثانیه تا جایزه بعدی باقی مانده است."
        )


async def text_handler(update, context):
    if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
        return
    user_id = update.effective_user.id

    if banned(user_id):
        await update.message.reply_text("⛔ دسترسی شما مسدود است.")
        return

    if await admin_text(update, context):
        return

    qid = context.user_data.get("answering_question")
    if qid:
        context.user_data.pop("answering_question", None)
        result = save_answer(user_id, qid, update.message.text)
        await send_answer_result(update.message, result)
        return

    await update.message.reply_text(
        "از منوی اصلی استفاده کن. برای نمایش منو /start را بفرست."
    )


async def cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text("عملیات لغو شد. /start")


def main():
    env_file = BASE_DIR / ".env"
    env = {}
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")

    token = env.get("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN در فایل .env پیدا نشد")

    init_db()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("cancel", cancel))

    app.add_handler(CallbackQueryHandler(check_join, pattern="^check_join$"))
    app.add_handler(CallbackQueryHandler(main_menu, pattern="^menu$"))
    app.add_handler(CallbackQueryHandler(competitions, pattern="^competitions$"))
    app.add_handler(CallbackQueryHandler(ranking, pattern="^ranking$"))
    app.add_handler(CallbackQueryHandler(referral_menu, pattern="^referral$"))
    app.add_handler(CallbackQueryHandler(open_question, pattern=r"^q:\d+$"))
    app.add_handler(CallbackQueryHandler(answer_choice, pattern=r"^a:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin$"))
    app.add_handler(CallbackQueryHandler(admin_callbacks, pattern="^admin:"))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
            godfather_handler
        ),
        group=-1
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )

    print("Mafioso Bot is running!")
    app.run_polling()


if __name__ == "__main__":
    main()
