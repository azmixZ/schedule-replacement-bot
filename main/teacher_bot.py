import logging
import os
from datetime import time, timedelta

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot_utils import StorageError, broadcast, load_json, set_user_value, setup_logging
from schedule import DAY_NAMES, KZ_TZ, now_kz, resolve_date, teacher_schedule

MAIN_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(MAIN_DIR)

# Токен — в том же .env, что и у бота студентов: TEACHER_BOT_TOKEN=...
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
TEACHER_BOT_TOKEN = os.getenv("TEACHER_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # Telegram ID администратора

# Преподаватели хранятся отдельно от студентов (путь прежний: main/data/teachers.json).
TEACHER_DATA_FILE = os.path.join(MAIN_DIR, "data", "teachers.json")

CHOOSING_TEACHER = 1
ADMIN_BROADCAST = 2

BTN_TODAY = "Расписание на сегодня"
BTN_TOMORROW = "Расписание на завтра"
BTN_CHANGE = "Сменить фамилию"
BTN_MESSAGE_ALL = "📢 Написать всем преподам"
MENU_BUTTONS = {BTN_TODAY, BTN_TOMORROW, BTN_CHANGE, BTN_MESSAGE_ALL, *DAY_NAMES}

setup_logging()
log = logging.getLogger("teacher_bot")


def load_teachers() -> dict:
    return load_json(TEACHER_DATA_FILE)


def get_main_menu(user_id):
    keyboard = [
        [BTN_TODAY, BTN_TOMORROW],
        ["Понедельник", "Вторник", "Среда"],
        ["Четверг", "Пятница"],
        [BTN_CHANGE],
    ]
    if user_id == ADMIN_ID:
        keyboard.append([BTN_MESSAGE_ALL])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# --- АДМИН ---

async def admin_broadcast_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    await update.message.reply_text(
        "Введите сообщение для рассылки всем преподавателям (или /cancel):", reply_markup=ReplyKeyboardRemove())
    return ADMIN_BROADCAST


async def admin_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = load_teachers()
    text = f"📢 <b>Сообщение от администрации:</b>\n\n{update.message.text_html}"
    await update.message.reply_text(
        f"⏳ Запускаю рассылку для {len(users)} преподавателей...", reply_markup=get_main_menu(ADMIN_ID))

    async def run():
        sent, failed = await broadcast(context.bot, ((uid, text) for uid in users), "сообщение преподам")
        await update.message.reply_text(f"✅ Отправлено {sent}, не доставлено {failed}.")

    context.application.create_task(run())
    return ConversationHandler.END


async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=get_main_menu(update.effective_user.id))
    return ConversationHandler.END


# --- РАСПИСАНИЕ ---

async def handle_schedule_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    teacher_name = load_teachers().get(str(user_id))
    if not teacher_name:
        await update.message.reply_text("Используй /start для ввода фамилии.")
        return

    today = now_kz().date()
    if text == BTN_TODAY:
        d = today
    elif text == BTN_TOMORROW:
        d = today + timedelta(days=1)
    elif text in DAY_NAMES:
        d = resolve_date(text, today)
    else:
        await update.message.reply_text("Выберите кнопку в меню.", reply_markup=get_main_menu(user_id))
        return

    try:
        response, _ = teacher_schedule(teacher_name, d)
    except Exception:
        log.exception("Ошибка при сборке расписания для %s", teacher_name)
        response = "⚠️ Не получилось прочитать расписание, попробуйте позже."
    await update.message.reply_text(response, parse_mode="HTML", reply_markup=get_main_menu(user_id))


def teacher_broadcast_items(d, prefix):
    for user_id, teacher_name in load_teachers().items():
        text, has_pairs = teacher_schedule(teacher_name, d)
        yield user_id, (f"<b>{prefix}</b>\n\n{text}" if has_pairs else "")


async def morning_job(context: ContextTypes.DEFAULT_TYPE):
    d = now_kz().date()
    if d.weekday() <= 4:
        await broadcast(context.bot, teacher_broadcast_items(d, "☀️ Утренняя рассылка:"), "утренняя преподам")


# --- РЕГИСТРАЦИЯ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    teacher = load_teachers().get(str(user_id))
    if teacher:
        await update.message.reply_text(f"Вы авторизованы как: {teacher}.", reply_markup=get_main_menu(user_id))
        return ConversationHandler.END
    await update.message.reply_text("👋 Привет!\nВведите вашу фамилию для поиска пар:",
                                    reply_markup=ReplyKeyboardRemove())
    return CHOOSING_TEACHER


async def save_teacher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    teacher_name = update.message.text.strip()
    if teacher_name in MENU_BUTTONS or len(teacher_name) < 3:
        await update.message.reply_text("Введите фамилию (например: Батаева) или /cancel.")
        return CHOOSING_TEACHER
    try:
        set_user_value(TEACHER_DATA_FILE, user_id, teacher_name)
    except StorageError:
        await update.message.reply_text("⚠️ Не удалось сохранить, попробуйте позже.")
        return ConversationHandler.END
    await update.message.reply_text(f"✅ Сохранено: {teacher_name}", reply_markup=get_main_menu(user_id))
    return ConversationHandler.END


async def change_teacher_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите новую фамилию (или /cancel):", reply_markup=ReplyKeyboardRemove())
    return CHOOSING_TEACHER


def main():
    if not TEACHER_BOT_TOKEN:
        raise SystemExit(f"Нет TEACHER_BOT_TOKEN в {os.path.join(PROJECT_ROOT, '.env')}")
    application = Application.builder().token(TEACHER_BOT_TOKEN).build()
    application.job_queue.run_daily(morning_job, time(hour=7, minute=30, tzinfo=KZ_TZ))

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex(f"^{BTN_CHANGE}$"), change_teacher_request),
        ],
        states={CHOOSING_TEACHER: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_teacher)]},
        fallbacks=[CommandHandler("start", start), CommandHandler("cancel", cancel_action)],
    )
    admin_msg_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^{BTN_MESSAGE_ALL}$"), admin_broadcast_request)],
        states={ADMIN_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_send)]},
        fallbacks=[CommandHandler("cancel", cancel_action)],
    )

    application.add_handler(conv_handler)
    application.add_handler(admin_msg_handler)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_schedule_request))

    print("🤖 Бот для преподавателей запущен...")
    application.run_polling()


if __name__ == "__main__":
    main()
