import re
import json
import asyncio
import os
import threading
from datetime import datetime, timedelta
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters

# ==================== ВЕБ-СЕРВЕР ====================
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "Bot is running!"

@flask_app.route('/health')
def health():
    return "OK", 200

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host='0.0.0.0', port=port, debug=False)

threading.Thread(target=run_web_server, daemon=True).start()
print("Веб-сервер запущен")

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

# ==================== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ====================
waiting_for_forward = set()
waiting_for_support = set()
forwarded = {}
support_forwarded = {}
admin_replies = {}
support_admin_replies = {}
banned_users = set()
muted_users = set()
user_profiles = {}
profile_sent = set()
broadcast_data = {}
user_has_message = set()
ban_until = {}
mute_until = {}
group_warnings = {}
application_messages = {}

# ==================== БАЗА ДАННЫХ ====================
try:
    from replit import db
    print("Replit Database подключена")
except ImportError:
    db = {}
    print("Локальная база")

def load_db():
    global user_profiles, banned_users, muted_users, ban_until, mute_until
    try:
        data = db.get("users_data", {}) if hasattr(db, 'get') else db.get("users_data", {})
        user_profiles = {int(k): v for k, v in data.get('profiles', {}).items()}
        for uid_str, until_str in data.get('banned', {}).items():
            uid, until = int(uid_str), datetime.fromisoformat(until_str)
            if until > datetime.now():
                banned_users.add(uid)
                ban_until[uid] = until
        for uid_str, until_str in data.get('muted', {}).items():
            uid, until = int(uid_str), datetime.fromisoformat(until_str)
            if until > datetime.now():
                muted_users.add(uid)
                mute_until[uid] = until
        print(f"Загружено {len(user_profiles)} пользователей")
    except Exception as e:
        print(f"Ошибка загрузки: {e}")

def save_db():
    try:
        data = {
            'profiles': {str(k): v for k, v in user_profiles.items()},
            'banned': {str(uid): until.isoformat() for uid, until in ban_until.items() if until > datetime.now()},
            'muted': {str(uid): until.isoformat() for uid, until in mute_until.items() if until > datetime.now()}
        }
        db["users_data"] = data
    except Exception as e:
        print(f"Ошибка сохранения: {e}")

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

def get_user_name(user_id):
    p = user_profiles.get(user_id, {})
    return p.get('first_name') or f"ID:{user_id}"

def remove_blocked_user(user_id):
    if user_id in user_profiles:
        del user_profiles[user_id]
        save_db()
        waiting_for_forward.discard(user_id)
        waiting_for_support.discard(user_id)
        profile_sent.discard(user_id)
        user_has_message.discard(user_id)
        return True
    return False

def get_gender_emoji(gender):
    if gender == 'male':
        return "♂️ Мужской"
    elif gender == 'female':
        return "♀️ Женский"
    else:
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
    multipliers = {
        'д': 86400, 'd': 86400,
        'ч': 3600, 'h': 3600,
        'м': 60, 'm': 60,
        'с': 1, 's': 1
    }
    return value * multipliers.get(unit, 60)

def format_time_remaining(seconds):
    if seconds < 60:
        return f"{int(seconds)} сек."
    elif seconds < 3600:
        return f"{int(seconds // 60)} мин."
    elif seconds < 86400:
        return f"{int(seconds // 3600)} ч."
    else:
        return f"{int(seconds // 86400)} д."

def get_uid_from_reply(msg, fwd_dict):
    if not msg.reply_to_message:
        return None
    if msg.reply_to_message.message_id in fwd_dict:
        return fwd_dict[msg.reply_to_message.message_id][0]
    return None

def clear_user_data(uid):
    waiting_for_forward.discard(uid)
    waiting_for_support.discard(uid)
    profile_sent.discard(uid)
    user_has_message.discard(uid)

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

# ==================== КЛАВИАТУРЫ ====================
def main_menu():
    keyboard = [
        [InlineKeyboardButton("🖊 Написать админу", callback_data="admin"),
         InlineKeyboardButton("👨‍💻 Тех.поддержка", callback_data="support")],
        [InlineKeyboardButton("⚙ Настройки", callback_data="settings"),
         InlineKeyboardButton("📝 Отзывы", url=REVIEWS_LINK),
         InlineKeyboardButton("📘 Правила", url=PRAVILA)],
        [InlineKeyboardButton("👑 Попасть в администрацию", callback_data="admins")]
    ]
    return InlineKeyboardMarkup(keyboard)

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
        [InlineKeyboardButton("🏷️ Изменить тип", callback_data=f"edit_user_type_{target_id}")],
        [InlineKeyboardButton("📨 Отправить сообщение", callback_data=f"send_msg_to_{target_id}")]
    ]
    if target_id in banned_users:
        keyboard.append([InlineKeyboardButton("✅ Снять бан", callback_data=f"unban_user_{target_id}")])
    if target_id in muted_users:
        keyboard.append([InlineKeyboardButton("✅ Снять мут", callback_data=f"unmute_user_{target_id}")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="user_back_main")])
    return InlineKeyboardMarkup(keyboard)

def info_buttons(target_id, is_owner=False):
    keyboard = [
        [InlineKeyboardButton("👤 Показать данные", callback_data=f"full_info_{target_id}")]
    ]
    return InlineKeyboardMarkup(keyboard)

