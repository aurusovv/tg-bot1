import re
import json
import asyncio
import os
import signal
import sys
import logging
import threading
import time
import csv
import io
import requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from telegram.error import TelegramError, Forbidden, BadRequest
import psycopg2
import psycopg2.extras
from psycopg2 import pool
from flask import Flask, request, jsonify

# ==================== НАСТРОЙКА ЛОГИРОВАНИЯ ====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== КОНФИГУРАЦИЯ ====================
TOKEN = os.environ.get("TOKEN", "8608054971:AAEWfrZNqG-TXSyu1Udnvy7bZWEufuX807k")
ADMIN_GROUP_ID = -1003882437553
SUPPORT_GROUP_ID = -1003452680450
ADMIN_APPLICATION_GROUP_ID = -1002582416308
CHANNEL_ID = "@tgk_themissedpast"
CHANNEL_LINK = "https://t.me/tgk_themissedpast"
PRAVILA = "https://telegra.ph/Pravila-polzovaniya-botom-03-22"
REVIEWS_LINK = "https://t.me/otz_themissedpast"
ALLOWED_GROUPS = {ADMIN_GROUP_ID, SUPPORT_GROUP_ID, ADMIN_APPLICATION_GROUP_ID}
OWNER_ID = 8098729751

# ==================== ПОДКЛЮЧЕНИЕ К БАЗЕ ДАННЫХ ====================
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise Exception("DATABASE_URL не задан в переменных окружения")

db_pool = pool.SimpleConnectionPool(1, 20, DATABASE_URL, sslmode='require')

def get_db_connection():
    return db_pool.getconn()

def release_db_connection(conn):
    db_pool.putconn(conn)

def init_db():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    first_name TEXT,
                    username TEXT,
                    registered_at TIMESTAMP,
                    name TEXT,
                    age TEXT,
                    gender TEXT,
                    type TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bans (
                    user_id BIGINT PRIMARY KEY,
                    until_date TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mutes (
                    user_id BIGINT PRIMARY KEY,
                    until_date TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS active_dialogs (
                    user_id BIGINT PRIMARY KEY,
                    chat_type TEXT NOT NULL,
                    started_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS forwarded_messages (
                    group_id BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    chat_type TEXT NOT NULL,
                    PRIMARY KEY (group_id, message_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS warnings (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    reason TEXT,
                    warned_by BIGINT,
                    warned_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS admin_replies (
                    group_id BIGINT NOT NULL,
                    admin_msg_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    user_msg_id BIGINT NOT NULL,
                    chat_type TEXT NOT NULL,
                    PRIMARY KEY (group_id, admin_msg_id)
                )
            """)
            cur.execute("""
                INSERT INTO bot_settings (key, value) VALUES ('maintenance_mode', 'false')
                ON CONFLICT (key) DO NOTHING
            """)
            conn.commit()
    finally:
        release_db_connection(conn)

def load_maintenance_mode():
    global maintenance_mode
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM bot_settings WHERE key = 'maintenance_mode'")
            row = cur.fetchone()
            maintenance_mode = (row and row[0].lower() == 'true') or False
        logger.info(f"Режим тех. работ загружен: {maintenance_mode}")
    except Exception as e:
        logger.error(f"Ошибка загрузки maintenance_mode: {e}")
        maintenance_mode = False
    finally:
        release_db_connection(conn)

def save_maintenance_mode(value):
    global maintenance_mode
    maintenance_mode = value
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE bot_settings SET value = %s, updated_at = NOW() WHERE key = 'maintenance_mode'", ('true' if value else 'false'))
            conn.commit()
        logger.info(f"Режим тех. работ сохранён: {value}")
    except Exception as e:
        logger.error(f"Ошибка сохранения maintenance_mode: {e}")
    finally:
        release_db_connection(conn)

# ==================== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ====================
waiting_for_forward = set()
waiting_for_support = set()
forwarded = {}
support_forwarded = {}
admin_replies = {}
support_admin_replies = {}
profile_sent = set()
broadcast_data = {}
user_has_message = set()
group_warnings = {}
application_messages = {}
banned_users = set()
muted_users = set()
ban_until = {}
mute_until = {}
user_profiles = {}
maintenance_mode = False

# ==================== РАБОТА С БАЗОЙ ДАННЫХ (ДИАЛОГИ) ====================
def save_active_dialog(user_id, chat_type):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO active_dialogs (user_id, chat_type, started_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE SET chat_type = EXCLUDED.chat_type
            """, (user_id, chat_type))
            conn.commit()
    except Exception as e:
        logger.error(f"Ошибка сохранения активного диалога: {e}")
    finally:
        release_db_connection(conn)

def remove_active_dialog(user_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM active_dialogs WHERE user_id = %s", (user_id,))
            conn.commit()
    except Exception as e:
        logger.error(f"Ошибка удаления активного диалога: {e}")
    finally:
        release_db_connection(conn)

def load_active_dialogs():
    global waiting_for_forward, waiting_for_support
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, chat_type FROM active_dialogs")
            for uid, ct in cur.fetchall():
                if ct == 'admin':
                    waiting_for_forward.add(uid)
                elif ct == 'support':
                    waiting_for_support.add(uid)
        logger.info(f"Загружено диалогов: admin={len(waiting_for_forward)}, support={len(waiting_for_support)}")
    except Exception as e:
        logger.error(f"Ошибка загрузки активных диалогов: {e}")
    finally:
        release_db_connection(conn)

def save_forwarded_message(group_id, message_id, user_id, chat_type):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO forwarded_messages (group_id, message_id, user_id, chat_type)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (group_id, message_id) DO UPDATE SET user_id = EXCLUDED.user_id
            """, (group_id, message_id, user_id, chat_type))
            conn.commit()
    except Exception as e:
        logger.error(f"Ошибка сохранения пересланного сообщения: {e}")
    finally:
        release_db_connection(conn)

def load_forwarded_messages():
    global forwarded, support_forwarded
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT group_id, message_id, user_id, chat_type FROM forwarded_messages")
            for gid, mid, uid, ct in cur.fetchall():
                if gid == ADMIN_GROUP_ID:
                    forwarded[mid] = (uid, None)
                elif gid == SUPPORT_GROUP_ID:
                    support_forwarded[mid] = (uid, None)
        logger.info(f"Загружено пересланных: admin={len(forwarded)}, support={len(support_forwarded)}")
    except Exception as e:
        logger.error(f"Ошибка загрузки пересланных сообщений: {e}")
    finally:
        release_db_connection(conn)

def remove_forwarded_message(group_id, message_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM forwarded_messages WHERE group_id = %s AND message_id = %s", (group_id, message_id))
            conn.commit()
    except Exception as e:
        logger.error(f"Ошибка удаления пересланного сообщения: {e}")
    finally:
        release_db_connection(conn)

def save_admin_reply(group_id, admin_msg_id, user_id, user_msg_id, chat_type):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO admin_replies (group_id, admin_msg_id, user_id, user_msg_id, chat_type)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (group_id, admin_msg_id) DO UPDATE SET user_msg_id = EXCLUDED.user_msg_id
            """, (group_id, admin_msg_id, user_id, user_msg_id, chat_type))
            conn.commit()
    except Exception as e:
        logger.error(f"Ошибка сохранения ответа: {e}")
    finally:
        release_db_connection(conn)

def load_admin_replies():
    global admin_replies, support_admin_replies
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT group_id, admin_msg_id, user_id, user_msg_id, chat_type FROM admin_replies")
            for gid, admin_mid, uid, user_mid, ct in cur.fetchall():
                if ct == 'admin':
                    admin_replies[admin_mid] = (uid, user_mid)
                else:
                    support_admin_replies[admin_mid] = (uid, user_mid)
        logger.info(f"Загружено ответов: admin={len(admin_replies)}, support={len(support_admin_replies)}")
    except Exception as e:
        logger.error(f"Ошибка загрузки ответов: {e}")
    finally:
        release_db_connection(conn)

# ==================== ОСТАЛЬНЫЕ ФУНКЦИИ РАБОТЫ С БАЗОЙ ====================
def load_db():
    global banned_users, muted_users, ban_until, mute_until, user_profiles
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM users")
            user_profiles = {}
            for row in cur:
                name = row['name']
                age = row['age']
                gender = row['gender']
                p_type = row['type']
                user_profiles[row['user_id']] = {
                    'first_name': row['first_name'],
                    'username': row['username'],
                    'registered_at': row['registered_at'].isoformat() if row['registered_at'] else None,
                    'name': name if name and name.strip() else None,
                    'age': age if age and age.strip() else None,
                    'gender': gender if gender and gender.strip() else None,
                    'type': p_type if p_type and p_type.strip() else None
                }
            cur.execute("SELECT user_id, until_date FROM bans")
            banned_users = set()
            ban_until = {}
            for row in cur:
                uid = row['user_id']
                until = row['until_date']
                if until > datetime.now():
                    banned_users.add(uid)
                    ban_until[uid] = until
            cur.execute("SELECT user_id, until_date FROM mutes")
            muted_users = set()
            mute_until = {}
            for row in cur:
                uid = row['user_id']
                until = row['until_date']
                if until > datetime.now():
                    muted_users.add(uid)
                    mute_until[uid] = until
        logger.info(f"Загружено {len(user_profiles)} пользователей")
    except Exception as e:
        logger.error(f"Ошибка загрузки: {e}")
    finally:
        release_db_connection(conn)

def save_db():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            for uid, data in user_profiles.items():
                cur.execute("""
                    INSERT INTO users (user_id, first_name, username, registered_at, name, age, gender, type)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET
                        first_name = EXCLUDED.first_name,
                        username = EXCLUDED.username,
                        registered_at = EXCLUDED.registered_at,
                        name = EXCLUDED.name,
                        age = EXCLUDED.age,
                        gender = EXCLUDED.gender,
                        type = EXCLUDED.type
                """, (
                    uid, data.get('first_name'), data.get('username'),
                    datetime.fromisoformat(data['registered_at']) if data.get('registered_at') else None,
                    data.get('name'), data.get('age'), data.get('gender'), data.get('type')
                ))
            cur.execute("DELETE FROM bans")
            for uid, until in ban_until.items():
                if until > datetime.now():
                    cur.execute("INSERT INTO bans (user_id, until_date) VALUES (%s, %s)", (uid, until))
            cur.execute("DELETE FROM mutes")
            for uid, until in mute_until.items():
                if until > datetime.now():
                    cur.execute("INSERT INTO mutes (user_id, until_date) VALUES (%s, %s)", (uid, until))
            conn.commit()
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")
    finally:
        release_db_connection(conn)

def update_user_info(user_id, first_name, username):
    if user_id in user_profiles:
        p = user_profiles[user_id]
        changed = False
        if p.get('first_name') != first_name:
            p['first_name'] = first_name
            changed = True
        if p.get('username') != username:
            p['username'] = username
            changed = True
        if changed:
            save_db()
    else:
        user_profiles[user_id] = {
            "name": None, "age": None, "gender": None, "type": None,
            "first_name": first_name, "username": username,
            "registered_at": datetime.now().isoformat()
        }
        save_db()

def update_profile(user_id, name, age, gender, p_type):
    if user_id in user_profiles:
        user_profiles[user_id].update({"name": name, "age": age, "gender": gender, "type": p_type})
        save_db()

def remove_blocked_user(user_id):
    if user_id in user_profiles:
        del user_profiles[user_id]
        banned_users.discard(user_id)
        ban_until.pop(user_id, None)
        muted_users.discard(user_id)
        mute_until.pop(user_id, None)
        waiting_for_forward.discard(user_id)
        waiting_for_support.discard(user_id)
        remove_active_dialog(user_id)
        profile_sent.discard(user_id)
        user_has_message.discard(user_id)
        save_db()
        return True
    return False

def get_user_name(user_id):
    p = user_profiles.get(user_id, {})
    return p.get('first_name') or f"ID:{user_id}"

def get_gender_emoji(gender):
    if gender == 'male':
        return "♂️ Мужской"
    elif gender == 'female':
        return "♀️ Женский"
    return "❓ Не указан"

def parse_time(time_str):
    if not time_str:
        return None
    time_str = time_str.lower().strip()
    match = re.match(r'^(\d+)([дdчhмmсs]?)$', time_str)
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2) if match.group(2) else 'м'
    mult = {'д':86400,'d':86400,'ч':3600,'h':3600,'м':60,'m':60,'с':1,'s':1}
    return value * mult.get(unit, 60)

def format_time_remaining(seconds):
    if seconds < 60:
        return f"{int(seconds)} сек."
    elif seconds < 3600:
        return f"{int(seconds//60)} мин."
    elif seconds < 86400:
        return f"{int(seconds//3600)} ч."
    else:
        return f"{int(seconds//86400)} д."

def get_uid_from_reply(msg, fwd_dict):
    if not msg.reply_to_message:
        return None
    rid = msg.reply_to_message.message_id
    if rid in fwd_dict:
        return fwd_dict[rid][0]
    return None

def clear_user_data(uid):
    waiting_for_forward.discard(uid)
    waiting_for_support.discard(uid)
    profile_sent.discard(uid)
    user_has_message.discard(uid)
    remove_active_dialog(uid)

def is_banned(uid):
    if uid in banned_users:
        if uid in ban_until and datetime.now() >= ban_until[uid]:
            banned_users.discard(uid)
            ban_until.pop(uid, None)
            save_db()
            return False
        return True
    return False

def is_muted(uid):
    if uid in muted_users:
        if uid in mute_until and datetime.now() >= mute_until[uid]:
            muted_users.discard(uid)
            mute_until.pop(uid, None)
            save_db()
            return False
        return True
    return False

def is_profile_complete(uid):
    profile = user_profiles.get(uid)
    if not profile:
        return False
    name = profile.get('name')
    age = profile.get('age')
    if not name or not str(name).strip():
        return False
    if not age or not str(age).strip():
        return False
    try:
        int(str(age).strip())
        return True
    except:
        return False

# ==================== КЛАВИАТУРЫ ====================
def main_menu(user_id):
    has_profile = is_profile_complete(user_id)
    keyboard = [
        [InlineKeyboardButton("🖊 Написать админу", callback_data="admin"),
         InlineKeyboardButton("👨‍💻 Тех.поддержка", callback_data="support")]
    ]
    second_row = []
    if has_profile:
        second_row.append(InlineKeyboardButton("⚙ Настройки", callback_data="settings"))
    second_row.append(InlineKeyboardButton("📝 Отзывы", url=REVIEWS_LINK))
    second_row.append(InlineKeyboardButton("📘 Правила", url=PRAVILA))
    keyboard.append(second_row)
    keyboard.append([InlineKeyboardButton("👑 Попасть в администрацию", callback_data="admins")])
    return InlineKeyboardMarkup(keyboard)

def admin_panel_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("📋 Список пользователей", callback_data="admin_list_users")],
        [InlineKeyboardButton("🔍 Поиск пользователя", callback_data="admin_search_user")],
        [InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🛠 Тех. работы", callback_data="admin_maintenance")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")]
    ])

