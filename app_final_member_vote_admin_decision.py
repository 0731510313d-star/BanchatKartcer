import os
import sqlite3
import time
import threading
import json
import urllib.parse
import urllib.request
import traceback

from flask import Flask, request

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

VOTE_TIME = 10 * 60
MIN_VOTES = 20
BAN_PERCENT = 60
NEW_MEMBER_WAIT = 7 * 24 * 60 * 60
ACTIVE_WINDOW = 30 * 24 * 60 * 60
ACTIVE_MIN_MESSAGES = 5
ACTIVE_MIN_DAYS = 2
TRANSITION_DAYS = 30 * 24 * 60 * 60
BOT_STATS_STARTED_AT = int(time.time())

app = Flask(__name__)

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
    active INTEGER DEFAULT 1,
    message_id INTEGER,
    decision_status TEXT DEFAULT 'none'
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
db.execute("""CREATE TABLE IF NOT EXISTS activity(
    chat_id INTEGER,
    user_id INTEGER,
    day INTEGER,
    messages INTEGER DEFAULT 0,
    PRIMARY KEY(chat_id, user_id, day)
)""")
db.execute("""CREATE TABLE IF NOT EXISTS bot_settings(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)""")
# Миграция старой базы, если столбца message_id ещё нет.
try:
    db.execute("ALTER TABLE votes ADD COLUMN message_id INTEGER")
except sqlite3.OperationalError:
    pass
try:
    db.execute("ALTER TABLE votes ADD COLUMN decision_status TEXT DEFAULT 'none'")
except sqlite3.OperationalError:
    pass
# Дата начала сбора статистики хранится в БД и больше не сбрасывается
# при рестарте/деплое Render.
row = db.execute(
    "SELECT value FROM bot_settings WHERE key='stats_started_at'"
).fetchone()
if row:
    BOT_STATS_STARTED_AT = int(row[0])
else:
    BOT_STATS_STARTED_AT = int(time.time())
    db.execute(
        "INSERT INTO bot_settings(key,value) VALUES('stats_started_at',?)",
        (str(BOT_STATS_STARTED_AT),),
    )
db.commit()

db_lock = threading.Lock()


def api(method, params=None, timeout=20):
    params = params or {}
    encoded = {}
    for k, v in params.items():
        if isinstance(v, (dict, list)):
            encoded[k] = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, bool):
            encoded[k] = "true" if v else "false"
        else:
            encoded[k] = str(v)

    data = urllib.parse.urlencode(encoded).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=data,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        result = json.loads(r.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method}: {result}")
    return result.get("result")


def send_message(chat_id, text, reply_markup=None, reply_to_message_id=None):
    params = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    if reply_to_message_id is not None:
        params["reply_parameters"] = {"message_id": reply_to_message_id}
    return api("sendMessage", params)


def answer_callback(callback_id, text="", alert=False):
    try:
        api("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": text,
            "show_alert": alert,
        })
    except Exception as e:
        print("ANSWER CALLBACK ERROR:", repr(e), flush=True)


def keyboard(vote_id, yes, no):
    return {
        "inline_keyboard": [
            [{"text": f"🔨 ЗА БАН — {yes}", "callback_data": f"vote:{vote_id}:yes"}],
            [{"text": f"🛡 ПРОТИВ — {no}", "callback_data": f"vote:{vote_id}:no"}],
        ]
    }


def edit_keyboard(chat_id, message_id, vote_id, yes, no):
    api("editMessageReplyMarkup", {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": keyboard(vote_id, yes, no),
    })


def record_activity(chat_id, user_id, event_time=None):
    if not chat_id or not user_id:
        return
    ts = int(event_time or time.time())
    day = ts // 86400
    with db_lock:
        db.execute(
            "INSERT INTO activity(chat_id,user_id,day,messages) VALUES(?,?,?,1) "
            "ON CONFLICT(chat_id,user_id,day) DO UPDATE SET messages=messages+1",
            (int(chat_id), int(user_id), int(day)),
        )
        db.commit()