def profile_view_buttons(target_id, from_info=False):
    keyboard = [
        [InlineKeyboardButton("✏️ Изменить имя", callback_data=f"edit_name_{target_id}_{from_info}")],
        [InlineKeyboardButton("📅 Изменить возраст", callback_data=f"edit_age_{target_id}_{from_info}")],
        [InlineKeyboardButton("🏷️ Изменить тип", callback_data=f"edit_type_{target_id}_{from_info}")],
        [InlineKeyboardButton("🔙 Назад", callback_data=f"back_to_info_{from_info}_{target_id}")]
    ]
    return InlineKeyboardMarkup(keyboard)

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
        await message.reply_text("Вы уже заполняете анкету. Пожалуйста, завершите её.")
        return

    kb = [
        [InlineKeyboardButton("#общение", callback_data="app_type_communication")],
        [InlineKeyboardButton("#поддержка", callback_data="app_type_support")],
        [InlineKeyboardButton("#общение_поддержка", callback_data="app_type_both")]
    ]
    await message.reply_text(
        "Вы начали заполнение анкеты для вступления в администрацию.\n"
        "Сначала выберите ваш тип деятельности:",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    context.user_data['admin_application'] = {
        'type': None,
        'answers': [],
        'current_q': 0,
        'questions': []
    }

async def process_application_answer(update, context):
    if not context.user_data.get('admin_application'):
        return
    app_data = context.user_data['admin_application']
    if app_data['current_q'] >= len(app_data['questions']):
        await finish_application(update, context)
        return
    answer = update.message.text
    app_data['answers'].append(answer)
    app_data['current_q'] += 1
    if app_data['current_q'] < len(app_data['questions']):
        next_q = app_data['questions'][app_data['current_q']]
        await update.message.reply_text(next_q)
    else:
        await finish_application(update, context)

async def finish_application(update, context):
    app_data = context.user_data['admin_application']
    user = update.effective_user
    user_id = user.id
    first_name = user.first_name
    username = user.username or "нет"

    text = f"📝 **Новая анкета кандидата**\n\n"
    text += f"👤 Пользователь: {first_name} (@{username})\n"
    text += f"🆔 ID: `{user_id}`\n"
    text += f"🏷️ Тип деятельности: {app_data['type']}\n\n"
    text += "**Ответы:**\n"
    for i, q in enumerate(app_data['questions'], 1):
        answer = app_data['answers'][i-1] if i-1 < len(app_data['answers']) else "❌ нет ответа"
        if len(answer) > 300:
            answer = answer[:300] + "..."
        text += f"*{q}*\n{answer}\n\n"
    text += f"📅 Отправлено: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"

    try:
        sent = await context.bot.send_message(
            chat_id=ADMIN_APPLICATION_GROUP_ID,
            text=text,
            parse_mode="Markdown"
        )
        global application_messages
        application_messages[sent.message_id] = user_id
    except Exception as e:
        print(f"Ошибка отправки анкеты в группу: {e}")
        await update.message.reply_text("❌ Произошла ошибка при отправке анкеты. Пожалуйста, обратитесь в тех поддержку, и попробуйте позже.")
        del context.user_data['admin_application']
        return

    await update.message.reply_text(
        "✅ Анкета отправлена на проверку. Ожидайте ответа администратора.\n"
        "Обычно это занимает до 48 часов. Спасибо!"
    )
    del context.user_data['admin_application']

async def application_type_callback(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    app_data = context.user_data.get('admin_application')
    if not app_data:
        await query.edit_message_text("❌ Что-то пошло не так. Попробуйте /start и кнопку снова.")
        return

    if data == "app_type_communication":
        app_data['type'] = "общение"
        questions = COMMON_QUESTIONS[:12]
        questions.append(COMMUNICATION_QUESTION)
        app_data['questions'] = questions
    elif data == "app_type_support":
        app_data['type'] = "поддержка"
        app_data['questions'] = COMMON_QUESTIONS.copy()
    else:
        app_data['type'] = "общение и поддержка"
        app_data['questions'] = COMMON_QUESTIONS.copy()

    app_data['answers'] = []
    app_data['current_q'] = 0
    await query.edit_message_text(app_data['questions'][0])

# ==================== ПРОВЕРКА ГРУПП ====================
async def check_group_access(update, context):
    chat = update.effective_chat
    if chat.type in ["group", "supergroup"] and chat.id not in ALLOWED_GROUPS:
        warning_count = group_warnings.get(chat.id, 0) + 1
        group_warnings[chat.id] = warning_count
        await update.message.reply_text(
            f"⚠️ НЕСАНКЦИОНИРОВАННОЕ ИСПОЛЬЗОВАНИЕ!\n\n"
            f"❌ ID группы: `{chat.id}`\n"
            f"📊 Предупреждение #{warning_count}\n"
            f"🚪 Бот покинет группу через 5 секунд...",
            parse_mode="Markdown"
        )
        await asyncio.sleep(5)
        await context.bot.leave_chat(chat.id)
        return False
    return True

async def check_subscription(update, context):
    user_id = update.effective_user.id
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

# ==================== ФУНКЦИЯ ДЛЯ ОТПРАВКИ ГЛАВНОГО МЕНЮ ====================
async def send_main_menu(update, context, chat_id=None, message_id=None):
    text = "Привет! Тебя приветствует бот\n\n<<𐔤ᥒ𐔤պᥱⲏⲏ𐔖ᥱ ᥒρ𐔖ɯ᥈𐔖ᥱ>>\n\nГлавное меню\n\n"
    if chat_id is None:
        chat_id = update.effective_chat.id

    try:
        with open("welcome.png", "rb") as photo:
            if message_id:
                try:
                    await context.bot.edit_message_media(
                        chat_id=chat_id,
                        message_id=message_id,
                        media=InputMediaPhoto(media=photo, caption=text, parse_mode="Markdown"),
                        reply_markup=main_menu()
                    )
                except Exception:
                    await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=photo,
                        caption=text,
                        parse_mode="Markdown",
                        reply_markup=main_menu()
                    )
            else:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=text,
                    parse_mode="Markdown",
                    reply_markup=main_menu()
                )
    except FileNotFoundError:
        if message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=main_menu()
                )
            except Exception:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=main_menu()
                )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=main_menu()
            )

# ==================== КОМАНДЫ ДЛЯ ПОЛЬЗОВАТЕЛЕЙ ====================
async def start(update, context):
    user = update.effective_user
    update_user_info(user.id, user.first_name, user.username)
    if not await check_subscription(update, context):
        await update.message.reply_text(
            "❌ Для использования бота необходимо подписаться на наш канал!\n\n"
            "Подпишитесь и нажмите /start снова.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 Подписаться", url=CHANNEL_LINK)]])
        )
        return
    await send_main_menu(update, context)

async def help_command(update, context):
    await update.message.reply_text("Недоступно", parse_mode="Markdown")

async def settings(update, context):
    uid = update.message.chat_id
    if not await check_subscription(update, context):
        await update.message.reply_text(
            "❌ Для использования бота необходимо подписаться на наш канал!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 Подписаться", url=CHANNEL_LINK)]])
        )
        return
    if uid not in user_profiles or not user_profiles[uid].get('name'):
        await update.message.reply_text(
            "❌ Настройки недоступны, ваша анкета еще не создана.\n"
            "Заполните ее через 'Написать админу'"
        )
        return
    p = user_profiles[uid]
    t = "🆘 #поддержка" if p['type'] == 'support' else "💬 #общение" if p['type'] == 'communication' else "❓ не выбрано"
    gender_text = get_gender_emoji(p.get('gender'))
    await update.message.reply_text(
        f"📋 **Ваша анкета:**\n\n"
        f"👤 Имя: `{p['name']}`\n"
        f"📅 Возраст: `{p['age']}`\n"
        f"{gender_text}\n"
        f"🏷️ Тип: {t}",
        parse_mode="Markdown",
        reply_markup=settings_buttons()
    )