def settings_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Изменить имя", callback_data="edit_name")],
        [InlineKeyboardButton("✏️ Изменить возраст", callback_data="edit_age")],
        [InlineKeyboardButton("🔄 Изменить тип", callback_data="edit_type")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")]
    ])

def cancel_btn():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])

def back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back")]])

def user_management_buttons(target_id):
    keyboard = [
        [InlineKeyboardButton("✏️ Изменить имя", callback_data=f"edit_user_name_{target_id}")],
        [InlineKeyboardButton("📅 Изменить возраст", callback_data=f"edit_user_age_{target_id}")],
        [InlineKeyboardButton("🔄 Изменить тип", callback_data=f"edit_user_type_{target_id}")],
        [InlineKeyboardButton("🔄 Изменить пол", callback_data=f"edit_user_gender_{target_id}")],
        [InlineKeyboardButton("⚠️ Выдать предупреждение", callback_data=f"warn_user_{target_id}")],
        [InlineKeyboardButton("📜 История предупреждений", callback_data=f"warnings_history_{target_id}")],
        [InlineKeyboardButton("🗑 Сбросить анкету", callback_data=f"reset_profile_{target_id}")],
        [InlineKeyboardButton("📨 Отправить сообщение", callback_data=f"send_msg_to_{target_id}")]
    ]
    if target_id in banned_users:
        keyboard.append([InlineKeyboardButton("✅ Снять бан", callback_data=f"unban_user_{target_id}")])
    if target_id in muted_users:
        keyboard.append([InlineKeyboardButton("✅ Снять мут", callback_data=f"unmute_user_{target_id}")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="admin_back_main")])
    return InlineKeyboardMarkup(keyboard)

def info_buttons(target_id, is_owner):
    if is_owner:
        return InlineKeyboardMarkup([[InlineKeyboardButton("👤 Показать данные", callback_data=f"full_info_{target_id}")]])
    return None

def profile_view_buttons(target_id, from_info=False):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Изменить имя", callback_data=f"edit_name_{target_id}_{from_info}")],
        [InlineKeyboardButton("📅 Изменить возраст", callback_data=f"edit_age_{target_id}_{from_info}")],
        [InlineKeyboardButton("🏷️ Изменить тип", callback_data=f"edit_type_{target_id}_{from_info}")],
        [InlineKeyboardButton("🔙 Назад", callback_data=f"back_to_info_{from_info}_{target_id}")]
    ])

def gender_change_buttons(target_id, from_info):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("♂️ Мужской", callback_data=f"set_gender_{target_id}_{from_info}_male")],
        [InlineKeyboardButton("♀️ Женский", callback_data=f"set_gender_{target_id}_{from_info}_female")],
        [InlineKeyboardButton("🔙 Назад", callback_data=f"back_to_info_{from_info}_{target_id}")]
    ])

# ==================== АНКЕТА ДЛЯ КАНДИДАТОВ ====================
COMMON_QUESTIONS = [
    "1. Ваше имя/псевдоним + тэг",
    "2. Ваш часовой пояс [±... К мск]",
    "3. Возраст [от 14 лет]",
    "4. Как много времени проводите в тг?",
    "5. Оцените [отдельно] свою грамотность; вежливость; умение строить и поддерживать диалог - от 1 до 10",
    "6. Был ли у Вас опыт в сфере ботов? Если был, то укажите юзы ботов. Сколько работали в этой сфере?",
    "7. 1) Как вы отреагируете на ссору? 2) Если Вы стали провокатором данной ссоры?",
    "8. Слабонервны ли вы? [Расчлененка ; кровь ; трудные, жизненные истории]",
    "9. Какова будет ваша реакция на варн, мут, бан?",
    "10. Почему мы должны взять именно Вас на роль админа?",
    "11. Что сделаете, если окажетесь в ситуации, когда совершенно не будете знать, как помочь/ответить пользователю?",
    "12. Объясните свою точку зрения:\n1) Почему важна анонимность?\n2) Почему не стоит обсуждать админов за их спинами?\n3) Какие качества важны для админа 'бота поддержки'?",
    "13. Как Вы считаете, Вы - больше поддержка, или анализ ситуации и помощь в поиске решений?",
    "14. Сейчас, в данном пункте Вы - админ. Вам написал пользователь. Прочитайте внимательно текст ситуации и напишите, как бы вы ответили и поддержали человека.\n\n1) Привет.. мне очень плохо, дело в том, что я занимаюсь селфхармом уже год, и я не могу бросить.. я пытался, но ничего не получается.. мне от него легче, всё равно некому выговориться, некому поддержать.а как мне бросить это? Я не знаю.. Уже просто падаю в отчаяние от безысходности, и вовсе ничего не занимает.. Психолог слишком дорогой, у нас нет денег на него,тем более кому нужны мои проблемы Нужен кто то рядом...\n\n2) Я осталась без работы, связи с родителями нет, а я одна в другом городе, руки опускаются, посоветуйте что-то.."
]
COMMUNICATION_QUESTION = "13. Напишите 15 развернутых вопросов на тему общения с пользователем"