def activity_status(chat_id, user_id):
    cutoff_day = (int(time.time()) - ACTIVE_WINDOW) // 86400
    with db_lock:
        row = db.execute(
            "SELECT COALESCE(SUM(messages),0), COUNT(*) FROM activity "
            "WHERE chat_id=? AND user_id=? AND day>=?",
            (int(chat_id), int(user_id), int(cutoff_day)),
        ).fetchone()
    messages = int(row[0] or 0)
    days = int(row[1] or 0)
    return messages, days


def can_vote_by_activity(chat_id, user_id):
    # Переходный период: первые 30 дней после запуска этой версии
    # старых участников не блокируем из-за отсутствия прошлой статистики.
    if int(time.time()) - BOT_STATS_STARTED_AT < TRANSITION_DAYS:
        return True, None
    messages, days = activity_status(chat_id, user_id)
    if messages >= ACTIVE_MIN_MESSAGES and days >= ACTIVE_MIN_DAYS:
        return True, None
    return False, (messages, days)


def remember_join(chat_id, user_id, joined_at=None):
    if not chat_id or not user_id:
        return
    joined_at = int(joined_at or time.time())
    with db_lock:
        db.execute(
            "INSERT INTO member_joins(chat_id,user_id,joined_at) VALUES(?,?,?) "
            "ON CONFLICT(chat_id,user_id) DO UPDATE SET joined_at=excluded.joined_at",
            (int(chat_id), int(user_id), joined_at),
        )
        db.commit()
    print("JOIN REMEMBERED:", chat_id, user_id, flush=True)


def forget_join(chat_id, user_id):
    with db_lock:
        db.execute(
            "DELETE FROM member_joins WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
        )
        db.commit()


def quarantine_left(chat_id, user_id):
    with db_lock:
        row = db.execute(
            "SELECT joined_at FROM member_joins WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
        ).fetchone()
    if not row:
        return 0
    return max(0, NEW_MEMBER_WAIT - (int(time.time()) - int(row[0])))