async def stop(update, context):
    uid = update.message.chat_id
    in_forward = uid in waiting_for_forward
    in_support = uid in waiting_for_support
    if not in_forward and not in_support:
        await update.message.reply_text("❌ Вы не находитесь в режиме общения")
        return
    name = get_user_name(uid)
    had_msg = uid in user_has_message
    if in_forward:
        waiting_for_forward.remove(uid)
        if had_msg:
            await context.bot.send_message(ADMIN_GROUP_ID, f"🚫 Пользователь {name} прекратил общение")
    if in_support:
        waiting_for_support.remove(uid)
        if had_msg:
            await context.bot.send_message(SUPPORT_GROUP_ID, f"🚫 Пользователь {name} прекратил общение")
    user_has_message.discard(uid)
    await update.message.reply_text("✅ Вы вышли из режима общения")
    await send_main_menu(update, context)

async def next_op(update, context):
    uid = update.message.chat_id
    if uid in waiting_for_forward:
        await context.bot.send_message(ADMIN_GROUP_ID, f"🔄 Пользователь {get_user_name(uid)} хочет сменить администратора")
        await update.message.reply_text(
            "🔄 Смена администратора\n\n"
            "Первый освободившийся администратор сразу вам ответит."
        )
    elif uid in waiting_for_support:
        await update.message.reply_text("❌ Команда /next недоступна в техподдержке")
    else:
        await update.message.reply_text("❌ Вы не в режиме общения")

# ==================== ГРУППОВЫЕ КОМАНДЫ ====================
async def ban(update, context):
    if not update.message.reply_to_message:
        return await update.message.reply_text("❌ Ответьте на сообщение пользователя")
    chat_id = update.effective_chat.id
    if chat_id == ADMIN_GROUP_ID:
        fwd_dict = forwarded
    elif chat_id == SUPPORT_GROUP_ID:
        fwd_dict = support_forwarded
    else:
        return await update.message.reply_text("❌ Эта группа не поддерживается")
    uid = get_uid_from_reply(update.message, fwd_dict)
    if not uid:
        return await update.message.reply_text("❌ Не удалось определить пользователя")
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
    seconds = parse_time(context.args[0])
    if not seconds:
        return await update.message.reply_text("❌ Примеры: 30м, 2ч, 1д")
    until = datetime.now() + timedelta(seconds=seconds)
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
        return await update.message.reply_text("❌ Ответьте на сообщение")
    chat_id = update.effective_chat.id
    if chat_id == ADMIN_GROUP_ID:
        fwd_dict = forwarded
    elif chat_id == SUPPORT_GROUP_ID:
        fwd_dict = support_forwarded
    else:
        return await update.message.reply_text("❌ Эта группа не поддерживается")
    uid = get_uid_from_reply(update.message, fwd_dict)
    if not uid:
        return await update.message.reply_text("❌ Не удалось определить пользователя")
    if uid not in banned_users:
        return await update.message.reply_text("❌ Пользователь не забанен")
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
        return await update.message.reply_text("❌ Ответьте на сообщение пользователя")
    chat_id = update.effective_chat.id
    if chat_id == ADMIN_GROUP_ID:
        fwd_dict = forwarded
    elif chat_id == SUPPORT_GROUP_ID:
        fwd_dict = support_forwarded
    else:
        return await update.message.reply_text("❌ Эта группа не поддерживается")
    uid = get_uid_from_reply(update.message, fwd_dict)
    if not uid:
        return await update.message.reply_text("❌ Не удалось определить пользователя")
    name = get_user_name(uid)
    clear_user_data(uid)
    if not context.args:
        return await update.message.reply_text("❌ Укажите время.\nПримеры: /mute 30м, /mute 2ч")
    seconds = parse_time(context.args[0])
    if not seconds:
        return await update.message.reply_text("❌ Примеры: 30м, 2ч, 1д")
    until = datetime.now() + timedelta(seconds=seconds)
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
        return await update.message.reply_text("❌ Ответьте на сообщение")
    chat_id = update.effective_chat.id
    if chat_id == ADMIN_GROUP_ID:
        fwd_dict = forwarded
    elif chat_id == SUPPORT_GROUP_ID:
        fwd_dict = support_forwarded
    else:
        return await update.message.reply_text("❌ Эта группа не поддерживается")
    uid = get_uid_from_reply(update.message, fwd_dict)
    if not uid:
        return await update.message.reply_text("❌ Не удалось определить пользователя")
    if uid not in muted_users:
        return await update.message.reply_text("❌ Пользователь не замучен")
    muted_users.discard(uid)
    mute_until.pop(uid, None)
    save_db()
    try:
        await context.bot.send_message(uid, "✅ Мут снят")
    except:
        pass
    await update.message.reply_text(f"✅ Мут снят с {get_user_name(uid)}")

async def info_command(update, context):
    if update.message.chat.type not in ["group", "supergroup"]:
        return await update.message.reply_text("❌ Команда /info доступна только в группах")
    if not update.message.reply_to_message:
        return await update.message.reply_text("❌ Ответьте на сообщение пользователя")
    chat_id = update.effective_chat.id
    if chat_id == ADMIN_GROUP_ID:
        fwd_dict = forwarded
    elif chat_id == SUPPORT_GROUP_ID:
        fwd_dict = support_forwarded
    else:
        return await update.message.reply_text("❌ Эта группа не поддерживается")
    uid = get_uid_from_reply(update.message, fwd_dict)
    if not uid:
        return await update.message.reply_text("❌ Не удалось определить пользователя")
    p = user_profiles.get(uid, {})
    is_owner = update.effective_user.id == OWNER_ID
    gender_text = get_gender_emoji(p.get('gender'))
    text = f"📋 **АНКЕТА ПОЛЬЗОВАТЕЛЯ:**\n\n"
    text += f"👤 {get_user_name(uid)}\n"
    text += f"✏️ Имя: {p.get('name', 'не указано')}\n"
    text += f"📅 Возраст: {p.get('age', 'не указан')}\n"
    text += f"{gender_text}\n"
    text += "🆘 #поддержка" if p.get('type') == 'support' else "💬 #общение" if p.get('type') == 'communication' else "❓ не выбрано"
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=info_buttons(uid, is_owner)
    )