async def start_admin_application(update, context):
    if update.callback_query:
        user = update.callback_query.from_user
        message = update.callback_query.message
    else:
        user = update.effective_user
        message = update.message
    if context.user_data.get('admin_application'):
        await message.reply_text("Вы уже заполняете анкету.")
        return
    kb = [
        [InlineKeyboardButton("#общение", callback_data="app_type_communication")],
        [InlineKeyboardButton("#поддержка", callback_data="app_type_support")],
        [InlineKeyboardButton("#общение_поддержка", callback_data="app_type_both")]
    ]
    await message.reply_text(
        "Вы начали заполнение анкеты для вступления в администрацию.\n"
        "⚠️ Отвечайте только текстом.\n\nСначала выберите ваш тип деятельности:",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    context.user_data['admin_application'] = {'type': None, 'answers': [], 'current_q': 0, 'questions': []}

async def process_application_answer(update, context):
    app_data = context.user_data.get('admin_application')
    if not app_data:
        return
    if app_data['current_q'] >= len(app_data['questions']):
        await finish_application(update, context)
        return
    answer = update.message.text
    app_data['answers'].append(answer)
    app_data['current_q'] += 1
    if app_data['current_q'] < len(app_data['questions']):
        await update.message.reply_text(app_data['questions'][app_data['current_q']], reply_markup=cancel_btn())
    else:
        await finish_application(update, context)

async def finish_application(update, context):
    app_data = context.user_data['admin_application']
    user = update.effective_user
    user_id = user.id
    first_name = user.first_name
    username = user.username or "нет"
    header = f"📝 Новая анкета кандидата\n\n👤 {first_name} (@{username})\n🆔 ID: {user_id}\n🏷️ Тип: {app_data['type']}\n\nОтветы:\n"
    body = ""
    for i, q in enumerate(app_data['questions'], 1):
        ans = app_data['answers'][i-1] if i-1 < len(app_data['answers']) else "❌ нет ответа"
        body += f"\n{q}\n{ans}\n\n"
    footer = f"\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    full_text = header + body + footer
    max_len = 4000
    if len(full_text) <= max_len:
        parts = [full_text]
    else:
        parts = []
        current_part = header
        for i, q in enumerate(app_data['questions'], 1):
            ans = app_data['answers'][i-1] if i-1 < len(app_data['answers']) else "❌ нет ответа"
            qa = f"\n{q}\n{ans}\n\n"
            if len(current_part) + len(qa) + len(footer) > max_len:
                parts.append(current_part + footer)
                current_part = header + "*(продолжение)*\n"
            current_part += qa
        parts.append(current_part + footer)
    global application_messages
    try:
        for idx, part in enumerate(parts):
            sent = await context.bot.send_message(
                chat_id=ADMIN_APPLICATION_GROUP_ID,
                text=part,
                parse_mode=None
            )
            if idx == 0:
                application_messages[sent.message_id] = user_id
    except Exception as e:
        logger.error(f"Ошибка отправки анкеты: {e}")
        try:
            import io
            file = io.BytesIO(full_text.encode('utf-8'))
            file.name = f"application_{user_id}.txt"
            sent = await context.bot.send_document(
                chat_id=ADMIN_APPLICATION_GROUP_ID,
                document=file,
                caption=f"📝 Анкета кандидата {first_name} (@{username})"
            )
            application_messages[sent.message_id] = user_id
        except Exception as e2:
            await update.message.reply_text("❌ Ошибка при отправке анкеты. Пожалуйста, попробуйте позже или обратитесь в техподдержку. Код: APP_ERR01")
            del context.user_data['admin_application']
            return
    await update.message.reply_text("✅ Анкета отправлена на проверку. Ожидайте ответа (до 48 часов).")
    del context.user_data['admin_application']

async def application_type_callback(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    app_data = context.user_data.get('admin_application')
    if not app_data:
        await query.edit_message_text("❌ Ошибка. Попробуйте /start")
        return
    if data == "app_type_communication":
        app_data['type'] = "общение"
        questions = COMMON_QUESTIONS[:12] + [COMMUNICATION_QUESTION]
        app_data['questions'] = questions
    elif data == "app_type_support":
        app_data['type'] = "поддержка"
        app_data['questions'] = COMMON_QUESTIONS.copy()
    else:
        app_data['type'] = "общение и поддержка"
        app_data['questions'] = COMMON_QUESTIONS.copy()
    app_data['answers'] = []
    app_data['current_q'] = 0
    await query.edit_message_text(app_data['questions'][0], reply_markup=cancel_btn())

# ==================== ФУНКЦИИ ДЛЯ КНОПКИ "ОТВЕТИТЬ" ПОЛЬЗОВАТЕЛЯ ====================
async def add_reply_button_to_user(user_id, chat_type, context, original_message_id=None):
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Ответить", callback_data=f"reply_to_{chat_type}_{original_message_id}")]])
    await context.bot.send_message(user_id, "Вы можете ответить администратору, нажав на кнопку ниже:", reply_markup=keyboard)

async def handle_user_reply_button(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    parts = data.split("_")
    if len(parts) < 3:
        await query.edit_message_text("❌ Ошибка. Попробуйте снова.")
        return
    chat_type = parts[2]
    if user_id in waiting_for_forward:
        waiting_for_forward.discard(user_id)
        remove_active_dialog(user_id)
        await context.bot.send_message(ADMIN_GROUP_ID, f"🔄 {get_user_name(user_id)} завершил старый диалог и начал новый.")
    if user_id in waiting_for_support:
        waiting_for_support.discard(user_id)
        remove_active_dialog(user_id)
        await context.bot.send_message(SUPPORT_GROUP_ID, f"🔄 {get_user_name(user_id)} завершил старый диалог и начал новый.")
    if chat_type == 'admin':
        waiting_for_forward.add(user_id)
        save_active_dialog(user_id, 'admin')
        await query.edit_message_text("✅ Вы снова в очереди на ответ администратору. Напишите ваше сообщение.")
    else:
        waiting_for_support.add(user_id)
        save_active_dialog(user_id, 'support')
        await query.edit_message_text("✅ Вы снова в очереди на ответ в техподдержку. Напишите ваше сообщение.")
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except:
        pass

# ==================== ОСТАЛЬНЫЕ ФУНКЦИИ ====================
async def check_group_access(update, context):
    chat = update.effective_chat
    if chat.type in ["group","supergroup"] and chat.id not in ALLOWED_GROUPS:
        cnt = group_warnings.get(chat.id, 0) + 1
        group_warnings[chat.id] = cnt
        await update.message.reply_text(f"⚠️ НЕСАНКЦИОНИРОВАННОЕ ИСПОЛЬЗОВАНИЕ!\n❌ ID: {chat.id}\n📊 Предупреждение #{cnt}\n🚪 Бот покинет группу через 5 сек...")
        await asyncio.sleep(5)
        await context.bot.leave_chat(chat.id)
        return False
    return True

async def check_subscription(update, context):
    user_id = update.effective_user.id
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ["member","administrator","creator"]
    except:
        return False

async def send_main_menu(update, context, chat_id=None, message_id=None):
    uid = update.effective_user.id if update.effective_user else (chat_id or update.effective_chat.id)
    text = "Привет! Тебя приветствует бот\n\n<<𐔤ᥒ𐔤պᥱⲏⲏ𐔖ᥱ ᥒρ𐔖ɯ᥈𐔖ᥱ>>\n\nГлавное меню\n\n"
    if chat_id is None:
        chat_id = update.effective_chat.id
    if os.path.exists("welcome.png"):
        with open("welcome.png", "rb") as photo:
            if message_id:
                try:
                    await context.bot.edit_message_media(
                        chat_id=chat_id, message_id=message_id,
                        media=InputMediaPhoto(media=photo, caption=text, parse_mode="Markdown"),
                        reply_markup=main_menu(uid)
                    )
                except:
                    await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
                    await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=text, parse_mode="Markdown", reply_markup=main_menu(uid))
            else:
                await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=text, parse_mode="Markdown", reply_markup=main_menu(uid))
    else:
        if message_id:
            try:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode="Markdown", reply_markup=main_menu(uid))
            except:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=main_menu(uid))
        else:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=main_menu(uid))

async def start(update, context):
    user = update.effective_user
    update_user_info(user.id, user.first_name, user.username)
    if maintenance_mode and user.id != OWNER_ID:
        await update.message.reply_text("🛠 Бот на технических работах. Пожалуйста, зайдите позже. Код: MAINT001")
        return
    if not await check_subscription(update, context):
        await update.message.reply_text(
            "❌ Для использования бота необходимо подписаться на наш канал!\n\nПодпишитесь и нажмите /start снова.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 Подписаться", url=CHANNEL_LINK)]])
        )
        return
    await send_main_menu(update, context)

async def help_command(update, context):
    await update.message.reply_text("Недоступно")

async def settings(update, context):
    uid = update.message.chat_id
    if maintenance_mode and update.effective_user.id != OWNER_ID:
        await update.message.reply_text("🛠 Технические работы. Код: MAINT001")
        return
    if not await check_subscription(update, context):
        await update.message.reply_text("❌ Подпишитесь на канал!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 Подписаться", url=CHANNEL_LINK)]]))
        return
    if not is_profile_complete(uid):
        await update.message.reply_text("❌ Настройки недоступны, анкета не создана.\nЗаполните её через 'Написать админу'")
        return
    p = user_profiles[uid]
    t = "🆘 #поддержка" if p['type']=='support' else "💬 #общение" if p['type']=='communication' else "❓ не выбрано"
    await update.message.reply_text(
        f"📋 **Ваша анкета:**\n\n👤 Имя: `{p['name']}`\n📅 Возраст: `{p['age']}`\n{get_gender_emoji(p.get('gender'))}\n🏷️ Тип: {t}",
        parse_mode="Markdown", reply_markup=settings_buttons()
    )

async def stop(update, context):
    uid = update.message.chat_id
    if context.user_data.get('admin_application'):
        del context.user_data['admin_application']
        await update.message.reply_text("❌ Заполнение анкеты администратора отменено.")
        return
    in_f = uid in waiting_for_forward
    in_s = uid in waiting_for_support
    if not in_f and not in_s:
        await update.message.reply_text("❌ Вы не находитесь в режиме общения")
        return
    name = get_user_name(uid)
    had = uid in user_has_message
    if in_f:
        waiting_for_forward.discard(uid)
        remove_active_dialog(uid)
        if had:
            await context.bot.send_message(ADMIN_GROUP_ID, f"🚫 Пользователь {name} завершил диалог")
    if in_s:
        waiting_for_support.discard(uid)
        remove_active_dialog(uid)
        if had:
            await context.bot.send_message(SUPPORT_GROUP_ID, f"🚫 Пользователь {name} завершил диалог")
    user_has_message.discard(uid)
    await update.message.reply_text("✅ Вы вышли из режима общения")
    await send_main_menu(update, context)

async def next_op(update, context):
    uid = update.message.chat_id
    if uid in waiting_for_forward:
        await context.bot.send_message(ADMIN_GROUP_ID, f"🔄 {get_user_name(uid)} хочет сменить администратора")
        await update.message.reply_text("🔄 Смена администратора. Первый освободившийся ответит.")
    elif uid in waiting_for_support:
        await update.message.reply_text("❌ Команда /next недоступна в техподдержке")
    else:
        await update.message.reply_text("❌ Вы не в режиме общения")

async def ban(update, context):
    if not update.message.reply_to_message:
        return await update.message.reply_text("❌ Ответьте на сообщение пользователя. Код: BAN_ERR01")
    cid = update.effective_chat.id
    if cid == ADMIN_GROUP_ID:
        fwd = forwarded
    elif cid == SUPPORT_GROUP_ID:
        fwd = support_forwarded
    else:
        return await update.message.reply_text("❌ Эта группа не поддерживается. Код: BAN_ERR02")
    uid = get_uid_from_reply(update.message, fwd)
    if not uid:
        return await update.message.reply_text("❌ Не удалось определить пользователя. Код: BAN_ERR03")
    name = get_user_name(uid)
    clear_user_data(uid)
    if not context.args:
        banned_users.add(uid)
        ban_until.pop(uid, None)
        save_db()
        try:
            await context.bot.send_message(uid, "🚫 Вы забанены навсегда")
        except:
            pass
        return await update.message.reply_text(f"✅ {name} забанен навсегда")
    sec = parse_time(context.args[0])
    if not sec:
        return await update.message.reply_text("❌ Примеры: 30м, 2ч, 1д. Код: BAN_ERR04")
    until = datetime.now() + timedelta(seconds=sec)
    banned_users.add(uid)
    ban_until[uid] = until
    save_db()
    try:
        await context.bot.send_message(uid, f"🚫 Вы забанены на {context.args[0]}")
    except:
        pass
    await update.message.reply_text(f"✅ {name} забанен на {context.args[0]}")

async def unban(update, context):
    if not update.message.reply_to_message:
        return await update.message.reply_text("❌ Ответьте на сообщение. Код: UNBAN_ERR01")
    cid = update.effective_chat.id
    if cid == ADMIN_GROUP_ID:
        fwd = forwarded
    elif cid == SUPPORT_GROUP_ID:
        fwd = support_forwarded
    else:
        return await update.message.reply_text("❌ Эта группа не поддерживается. Код: UNBAN_ERR02")
    uid = get_uid_from_reply(update.message, fwd)
    if not uid:
        return await update.message.reply_text("❌ Не удалось определить пользователя. Код: UNBAN_ERR03")
    if uid not in banned_users:
        return await update.message.reply_text("❌ Пользователь не забанен. Код: UNBAN_ERR04")
    banned_users.discard(uid)
    ban_until.pop(uid, None)
    save_db()
    try:
        await context.bot.send_message(uid, "✅ Вы разбанены")
    except:
        pass
    await update.message.reply_text(f"✅ {get_user_name(uid)} разбанен")