def human_wait(seconds):
    days = max(1, (int(seconds) + 86399) // 86400)
    return f"примерно {days} дн."


def is_admin(chat_id, user_id):
    member = api("getChatMember", {"chat_id": chat_id, "user_id": user_id})
    return member.get("status") in ("administrator", "creator")


def display_name(user):
    first = user.get("first_name") or ""
    last = user.get("last_name") or ""
    name = (first + " " + last).strip()
    if name:
        return name
    if user.get("username"):
        return "@" + user["username"]
    return str(user.get("id", "пользователь"))


def finish_vote(vote_id):
    with db_lock:
        row = db.execute(
            "SELECT chat_id,target_id,target_name,reason,yes,no,active,message_id "
            "FROM votes WHERE id=?",
            (vote_id,),
        ).fetchone()

        if not row or not row[6]:
            return

        chat_id, target_id, target_name, reason, yes, no, active, message_id = row
        db.execute("UPDATE votes SET active=0 WHERE id=?", (vote_id,))
        db.commit()

    total = yes + no

    if total < MIN_VOTES:
        with db_lock:
            db.execute(
                "UPDATE votes SET decision_status='closed' WHERE id=?",
                (vote_id,),
            )
            db.commit()

        text = (
            "⚖️ <b>Голосование завершено</b>\n\n"
            f"👤 {target_name}\n"
            "❌ Недостаточно голосов.\n"
            f"Всего голосов: {total} из необходимых {MIN_VOTES}."
        )
        send_message(chat_id, text)

    else:
        percent = yes / total * 100

        if percent >= BAN_PERCENT:
            with db_lock:
                db.execute(
                    "UPDATE votes SET decision_status='pending' WHERE id=?",
                    (vote_id,),
                )
                db.commit()

            admin_keyboard = {
                "inline_keyboard": [
                    [
                        {
                            "text": "🔨 ЗАБАНИТЬ",
                            "callback_data": f"admin:{vote_id}:ban",
                        }
                    ],
                    [
                        {
                            "text": "🛡 ОСТАВИТЬ",
                            "callback_data": f"admin:{vote_id}:keep",
                        }
                    ],
                ]
            }

            text = (
                "✅ <b>ГОЛОСОВАНИЕ ПРОШЛО</b>\n\n"
                f"👤 {target_name}\n"
                f"📝 Причина: {reason}\n\n"
                f"🔨 За бан: {yes}\n"
                f"🛡 Против: {no}\n"
                f"📊 За бан: {percent:.0f}%\n\n"
                "👮 <b>Финальное решение — за администрацией.</b>"
            )

            send_message(
                chat_id,
                text,
                reply_markup=admin_keyboard,
            )

        else:
            with db_lock:
                db.execute(
                    "UPDATE votes SET decision_status='closed' WHERE id=?",
                    (vote_id,),
                )
                db.commit()

            text = (
                "🛡 <b>БАН НЕ ПОДДЕРЖАН</b>\n\n"
                f"👤 {target_name}\n"
                f"🔨 За бан: {yes}\n"
                f"🛡 Против: {no}\n"
                f"📊 За бан: {percent:.0f}%\n\n"
                f"Для передачи решения админам нужно минимум {BAN_PERCENT}% голосов «ЗА»."
            )
            send_message(chat_id, text)

    if message_id:
        try:
            api("editMessageReplyMarkup", {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            })
        except Exception as e:
            print("FINISH BUTTON REMOVE ERROR:", repr(e), flush=True)

def finish_expired_votes():
    now = int(time.time())
    with db_lock:
        ids = [
            r[0] for r in db.execute(
                "SELECT id FROM votes WHERE active=1 AND end_time<=?",
                (now,),
            ).fetchall()
        ]
    for vote_id in ids:
        try:
            finish_vote(vote_id)
        except Exception:
            traceback.print_exc()


def schedule_finish(vote_id, end_time):
    delay = max(1, int(end_time) - int(time.time()))
    timer = threading.Timer(delay, finish_vote, args=(vote_id,))
    timer.daemon = True
    timer.start()


def track_members(data):
    try:
        cm = data.get("chat_member")
        if cm:
            chat_id = (cm.get("chat") or {}).get("id")
            old = cm.get("old_chat_member") or {}
            new = cm.get("new_chat_member") or {}
            old_status = old.get("status")
            new_status = new.get("status")
            user_id = (new.get("user") or {}).get("id")
            event_time = cm.get("date") or int(time.time())

            if old_status in ("left", "kicked") and new_status in (
                "member", "administrator", "creator", "restricted"
            ):
                remember_join(chat_id, user_id, event_time)
            elif new_status in ("left", "kicked"):
                forget_join(chat_id, user_id)

        msg = data.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        event_time = msg.get("date") or int(time.time())
        for user in msg.get("new_chat_members") or []:
            if not user.get("is_bot"):
                remember_join(chat_id, user.get("id"), event_time)
    except Exception as e:
        print("MEMBERSHIP ERROR:", repr(e), flush=True)


def handle_message(message):
    text = message.get("text") or ""
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    sender = message.get("from") or {}
    sender_id = sender.get("id")
    message_id = message.get("message_id")

    print("MESSAGE:", {
        "chat_id": chat_id,
        "type": chat_type,
        "from": sender_id,
        "text": text,
        "reply": bool(message.get("reply_to_message")),
    }, flush=True)

    if chat_type in ("group", "supergroup") and sender_id and not sender.get("is_bot"):
        record_activity(chat_id, sender_id, message.get("date"))

    command = text.split()[0].lower() if text else ""
    # В группах команда может приходить как /vote@ИмяБота
    base_command = command.split("@")[0]

    if base_command == "/start":
        send_message(
            chat_id,
            "🦝 <b>BanchatKartcer</b>\n\n"
            "Я создаю голосования за бан в групповых чатах.\n"
            "Допущенный участник может запустить голосование командой /vote в ответ на сообщение нарушителя.\n"
            "Новые и повторно вошедшие участники первые 7 дней не могут голосовать и запускать /vote.\n"
            "После переходного периода участвовать могут только активные: минимум 5 сообщений в 2 разные дни за последние 30 дней.\n\n"
            "Команда:\n"
            "<code>/vote причина</code>\n\n"
            "Если голосование проходит, финальное решение о бане принимает администратор.",
            reply_to_message_id=message_id,
        )
        return

    if base_command != "/vote":
        return

    print("VOTE HANDLER ENTERED", flush=True)

    if chat_type not in ("group", "supergroup"):
        send_message(chat_id, "Эта команда работает только в группе.", reply_to_message_id=message_id)
        return

    if not sender_id:
        send_message(chat_id, "Не удалось определить отправителя команды.", reply_to_message_id=message_id)
        return

    wait = quarantine_left(chat_id, sender_id)
    if wait > 0:
        send_message(
            chat_id,
            "⏳ Новые и повторно вошедшие участники не могут запускать бан-голосования "
            f"первые 7 дней после входа. Осталось: {human_wait(wait)}",
            reply_to_message_id=message_id,
        )
        return

    allowed, details = can_vote_by_activity(chat_id, sender_id)
    if not allowed:
        messages, days = details
        send_message(
            chat_id,
            "⛔ Запускать голосование могут только активные участники: минимум 5 сообщений "
            "в 2 разные дни за последние 30 дней. "
            f"Сейчас: {messages} сообщений, {days} дней.",
            reply_to_message_id=message_id,
        )
        return

    reply = message.get("reply_to_message")
    if not reply:
        send_message(
            chat_id,
            "⚠️ Ответь командой <code>/vote причина</code> "
            "на сообщение пользователя, за которого хочешь запустить голосование.",
            reply_to_message_id=message_id,
        )
        return

    target = reply.get("from") or {}
    target_id = target.get("id")

    if not target_id:
        send_message(chat_id, "Не удалось определить пользователя из сообщения.", reply_to_message_id=message_id)
        return

    if target.get("is_bot"):
        send_message(chat_id, "🤖 Нельзя запускать голосование против бота.", reply_to_message_id=message_id)
        return

    if target_id == sender_id:
        send_message(chat_id, "😅 Нельзя запускать голосование против самого себя.", reply_to_message_id=message_id)
        return

    try:
        if is_admin(chat_id, target_id):
            send_message(
                chat_id,
                "⛔ Нельзя запускать голосование против администратора/владельца.",
                reply_to_message_id=message_id,
            )
            return
    except Exception as e:
        print("TARGET CHECK ERROR:", repr(e), flush=True)

    parts = text.split(maxsplit=1)
    reason = parts[1].strip() if len(parts) > 1 else "Причина не указана"
    target_name = display_name(target)
    end_time = int(time.time()) + VOTE_TIME

    with db_lock:
        cur = db.execute(
            "INSERT INTO votes(chat_id,target_id,target_name,reason,end_time,active) "
            "VALUES(?,?,?,?,?,1)",
            (chat_id, target_id, target_name, reason, end_time),
        )
        vote_id = cur.lastrowid
        db.commit()

    result = send_message(
        chat_id,
        "⚖️ <b>ГОЛОСОВАНИЕ ЗА БАН</b>\n\n"
        f"👤 {target_name}\n"
        f"📝 Причина: {reason}\n\n"
        "🔨 За бан: <b>0</b>\n"
        "🛡 Против: <b>0</b>\n\n"
        "⏱ Голосование: 10 минут\n"
        f"📊 Минимум {MIN_VOTES} голосов, для бана нужно {BAN_PERCENT}% за.",
        reply_markup=keyboard(vote_id, 0, 0),
    )

    bot_message_id = (result or {}).get("message_id")
    with db_lock:
        db.execute("UPDATE votes SET message_id=? WHERE id=?", (bot_message_id, vote_id))
        db.commit()

    schedule_finish(vote_id, end_time)


def handle_callback(callback):
    callback_id = callback.get("id")
    data = callback.get("data") or ""
    user = callback.get("from") or {}
    user_id = user.get("id")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    bot_message_id = message.get("message_id")

    print("CALLBACK:", {"data": data, "from": user_id}, flush=True)

    if data.startswith("admin:"):
        try:
            _, vote_id_s, action = data.split(":")
            vote_id = int(vote_id_s)
        except Exception:
            answer_callback(callback_id, "Неверная кнопка.", True)
            return

        try:
            if not is_admin(chat_id, user_id):
                answer_callback(
                    callback_id,
                    "⛔ Финальное решение может принять только администратор.",
                    True,
                )
                return
        except Exception as e:
            print("ADMIN DECISION CHECK ERROR:", repr(e), flush=True)
            answer_callback(callback_id, "Не удалось проверить права администратора.", True)
            return

        with db_lock:
            row = db.execute(
                "SELECT target_id,target_name,reason,yes,no,decision_status "
                "FROM votes WHERE id=?",
                (vote_id,),
            ).fetchone()

        if not row:
            answer_callback(callback_id, "Голосование не найдено.", True)
            return

        target_id, target_name, reason, yes, no, decision_status = row

        if decision_status != "pending":
            answer_callback(callback_id, "По этому голосованию решение уже принято.", True)
            return

        if action == "ban":
            try:
                api("banChatMember", {"chat_id": chat_id, "user_id": target_id})
                with db_lock:
                    db.execute(
                        "UPDATE votes SET decision_status='banned' WHERE id=?",
                        (vote_id,),
                    )
                    db.commit()

                try:
                    api("editMessageReplyMarkup", {
                        "chat_id": chat_id,
                        "message_id": bot_message_id,
                        "reply_markup": {"inline_keyboard": []},
                    })
                except Exception as e:
                    print("ADMIN BUTTON REMOVE ERROR:", repr(e), flush=True)

                answer_callback(callback_id, "Пользователь забанен.", False)
                send_message(
                    chat_id,
                    "🔨 <b>АДМИНИСТРАТОР ПОДТВЕРДИЛ БАН</b>\n\n"
                    f"👤 {target_name}\n"
                    f"📝 Причина: {reason}\n"
                    f"🔨 За бан: {yes}\n"
                    f"🛡 Против: {no}"
                )
            except Exception as e:
                print("ADMIN BAN ERROR:", repr(e), flush=True)
                answer_callback(
                    callback_id,
                    "⚠️ Не удалось забанить. Проверь права бота.",
                    True,
                )
            return

        if action == "keep":
            with db_lock:
                db.execute(
                    "UPDATE votes SET decision_status='rejected' WHERE id=?",
                    (vote_id,),
                )
                db.commit()

            try:
                api("editMessageReplyMarkup", {
                    "chat_id": chat_id,
                    "message_id": bot_message_id,
                    "reply_markup": {"inline_keyboard": []},
                })
            except Exception as e:
                print("ADMIN BUTTON REMOVE ERROR:", repr(e), flush=True)

            answer_callback(callback_id, "Решение: оставить участника.", False)
            send_message(
                chat_id,
                "🛡 <b>АДМИНИСТРАТОР ОТКЛОНИЛ БАН</b>\n\n"
                f"👤 {target_name} остаётся в чате."
            )
            return

        answer_callback(callback_id, "Неизвестное действие.", True)
        return

    if not data.startswith("vote:"):
        answer_callback(callback_id)
        return

    try:
        _, vote_id_s, choice = data.split(":")
        vote_id = int(vote_id_s)
    except Exception:
        answer_callback(callback_id, "Неверная кнопка.", True)
        return

    wait = quarantine_left(chat_id, user_id)
    if wait > 0:
        answer_callback(
            callback_id,
            "⏳ Голосовать можно только после 7 дней в чате. "
            f"Осталось: {human_wait(wait)}",
            True,
        )
        return

    allowed, details = can_vote_by_activity(chat_id, user_id)
    if not allowed:
        messages, days = details
        answer_callback(
            callback_id,
            "⛔ Голосовать могут только активные участники: минимум 5 сообщений "
            "в 2 разные дни за последние 30 дней. "
            f"Сейчас: {messages} сообщений, {days} дней.",
            True,
        )
        return

    with db_lock:
        row = db.execute(
            "SELECT yes,no,end_time,active FROM votes WHERE id=?",
            (vote_id,),
        ).fetchone()

        if not row or not row[3]:
            answer_callback(callback_id, "Это голосование уже закрыто.", True)
            return

        yes, no, end_time, active = row

        if int(time.time()) >= end_time:
            expired = True
        else:
            expired = False
            exists = db.execute(
                "SELECT 1 FROM voters WHERE vote_id=? AND user_id=?",
                (vote_id, user_id),
            ).fetchone()
            if exists:
                answer_callback(callback_id, "Ты уже проголосовал 😎", True)
                return

            db.execute(
                "INSERT INTO voters(vote_id,user_id,choice) VALUES(?,?,?)",
                (vote_id, user_id, choice),
            )
            if choice == "yes":
                db.execute("UPDATE votes SET yes=yes+1 WHERE id=?", (vote_id,))
            else:
                db.execute("UPDATE votes SET no=no+1 WHERE id=?", (vote_id,))
            db.commit()

    if expired:
        finish_vote(vote_id)
        answer_callback(callback_id, "Время голосования закончилось.", True)
        return

    with db_lock:
        yes, no = db.execute("SELECT yes,no FROM votes WHERE id=?", (vote_id,)).fetchone()

    try:
        edit_keyboard(chat_id, bot_message_id, vote_id, yes, no)
    except Exception as e:
        print("EDIT KEYBOARD ERROR:", repr(e), flush=True)

    answer_callback(callback_id, "Голос принят 👍", False)


def process_update(data):
    track_members(data)
    finish_expired_votes()

    print("PROCESS UPDATE:", {
        "update_id": data.get("update_id"),
        "message": "message" in data,
        "callback": "callback_query" in data,
        "chat_member": "chat_member" in data,
    }, flush=True)

    if "message" in data:
        handle_message(data["message"])
    elif "callback_query" in data:
        handle_callback(data["callback_query"])


@app.route("/", methods=["GET"])
def home():
    return "BanchatKartcer is alive 🦝", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        print("WEBHOOK RECEIVED:", {
            "update_id": data.get("update_id"),
            "has_message": "message" in data,
            "has_callback": "callback_query" in data,
            "has_chat_member": "chat_member" in data,
        }, flush=True)

        # ВАЖНО: обрабатываем прямо здесь, без asyncio и фонового event loop.
        process_update(data)
        return "ok", 200
    except Exception as e:
        print("WEBHOOK ERROR:", repr(e), flush=True)
        traceback.print_exc()
        # Telegram должен получить 200, иначе будет слать один и тот же update повторно.
        return "ok", 200


@app.route("/set-webhook", methods=["GET"])
def set_webhook():
    try:
        base = request.host_url.rstrip("/")
        url = f"{base}/webhook"
        result = api("setWebhook", {
            "url": url,
            "allowed_updates": ["message", "callback_query", "chat_member"],
            "drop_pending_updates": True,
        })
        return (
            f"Webhook set to {url}\n"
            "SYNC MODE: ON\n"
            "New-member protection: ON (7 days)\n"
            "Active-voter protection: ON (5 messages / 2 days / 30 days)\n"
            "Member-started voting: ON\n"
            "Admin final decision: ON\n"
            "Minimum votes: 20\n"
            "Transition period: 30 days\n"
            "Allowed updates: message, callback_query, chat_member"
        ), 200, {"Content-Type": "text/plain; charset=utf-8"}
    except Exception as e:
        print("SET WEBHOOK ERROR:", repr(e), flush=True)
        traceback.print_exc()
        return f"Webhook error: {type(e).__name__}: {e}", 500


@app.route("/webhook-info", methods=["GET"])
def webhook_info():
    try:
        info = api("getWebhookInfo")
        return (
            f"url={info.get('url')}\n"
            f"pending_update_count={info.get('pending_update_count')}\n"
            f"last_error_date={info.get('last_error_date')}\n"
            f"last_error_message={info.get('last_error_message')}\n"
            f"allowed_updates={info.get('allowed_updates')}\n"
        ), 200, {"Content-Type": "text/plain; charset=utf-8"}
    except Exception as e:
        return f"error={type(e).__name__}: {e}", 500