# ==================== КОМАНДЫ ДЛЯ ВЛАДЕЛЬЦА ====================
async def stats(update, context):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ У вас нет прав на эту команду")
        return
    total = len(user_profiles)
    sup = sum(1 for p in user_profiles.values() if p.get('type') == 'support')
    com = sum(1 for p in user_profiles.values() if p.get('type') == 'communication')
    await update.message.reply_text(
        f"📊 **Статистика**\n\n"
        f"👥 Всего: `{total}`\n"
        f"🆘 Поддержка: `{sup}`\n"
        f"💬 Общение: `{com}`\n"
        f"❓ Не выбрали: `{total - sup - com}`",
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
        parse_mode="Markdown"
    )

async def save_broadcast_data(update, context, uid):
    msg = update.message
    if msg.text:
        broadcast_data[uid] = {'type': 'text', 'content': msg.text, 'parse_mode': msg.parse_mode if hasattr(msg, 'parse_mode') else None}
    elif msg.photo:
        broadcast_data[uid] = {'type': 'photo', 'content': msg.photo[-1].file_id, 'caption': msg.caption, 'parse_mode': msg.parse_mode if hasattr(msg, 'parse_mode') else None}
    elif msg.video:
        broadcast_data[uid] = {'type': 'video', 'content': msg.video.file_id, 'caption': msg.caption, 'parse_mode': msg.parse_mode if hasattr(msg, 'parse_mode') else None}
    elif msg.animation:
        broadcast_data[uid] = {'type': 'animation', 'content': msg.animation.file_id, 'caption': msg.caption, 'parse_mode': msg.parse_mode if hasattr(msg, 'parse_mode') else None}
    elif msg.voice:
        broadcast_data[uid] = {'type': 'voice', 'content': msg.voice.file_id, 'caption': msg.caption}
    elif msg.document:
        broadcast_data[uid] = {'type': 'document', 'content': msg.document.file_id, 'caption': msg.caption, 'parse_mode': msg.parse_mode if hasattr(msg, 'parse_mode') else None}
    else:
        await update.message.reply_text("❌ Этот тип не поддерживается")
        return
    await update.message.reply_text("📢 **ПРЕВЬЮ РАССЫЛКИ:**", parse_mode="Markdown")
    if broadcast_data[uid]['type'] == 'text':
        await update.message.reply_text(broadcast_data[uid]['content'], parse_mode=broadcast_data[uid].get('parse_mode', 'HTML'))
    elif broadcast_data[uid]['type'] == 'photo':
        await update.message.reply_photo(photo=broadcast_data[uid]['content'], caption=broadcast_data[uid].get('caption'), parse_mode=broadcast_data[uid].get('parse_mode'))
    elif broadcast_data[uid]['type'] == 'video':
        await update.message.reply_video(video=broadcast_data[uid]['content'], caption=broadcast_data[uid].get('caption'), parse_mode=broadcast_data[uid].get('parse_mode'))
    elif broadcast_data[uid]['type'] == 'animation':
        await update.message.reply_animation(animation=broadcast_data[uid]['content'], caption=broadcast_data[uid].get('caption'), parse_mode=broadcast_data[uid].get('parse_mode'))
    elif broadcast_data[uid]['type'] == 'voice':
        await update.message.reply_voice(voice=broadcast_data[uid]['content'], caption=broadcast_data[uid].get('caption'))
    elif broadcast_data[uid]['type'] == 'document':
        await update.message.reply_document(document=broadcast_data[uid]['content'], caption=broadcast_data[uid].get('caption'), parse_mode=broadcast_data[uid].get('parse_mode'))
    kb = [[InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_broad"), InlineKeyboardButton("❌ Отмена", callback_data="cancel_broad")]]
    await update.message.reply_text("Отправить рассылку?", reply_markup=InlineKeyboardMarkup(kb))

async def execute_broadcast(update, context, uid):
    data = broadcast_data.pop(uid, None)
    if not data:
        await update.callback_query.edit_message_text("❌ Нет данных")
        return
    await update.callback_query.edit_message_text(f"🚀 Рассылка {len(user_profiles)} пользователям...")
    sent, blocked = 0, 0
    for uid2 in list(user_profiles.keys()):
        try:
            if data['type'] == 'text':
                await context.bot.send_message(chat_id=uid2, text=data['content'], parse_mode=data.get('parse_mode', 'HTML'))
            elif data['type'] == 'photo':
                await context.bot.send_photo(chat_id=uid2, photo=data['content'], caption=data.get('caption'), parse_mode=data.get('parse_mode'))
            elif data['type'] == 'video':
                await context.bot.send_video(chat_id=uid2, video=data['content'], caption=data.get('caption'), parse_mode=data.get('parse_mode'))
            elif data['type'] == 'animation':
                await context.bot.send_animation(chat_id=uid2, animation=data['content'], caption=data.get('caption'), parse_mode=data.get('parse_mode'))
            elif data['type'] == 'voice':
                await context.bot.send_voice(chat_id=uid2, voice=data['content'], caption=data.get('caption'))
            elif data['type'] == 'document':
                await context.bot.send_document(chat_id=uid2, document=data['content'], caption=data.get('caption'), parse_mode=data.get('parse_mode'))
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            error_msg = str(e).lower()
            if "blocked" in error_msg or "deactivated" in error_msg or "chat not found" in error_msg:
                if uid2 in user_profiles:
                    del user_profiles[uid2]
                    blocked += 1
                    save_db()
                    print(f"Пользователь {uid2} удален из базы")
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
    await send_list_page(update.message.chat.id, None, page, context)

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
        target_id = int(query)
        if target_id in user_profiles:
            await show_user_full_info(update, context, target_id)
        else:
            await update.message.reply_text(f"❌ Пользователь с ID {target_id} не найден")
        return
    username = query.lstrip('@').lower()
    found = None
    for uid, data in user_profiles.items():
        if data.get('username', '').lower() == username:
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
        f"📝 First name: {p.get('first_name', 'не указан')}\n"
        f"🔖 Username: @{p.get('username', 'нет')}\n"
        f"📅 Зарегистрирован: {p.get('registered_at', 'неизвестно')}\n"
        f"👤 Имя в анкете: {p.get('name', 'не указано')}\n"
        f"📅 Возраст: {p.get('age', 'не указан')}\n"
        f"{get_gender_emoji(p.get('gender'))}\n"
        f"🏷️ Тип: {'🆘 #поддержка' if p.get('type') == 'support' else '💬 #общение' if p.get('type') == 'communication' else '❓ не выбрано'}"
    )
    if hasattr(update, 'message') and update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=user_management_buttons(target_id))
    elif hasattr(update, 'callback_query') and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=user_management_buttons(target_id))

