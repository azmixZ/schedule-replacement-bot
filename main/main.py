import logging
import os
import json
import pytz
import asyncio
from datetime import datetime, timedelta, time
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)

# импорт рабочего файла
try:
    from schedule import get_schedule, days_map, WORD_FILE
except ImportError:
    print("Ошибка: Не найден файл schedule.py рядом с ботом!")

# настройка
TOKEN = "YOUR_TOKEN"
MAIN_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(MAIN_DIR)
USER_DATA_FILE = os.path.join(PROJECT_ROOT, "data", "users.json")

# глобальная переменная
LAST_MOD_TIME = 0

# состояние
CHOOSING_GROUP = 1

# лог
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# json

def load_users():
    if os.path.exists(USER_DATA_FILE):
        try:
            with open(USER_DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_user_to_json(user_id, group):
    users = load_users()
    users[str(user_id)] = group
    with open(USER_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=4)

# меню

def get_main_menu():
    keyboard = [
        ["Расписание на сегодня", "Расписание на завтра"],
        ["Понедельник", "Вторник", "Среда"],
        ["Четверг", "Пятница"],
        ["Сменить группу"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# функция рассылки

async def send_broadcast_schedule(context: ContextTypes.DEFAULT_TYPE, is_tomorrow=False):

    users = load_users()
    delta = 1 if is_tomorrow else 0
    target_dt = datetime.now(pytz.timezone("Asia/Almaty")) + timedelta(days=delta)
    target_day_name = days_map.get(target_dt.strftime("%A"))

    for user_id, group in users.items():
        try:
            response = get_schedule(group, target_day=target_day_name)
            prefix = "🌙 Вечерний обзор на завтра:" if is_tomorrow else "☀️ Утренняя рассылка:"
            await context.bot.send_message(
                chat_id=user_id, 
                text=f"<b>{prefix}</b>\n\n{response}", 
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Ошибка рассылки юзеру {user_id}: {e}")

async def morning_job(context: ContextTypes.DEFAULT_TYPE):
    # пн=0, вт=1, ср=2, чт=3, пт=4, сб=5, вс=6
    day_of_week = datetime.now(pytz.timezone("Asia/Almaty")).weekday()
    
    # рассылка только в будние дни (пн-пт)
    if day_of_week <= 4:
        await send_broadcast_schedule(context, is_tomorrow=False)

async def evening_job(context: ContextTypes.DEFAULT_TYPE):
    day_of_week = datetime.now(pytz.timezone("Asia/Almaty")).weekday()
    
    # пятница (4) и суббота (5) — отдыхаем, рассылку на завтра (сб и вс) не шлем
    if day_of_week == 4 or day_of_week == 5:
        return
        
    # в остальных случаях (пн, вт, ср, чт и вс) шлем расписание на завтра
    await send_broadcast_schedule(context, is_tomorrow=True)

async def check_file_update_job(context: ContextTypes.DEFAULT_TYPE):
    global LAST_MOD_TIME
    if os.path.exists(WORD_FILE):
        current_time = os.path.getmtime(WORD_FILE)
        if LAST_MOD_TIME == 0: 
            LAST_MOD_TIME = current_time
            return
        
        if current_time > LAST_MOD_TIME:
            LAST_MOD_TIME = current_time
            users = load_users()
            for user_id in users.keys():
                try:
                    await context.bot.send_message(
                        chat_id=user_id, 
                        text="🔔 <b>Файл замен обновился!</b>\nПроверьте расписание кнопкой 'Сегодня' или 'Завтра'.",
                        parse_mode="HTML"
                    )
                    await asyncio.sleep(0.05) # маленькая пауза, чтобы телеграмм не ругался
                except Exception as e:
                    logging.error(f"Не удалось отправить уведомление {user_id}: {e}")

# команды

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    users = load_users()
    
    if str(user_id) in users:
        group = users[str(user_id)]
        context.user_data["group"] = group
        await update.message.reply_text(
            f"Твоя группа: {group}. Что хочешь узнать?",
            reply_markup=get_main_menu()
        )
        return ConversationHandler.END
    else:
        await update.message.reply_text(
            "Введите название вашей группы (например, ББ-9-99):",
            reply_markup=ReplyKeyboardRemove()
        )
        return CHOOSING_GROUP

async def change_group_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введите новое название группы:",
        reply_markup=ReplyKeyboardRemove()
    )
    return CHOOSING_GROUP

async def save_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    group_name = update.message.text.strip().upper()
    context.user_data["group"] = group_name
    save_user_to_json(user_id, group_name)
    
    await update.message.reply_text(
        f"✅ Группа {group_name} сохранена!",
        reply_markup=get_main_menu()
    )
    return ConversationHandler.END

async def handle_schedule_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    
    if "group" not in context.user_data:
        users = load_users()
        if str(user_id) in users:
            context.user_data["group"] = users[str(user_id)]
        else:
            await update.message.reply_text("Сначала напиши /start и укажи группу.")
            return

    group = context.user_data["group"]
    target_day = None

    # кнопки
    kz_tz = pytz.timezone("Asia/Almaty")
    if text == "Расписание на сегодня":
        target_day = days_map.get(datetime.now(kz_tz).strftime("%A"))
    elif text == "Расписание на завтра":
        tomorrow = datetime.now(kz_tz) + timedelta(days=1)
        target_day = days_map.get(tomorrow.strftime("%A"))
    elif text in days_map.values():
        target_day = text
    
    if target_day:
        try:
            response = get_schedule(group, target_day=target_day)
        except Exception as e:
            response = f"Ошибка: {e}"
        await update.message.reply_text(response, parse_mode="HTML", reply_markup=get_main_menu())

# запуск

def main():
    # создаем пподдержку JobQueue
    application = Application.builder().token(TOKEN).build()
    job_queue = application.job_queue
    kz_tz = pytz.timezone("Asia/Almaty")

    # расписание рассылки
    # утро 
    job_queue.run_daily(morning_job, time(hour=7, minute=30, tzinfo=kz_tz))
    # вечер 
    job_queue.run_daily(evening_job, time(hour=22, minute=59, tzinfo=kz_tz))
    # проверка замен
    job_queue.run_repeating(check_file_update_job, interval=60, first=10)

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^Сменить группу$"), change_group_request)
        ],
        states={
            CHOOSING_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_group)],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_schedule_request))

    print("Бот запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()
