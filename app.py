import os
import sqlite3
import time
import asyncio
import threading

from flask import Flask, request
from aiogram import Bot, Dispatcher, F
from aiogram.types import Update, Message
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

VOTE_TIME = 10 * 60
MIN_VOTES = 3
BAN_PERCENT = 60

db = sqlite3.connect("banchatkartcer.db", check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS votes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    target_id INTEGER,
    target_name TEXT,
    reason TEXT,
    yes INTEGER DEFAULT 0,
    no INTEGER DEFAULT 0,
    end_time INTEGER,
    active INTEGER DEFAULT 1
)""")
db.execute("""CREATE TABLE IF NOT EXISTS voters(
    vote_id INTEGER,
    user_id INTEGER,
    choice TEXT,
    UNIQUE(vote_id,user_id)
)""")
db.commit()
db_lock = threading.Lock()

bot = Bot(TOKEN)
dp = Dispatcher()
app = Flask(__name__)

def keyboard(vote_id, yes, no):
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🔨 ЗА БАН — {yes}", callback_data=f"vote:{vote_id}:yes")
    kb.button(text=f"🛡 ПРОТИВ — {no}", callback_data=f"vote:{vote_id}:no")
    kb.adjust(1)
    return kb.as_markup()

async def is_admin(message: Message):
    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    return member.status in ("administrator", "creator")

async def finish_vote(vote_id):
    with db_lock:
        row = db.execute("SELECT * FROM votes WHERE id=? AND active=1", (vote_id,)).fetchone()
        if not row:
            return
        _, chat_id, target_id, target_name, reason, yes, no, end_time, _ = row
        db.execute("UPDATE votes SET active=0 WHERE id=?", (vote_id,))
        db.commit()

    total = yes + no
    if total < MIN_VOTES:
        text = (f"⚖️ <b>Голосование завершено</b>\n\n"
                f"👤 {target_name}\n"
                f"❌ Недостаточно голосов.\n"
                f"Всего голосов: {total}")
    else:
        percent = yes / total * 100
        if percent >= BAN_PERCENT:
            try:
                await bot.ban_chat_member(chat_id, target_id)
                text = (f"🔨 <b>ПОЛЬЗОВАТЕЛЬ ЗАБАНЕН</b>\n\n"
                        f"👤 {target_name}\n"
                        f"📝 Причина: {reason}\n\n"
                        f"🔨 За бан: {yes}\n🛡 Против: {no}\n"
                        f"📊 За бан: {percent:.0f}%")
            except Exception:
                text = ("⚠️ Голосование выиграло, но бот не смог забанить пользователя.\n\n"
                        "Проверь, что BanchatKartcer — администратор с правом блокировки пользователей.")
        else:
            text = (f"🛡 <b>БАН НЕ СОСТОЯЛСЯ</b>\n\n"
                    f"👤 {target_name}\n"
                    f"🔨 За бан: {yes}\n🛡 Против: {no}\n"
                    f"📊 За бан: {percent:.0f}%")
    await bot.send_message(chat_id, text, parse_mode="HTML")

async def expiry_loop():
    while True:
        await asyncio.sleep(15)
        now = int(time.time())
        with db_lock:
            ids = [r[0] for r in db.execute(
                "SELECT id FROM votes WHERE active=1 AND end_time<=?", (now,)
            ).fetchall()]
        for vote_id in ids:
            await finish_vote(vote_id)

def run_expiry_loop():
    asyncio.run(expiry_loop())

threading.Thread(target=run_expiry_loop, daemon=True).start()

@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "🦝 <b>BanchatKartcer</b>\n\n"
        "Я создаю голосования за бан в групповых чатах.\n\n"
        "Админ должен ответить на сообщение пользователя командой:\n"
        "<code>/vote причина</code>",
        parse_mode="HTML"
    )

@dp.message(Command("vote"))
async def create_vote(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return

    if not await is_admin(message):
        await message.answer("⛔ Создавать голосования могут только администраторы.")
        return

    if not message.reply_to_message:
        await message.answer("⚠️ Используй /vote ответом на сообщение пользователя.")
        return

    target = message.reply_to_message.from_user
    if target.is_bot:
        await message.answer("🤖 Боты не участвуют в голосованиях за бан.")
        return

    reason = message.text.partition(" ")[2].strip() or "Причина не указана"
    end_time = int(time.time()) + VOTE_TIME

    with db_lock:
        cur = db.execute(
            "INSERT INTO votes(chat_id,target_id,target_name,reason,end_time) VALUES(?,?,?,?,?)",
            (message.chat.id, target.id, target.full_name, reason, end_time)
        )
        vote_id = cur.lastrowid
        db.commit()

    await message.answer(
        f"⚖️ <b>ГОЛОСОВАНИЕ ЗА БАН</b>\n\n"
        f"👤 {target.full_name}\n"
        f"📝 Причина: {reason}\n\n"
        f"🔨 За бан: <b>0</b>\n"
        f"🛡 Против: <b>0</b>\n\n"
        f"⏱ Голосование: 10 минут\n"
        f"📊 Нужно минимум {MIN_VOTES} голосов и {BAN_PERCENT}% за бан.",
        reply_markup=keyboard(vote_id, 0, 0),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("vote:"))
async def vote(callback):
    _, vote_id_s, choice = callback.data.split(":")
    vote_id = int(vote_id_s)

    with db_lock:
        row = db.execute("SELECT * FROM votes WHERE id=? AND active=1", (vote_id,)).fetchone()
        if not row:
            await callback.answer("Это голосование уже закрыто.", show_alert=True)
            return

        if int(time.time()) >= row[7]:
            expired = True
        else:
            expired = False

        if expired:
            # Finish outside the database lock.
            pass
        else:
            exists = db.execute(
                "SELECT 1 FROM voters WHERE vote_id=? AND user_id=?",
                (vote_id, callback.from_user.id)
            ).fetchone()
            if exists:
                await callback.answer("Ты уже проголосовал 😎", show_alert=True)
                return

            db.execute(
                "INSERT INTO voters(vote_id,user_id,choice) VALUES(?,?,?)",
                (vote_id, callback.from_user.id, choice)
            )
            if choice == "yes":
                db.execute("UPDATE votes SET yes=yes+1 WHERE id=?", (vote_id,))
            else:
                db.execute("UPDATE votes SET no=no+1 WHERE id=?", (vote_id,))
            db.commit()

    if expired:
        await finish_vote(vote_id)
        await callback.answer("Время голосования закончилось.", show_alert=True)
        return

    with db_lock:
        yes, no = db.execute("SELECT yes,no FROM votes WHERE id=?", (vote_id,)).fetchone()
    await callback.message.edit_reply_markup(reply_markup=keyboard(vote_id, yes, no))
    await callback.answer("Голос принят 👍")

@app.route("/", methods=["GET"])
def home():
    return "BanchatKartcer is alive 🦝", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    update = Update.model_validate(data)
    asyncio.run(dp.feed_update(bot, update))
    return "ok", 200

@app.route("/set-webhook", methods=["GET"])
def set_webhook():
    base = request.host_url.rstrip("/")
    asyncio.run(bot.set_webhook(f"{base}/webhook"))
    return f"Webhook set to {base}/webhook", 200