# ==================== ФУНКЦИЯ ДЛЯ ОТПРАВКИ СТРАНИЦЫ СПИСКА ====================
async def send_list_page(chat_id, message_id, page, context):
    users_per_page = 2
    users_list = list(user_profiles.items())
    total_pages = (len(users_list) + users_per_page - 1) // users_per_page
    if page < 1 or page > total_pages:
        page = 1
    start = (page - 1) * users_per_page
    end = start + users_per_page
    text = f"📋 **Пользователи (стр. {page}/{total_pages})**\n\n"
    for uid, data in users_list[start:end]:
        name = data.get('name', '❌')
        age = data.get('age', '❌')
        gender = get_gender_emoji(data.get('gender'))
        p_type = "🆘" if data.get('type') == 'support' else "💬" if data.get('type') == 'communication' else "❓"
        username = data.get('username')
        if username:
            text += f"🆔 `{uid}` | @{username}\n"
        else:
            text += f"🆔 `{uid}`\n"
        text += f"👤 {name} | {age} | {gender} | {p_type}\n\n"
    keyboard = []
    if page > 1:
        keyboard.append(InlineKeyboardButton("◀️ Назад", callback_data=f"list_page_{page-1}"))
    if page < total_pages:
        keyboard.append(InlineKeyboardButton("Вперед ▶️", callback_data=f"list_page_{page+1}"))
    reply_markup = InlineKeyboardMarkup([keyboard]) if keyboard else None
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    except Exception:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )

# ==================== АНКЕТА ДЛЯ ПОЛЬЗОВАТЕЛЯ ====================
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
        await update.message.reply_text("📝 Введите возраст (от 1 до 50 лет):")
    elif step == 2:
        if not text.isdigit():
            await update.message.reply_text("❌ Пожалуйста, введите цифры!")
            return
        age = int(text)
        if age < 1 or age > 50:
            await update.message.reply_text("❌ Пожалуйста, укажите корректный возраст")
            return
        context.user_data['data']['age'] = text
        context.user_data['step'] = 3
        kb = [[InlineKeyboardButton("♂️ Мужской", callback_data="gender_male")], [InlineKeyboardButton("♀️ Женский", callback_data="gender_female")]]
        await update.message.reply_text("👤 Выберите ваш пол:", reply_markup=InlineKeyboardMarkup(kb))

async def profile_text(uid):
    p = user_profiles.get(uid, {})
    name = get_user_name(uid)
    gender_text = get_gender_emoji(p.get('gender'))
    status_text = ""
    if is_banned(uid):
        if uid in ban_until:
            remaining = (ban_until[uid] - datetime.now()).total_seconds()
            status_text = f"\n🚫 **ЗАБАНЕН** (осталось: {format_time_remaining(remaining)})\n"
        else:
            status_text = "\n🚫 **ЗАБАНЕН НАВСЕГДА**\n"
    elif is_muted(uid):
        if uid in mute_until:
            remaining = (mute_until[uid] - datetime.now()).total_seconds()
            status_text = f"\n🔇 **ЗАМУЧЕН** (осталось: {format_time_remaining(remaining)})\n"
        else:
            status_text = "\n🔇 **ЗАМУЧЕН**\n"
    return f"📋 **НОВОЕ СООБЩЕНИЕ:**\n\n👤 Имя пользователя (аккаунт): {name}\n✏️ Имя: {p.get('name', 'не указано')}\n📅 Возраст: {p.get('age', 'не указан')}\n{gender_text}{status_text}{'🆘 #поддержка' if p.get('type') == 'support' else '💬 #общение' if p.get('type') == 'communication' else '❓ не выбрано'}\n{'─' * 30}\n"

# ==================== ОТПРАВКА МЕДИА ====================
async def send_media_to_user(bot, user_id, message):
    try:
        if message.text:
            return await bot.send_message(chat_id=user_id, text=message.text, parse_mode=message.parse_mode if hasattr(message, 'parse_mode') else None)
        elif message.photo:
            return await bot.send_photo(chat_id=user_id, photo=message.photo[-1].file_id, caption=message.caption, parse_mode=message.parse_mode if hasattr(message, 'parse_mode') else None)
        elif message.video:
            return await bot.send_video(chat_id=user_id, video=message.video.file_id, caption=message.caption, parse_mode=message.parse_mode if hasattr(message, 'parse_mode') else None)
        elif message.animation:
            return await bot.send_animation(chat_id=user_id, animation=message.animation.file_id, caption=message.caption, parse_mode=message.parse_mode if hasattr(message, 'parse_mode') else None)
        elif message.voice:
            return await bot.send_voice(chat_id=user_id, voice=message.voice.file_id, caption=message.caption)
        elif message.document:
            return await bot.send_document(chat_id=user_id, document=message.document.file_id, caption=message.caption, parse_mode=message.parse_mode if hasattr(message, 'parse_mode') else None)
        elif message.sticker:
            return await bot.send_sticker(chat_id=user_id, sticker=message.sticker.file_id)
        elif message.video_note:
            return await bot.send_video_note(chat_id=user_id, video_note=message.video_note.file_id)
        else:
            return await bot.forward_message(chat_id=user_id, from_chat_id=message.chat_id, message_id=message.message_id)
    except Exception as e:
        raise e