async def mute(update, context):
    if not update.message.reply_to_message:
        return await update.message.reply_text("❌ Ответьте на сообщение. Код: MUTE_ERR01")
    cid = update.effective_chat.id
    if cid == ADMIN_GROUP_ID:
        fwd = forwarded
    elif cid == SUPPORT_GROUP_ID:
        fwd = support_forwarded
    else:
        return await update.message.reply_text("❌ Эта группа не поддерживается. Код: MUTE_ERR02")
    uid = get_uid_from_reply(update.message, fwd)
    if not uid:
        return await update.message.reply_text("❌ Не удалось определить пользователя. Код: MUTE_ERR03")
    name = get_user_name(uid)
    clear_user_data(uid)
    if not context.args:
        return await update.message.reply_text("❌ Укажите время: /mute 30м. Код: MUTE_ERR04")
    sec = parse_time(context.args[0])
    if not sec:
        return await update.message.reply_text("❌ Примеры: 30м, 2ч, 1д. Код: MUTE_ERR05")
    until = datetime.now() + timedelta(seconds=sec)
    muted_users.add(uid)
    mute_until[uid] = until
    save_db()
    try:
        await context.bot.send_message(uid, f"🔇 Вы замучены на {context.args[0]}")
    except:
        pass
    await update.message.reply_text(f"✅ {name} замучен на {context.args[0]}")

async def unmute(update, context):
    if not update.message.reply_to_message:
        return await update.message.reply_text("❌ Ответьте на сообщение. Код: UNMUTE_ERR01")
    cid = update.effective_chat.id
    if cid == ADMIN_GROUP_ID:
        fwd = forwarded
    elif cid == SUPPORT_GROUP_ID:
        fwd = support_forwarded
    else:
        return await update.message.reply_text("❌ Эта группа не поддерживается. Код: UNMUTE_ERR02")
    uid = get_uid_from_reply(update.message, fwd)
    if not uid:
        return await update.message.reply_text("❌ Не удалось определить пользователя. Код: UNMUTE_ERR03")
    if uid not in muted_users:
        return await update.message.reply_text("❌ Пользователь не замучен. Код: UNMUTE_ERR04")
    muted_users.discard(uid)
    mute_until.pop(uid, None)
    save_db()
    try:
        await context.bot.send_message(uid, "✅ Мут снят")
    except:
        pass
    await update.message.reply_text(f"✅ Мут снят с {get_user_name(uid)}")

async def info_command(update, context):
    if update.message.chat.type not in ["group","supergroup"]:
        return await update.message.reply_text("❌ Команда /info доступна только в группах")
    if not update.message.reply_to_message:
        return await update.message.reply_text("❌ Ответьте на сообщение пользователя")
    cid = update.effective_chat.id
    if cid == ADMIN_GROUP_ID:
        fwd = forwarded
    elif cid == SUPPORT_GROUP_ID:
        fwd = support_forwarded
    else:
        return await update.message.reply_text("❌ Эта группа не поддерживается")
    uid = get_uid_from_reply(update.message, fwd)
    if not uid:
        return await update.message.reply_text("❌ Не удалось определить пользователя")
    p = user_profiles.get(uid, {})
    is_owner = update.effective_user.id == OWNER_ID
    text = f"📋 **АНКЕТА ПОЛЬЗОВАТЕЛЯ:**\n\n👤 {get_user_name(uid)}\n✏️ Имя: {p.get('name','не указано')}\n📅 Возраст: {p.get('age','не указан')}\n{get_gender_emoji(p.get('gender'))}\n{'🆘 #поддержка' if p.get('type')=='support' else '💬 #общение' if p.get('type')=='communication' else '❓ не выбрано'}"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=info_buttons(uid, is_owner))

async def admin_panel(update, context):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ У вас нет прав на эту команду")
        return
    await update.message.reply_text("👑 **Панель управления владельца**\n\nВыберите действие:", parse_mode="Markdown", reply_markup=admin_panel_buttons())

async def clear_all_dialogs(context):
    global waiting_for_forward, waiting_for_support, user_has_message, profile_sent
    fwd_users = list(waiting_for_forward)
    sup_users = list(waiting_for_support)
    for uid in fwd_users:
        try:
            await context.bot.send_message(uid, "🛠 Бот перешёл в режим технических работ. Все диалоги завершены.")
        except:
            pass
        waiting_for_forward.discard(uid)
        remove_active_dialog(uid)
        user_has_message.discard(uid)
        profile_sent.discard(uid)
    for uid in sup_users:
        try:
            await context.bot.send_message(uid, "🛠 Бот перешёл в режим технических работ. Все диалоги завершены.")
        except:
            pass
        waiting_for_support.discard(uid)
        remove_active_dialog(uid)
        user_has_message.discard(uid)
        profile_sent.discard(uid)
    if fwd_users:
        await context.bot.send_message(ADMIN_GROUP_ID, "🔧 Режим технических работ ВКЛЮЧЁН. Все диалоги с администраторами завершены.")
    if sup_users:
        await context.bot.send_message(SUPPORT_GROUP_ID, "🔧 Режим технических работ ВКЛЮЧЁН. Все диалоги с техподдержкой завершены.")
    logger.info(f"Завершено диалогов: admin={len(fwd_users)}, support={len(sup_users)}")

async def maintenance_command(update, context):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ У вас нет прав на эту команду")
        return
    if not context.args:
        await update.message.reply_text("Использование: /maintenance on|off")
        return
    arg = context.args[0].lower()
    if arg == "on":
        if not maintenance_mode:
            await clear_all_dialogs(context)
        save_maintenance_mode(True)
        await update.message.reply_text("🛠 Режим технических работ ВКЛЮЧЁН. Обычные пользователи не могут использовать бота. Все диалоги завершены.")
    elif arg == "off":
        save_maintenance_mode(False)
        await update.message.reply_text("✅ Режим технических работ ВЫКЛЮЧЁН. Бот работает в штатном режиме.")
    else:
        await update.message.reply_text("Неверный аргумент. Используйте on или off")

async def stats(update, context):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ У вас нет прав на эту команду")
        return
    total = len(user_profiles)
    sup = sum(1 for p in user_profiles.values() if p.get('type') == 'support')
    com = sum(1 for p in user_profiles.values() if p.get('type') == 'communication')
    await update.message.reply_text(
        f"📊 **Статистика**\n\n"
        f"👥 Всего пользователей: `{total}`\n"
        f"🆘 Поддержка: `{sup}`\n"
        f"💬 Общение: `{com}`\n"
        f"❓ Не выбрали тип: `{total - sup - com}`",
        parse_mode="Markdown"
    )

async def broadcast(update, context):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ У вас нет прав на эту команду")
        return
    if update.message.chat.type != "private":
        return
    context.user_data['awaiting_broadcast'] = True
    await update.message.reply_text(
        "📢 **РАССЫЛКА**\n\n"
        "Отправьте сообщение для рассылки.\n\n"
        "Поддерживаются:\n"
        "• Текст (с форматированием)\n"
        "• Фото\n"
        "• Видео\n"
        "• GIF\n"
        "• Голосовые\n"
        "• Документы",
        parse_mode="Markdown",
        reply_markup=cancel_btn()
    )

async def save_broadcast_data(update, context, uid):
    msg = update.message
    if msg.text:
        broadcast_data[uid] = {'type':'text','content':msg.text,'parse_mode':msg.parse_mode}
    elif msg.photo:
        broadcast_data[uid] = {'type':'photo','content':msg.photo[-1].file_id,'caption':msg.caption,'parse_mode':msg.parse_mode}
    elif msg.video:
        broadcast_data[uid] = {'type':'video','content':msg.video.file_id,'caption':msg.caption,'parse_mode':msg.parse_mode}
    elif msg.animation:
        broadcast_data[uid] = {'type':'animation','content':msg.animation.file_id,'caption':msg.caption,'parse_mode':msg.parse_mode}
    elif msg.voice:
        broadcast_data[uid] = {'type':'voice','content':msg.voice.file_id,'caption':msg.caption}
    elif msg.document:
        broadcast_data[uid] = {'type':'document','content':msg.document.file_id,'caption':msg.caption,'parse_mode':msg.parse_mode}
    else:
        await update.message.reply_text("❌ Этот тип не поддерживается. Код: BC_ERR01")
        return
    await update.message.reply_text("📢 **ПРЕВЬЮ РАССЫЛКИ:**", parse_mode="Markdown")
    if broadcast_data[uid]['type']=='text':
        await update.message.reply_text(broadcast_data[uid]['content'], parse_mode=broadcast_data[uid].get('parse_mode','HTML'))
    elif broadcast_data[uid]['type']=='photo':
        await update.message.reply_photo(photo=broadcast_data[uid]['content'], caption=broadcast_data[uid].get('caption'), parse_mode=broadcast_data[uid].get('parse_mode'))
    elif broadcast_data[uid]['type']=='video':
        await update.message.reply_video(video=broadcast_data[uid]['content'], caption=broadcast_data[uid].get('caption'), parse_mode=broadcast_data[uid].get('parse_mode'))
    elif broadcast_data[uid]['type']=='animation':
        await update.message.reply_animation(animation=broadcast_data[uid]['content'], caption=broadcast_data[uid].get('caption'), parse_mode=broadcast_data[uid].get('parse_mode'))
    elif broadcast_data[uid]['type']=='voice':
        await update.message.reply_voice(voice=broadcast_data[uid]['content'], caption=broadcast_data[uid].get('caption'))
    elif broadcast_data[uid]['type']=='document':
        await update.message.reply_document(document=broadcast_data[uid]['content'], caption=broadcast_data[uid].get('caption'), parse_mode=broadcast_data[uid].get('parse_mode'))
    kb = [[InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_broad"), InlineKeyboardButton("❌ Отмена", callback_data="cancel_broad")]]
    await update.message.reply_text("Отправить рассылку?", reply_markup=InlineKeyboardMarkup(kb))

async def execute_broadcast(update, context, uid):
    data = broadcast_data.pop(uid, None)
    if not data:
        await update.callback_query.edit_message_text("❌ Нет данных. Код: BC_ERR02")
        return
    await update.callback_query.edit_message_text(f"🚀 Рассылка {len(user_profiles)} пользователям...")
    sent = blocked = 0
    for uid2 in list(user_profiles.keys()):
        try:
            if data['type']=='text':
                await context.bot.send_message(uid2, data['content'], parse_mode=data.get('parse_mode','HTML'))
            elif data['type']=='photo':
                await context.bot.send_photo(uid2, data['content'], caption=data.get('caption'), parse_mode=data.get('parse_mode'))
            elif data['type']=='video':
                await context.bot.send_video(uid2, data['content'], caption=data.get('caption'), parse_mode=data.get('parse_mode'))
            elif data['type']=='animation':
                await context.bot.send_animation(uid2, data['content'], caption=data.get('caption'), parse_mode=data.get('parse_mode'))
            elif data['type']=='voice':
                await context.bot.send_voice(uid2, data['content'], caption=data.get('caption'))
            elif data['type']=='document':
                await context.bot.send_document(uid2, data['content'], caption=data.get('caption'), parse_mode=data.get('parse_mode'))
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            err = str(e).lower()
            if "blocked" in err or "deactivated" in err or "chat not found" in err:
                if uid2 in user_profiles:
                    del user_profiles[uid2]
                    blocked += 1
                    save_db()
    await update.callback_query.message.reply_text(
        f"✅ **Рассылка завершена!**\n\n"
        f"📨 Отправлено: `{sent}`\n"
        f"🚫 Удалено: `{blocked}`\n"
        f"👥 Осталось: `{len(user_profiles)}`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Меню", callback_data="back")]])
    )

