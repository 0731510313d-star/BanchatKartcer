import os
import sqlite3
import time
import asyncio
import threading
import traceback
import json
import urllib.parse
import urllib.request

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
NEW_MEMBER_WAIT = 7 * 24 * 60 * 60  # 7 дней

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
db.execute("""CREATE TABLE IF NOT EXISTS member_joins(
    chat_id INTEGER,
    user_id INTEGER,
    joined_at INTEGER,
    PRIMARY KEY(chat_id, user_id)
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

def remember_join(chat_id, user_id, joined_at=None):
    if not chat_id or not user_id:
        return
    joined_at = int(joined_at or time.time())
    with db_lock:
        db.execute(
            "INSERT INTO member_joins(chat_id,user_id,joined_at) VALUES(?,?,?) "
            "ON CONFLICT(chat_id,user_id) DO UPDATE SET joined_at=excluded.joined_at",
            (int(chat_id), int(user_id), joined_at)
        )
        db.commit()
    print("JOIN REMEMBERED:", chat_id, user_id, joined_at, flush=True)

def forget_join(chat_id, user_id):
    if not chat_id or not user_id:
        return
    with db_lock:
        db.execute(
            "DELETE FROM member_joins WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id))
        )
        db.commit()

def quarantine_left(chat_id, user_id):
    """
    Возвращает оставшееся время карантина в секундах.
    0 означает: участник старый или уже находится в чате 7+ дней.
    Старые участники, которые были в чате до установки этой версии,
    не имеют записи и потому не блокируются.
    """
    with db_lock:
        row = db.execute(
            "SELECT joined_at FROM member_joins WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id))
        ).fetchone()

    if not row:
        return 0

    left = NEW_MEMBER_WAIT - (int(time.time()) - int(row[0]))
    return max(0, left)

def human_wait(seconds):
    days = max(1, (int(seconds) + 86399) // 86400)
    return f"примерно {days} дн."

def track_membership_from_raw(data):
    """Запоминаем новых и повторно вошедших участников прямо из Telegram Update."""
    try:
        cm = data.get("chat_member")
        if cm:
            chat_id = (cm.get("chat") or {}).get("id")
            old_status = ((cm.get("old_chat_member") or {}).get("status"))
            new_obj = cm.get("new_chat_member") or {}
            new_status = new_obj.get("status")
            user_id = (new_obj.get("user") or {}).get("id")
            event_time = cm.get("date") or int(time.time())

            was_out = old_status in ("left", "kicked")
            is_in = new_status in ("member", "administrator", "creator", "restricted")
            is_out = new_status in ("left", "kicked")

            if was_out and is_in:
                remember_join(chat_id, user_id, event_time)
            elif is_out:
                forget_join(chat_id, user_id)

        msg = data.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        event_time = msg.get("date") or int(time.time())
        for user in msg.get("new_chat_members") or []:
            if not user.get("is_bot"):
                remember_join(chat_id, user.get("id"), event_time)
    except Exception as e:
        print("MEMBERSHIP TRACK ERROR:", repr(e), flush=True)

def telegram_api(method, params=None, timeout=15):
    """Синхронный вызов Telegram Bot API для служебных страниц Flask."""
    params = params or {}
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=body,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

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
        "Я создаю голосования за бан в групповых чатах.\n"
        "Новые и повторно вошедшие участники первые 7 дней не могут голосовать и запускать /vote.\n\n"
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

        wait = quarantine_left(message.chat.id, message.from_user.id)
        if wait > 0:
            await message.answer(
                "⏳ Новые и повторно вошедшие участники не могут запускать бан-голосования "
                f"первые 7 дней после входа. Осталось: {human_wait(wait)}"
            )
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

        chat_id = callback.message.chat.id if callback.message else None
        if chat_id is not None:
            wait = quarantine_left(chat_id, callback.from_user.id)
            if wait > 0:
                await callback.answer(
                    "⏳ Голосовать можно только после 7 дней в чате. "
                    f"Осталось: {human_wait(wait)}",
                    show_alert=True
                )
                return

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
        track_membership_from_raw(data)

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
                "has_chat_member": "chat_member" in data,
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

        result = telegram_api(
            "setWebhook",
            {
                "url": url,
                "allowed_updates": json.dumps(["message", "callback_query", "chat_member"]),
                "drop_pending_updates": "true",
            },
            timeout=15,
        )

        if result.get("ok"):
            return (
                f"Webhook set to {url}\n"
                "New-member protection: ON (7 days)\n"
                "Allowed updates: message, callback_query, chat_member"
            ), 200, {"Content-Type": "text/plain; charset=utf-8"}

        return f"Telegram error: {result}", 500

    except Exception as e:
        print("SET WEBHOOK ERROR:", repr(e), flush=True)
        traceback.print_exc()
        return f"Webhook error: {type(e).__name__}: {e}", 500

@app.route("/webhook-info", methods=["GET"])
def webhook_info():
    try:
        result = telegram_api("getWebhookInfo", timeout=15)
        info = result.get("result") or {}
        return (
            f"ok={result.get('ok')}\n"
            f"url={info.get('url')}\n"
            f"pending_update_count={info.get('pending_update_count')}\n"
            f"last_error_date={info.get('last_error_date')}\n"
            f"last_error_message={info.get('last_error_message')}\n"
            f"allowed_updates={info.get('allowed_updates')}\n"
        ), 200, {"Content-Type": "text/plain; charset=utf-8"}
    except Exception as e:
        return f"error={type(e).__name__}: {e}", 500