# ==================== ОСНОВНЫЕ ОБРАБОТЧИКИ ====================
async def forward_msg(update, context):
    if not update.message or update.message.chat.type != "private":
        return
    uid = update.message.chat_id

    # Анкета для админов
    if context.user_data.get('admin_application') and context.user_data['admin_application'].get('questions'):
        await process_application_answer(update, context)
        return

    if not await check_subscription(update, context):
        await update.message.reply_text(
            "❌ **Для использования бота необходимо подписаться на наш канал!**\n\n"
            "Подпишитесь и нажмите /start снова.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 Подписаться", url=CHANNEL_LINK)]])
        )
        return

    # Рассылка
    if context.user_data.get('awaiting_broadcast'):
        context.user_data['awaiting_broadcast'] = False
        await save_broadcast_data(update, context, uid)
        return

    # Отправка сообщения пользователю от владельца
    if context.user_data.get('send_to_user'):
        target_id = context.user_data.pop('send_to_user')
        msg = update.message
        try:
            await context.bot.send_message(
                target_id,
                f"📨 *Сообщение от администрации:*\n\n{msg.text}",
                parse_mode="Markdown"
            )
            await update.message.reply_text("✅ Сообщение отправлено пользователю")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")
        return

    # Редактирование профиля (из кнопок)
    if context.user_data.get('edit_target') and context.user_data.get('edit_field'):
        target_id = context.user_data['edit_target']
        field = context.user_data['edit_field']
        from_info = context.user_data.get('edit_from_info', False)
        if field == 'name':
            user_profiles[target_id]['name'] = update.message.text
            await update.message.reply_text("✅ Имя изменено")
        elif field == 'age':
            if not update.message.text.isdigit():
                await update.message.reply_text("❌ Только цифры!")
                return
            age = int(update.message.text)
            if age < 1 or age > 50:
                await update.message.reply_text("❌ Пожалуйста, укажите корректный возраст")
                return
            user_profiles[target_id]['age'] = update.message.text
            await update.message.reply_text("✅ Возраст изменен")
        save_db()
        p = user_profiles.get(target_id, {})
        gender_text = get_gender_emoji(p.get('gender'))
        text = (
            f"👤 **Пользователь**\n\n"
            f"🆔 ID: `{target_id}`\n"
            f"👤 Имя: {p.get('name', 'не указано')}\n"
            f"📅 Возраст: {p.get('age', 'не указан')}\n"
            f"{gender_text}\n"
            f"🏷️ Тип: {'🆘 #поддержка' if p.get('type') == 'support' else '💬 #общение' if p.get('type') == 'communication' else '❓ не выбрано'}\n"
            f"📝 First name: {p.get('first_name', 'не указан')}\n"
            f"🔖 Username: @{p.get('username', 'нет')}\n"
            f"📅 Зарегистрирован: {p.get('registered_at', 'неизвестно')}"
        )
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=profile_view_buttons(target_id, from_info))
        del context.user_data['edit_target']
        del context.user_data['edit_field']
        if 'edit_from_info' in context.user_data:
            del context.user_data['edit_from_info']
        return

    # Редактирование собственного профиля (через настройки)
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

    # Заполнение обычной анкеты
    if context.user_data.get('awaiting'):
        await save_profile(update, context)
        return

    # Режим общения
    if uid in waiting_for_forward:
        if is_banned(uid) or is_muted(uid):
            if is_banned(uid):
                if uid in ban_until:
                    remaining = (ban_until[uid] - datetime.now()).total_seconds()
                    await update.message.reply_text(f"🚫 Вы забанены. Осталось: {format_time_remaining(remaining)}")
                else:
                    await update.message.reply_text("🚫 Вы забанены")
            else:
                if uid in mute_until:
                    remaining = (mute_until[uid] - datetime.now()).total_seconds()
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
        except Exception as e:
            error_msg = str(e).lower()
            if "blocked" in error_msg or "deactivated" in error_msg or "chat not found" in error_msg:
                if remove_blocked_user(uid):
                    await update.message.reply_text("❌ Вы заблокировали бота. Пользователь удален из базы.")
            else:
                await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")
    elif uid in waiting_for_support:
        user_has_message.add(uid)
        if uid not in profile_sent:
            await context.bot.send_message(SUPPORT_GROUP_ID, await profile_text(uid), parse_mode="Markdown")
            profile_sent.add(uid)
        try:
            s = await context.bot.forward_message(SUPPORT_GROUP_ID, uid, update.message.message_id)
            support_forwarded[s.message_id] = (uid, None)
        except Exception as e:
            error_msg = str(e).lower()
            if "blocked" in error_msg or "deactivated" in error_msg or "chat not found" in error_msg:
                if remove_blocked_user(uid):
                    await update.message.reply_text("❌ Вы заблокировали бота. Пользователь удален из базы.")
            else:
                await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")
    else:
        if is_banned(uid):
            if uid in ban_until:
                remaining = (ban_until[uid] - datetime.now()).total_seconds()
                await update.message.reply_text(f"🚫 Вы забанены. Осталось: {format_time_remaining(remaining)}")
            else:
                await update.message.reply_text("🚫 Вы забанены")
            return
        if is_muted(uid):
            if uid in mute_until:
                remaining = (mute_until[uid] - datetime.now()).total_seconds()
                await update.message.reply_text(f"🔇 Вы замучены. Осталось: {format_time_remaining(remaining)}")
            else:
                await update.message.reply_text("🔇 Вы замучены")
            return
        await update.message.reply_text("❌ Нажмите 'Написать админу' или 'Тех.поддержка'")

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

    # Ответ на анкету
    if cid == ADMIN_APPLICATION_GROUP_ID:
        user_id = application_messages.get(rid)
        if user_id:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"📨 *Ответ на вашу анкету:*\n\n{msg.text}",
                    parse_mode="Markdown"
                )
                await msg.reply_text("✅ Ответ отправлен пользователю.")
            except Exception as e:
                await msg.reply_text(f"❌ Ошибка при отправке пользователю: {str(e)[:100]}")
            return

    # Обычное общение
    if cid == ADMIN_GROUP_ID and rid in forwarded:
        fwd, rep = forwarded, admin_replies
    elif cid == SUPPORT_GROUP_ID and rid in support_forwarded:
        fwd, rep = support_forwarded, support_admin_replies
    else:
        return

    uid, _ = fwd[rid]
    if not msg.edit_date:
        try:
            sent = await send_media_to_user(context.bot, uid, msg)
            rep[msg.message_id] = (uid, sent.message_id)
        except Exception as e:
            error_msg = str(e).lower()
            if "blocked" in error_msg or "deactivated" in error_msg or "chat not found" in error_msg:
                await msg.reply_text(f"⚠️ {get_user_name(uid)} заблокировал бота. Пользователь удален из базы.")
                remove_blocked_user(uid)
            else:
                await msg.reply_text(f"❌ Ошибка: {str(e)[:100]}")
    elif msg.edit_date and msg.message_id in rep:
        uid, mid = rep[msg.message_id]
        try:
            if msg.text:
                await context.bot.edit_message_text(chat_id=uid, message_id=mid, text=msg.text, parse_mode=msg.parse_mode if hasattr(msg, 'parse_mode') else None)
            elif msg.caption:
                await context.bot.edit_message_caption(chat_id=uid, message_id=mid, caption=msg.caption, parse_mode=msg.parse_mode if hasattr(msg, 'parse_mode') else None)
        except Exception as e:
            error_msg = str(e).lower()
            if "blocked" in error_msg or "deactivated" in error_msg:
                remove_blocked_user(uid)
            elif "Message is not modified" not in str(e):
                print(f"Ошибка: {e}")