async def list_users(update, context):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ У вас нет прав на эту команду")
        return
    page = 1
    if context.args and context.args[0].isdigit():
        page = int(context.args[0])
    await send_list_page(update.message.chat.id, page, context)

async def user_info(update, context):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ У вас нет прав на эту команду")
        return
    if not context.args:
        await update.message.reply_text(
            "🔍 **Поиск пользователя**\n\n"
            "Использование:\n"
            "`/user_info <id или @username>`\n\n"
            "Примеры:\n"
            "`/user_info 123456789`\n"
            "`/user_info @username`",
            parse_mode="Markdown"
        )
        return
    query = context.args[0]
    if query.isdigit():
        target = int(query)
        if target in user_profiles:
            await show_user_full_info(update, context, target)
        else:
            await update.message.reply_text(f"❌ Пользователь с ID {target} не найден")
        return
    username = query.lstrip('@').lower()
    found = None
    for uid, data in user_profiles.items():
        if data.get('username','').lower() == username:
            found = uid
            break
    if found:
        await show_user_full_info(update, context, found)
    else:
        await update.message.reply_text(f"❌ Пользователь с username @{username} не найден")

async def show_user_full_info(update, context, target_id):
    p = user_profiles.get(target_id, {})
    text = (
        f"👤 **Полная информация о пользователе**\n\n"
        f"🆔 ID: `{target_id}`\n"
        f"📝 First name: {p.get('first_name','не указан')}\n"
        f"🔖 Username: @{p.get('username','нет')}\n"
        f"📅 Зарегистрирован: {p.get('registered_at','неизвестно')}\n"
        f"👤 Имя в анкете: {p.get('name','не указано')}\n"
        f"📅 Возраст: {p.get('age','не указан')}\n"
        f"{get_gender_emoji(p.get('gender'))}\n"
        f"🏷️ Тип: {'🆘 #поддержка' if p.get('type')=='support' else '💬 #общение' if p.get('type')=='communication' else '❓ не выбрано'}"
    )
    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=user_management_buttons(target_id))
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=user_management_buttons(target_id))

