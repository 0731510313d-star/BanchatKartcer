import os
import sqlite3
import time
import asyncio
import threading
import traceback

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
    UNIQUE(vote_id, user_id)
)""")
db.commit()
db_lock = threading.Lock()

bot = Bot(TOKEN)
dp = Dispatcher()
app = Flask(__name__)

# Один постоянный asyncio event loop для aiogram.
loop = asyncio.new_event_loop()

def loop_worker():
    asyncio.set_event_loop(loop)
    loop.run_forever()

threading.Thread(target=loop_worker, daemon=True).start()

def run_async(coro, timeout=25):
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)

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
        row = db.execute(
            "SELECT id, chat_id, target_id, target_name, reason, yes, no, end_time, active "
            "FROM votes WHERE id=? AND active=1",
            (vote_id,)
        ).fetchone()

        if not row:
            return

        _, chat_id, target_id, target_name, reason, yes, no, end_time, _ = row
        db.execute("UPDATE votes SET active=0 WHERE id=?", (vote_id,))
        db.commit()

    total = yes + no

    if total < MIN_VOTES:
        text = (
            f"⚖️ <b>Голосование завершено</b>\n\n"
            f"👤 {target_name}\n"
            f"❌ Недостаточно голосов.\n"
            f"Всего голосов: {total}"
        )
    else:
        percent = yes / total * 100

        if percent >= BAN_PERCENT:
            try:
                await bot.ban_chat_member(chat_id, target_id)
                text = (
                    f"🔨 <b>ПОЛЬЗОВАТЕЛЬ ЗАБАНЕН</b>\n\n"
                    f"👤 {target_name}\n"
                    f"📝 Причина: {reason}\n\n"
                    f"🔨 За бан: {yes}\n"
                    f"🛡 Против: {no}\n"
                    f"📊 За бан: {percent:.0f}%"
                )
            except Exception as e:
                print("BAN ERROR:", repr(e), flush=True)
                text = (
                    "⚠️ Голосование выиграло, но бот не смог забанить пользователя.\n\n"
                    "Проверь, что бот — администратор и у него есть право блокировать участников."
                )
        else:
            text = (
                f"🛡 <b>БАН НЕ СОСТОЯЛСЯ</b>\n\n"
                f"👤 {target_name}\n"
                f"🔨 За бан: {yes}\n"
                f"🛡 Против: {no}\n"
                f"📊 За бан: {percent:.0f}%"
            )

    await bot.send_message(chat_id, text, parse_mode="HTML")

async def expiry_loop():
    while True:
        await asyncio.sleep(15)
        now = int(time.time())

        with db_lock:
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM votes WHERE active=1 AND end_time<=?",
                    (now,)
                ).fetchall()
            ]

        for vote_id in ids:
            try:
                await finish_vote(vote_id)
            except Exception as e:
                print("EXPIRY ERROR:", repr(e), flush=True)

asyncio.run_coroutine_threadsafe(expiry_loop(), loop)

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
    print("VOTE HANDLER ENTERED", flush=True)

    try:
        if message.chat.type not in ("group", "supergroup"):
            await message.answer("Эта команда работает только в группе.")
            return

        if not message.from_user:
            await message.answer("Не удалось определить отправителя команды.")
            return

        if not await is_admin(message):
            await message.answer("⛔ Создавать голосования могут только администраторы.")
            return

        if not message.reply_to_message:
            await message.answer(
                "⚠️ Ответь командой <code>/vote причина</code> "
                "на сообщение пользователя, за которого хочешь запустить голосование.",
                parse_mode="HTML"
            )
            return

        target = message.reply_to_message.from_user
        if not target:
            await message.answer("Не удалось определить пользователя из сообщения.")
            return

        if target.is_bot:
            await message.answer("🤖 Нельзя запускать голосование против бота.")
            return

        if target.id == message.from_user.id:
            await message.answer("😅 Нельзя запускать голосование против самого себя.")
            return

        try:
            target_member = await bot.get_chat_member(message.chat.id, target.id)
            if target_member.status in ("administrator", "creator"):
                await message.answer("⛔ Нельзя запускать голосование против администратора/владельца.")
                return
        except Exception as e:
            print("TARGET CHECK ERROR:", repr(e), flush=True)

        reason = ""
        if message.text:
            parts = message.text.split(maxsplit=1)
            if len(parts) > 1:
                reason = parts[1].strip()

        reason = reason or "Причина не указана"
        end_time = int(time.time()) + VOTE_TIME

        with db_lock:
            cur = db.execute(
                "INSERT INTO votes(chat_id,target_id,target_name,reason,end_time) "
                "VALUES(?,?,?,?,?)",
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
            f"📊 Минимум {MIN_VOTES} голосов, для бана нужно {BAN_PERCENT}% за.",
            reply_markup=keyboard(vote_id, 0, 0),
            parse_mode="HTML"
        )

    except Exception as e:
        print("VOTE ERROR:", repr(e), flush=True)
        traceback.print_exc()
        try:
            await message.answer(f"⚠️ Ошибка запуска голосования: {type(e).__name__}")
        except Exception:
            pass

@dp.callback_query(F.data.startswith("vote:"))
async def vote_callback(callback):
    try:
        _, vote_id_s, choice = callback.data.split(":")
        vote_id = int(vote_id_s)

        with db_lock:
            row = db.execute(
                "SELECT id, chat_id, target_id, target_name, reason, yes, no, end_time, active "
                "FROM votes WHERE id=? AND active=1",
                (vote_id,)
            ).fetchone()

            if not row:
                await callback.answer("Это голосование уже закрыто.", show_alert=True)
                return

            expired = int(time.time()) >= row[7]

            if not expired:
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
            yes, no = db.execute(
                "SELECT yes,no FROM votes WHERE id=?",
                (vote_id,)
            ).fetchone()

        await callback.message.edit_reply_markup(
            reply_markup=keyboard(vote_id, yes, no)
        )
        await callback.answer("Голос принят 👍")

    except Exception as e:
        print("CALLBACK ERROR:", repr(e), flush=True)
        traceback.print_exc()
        try:
            await callback.answer("Ошибка при обработке голоса.", show_alert=True)
        except Exception:
            pass

async def process_update(data):
    try:
        # Диагностика без перехвата сообщений: логируем сам Telegram Update.
        print(
            "UPDATE:",
            {
                "update_id": data.get("update_id"),
                "has_message": "message" in data,
                "has_callback": "callback_query" in data,
                "text": (data.get("message") or {}).get("text"),
            },
            flush=True,
        )
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
    except Exception as e:
        print("ASYNC ERROR:", repr(e), flush=True)
        traceback.print_exc()

@app.route("/", methods=["GET"])
def home():
    return "BanchatKartcer is alive 🦝", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)

        # Логируем тип апдейта, но сразу отвечаем Telegram 200.
        print(
            "WEBHOOK RECEIVED:",
            {
                "update_id": data.get("update_id"),
                "has_message": "message" in data,
                "has_callback": "callback_query" in data,
            },
            flush=True
        )

        asyncio.run_coroutine_threadsafe(process_update(data), loop)
        return "ok", 200

    except Exception as e:
        print("WEBHOOK ERROR:", repr(e), flush=True)
        traceback.print_exc()
        return "ok", 200

@app.route("/set-webhook", methods=["GET"])
def set_webhook():
    try:
        base = request.host_url.rstrip("/")
        url = f"{base}/webhook"
        run_async(bot.set_webhook(url))
        return f"Webhook set to {url}", 200
    except Exception as e:
        print("SET WEBHOOK ERROR:", repr(e), flush=True)
        traceback.print_exc()
        return f"Webhook error: {type(e).__name__}", 500

@app.route("/webhook-info", methods=["GET"])
def webhook_info():
    try:
        info = run_async(bot.get_webhook_info())
        return (
            f"url={info.url}\n"
            f"pending_update_count={info.pending_update_count}\n"
            f"last_error_date={info.last_error_date}\n"
            f"last_error_message={info.last_error_message}\n"
        ), 200, {"Content-Type": "text/plain; charset=utf-8"}
    except Exception as e:
        return f"error={repr(e)}", 500