# ==================== ОБРАБОТЧИК КНОПОК ====================
async def button_handler(update, context):
    q = update.callback_query
    await q.answer()
    data, uid = q.data, q.from_user.id

    async def safe_send(text, reply_markup=None, parse_mode=None):
        try:
            await q.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        except:
            try:
                await q.message.delete()
            except:
                pass
            await q.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)

    # Выбор пола в анкете
    if data.startswith("gender_"):
        gender = 'male' if data == "gender_male" else 'female'
        context.user_data['data']['gender'] = gender
        context.user_data['step'] = 5
        kb = [[InlineKeyboardButton("🆘 #поддержка", callback_data="type_support")],
              [InlineKeyboardButton("💬 #общение", callback_data="type_comm")]]
        await safe_send("Выберите цель:", reply_markup=InlineKeyboardMarkup(kb))
        return
    # Выбор типа в анкете
    if data in ("type_support", "type_comm"):
        if context.user_data.get('step') == 5:
            p_type = 'support' if data == "type_support" else 'communication'
            update_profile(uid,
                          context.user_data['data']['name'],
                          context.user_data['data']['age'],
                          context.user_data['data']['gender'],
                          p_type)
            target = context.user_data.get('target', 'admin')
            (waiting_for_forward if target == "admin" else waiting_for_support).add(uid)
            for k in ['step', 'data', 'awaiting', 'target_type']:
                context.user_data.pop(k, None)
            await safe_send("✅ Анкета сохранена!\n📨 Напишите сообщение", reply_markup=cancel_btn())
        return
    # Кнопка "Показать данные" (только владелец)
    if data.startswith("full_info_"):
        if uid != OWNER_ID:
            await q.answer("❌ Просмотр полных данных доступен только владельцу", show_alert=True)
            return
        target_id = int(data.split("_")[2])
        p = user_profiles.get(target_id, {})
        text = (
            f"👤 **Полная информация о пользователе**\n\n"
            f"🆔 ID: `{target_id}`\n"
            f"📝 First name: {p.get('first_name', 'не указан')}\n"
            f"🔖 Username: @{p.get('username', 'нет')}\n"
            f"📅 Зарегистрирован: {p.get('registered_at', 'неизвестно')}\n"
            f"👤 Имя в анкете: {p.get('name', 'не указано')}\n"
            f"📅 Возраст: {p.get('age', 'не указан')}\n"
            f"{get_gender_emoji(p.get('gender'))}\n"
            f"🏷️ Тип: {'🆘 #поддержка' if p.get('type') == 'support' else '💬 #общение' if p.get('type') == 'communication' else '❓ не выбрано'}"
        )
        await safe_send(text, parse_mode="Markdown", reply_markup=profile_view_buttons(target_id, from_info=True))
        return
    # Редактирование из полного профиля
    if data.startswith("edit_name_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        parts = data.split("_")
        target_id = int(parts[2])
        from_info = parts[3] == 'True'
        context.user_data['edit_target'] = target_id
        context.user_data['edit_field'] = 'name'
        context.user_data['edit_from_info'] = from_info
        await safe_send("✏️ Введите новое имя:")
        return
    if data.startswith("edit_age_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        parts = data.split("_")
        target_id = int(parts[2])
        from_info = parts[3] == 'True'
        context.user_data['edit_target'] = target_id
        context.user_data['edit_field'] = 'age'
        context.user_data['edit_from_info'] = from_info
        await safe_send("📅 Введите новый возраст (от 1 до 50):")
        return
    if data.startswith("edit_type_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        parts = data.split("_")
        target_id = int(parts[2])
        from_info = parts[3] == 'True'
        kb = [[InlineKeyboardButton("🆘 #поддержка", callback_data=f"confirm_type_{target_id}_{from_info}_support"),
               InlineKeyboardButton("💬 #общение", callback_data=f"confirm_type_{target_id}_{from_info}_comm")]]
        await safe_send("Выберите новый тип:", reply_markup=InlineKeyboardMarkup(kb))
        return
    if data.startswith("confirm_type_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        parts = data.split("_")
        target_id = int(parts[1])
        from_info = parts[2] == 'True'
        new_type = 'support' if parts[3] == 'support' else 'communication'
        user_profiles[target_id]['type'] = new_type
        save_db()
        p = user_profiles.get(target_id, {})
        text = (
            f"👤 **Пользователь**\n\n"
            f"🆔 ID: `{target_id}`\n"
            f"👤 Имя: {p.get('name', 'не указано')}\n"
            f"📅 Возраст: {p.get('age', 'не указан')}\n"
            f"{get_gender_emoji(p.get('gender'))}\n"
            f"🏷️ Тип: {'🆘 #поддержка' if p.get('type') == 'support' else '💬 #общение' if p.get('type') == 'communication' else '❓ не выбрано'}\n"
            f"📝 First name: {p.get('first_name', 'не указан')}\n"
            f"🔖 Username: @{p.get('username', 'нет')}\n"
            f"📅 Зарегистрирован: {p.get('registered_at', 'неизвестно')}"
        )
        await safe_send(text, parse_mode="Markdown", reply_markup=profile_view_buttons(target_id, from_info))
        return
    if data.startswith("back_to_info_"):
        parts = data.split("_")
        from_info = parts[3] == 'True'
        target_id = int(parts[4])
        if from_info:
            p = user_profiles.get(target_id, {})
            text = f"📋 **АНКЕТА ПОЛЬЗОВАТЕЛЯ:**\n\n"
            text += f"👤 {get_user_name(target_id)}\n"
            text += f"✏️ Имя: {p.get('name', 'не указано')}\n"
            text += f"📅 Возраст: {p.get('age', 'не указан')}\n"
            text += f"{get_gender_emoji(p.get('gender'))}\n"
            text += "🆘 #поддержка" if p.get('type') == 'support' else "💬 #общение" if p.get('type') == 'communication' else "❓ не выбрано"
            await safe_send(text, parse_mode="Markdown", reply_markup=info_buttons(target_id, uid == OWNER_ID))
        else:
            await safe_send("Возврат к списку пользователей", reply_markup=back_button())
        return
    # Кнопки для /user_info
    if data.startswith("edit_user_name_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[3])
        context.user_data['edit_target'] = target_id
        context.user_data['edit_field'] = 'name'
        context.user_data['edit_from_info'] = False
        await safe_send("✏️ Введите новое имя:")
        return
    if data.startswith("edit_user_age_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[3])
        context.user_data['edit_target'] = target_id
        context.user_data['edit_field'] = 'age'
        context.user_data['edit_from_info'] = False
        await safe_send("📅 Введите новый возраст (от 1 до 50):")
        return
    if data.startswith("edit_user_type_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[3])
        kb = [[InlineKeyboardButton("🆘 #поддержка", callback_data=f"confirm_type_{target_id}_False_support"),
               InlineKeyboardButton("💬 #общение", callback_data=f"confirm_type_{target_id}_False_comm")]]
        await safe_send("Выберите новый тип:", reply_markup=InlineKeyboardMarkup(kb))
        return
    if data.startswith("send_msg_to_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[3])
        context.user_data['send_to_user'] = target_id
        await safe_send("📝 Введите сообщение для пользователя (оно будет отправлено с пометкой *от администрации*):", parse_mode="Markdown")
        return
    # Кнопки снятия бана/мута
    if data.startswith("unban_user_"):
        if uid != OWNER_ID:
            await q.answer("❌ У вас нет прав", show_alert=True)
            return
        target_id = int(data.split("_")[2])
        if target_id in banned_users:
            banned_users.discard(target_id)
            ban_until.pop(target_id, None)
            save_db()
            # Отправляем уведомление пользователю
            try:
                await context.bot.send_message(target_id, "👑 Владелец снял с вас бан.")
            except:
                pass
            await safe_send(f"✅ Пользователь {get_user_name(target_id)} разбанен.")
            # Обновляем кнопки
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
    # Кнопки пагинации
    if data.startswith("list_page_"):
        page = int(data.split("_")[2])
        await send_list_page(q.message.chat.id, q.message.message_id, page, context)
        return
    if data == "user_back_main":
        await send_main_menu(update, context, chat_id=q.message.chat.id, message_id=q.message.message_id)
        return
    # Выбор типа в анкете для админов
    if data.startswith("app_type_"):
        await application_type_callback(update, context)
        return
    # Остальные кнопки
    if data == "confirm_broad":
        await execute_broadcast(update, context, uid)
    elif data == "cancel_broad":
        broadcast_data.pop(uid, None)
        await safe_send("❌ Отменено")
        await send_main_menu(update, context)
    elif data in ("admin", "support"):
        if data == "admin":
            if is_banned(uid):
                if uid in ban_until:
                    remaining = (ban_until[uid] - datetime.now()).total_seconds()
                    await safe_send(f"🚫 **Вы забанены!**\n\nОсталось: {format_time_remaining(remaining)}", parse_mode="Markdown")
                else:
                    await safe_send("🚫 **Вы забанены!**", parse_mode="Markdown")
                return
            if is_muted(uid):
                if uid in mute_until:
                    remaining = (mute_until[uid] - datetime.now()).total_seconds()
                    await safe_send(f"🔇 **Вы замучены!**\n\nОсталось: {format_time_remaining(remaining)}", parse_mode="Markdown")
                else:
                    await safe_send("🔇 **Вы замучены!**", parse_mode="Markdown")
                return
        if not await check_subscription(update, context):
            await safe_send(
                "❌ **Для использования бота необходимо подписаться на наш канал!**",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📢 Подписаться", url=CHANNEL_LINK)]])
            )
            return
        context.user_data['target'] = data
        if uid in user_profiles and user_profiles[uid].get('name'):
            (waiting_for_forward if data == "admin" else waiting_for_support).add(uid)
            await safe_send("📨 Напишите сообщение", reply_markup=cancel_btn())
        else:
            context.user_data.update({'awaiting': True, 'step': 1, 'data': {}, 'target_type': data})
            await safe_send("📝 Заполните анкету:\n\nВведите имя:", reply_markup=cancel_btn())
    elif data == "admins":
        await start_admin_application(update, context)
    elif data == "settings":
        p = user_profiles.get(uid, {})
        if not p.get('name'):
            await safe_send("❌ Сначала заполните анкету")
            return
        t = "🆘 #поддержка" if p['type'] == 'support' else "💬 #общение" if p['type'] == 'communication' else "❓ не выбрано"
        gender_text = get_gender_emoji(p.get('gender'))
        await safe_send(f"📋 Анкета:\n👤 {p['name']}\n📅 {p['age']}\n{gender_text}\n🏷️ {t}", reply_markup=settings_buttons())
    elif data in ("edit_name", "edit_age"):
        context.user_data['edit'] = data.split('_')[1]
        await safe_send(f"✏️ Введите новое {'имя' if data == 'edit_name' else 'возраст'}:")
    elif data == "edit_type":
        kb = [[InlineKeyboardButton("🆘 #поддержка", callback_data="ch_type_support")], [InlineKeyboardButton("💬 #общение", callback_data="ch_type_comm")]]
        await safe_send("Выберите тип:", reply_markup=InlineKeyboardMarkup(kb))
    elif data in ("ch_type_support", "ch_type_comm"):
        user_profiles[uid]['type'] = 'support' if data == "ch_type_support" else 'communication'
        save_db()
        await safe_send("✅ Тип изменен", reply_markup=settings_buttons())
    elif data == "cancel":
        had_msg = uid in user_has_message
        for k in ['awaiting', 'step', 'data', 'target', 'edit', 'awaiting_broadcast', 'target_type']:
            context.user_data.pop(k, None)
        if uid in waiting_for_forward:
            waiting_for_forward.remove(uid)
            if had_msg:
                await context.bot.send_message(ADMIN_GROUP_ID, f"🚫 {get_user_name(uid)} отменил")
        if uid in waiting_for_support:
            waiting_for_support.remove(uid)
            if had_msg:
                await context.bot.send_message(SUPPORT_GROUP_ID, f"🚫 {get_user_name(uid)} отменил")
        user_has_message.discard(uid)
        await safe_send("❌ Отменено")
        await send_main_menu(update, context)
    elif data == "back":
        for k in ['awaiting', 'step', 'data', 'target', 'edit', 'awaiting_broadcast', 'target_type']:
            context.user_data.pop(k, None)
        await send_main_menu(update, context, chat_id=q.message.chat.id, message_id=q.message.message_id)
    elif data in ("mi", "admins"):
        text = "ℹ️ В разработке" if data == "mi" else "👑 Напишите пожалуйста в бот: @anker_reachedtheend_bot"
        await safe_send(text, reply_markup=back_button())
# ==================== ЗАПУСК ====================
def run():
    load_db()
    app = Application.builder().token(TOKEN).build()

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

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ALL & filters.ChatType.PRIVATE, forward_msg))
    app.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, reply_to))

    print("Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    run()