async def send_list_page(chat_id, page, context):
    users_per_page = 6
    users_list = list(user_profiles.items())
    total = len(users_list)
    if total == 0:
        await context.bot.send_message(chat_id, "📋 Пользователи\n\nНет зарегистрированных пользователей.")
        return
    total_pages = max(1, (total + users_per_page - 1)//users_per_page)
    page = max(1, min(page, total_pages))
    start = (page-1)*users_per_page
    end = min(start+users_per_page, total)
    text = f"📋 Пользователи (стр. {page}/{total_pages})\n\n"
    for uid, data in users_list[start:end]:
        name = data.get('name','❌')
        age = data.get('age','❌')
        gender = get_gender_emoji(data.get('gender'))
        p_type = "🆘" if data.get('type')=='support' else "💬" if data.get('type')=='communication' else "❓"
        username = data.get('username')
        if username:
            text += f"🆔 {uid} | @{username}\n"
        else:
            text += f"🆔 {uid}\n"
        text += f"👤 {name} | {age} | {gender} | {p_type}\n\n"
    keyboard = []
    if page > 1:
        keyboard.append(InlineKeyboardButton("◀️ Назад", callback_data=f"list_page_{page-1}"))
    if page < total_pages:
        keyboard.append(InlineKeyboardButton("Вперед ▶️", callback_data=f"list_page_{page+1}"))
    reply_markup = InlineKeyboardMarkup([keyboard]) if keyboard else None
    await context.bot.send_message(chat_id, text, reply_markup=reply_markup)

async def save_profile(update, context):
    uid = update.message.chat_id
    text = update.message.text
    if 'step' not in context.user_data:
        context.user_data['step'] = 1
        context.user_data['data'] = {}
    step = context.user_data['step']
    if step == 1:
        context.user_data['data']['name'] = text
        context.user_data['step'] = 2
        await update.message.reply_text("📝 Введите возраст (от 1 до 50 лет):", reply_markup=cancel_btn())
    elif step == 2:
        if not text.isdigit():
            await update.message.reply_text("❌ Пожалуйста, введите цифры!", reply_markup=cancel_btn())
            return
        age = int(text)
        if age < 1 or age > 50:
            await update.message.reply_text("❌ Пожалуйста, укажите корректный возраст", reply_markup=cancel_btn())
            return
        context.user_data['data']['age'] = text
        context.user_data['step'] = 3
        kb = [[InlineKeyboardButton("♂️ Мужской", callback_data="gender_male")],[InlineKeyboardButton("♀️ Женский", callback_data="gender_female")]]
        await update.message.reply_text("👤 Выберите ваш пол:", reply_markup=InlineKeyboardMarkup(kb))

async def profile_text(uid):
    p = user_profiles.get(uid, {})
    name = get_user_name(uid)
    gender_text = get_gender_emoji(p.get('gender'))
    status = ""
    if is_banned(uid):
        if uid in ban_until:
            remaining = (ban_until[uid]-datetime.now()).total_seconds()
            status = f"\n🚫 **ЗАБАНЕН** (осталось: {format_time_remaining(remaining)})"
        else:
            status = "\n🚫 **ЗАБАНЕН НАВСЕГДА**"
    elif is_muted(uid):
        if uid in mute_until:
            remaining = (mute_until[uid]-datetime.now()).total_seconds()
            status = f"\n🔇 **ЗАМУЧЕН** (осталось: {format_time_remaining(remaining)})"
        else:
            status = "\n🔇 **ЗАМУЧЕН**"
    return f"📋 **НОВОЕ СООБЩЕНИЕ:**\n\n👤 Аккаунт: {name}\n✏️ Имя: {p.get('name','не указано')}\n📅 Возраст: {p.get('age','не указан')}\n{gender_text}{status}\n{'🆘 #поддержка' if p.get('type')=='support' else '💬 #общение' if p.get('type')=='communication' else '❓ не выбрано'}\n{'─'*30}"

async def send_media_to_user(bot, user_id, message):
    if message.text:
        return await bot.send_message(user_id, message.text, parse_mode=message.parse_mode if hasattr(message, 'parse_mode') else None)
    elif message.photo:
        return await bot.send_photo(user_id, message.photo[-1].file_id, caption=message.caption, parse_mode=message.parse_mode if hasattr(message, 'parse_mode') else None)
    elif message.video:
        return await bot.send_video(user_id, message.video.file_id, caption=message.caption, parse_mode=message.parse_mode if hasattr(message, 'parse_mode') else None)
    elif message.animation:
        return await bot.send_animation(user_id, message.animation.file_id, caption=message.caption, parse_mode=message.parse_mode if hasattr(message, 'parse_mode') else None)
    elif message.voice:
        return await bot.send_voice(user_id, message.voice.file_id, caption=message.caption)
    elif message.document:
        return await bot.send_document(user_id, message.document.file_id, caption=message.caption, parse_mode=message.parse_mode if hasattr(message, 'parse_mode') else None)
    elif message.sticker:
        return await bot.send_sticker(user_id, message.sticker.file_id)
    elif message.video_note:
        return await bot.send_video_note(user_id, message.video_note.file_id)
    else:
        return await bot.forward_message(user_id, message.chat_id, message.message_id)

# ==================== ЭКСПОРТ ПОЛЬЗОВАТЕЛЕЙ В CSV ====================
async def export_users(update, context):
    """Команда для выгрузки всех пользователей из БД в CSV-файл (только для владельца)"""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ У вас нет прав на эту команду.")
        return
    await update.message.reply_text("⏳ Формирую файл с пользователями...")
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT user_id, first_name, username, registered_at, name, age, gender, type
                FROM users
                ORDER BY user_id
            """)
            rows = cur.fetchall()
            if not rows:
                await update.message.reply_text("📭 База пользователей пуста.")
                return
            # Создаём CSV в памяти
            output = io.StringIO()
            writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_MINIMAL)
            writer.writerow(['user_id', 'first_name', 'username', 'registered_at', 'name', 'age', 'gender', 'type'])
            for row in rows:
                writer.writerow([
                    row['user_id'],
                    row['first_name'] or '',
                    row['username'] or '',
                    row['registered_at'].isoformat() if row['registered_at'] else '',
                    row['name'] or '',
                    row['age'] or '',
                    row['gender'] or '',
                    row['type'] or ''
                ])
            output.seek(0)
            # Отправляем файл
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=output.getvalue().encode('utf-8-sig'),
                filename=f"users_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                caption=f"📊 Всего пользователей: {len(rows)}"
            )
            logger.info(f"Владелец {OWNER_ID} выгрузил {len(rows)} пользователей")
    except Exception as e:
        logger.error(f"Ошибка экспорта пользователей: {e}")
        await update.message.reply_text("❌ Не удалось выгрузить пользователей. Обратитесь в техподдержку. Код: EXP_ERR01")
    finally:
        release_db_connection(conn)

# ==================== ОСНОВНОЙ ОБРАБОТЧИК ЛИЧНЫХ СООБЩЕНИЙ ====================
async def forward_msg(update, context):
    if not update.message or update.message.chat.type != "private":
        return
    uid = update.message.chat_id
    user = update.effective_user
    update_user_info(user.id, user.first_name, user.username)

    # ----- ОБРАБОТКА СОСТОЯНИЙ (ОТВЕТ НА АНКЕТУ, ПРЕДУПРЕЖДЕНИЕ, ПОИСК) -----
    if context.user_data.get('reply_to_applicant'):
        applicant_id = context.user_data.pop('reply_to_applicant')
        msg_id = context.user_data.pop('reply_to_message_id', None)
        if uid != OWNER_ID:
            await update.message.reply_text("❌ У вас нет прав на это действие.")
            return
        try:
            await context.bot.send_message(applicant_id, f"📨 *Ответ на вашу анкету:*\n\n{update.message.text}", parse_mode="Markdown")
            await update.message.reply_text("✅ Ответ отправлен кандидату.")
            if msg_id and msg_id in application_messages:
                try:
                    await context.bot.edit_message_reply_markup(chat_id=ADMIN_APPLICATION_GROUP_ID, message_id=msg_id, reply_markup=None)
                except:
                    pass
        except Exception as e:
            logger.error(f"Ошибка ответа кандидату: {e}")
            await update.message.reply_text("❌ Не удалось отправить ответ. Пожалуйста, обратитесь в техподдержку. Код: APP_ERR06")
        return

    if context.user_data.get('warn_target'):
        target_id = context.user_data.pop('warn_target')
        reason = update.message.text
        if uid != OWNER_ID:
            await update.message.reply_text("❌ У вас нет прав на это действие.")
            return
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO warnings (user_id, reason, warned_by) VALUES (%s, %s, %s)", (target_id, reason, uid))
                conn.commit()
            await context.bot.send_message(target_id, f"⚠️ Вы получили предупреждение от администратора.\nПричина: {reason}")
            await update.message.reply_text(f"✅ Предупреждение выдано пользователю {get_user_name(target_id)}.")
        except Exception as e:
            logger.error(f"Ошибка выдачи предупреждения: {e}")
            await update.message.reply_text("❌ Не удалось выдать предупреждение. Пожалуйста, обратитесь в техподдержку. Код: WARN_ERR02")
        finally:
            release_db_connection(conn)
        return

    if context.user_data.get('awaiting_user_search'):
        context.user_data.pop('awaiting_user_search')
        if uid != OWNER_ID:
            await update.message.reply_text("❌ У вас нет прав на это действие.")
            return
        query = update.message.text
        if query.isdigit():
            target_id = int(query)
            if target_id in user_profiles:
                await show_user_full_info(update, context, target_id)
            else:
                await update.message.reply_text(f"❌ Пользователь с ID {target_id} не найден")
        else:
            username = query.lstrip('@').lower()
            found = None
            for uid2, data in user_profiles.items():
                if data.get('username','').lower() == username:
                    found = uid2
                    break
            if found:
                await show_user_full_info(update, context, found)
            else:
                await update.message.reply_text(f"❌ Пользователь с username @{username} не найден")
        return

    # ----- ОСТАЛЬНЫЕ ОБРАБОТКИ -----
    if context.user_data.get('admin_application'):
        await process_application_answer(update, context)
        return
    if not await check_subscription(update, context):
        await update.message.reply_text("❌ Подпишитесь на канал!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 Подписаться", url=CHANNEL_LINK)]]))
        return
    if context.user_data.get('awaiting_broadcast'):
        context.user_data['awaiting_broadcast'] = False
        await save_broadcast_data(update, context, uid)
        return
    if context.user_data.get('send_to_user'):
        target = context.user_data.pop('send_to_user')
        msg = update.message
        try:
            await context.bot.send_message(target, f"👑 *Сообщение от владельца:*\n\n{msg.text}", parse_mode="Markdown")
            await update.message.reply_text("✅ Сообщение отправлено пользователю")
        except Exception as e:
            logger.error(f"Ошибка отправки от владельца: {e}")
            await update.message.reply_text("❌ Не удалось отправить сообщение. Код: OWN_ERR02")
        return
    if context.user_data.get('edit_target') and context.user_data.get('edit_field'):
        target = context.user_data['edit_target']
        field = context.user_data['edit_field']
        from_info = context.user_data.get('edit_from_info', False)
        if field == 'name':
            user_profiles[target]['name'] = update.message.text
            await update.message.reply_text("✅ Имя изменено")
        elif field == 'age':
            if not update.message.text.isdigit():
                await update.message.reply_text("❌ Только цифры!")
                return
            age = int(update.message.text)
            if age < 1 or age > 50:
                await update.message.reply_text("❌ Пожалуйста, укажите корректный возраст")
                return
            user_profiles[target]['age'] = update.message.text
            await update.message.reply_text("✅ Возраст изменен")
        save_db()
        p = user_profiles.get(target, {})
        text = f"👤 **Пользователь**\n🆔 ID: `{target}`\n👤 Имя: {p.get('name','не указано')}\n📅 Возраст: {p.get('age','не указан')}\n{get_gender_emoji(p.get('gender'))}\n🏷️ Тип: {'🆘 #поддержка' if p.get('type')=='support' else '💬 #общение' if p.get('type')=='communication' else '❓ не выбрано'}\n📝 First name: {p.get('first_name','не указан')}\n🔖 Username: @{p.get('username','нет')}\n📅 Зарегистрирован: {p.get('registered_at','неизвестно')}"
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=profile_view_buttons(target, from_info))
        del context.user_data['edit_target']
        del context.user_data['edit_field']
        if 'edit_from_info' in context.user_data:
            del context.user_data['edit_from_info']
        return
    if context.user_data.get('edit'):
        field = context.user_data['edit']
        if field == 'name':
            user_profiles[uid]['name'] = update.message.text
            await update.message.reply_text("✅ Имя изменено")
        elif field == 'age':
            if not update.message.text.isdigit():
                await update.message.reply_text("❌ Только цифры!")
                return
            age = int(update.message.text)
            if age < 1 or age > 50:
                await update.message.reply_text("❌ Пожалуйста, укажите корректный возраст")
                return
            user_profiles[uid]['age'] = update.message.text
            await update.message.reply_text("✅ Возраст изменен")
        save_db()
        del context.user_data['edit']
        return
    if context.user_data.get('awaiting'):
        await save_profile(update, context)
        return
    if uid in waiting_for_forward:
        if is_banned(uid) or is_muted(uid):
            if is_banned(uid):
                if uid in ban_until:
                    remaining = (ban_until[uid]-datetime.now()).total_seconds()
                    await update.message.reply_text(f"🚫 Вы забанены. Осталось: {format_time_remaining(remaining)}")
                else:
                    await update.message.reply_text("🚫 Вы забанены")
            else:
                if uid in mute_until:
                    remaining = (mute_until[uid]-datetime.now()).total_seconds()
                    await update.message.reply_text(f"🔇 Вы замучены. Осталось: {format_time_remaining(remaining)}")
                else:
                    await update.message.reply_text("🔇 Вы замучены")
            return
        user_has_message.add(uid)
        if uid not in profile_sent:
            await context.bot.send_message(ADMIN_GROUP_ID, await profile_text(uid), parse_mode="Markdown")
            profile_sent.add(uid)
        try:
            s = await context.bot.forward_message(ADMIN_GROUP_ID, uid, update.message.message_id)
            forwarded[s.message_id] = (uid, None)
            save_forwarded_message(ADMIN_GROUP_ID, s.message_id, uid, 'admin')
        except Exception as e:
            err = str(e).lower()
            if "blocked" in err or "deactivated" in err:
                if remove_blocked_user(uid):
                    await update.message.reply_text("❌ Вы заблокировали бота. Пользователь удален из базы.")
            else:
                logger.error(f"Ошибка пересылки в админ-группу: {e}")
                await update.message.reply_text("❌ Произошла ошибка. Пожалуйста, попробуйте позже. Код: FWD_ERR03")
    elif uid in waiting_for_support:
        user_has_message.add(uid)
        if uid not in profile_sent:
            await context.bot.send_message(SUPPORT_GROUP_ID, await profile_text(uid), parse_mode="Markdown")
            profile_sent.add(uid)
        try:
            s = await context.bot.forward_message(SUPPORT_GROUP_ID, uid, update.message.message_id)
            support_forwarded[s.message_id] = (uid, None)
            save_forwarded_message(SUPPORT_GROUP_ID, s.message_id, uid, 'support')
        except Exception as e:
            err = str(e).lower()
            if "blocked" in err or "deactivated" in err:
                if remove_blocked_user(uid):
                    await update.message.reply_text("❌ Вы заблокировали бота. Пользователь удален из базы.")
            else:
                logger.error(f"Ошибка пересылки в группу поддержки: {e}")
                await update.message.reply_text("❌ Произошла ошибка. Пожалуйста, попробуйте позже. Код: FWD_ERR04")
    else:
        if is_banned(uid):
            if uid in ban_until:
                remaining = (ban_until[uid]-datetime.now()).total_seconds()
                await update.message.reply_text(f"🚫 Вы забанены. Осталось: {format_time_remaining(remaining)}")
            else:
                await update.message.reply_text("🚫 Вы забанены")
        elif is_muted(uid):
            if uid in mute_until:
                remaining = (mute_until[uid]-datetime.now()).total_seconds()
                await update.message.reply_text(f"🔇 Вы замучены. Осталось: {format_time_remaining(remaining)}")
            else:
                await update.message.reply_text("🔇 Вы замучены")
        else:
            await update.message.reply_text("❌ Нажмите 'Написать админу' или 'Тех.поддержка'")

# ==================== ОБРАБОТЧИК СООБЩЕНИЙ В ГРУППАХ (ОТВЕТЫ И РЕДАКТИРОВАНИЕ) ====================
async def reply_to(update, context):
    if not await check_group_access(update, context):
        return
    msg = update.message or update.edited_message
    if not msg or not msg.reply_to_message:
        return
    if msg.text and msg.text.startswith('//'):
        await msg.reply_text("📝 Внутреннее сообщение администратора (не отправлено пользователю)")
        return
    cid, rid = msg.chat.id, msg.reply_to_message.message_id
    
    # Ответ на анкету (с кнопкой)
    if cid == ADMIN_APPLICATION_GROUP_ID and rid in application_messages:
        user_id = application_messages[rid]
        if user_id:
            try:
                await context.bot.send_message(user_id, f"📨 *Ответ на вашу анкету:*\n\n{msg.text}", parse_mode="Markdown")
                await msg.reply_text("✅ Ответ отправлен пользователю.")
                await add_reply_button_to_user(user_id, 'admin', context, original_message_id=None)
            except Exception as e:
                logger.error(f"Ошибка ответа на анкету: {e}")
                await msg.reply_text(f"❌ Не удалось отправить ответ. Код: APP_ERR05")
        return

    # Обычные диалоги (админ и техподдержка)
    if cid == ADMIN_GROUP_ID and rid in forwarded:
        fwd, rep = forwarded, admin_replies
        chat_type = 'admin'
    elif cid == SUPPORT_GROUP_ID and rid in support_forwarded:
        fwd, rep = support_forwarded, support_admin_replies
        chat_type = 'support'
    else:
        return

    uid, _ = fwd[rid]
    if not msg.edit_date:  # Новое сообщение
        try:
            sent = await send_media_to_user(context.bot, uid, msg)
            rep[msg.message_id] = (uid, sent.message_id)
            save_admin_reply(cid, msg.message_id, uid, sent.message_id, chat_type)
        except Exception as e:
            err = str(e).lower()
            if "blocked" in err or "deactivated" in err:
                await msg.reply_text(f"⚠️ {get_user_name(uid)} заблокировал бота. Пользователь удален из базы.")
                remove_blocked_user(uid)
            else:
                if "not enough rights" not in err and "message is not modified" not in err:
                    logger.error(f"Ошибка отправки ответа: {e}")
                    await msg.reply_text("❌ Не удалось доставить сообщение пользователю. Код: REP_ERR06")
    elif msg.edit_date and msg.message_id in rep:  # Редактирование
        uid, mid = rep[msg.message_id]
        logger.info(f"Редактирование: найдено сообщение uid={uid}, mid={mid}")
        try:
            if msg.text:
                await context.bot.edit_message_text(uid, mid, text=msg.text, parse_mode=msg.parse_mode if hasattr(msg, 'parse_mode') else None)
                logger.info(f"Текст сообщения {mid} обновлён")
            elif msg.caption:
                await context.bot.edit_message_caption(uid, mid, caption=msg.caption, parse_mode=msg.parse_mode if hasattr(msg, 'parse_mode') else None)
                logger.info(f"Подпись сообщения {mid} обновлена")
        except Exception as e:
            err = str(e).lower()
            if "blocked" in err or "deactivated" in err:
                remove_blocked_user(uid)
            elif "message is not modified" not in err:
                logger.error(f"Ошибка редактирования: {e}")

async def edited_reply_to(update, context):
    await reply_to(update, context)

# ==================== ОБРАБОТЧИК КНОПОК (CALLBACK) ====================
async def button_handler(update, context):
    q = update.callback_query
    await q.answer()
    data, uid = q.data, q.from_user.id
    if maintenance_mode and uid != OWNER_ID and not data.startswith(("confirm_broad","cancel_broad","list_page_","admin_","reply_to_")):
        await q.edit_message_text("🛠 Бот на технических работах. Пожалуйста, зайдите позже. Код: MAINT001")
        return
    async def safe_send(text, reply_markup=None, parse_mode=None):
        try:
            await q.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        except:
            try:
                await q.message.delete()
            except:
                pass
            await q.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)

    if data.startswith("reply_to_"):
        await handle_user_reply_button(update, context)
        return

    if data.startswith("remove_warning_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        warn_id = int(data.split("_")[2])
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM warnings WHERE id = %s", (warn_id,))
                conn.commit()
            await q.message.reply_text(f"✅ Предупреждение #{warn_id} снято.")
            try:
                await q.message.edit_reply_markup(reply_markup=None)
            except:
                pass
        except Exception as e:
            logger.error(f"Ошибка удаления предупреждения: {e}")
            await q.message.reply_text("❌ Не удалось снять предупреждение. Код: WARN_ERR03")
        finally:
            release_db_connection(conn)
        return

    if data.startswith("reply_to_app_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        mid = int(data.split("_")[3])
        user_id = application_messages.get(mid)
        if user_id:
            context.user_data['reply_to_applicant'] = user_id
            context.user_data['reply_to_message_id'] = mid
            await safe_send("✏️ Введите текст ответа для кандидата:", reply_markup=cancel_btn())
        else:
            await safe_send("❌ Не удалось найти пользователя для ответа.")
        return

    # ========== АДМИН-ПАНЕЛЬ И СТАТИСТИКА ==========
    if data == "admin_stats":
        total = len(user_profiles)
        sup = sum(1 for p in user_profiles.values() if p.get('type') == 'support')
        com = sum(1 for p in user_profiles.values() if p.get('type') == 'communication')
        await safe_send(
            f"📊 **Статистика**\n\n"
            f"👥 Всего пользователей: `{total}`\n"
            f"🆘 Поддержка: `{sup}`\n"
            f"💬 Общение: `{com}`\n"
            f"❓ Не выбрали тип: `{total - sup - com}`",
            parse_mode="Markdown", reply_markup=admin_panel_buttons()
        )
        return
    if data == "admin_list_users":
        await send_list_page(q.message.chat.id, 1, context)
        try:
            await q.message.delete()
        except:
            pass
        return
    if data == "admin_search_user":
        context.user_data['awaiting_user_search'] = True
        await safe_send("🔍 Введите ID или @username пользователя:", reply_markup=cancel_btn())
        return
    if data == "admin_broadcast":
        context.user_data['awaiting_broadcast'] = True
        await safe_send(
            "📢 **РАССЫЛКА**\n\n"
            "Отправьте сообщение для рассылки.\n\n"
            "Поддерживаются:\n"
            "• Текст (с форматированием)\n"
            "• Фото\n"
            "• Видео\n"
            "• GIF\n"
            "• Голосовые\n"
            "• Документы",
            parse_mode="Markdown", reply_markup=cancel_btn()
        )
        return
    if data == "admin_maintenance":
        kb = [[InlineKeyboardButton("🛠 Включить", callback_data="maintenance_on"), InlineKeyboardButton("✅ Выключить", callback_data="maintenance_off")]]
        await safe_send("🔧 Управление режимом технических работ:", reply_markup=InlineKeyboardMarkup(kb))
        return
    if data == "maintenance_on":
        if not maintenance_mode:
            await clear_all_dialogs(context)
        save_maintenance_mode(True)
        await safe_send("🛠 Режим технических работ ВКЛЮЧЁН. Все диалоги завершены.", reply_markup=admin_panel_buttons())
        return
    if data == "maintenance_off":
        save_maintenance_mode(False)
        await safe_send("✅ Режим технических работ ВЫКЛЮЧЁН.", reply_markup=admin_panel_buttons())
        return
    if data == "admin_back_main":
        await safe_send("👑 **Панель управления владельца**\n\nВыберите действие:", parse_mode="Markdown", reply_markup=admin_panel_buttons())
        return

    # ========== ПРЕДУПРЕЖДЕНИЯ ==========
    if data.startswith("warn_user_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[2])
        context.user_data['warn_target'] = target_id
        await safe_send("⚠️ Введите причину предупреждения:", reply_markup=cancel_btn())
        return
    if data.startswith("warnings_history_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[2])
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, reason, warned_by, warned_at FROM warnings WHERE user_id = %s ORDER BY warned_at DESC", (target_id,))
                rows = cur.fetchall()
                if not rows:
                    await safe_send("📜 История предупреждений пуста.", reply_markup=user_management_buttons(target_id))
                else:
                    try:
                        await q.message.delete()
                    except:
                        pass
                    for warn_id, reason, warned_by, warned_at in rows:
                        text = f"⚠️ **Предупреждение #{warn_id}**\n📅 {warned_at.strftime('%d.%m.%Y %H:%M')}\n👮 Админ: {warned_by}\n📝 Причина: {reason}"
                        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Снять предупреждение", callback_data=f"remove_warning_{warn_id}")]])
                        await q.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
                    await q.message.reply_text("🔙 Вернуться к управлению пользователем", reply_markup=user_management_buttons(target_id))
        except Exception as e:
            logger.error(f"Ошибка получения предупреждений: {e}")
            await safe_send("❌ Не удалось загрузить историю. Код: WARN_ERR01")
        finally:
            release_db_connection(conn)
        return
    if data.startswith("reset_profile_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[2])
        if target_id in user_profiles:
            user_profiles[target_id]['name'] = None
            user_profiles[target_id]['age'] = None
            user_profiles[target_id]['gender'] = None
            user_profiles[target_id]['type'] = None
            save_db()
            await safe_send(f"✅ Анкета пользователя {get_user_name(target_id)} сброшена.", reply_markup=user_management_buttons(target_id))
        else:
            await safe_send("❌ Пользователь не найден.")
        return

    # ========== ОБРАБОТКА ЗАПОЛНЕНИЯ АНКЕТЫ ПОЛЬЗОВАТЕЛЯ ==========
    if data.startswith("gender_"):
        gender = 'male' if data=="gender_male" else 'female'
        context.user_data['data']['gender'] = gender
        context.user_data['step'] = 5
        kb = [[InlineKeyboardButton("🆘 #поддержка", callback_data="type_support")],[InlineKeyboardButton("💬 #общение", callback_data="type_comm")]]
        await safe_send("Выберите цель:", reply_markup=InlineKeyboardMarkup(kb))
        return
    if data in ("type_support","type_comm"):
        if context.user_data.get('step') == 5:
            p_type = 'support' if data=="type_support" else 'communication'
            update_profile(uid, context.user_data['data']['name'], context.user_data['data']['age'], context.user_data['data']['gender'], p_type)
            target = context.user_data.get('target','admin')
            if target == "admin":
                waiting_for_forward.add(uid)
                save_active_dialog(uid,'admin')
            else:
                waiting_for_support.add(uid)
                save_active_dialog(uid,'support')
            for k in ['step','data','awaiting','target_type']:
                context.user_data.pop(k,None)
            await safe_send("✅ Анкета сохранена!\n📨 Напишите сообщение", reply_markup=cancel_btn())
        return

    # ========== ИНФОРМАЦИЯ О ПОЛЬЗОВАТЕЛЕ (FULL_INFO) ==========
    if data.startswith("full_info_"):
        target_id = int(data.split("_")[2])
        p = user_profiles.get(target_id,{})
        text = f"👤 **Полная информация о пользователе**\n\n🆔 ID: `{target_id}`\n📝 First name: {p.get('first_name','не указан')}\n🔖 Username: @{p.get('username','нет')}\n📅 Зарегистрирован: {p.get('registered_at','неизвестно')}\n👤 Имя в анкете: {p.get('name','не указано')}\n📅 Возраст: {p.get('age','не указан')}\n{get_gender_emoji(p.get('gender'))}\n🏷️ Тип: {'🆘 #поддержка' if p.get('type')=='support' else '💬 #общение' if p.get('type')=='communication' else '❓ не выбрано'}"
        await q.message.reply_text(text, parse_mode="Markdown", reply_markup=profile_view_buttons(target_id, from_info=True))
        try:
            await q.message.delete()
        except:
            pass
        return

    # ========== РЕДАКТИРОВАНИЕ ПОЛЬЗОВАТЕЛЯ (ВЛАДЕЛЕЦ) ==========
    if data.startswith("edit_name_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        parts = data.split("_")
        target_id = int(parts[2])
        from_info = parts[3]=='True'
        context.user_data['edit_target'] = target_id
        context.user_data['edit_field'] = 'name'
        context.user_data['edit_from_info'] = from_info
        await safe_send("✏️ Введите новое имя:", reply_markup=cancel_btn())
        return
    if data.startswith("edit_age_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        parts = data.split("_")
        target_id = int(parts[2])
        from_info = parts[3]=='True'
        context.user_data['edit_target'] = target_id
        context.user_data['edit_field'] = 'age'
        context.user_data['edit_from_info'] = from_info
        await safe_send("📅 Введите новый возраст (от 1 до 50):", reply_markup=cancel_btn())
        return
    if data.startswith("edit_type_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        parts = data.split("_")
        target_id = int(parts[2])
        from_info = parts[3]=='True'
        kb = [[InlineKeyboardButton("🆘 #поддержка", callback_data=f"confirm_type_{target_id}_{from_info}_support"), InlineKeyboardButton("💬 #общение", callback_data=f"confirm_type_{target_id}_{from_info}_comm")]]
        await safe_send("Выберите новый тип:", reply_markup=InlineKeyboardMarkup(kb))
        return
    if data.startswith("edit_user_gender_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[3])
        kb = [[InlineKeyboardButton("♂️ Мужской", callback_data=f"set_gender_{target_id}_False_male"), InlineKeyboardButton("♀️ Женский", callback_data=f"set_gender_{target_id}_False_female")]]
        await safe_send("Выберите новый пол:", reply_markup=InlineKeyboardMarkup(kb))
        return
    if data.startswith("set_gender_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        parts = data.split("_")
        target_id = int(parts[1])
        from_info = parts[2]=='True'
        new_gender = parts[3]
        user_profiles[target_id]['gender'] = new_gender
        save_db()
        p = user_profiles.get(target_id,{})
        text = f"👤 **Пользователь**\n🆔 ID: `{target_id}`\n👤 Имя: {p.get('name','не указано')}\n📅 Возраст: {p.get('age','не указан')}\n{get_gender_emoji(p.get('gender'))}\n🏷️ Тип: {'🆘 #поддержка' if p.get('type')=='support' else '💬 #общение' if p.get('type')=='communication' else '❓ не выбрано'}\n📝 First name: {p.get('first_name','не указан')}\n🔖 Username: @{p.get('username','нет')}\n📅 Зарегистрирован: {p.get('registered_at','неизвестно')}"
        await safe_send(text, parse_mode="Markdown", reply_markup=profile_view_buttons(target_id, from_info))
        return
    if data.startswith("confirm_type_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        parts = data.split("_")
        target_id = int(parts[1])
        from_info = parts[2]=='True'
        new_type = 'support' if parts[3]=='support' else 'communication'
        user_profiles[target_id]['type'] = new_type
        save_db()
        p = user_profiles.get(target_id,{})
        text = f"👤 **Пользователь**\n🆔 ID: `{target_id}`\n👤 Имя: {p.get('name','не указано')}\n📅 Возраст: {p.get('age','не указан')}\n{get_gender_emoji(p.get('gender'))}\n🏷️ Тип: {'🆘 #поддержка' if p.get('type')=='support' else '💬 #общение' if p.get('type')=='communication' else '❓ не выбрано'}\n📝 First name: {p.get('first_name','не указан')}\n🔖 Username: @{p.get('username','нет')}\n📅 Зарегистрирован: {p.get('registered_at','неизвестно')}"
        await safe_send(text, parse_mode="Markdown", reply_markup=profile_view_buttons(target_id, from_info))
        return
    if data.startswith("back_to_info_"):
        parts = data.split("_")
        from_info = parts[3]=='True'
        target_id = int(parts[4])
        if from_info:
            p = user_profiles.get(target_id,{})
            text = f"📋 **АНКЕТА ПОЛЬЗОВАТЕЛЯ:**\n\n👤 {get_user_name(target_id)}\n✏️ Имя: {p.get('name','не указано')}\n📅 Возраст: {p.get('age','не указан')}\n{get_gender_emoji(p.get('gender'))}\n{'🆘 #поддержка' if p.get('type')=='support' else '💬 #общение' if p.get('type')=='communication' else '❓ не выбрано'}"
            await safe_send(text, parse_mode="Markdown", reply_markup=info_buttons(target_id, uid==OWNER_ID))
        else:
            await safe_send("Возврат к списку пользователей", reply_markup=back_button())
        return
    if data.startswith("edit_user_name_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[3])
        context.user_data['edit_target'] = target_id
        context.user_data['edit_field'] = 'name'
        context.user_data['edit_from_info'] = False
        await safe_send("✏️ Введите новое имя:", reply_markup=cancel_btn())
        return
    if data.startswith("edit_user_age_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[3])
        context.user_data['edit_target'] = target_id
        context.user_data['edit_field'] = 'age'
        context.user_data['edit_from_info'] = False
        await safe_send("📅 Введите новый возраст (от 1 до 50):", reply_markup=cancel_btn())
        return
    if data.startswith("edit_user_type_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[3])
        kb = [[InlineKeyboardButton("🆘 #поддержка", callback_data=f"confirm_type_{target_id}_False_support"), InlineKeyboardButton("💬 #общение", callback_data=f"confirm_type_{target_id}_False_comm")]]
        await safe_send("Выберите новый тип:", reply_markup=InlineKeyboardMarkup(kb))
        return
    if data.startswith("send_msg_to_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[3])
        context.user_data['send_to_user'] = target_id
        await safe_send("📝 Введите сообщение для пользователя (оно будет отправлено с пометкой *от владельца*):", parse_mode="Markdown", reply_markup=cancel_btn())
        return
    if data.startswith("unban_user_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[2])
        if target_id in banned_users:
            banned_users.discard(target_id)
            ban_until.pop(target_id, None)
            save_db()
            try:
                await context.bot.send_message(target_id, "👑 Владелец снял с вас бан.")
            except:
                pass
            await safe_send(f"✅ Пользователь {get_user_name(target_id)} разбанен.")
            await show_user_full_info(update, context, target_id)
        else:
            await safe_send("❌ Пользователь не забанен.")
        return
    if data.startswith("unmute_user_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[2])
        if target_id in muted_users:
            muted_users.discard(target_id)
            mute_until.pop(target_id, None)
            save_db()
            try:
                await context.bot.send_message(target_id, "👑 Владелец снял с вас мут.")
            except:
                pass
            await safe_send(f"✅ Мут снят с {get_user_name(target_id)}.")
            await show_user_full_info(update, context, target_id)
        else:
            await safe_send("❌ Пользователь не замучен.")
        return

    # ========== ПАГИНАЦИЯ СПИСКА ПОЛЬЗОВАТЕЛЕЙ ==========
    if data.startswith("list_page_"):
        page = int(data.split("_")[2])
        try:
            await q.message.delete()
        except:
            pass
        await send_list_page(q.message.chat.id, page, context)
        return
    if data == "user_back_main":
        await send_main_menu(update, context, chat_id=q.message.chat.id, message_id=q.message.message_id)
        return

    # ========== ТИПЫ АНКЕТЫ АДМИНИСТРАТОРА ==========
    if data.startswith("app_type_"):
        await application_type_callback(update, context)
        return

    # ========== РАССЫЛКА ==========
    if data == "confirm_broad":
        await execute_broadcast(update, context, uid)
        return
    if data == "cancel_broad":
        broadcast_data.pop(uid, None)
        await safe_send("❌ Отменено")
        await send_main_menu(update, context)
        return

    # ========== ГЛАВНОЕ МЕНЮ ==========
    if data in ("admin","support"):
        if data == "admin":
            if is_banned(uid):
                if uid in ban_until:
                    remaining = (ban_until[uid]-datetime.now()).total_seconds()
                    await safe_send(f"🚫 **Вы забанены!**\n\nОсталось: {format_time_remaining(remaining)}", parse_mode="Markdown")
                else:
                    await safe_send("🚫 **Вы забанены!**", parse_mode="Markdown")
                return
            if is_muted(uid):
                if uid in mute_until:
                    remaining = (mute_until[uid]-datetime.now()).total_seconds()
                    await safe_send(f"🔇 **Вы замучены!**\n\nОсталось: {format_time_remaining(remaining)}", parse_mode="Markdown")
                else:
                    await safe_send("🔇 **Вы замучены!**", parse_mode="Markdown")
                return
        if not await check_subscription(update, context):
            await safe_send("❌ **Для использования бота необходимо подписаться на наш канал!**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 Подписаться", url=CHANNEL_LINK)]]))
            return
        context.user_data['target'] = data
        if uid not in user_profiles:
            update_user_info(uid, q.from_user.first_name, q.from_user.username)
        if is_profile_complete(uid):
            if data == "admin":
                waiting_for_forward.add(uid)
                save_active_dialog(uid,'admin')
            else:
                waiting_for_support.add(uid)
                save_active_dialog(uid,'support')
            await safe_send("📨 Напишите сообщение", reply_markup=cancel_btn())
        else:
            logger.info(f"Пользователь {uid} без анкеты, запускаем заполнение")
            context.user_data.clear()
            context.user_data.update({'awaiting':True,'step':1,'data':{},'target_type':data})
            await safe_send("📝 **Заполните анкету:**\n\nВведите ваше имя:", reply_markup=cancel_btn())
        return
    if data == "admins":
        await start_admin_application(update, context)
        return
    if data == "settings":
        if not is_profile_complete(uid):
            await safe_send("❌ Сначала заполните анкету")
            return
        p = user_profiles[uid]
        t = "🆘 #поддержка" if p['type']=='support' else "💬 #общение" if p['type']=='communication' else "❓ не выбрано"
        await safe_send(f"📋 Анкета:\n👤 {p['name']}\n📅 {p['age']}\n{get_gender_emoji(p.get('gender'))}\n🏷️ {t}", reply_markup=settings_buttons())
        return
    if data in ("edit_name","edit_age"):
        context.user_data['edit'] = data.split('_')[1]
        await safe_send(f"✏️ Введите новое {'имя' if data=='edit_name' else 'возраст'}:", reply_markup=cancel_btn())
        return
    if data == "edit_type":
        kb = [[InlineKeyboardButton("🆘 #поддержка", callback_data="ch_type_support")],[InlineKeyboardButton("💬 #общение", callback_data="ch_type_comm")]]
        await safe_send("Выберите тип:", reply_markup=InlineKeyboardMarkup(kb))
        return
    if data in ("ch_type_support","ch_type_comm"):
        user_profiles[uid]['type'] = 'support' if data=="ch_type_support" else 'communication'
        save_db()
        await safe_send("✅ Тип изменен", reply_markup=settings_buttons())
        return
    if data == "cancel":
        had_msg = uid in user_has_message
        for k in ['awaiting','step','data','target','edit','awaiting_broadcast','target_type','send_to_user','edit_target','edit_field','edit_from_info','awaiting_user_search','reply_to_applicant','warn_target','admin_application']:
            context.user_data.pop(k, None)
        if uid in waiting_for_forward:
            waiting_for_forward.discard(uid)
            remove_active_dialog(uid)
            if had_msg:
                await context.bot.send_message(ADMIN_GROUP_ID, f"🚫 {get_user_name(uid)} завершил диалог")
        if uid in waiting_for_support:
            waiting_for_support.discard(uid)
            remove_active_dialog(uid)
            if had_msg:
                await context.bot.send_message(SUPPORT_GROUP_ID, f"🚫 {get_user_name(uid)} завершил диалог")
        user_has_message.discard(uid)
        await safe_send("❌ Отменено")
        await send_main_menu(update, context)
        return
    if data == "back":
        for k in ['awaiting','step','data','target','edit','awaiting_broadcast','target_type','send_to_user','edit_target','edit_field','edit_from_info','awaiting_user_search','reply_to_applicant','warn_target','admin_application']:
            context.user_data.pop(k, None)
        await send_main_menu(update, context, chat_id=q.message.chat.id, message_id=q.message.message_id)
        return

    await safe_send("❌ Неизвестная команда. Пожалуйста, обратитесь в техподдержку. Код: BUTTON_ERR01")

# ==================== ВЕБ-СЕРВЕР ДЛЯ RENDER (WEBHOOK) ====================
flask_app = Flask(__name__)

@flask_app.route('/')
@flask_app.route('/health')
def health():
    return "OK", 200

@flask_app.route(f'/webhook/{TOKEN}', methods=['POST'])
def webhook():
    """Принимает обновления от Telegram и передаёт их боту"""
    try:
        update = Update.de_json(request.get_json(force=True), app.bot)
        asyncio.run_coroutine_threadsafe(app.process_update(update), loop)
        return 'OK', 200
    except Exception as e:
        logger.error(f"Ошибка в webhook: {e}")
        return 'Internal Server Error', 500

async def setup_webhook():
    """Устанавливает вебхук для бота"""
    # Получаем URL приложения из переменной окружения Render
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not render_url:
        # fallback для локальной разработки
        render_url = "https://your-app.onrender.com"
    webhook_url = f"{render_url}/webhook/{TOKEN}"
    await app.bot.set_webhook(webhook_url, drop_pending_updates=True)
    logger.info(f"Webhook установлен на {webhook_url}")

# ==================== ЗАПУСК ====================
app = None
loop = None

def run():
    global app, loop
    init_db()
    load_db()
    load_active_dialogs()
    load_forwarded_messages()
    load_admin_replies()
    load_maintenance_mode()

    app = Application.builder().token(TOKEN).build()
    # Добавляем все обработчики
    app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("help", help_command, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("settings", settings, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("stop", stop, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("next", next_op, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("ban", ban, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("unban", unban, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("mute", mute, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("unmute", unmute, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("info", info_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("stats", stats, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("broadcast", broadcast, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("list_users", list_users, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("user_info", user_info, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("maintenance", maintenance_command, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("admin_panel", admin_panel, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("export_users", export_users, filters=filters.ChatType.PRIVATE))  # новая команда
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ALL & filters.ChatType.PRIVATE, forward_msg))
    app.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, reply_to))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.ChatType.GROUPS, edited_reply_to))

    # Инициализируем event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Устанавливаем вебхук (синхронно внутри asyncio)
    loop.run_until_complete(setup_webhook())

    # Запускаем Flask в отдельном потоке (основной сервер)
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Запуск Flask на порту {port}...")
    flask_app.run(host='0.0.0.0', port=port, debug=False)

def shutdown_handler(signum, frame):
    logger.info("Получен сигнал завершения, останавливаем бота...")
    if loop and app:
        # Удаляем вебхук при остановке (опционально)
        async def shutdown_webhook():
            await app.bot.delete_webhook()
        try:
            loop.run_until_complete(shutdown_webhook())
        except:
            pass
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    run()
