import html
import logging
import os
from datetime import time, timedelta

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import schedule
from bot_utils import (
    StorageError,
    broadcast,
    load_json,
    set_user_value,
    setup_logging,
)
from schedule import DAY_NAMES, KZ_TZ, WORD_FILE, EXCEL_FILE, group_schedule, now_kz, resolve_date

# --- НАСТРОЙКИ ---
MAIN_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(MAIN_DIR)

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
TOKEN = os.getenv("BOT_TOKEN")

USER_DATA_FILE = os.path.join(PROJECT_ROOT, "data", "users.json")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # Telegram ID администратора

CHOOSING_GROUP = 1
ADMIN_BROADCAST = 2

BTN_TODAY = "Расписание на сегодня"
BTN_TOMORROW = "Расписание на завтра"
BTN_CHANGE = "Сменить группу"
BTN_LESSONS_BROADCAST = "📢 Рассылка пар"
BTN_MESSAGE_ALL = "📢 Написать всем"
BTN_USERS = "👥 Список пользователей"
MENU_BUTTONS = {BTN_TODAY, BTN_TOMORROW, BTN_CHANGE, BTN_LESSONS_BROADCAST,
                BTN_MESSAGE_ALL, BTN_USERS, *DAY_NAMES}

setup_logging()
log = logging.getLogger("schedule_bot")
esc = html.escape


# --- ДАННЫЕ ---

def load_users() -> dict:
    return load_json(USER_DATA_FILE)


def is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == ADMIN_ID


def is_group_chat(update: Update) -> bool:
    return update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


# --- ИНТЕРФЕЙС ---

def get_main_menu(user_id):
    keyboard = [
        [BTN_TODAY, BTN_TOMORROW],
        ["Понедельник", "Вторник", "Среда"],
        ["Четверг", "Пятница"],
        [BTN_CHANGE],
    ]
    if user_id == ADMIN_ID:
        keyboard.append([BTN_LESSONS_BROADCAST, BTN_MESSAGE_ALL])
        keyboard.append([BTN_USERS])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# --- АДМИН-ФУНКЦИИ ---

async def get_users_list_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    users = load_users()
    if not users:
        await update.message.reply_text("📭 Список пользователей пуст.")
        return

    unknown = sum(1 for g in users.values() if not schedule.find_group(g))
    text = f"👥 <b>Всего пользователей: {len(users)}</b>"
    if unknown:
        text += f"\n⚠️ С группой, которой нет в расписании: {unknown}"
    text += "\n\n"
    for i, (uid, group) in enumerate(users.items()):
        if i >= 20:
            text += "\n<i>...остальные в файле ниже</i>"
            break
        text += f"• ID: <code>{esc(uid)}</code> — Группа: <b>{esc(str(group))}</b>\n"
    await update.message.reply_text(text, parse_mode="HTML")

    if os.path.exists(USER_DATA_FILE):
        with open(USER_DATA_FILE, "rb") as f:
            await update.message.reply_document(
                document=f, filename="users_backup.json", caption="📄 Полный бэкап базы пользователей"
            )


def schedule_broadcast_items(d, prefix):
    """Для рассылки: (chat_id, текст) только тем, у кого в этот день есть пары."""
    for user_id, group in load_users().items():
        text, has_pairs = group_schedule(group, d)
        yield user_id, (f"<b>{prefix}</b>\n\n{text}" if has_pairs else "")


async def manual_broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    d = now_kz().date() + timedelta(days=1)
    await update.message.reply_text(f"🚀 Рассылаю расписание на {DAY_NAMES[d.weekday()].lower()}, {d:%d.%m}...")

    async def run():
        sent, failed = await broadcast(
            context.bot, schedule_broadcast_items(d, "🌙 Вечерний обзор на завтра:"), "пары на завтра")
        await update.message.reply_text(f"✅ Рассылка завершена: отправлено {sent}, не доставлено {failed}.")

    context.application.create_task(run())


# --- ЗАГРУЗКА ФАЙЛОВ ЧЕРЕЗ TELEGRAM ---

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    doc = update.message.document
    name = (doc.file_name or "").lower()

    if name.endswith(".xlsx"):
        target, kind = EXCEL_FILE, "excel"
    elif name.endswith(".docx"):
        target, kind = WORD_FILE, "word"
    elif name.endswith(".doc"):
        await update.message.reply_text(
            "❌ Старый формат .doc бот прочитать не может. Пересохраните файл как .docx и пришлите ещё раз.")
        return
    elif name in ("users.json", "users_backup.json"):
        target, kind = USER_DATA_FILE, "users"
    else:
        await update.message.reply_text("❌ Формат не распознан. Кидай .xlsx, .docx или users.json")
        return

    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".upload" + os.path.splitext(target)[1]
    file = await context.bot.get_file(doc.file_id)
    await file.download_to_drive(tmp)

    # Сначала проверяем файл, и только потом подменяем рабочий:
    # битый файл не должен сломать бота.
    warnings = []
    try:
        if kind == "excel":
            tt = schedule._load_timetable(tmp)
            if not tt["groups"] or not tt["blocks"]:
                raise ValueError("не нашёл в таблице строку с группами или дни недели")
            report = f"групп: {len(tt['groups'])}, дней: {', '.join(d for d in tt['blocks'] if d != '*') or 'один день'}"
            if tt["practice"]:
                titles = sorted(tt["groups"][k] for k in tt["practice"])
                report += f"; на практике (скрыты или серые): {', '.join(titles)}"
        elif kind == "word":
            data = schedule._load_replacements(tmp)
            today = now_kz().date()
            doc_date = data["doc_date"]
            # Документ часто делают из прошлого файла и забывают поменять дату в первой строке,
            # а в названии файла дату пишут правильно («zameni 10.11.docx») — тогда верим названию.
            name_date = schedule.date_from_filename(doc.file_name, today)
            real_date = name_date or doc_date
            if real_date is None:
                raise ValueError("нет даты ни в первой строке документа («10 ноября 2026 г.»), "
                                 "ни в названии файла («zameni 10.11.docx»)")
            report = f"замены на {DAY_NAMES[real_date.weekday()].lower()}, {real_date:%d.%m.%Y}, строк: {len(data['items'])}"
            if name_date and doc_date and name_date != doc_date:
                warnings.append(f"В самом документе написано {doc_date:%d.%m.%Y}, а в названии файла — "
                                f"{name_date:%d.%m}. Беру дату из названия; первую строку документа "
                                f"лучше поправить в следующий раз.")
            if real_date < today:
                warnings.append("Эта дата уже прошла, студенты эти замены не увидят.")
            st = os.stat(tmp)
            schedule.save_date_override(target, name_date if name_date != doc_date else None, st.st_size, st.st_mtime)
        else:
            users = load_json(tmp, strict=True)
            report = f"пользователей: {len(users)}"
    except Exception as e:
        os.remove(tmp)
        await update.message.reply_text(f"❌ Файл не принят, рабочий файл не тронут.\nПричина: {esc(str(e))}",
                                        parse_mode="HTML")
        return

    os.replace(tmp, target)  # время изменения файла сохраняется, привязка даты остаётся верной
    await update.message.reply_text(
        f"✅ Файл загружен и применён ({report})." + "".join(f"\n⚠️ {w}" for w in warnings))


# --- СООБЩЕНИЕ ОТ АДМИНА ВСЕМ ---

async def admin_broadcast_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "Введите текст сообщения для рассылки всем пользователям (или /cancel для отмены):",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ADMIN_BROADCAST


async def admin_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # text_html сохраняет жирный/курсив из Telegram и экранирует «<», «&».
    text = f"📢 <b>Сообщение от администрации:</b>\n\n{update.message.text_html}"
    users = load_users()
    await update.message.reply_text(
        f"⏳ Начинаю рассылку для {len(users)} пользователей...", reply_markup=get_main_menu(ADMIN_ID))

    async def run():
        sent, failed = await broadcast(context.bot, ((uid, text) for uid in users), "сообщение админа")
        await update.message.reply_text(f"✅ Отправлено {sent}, не доставлено {failed}.")

    context.application.create_task(run())
    return ConversationHandler.END


async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Действие отменено.", reply_markup=get_main_menu(update.effective_user.id))
    return ConversationHandler.END


# --- РАСПИСАНИЕ ---

async def handle_schedule_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    in_group = is_group_chat(update)

    today = now_kz().date()
    if text == BTN_TODAY:
        d = today
    elif text == BTN_TOMORROW:
        d = today + timedelta(days=1)
    elif text in DAY_NAMES:
        d = resolve_date(text, today)
    else:
        if not in_group:
            await update.message.reply_text("Выберите кнопку в меню или нажмите /start.")
        return

    group = load_users().get(str(user_id))
    if not group:
        if not in_group:
            await update.message.reply_text("Сначала выберите группу: /start")
        return

    try:
        response, _ = group_schedule(group, d)
    except Exception:
        log.exception("Ошибка при сборке расписания для %s", group)
        response = "⚠️ Не получилось прочитать расписание. Администратор уже может посмотреть ошибку в логах."
    await update.message.reply_text(
        response,
        parse_mode="HTML",
        reply_to_message_id=update.message.message_id if in_group else None,
        # В группах клавиатуру не показываем: её увидели бы все участники.
        reply_markup=None if in_group else get_main_menu(user_id),
    )


# --- АВТОМАТИЧЕСКИЕ ЗАДАЧИ ---

async def morning_job(context: ContextTypes.DEFAULT_TYPE):
    d = now_kz().date()
    if d.weekday() <= 4:
        await broadcast(context.bot, schedule_broadcast_items(d, "☀️ Утренняя рассылка:"), "утренняя")


async def check_file_update_job(context: ContextTypes.DEFAULT_TYPE):
    """Раз в минуту: если файл замен поменялся — оповестить всех."""
    if not os.path.exists(WORD_FILE):
        return
    mtime = os.path.getmtime(WORD_FILE)
    last = context.bot_data.get("zameni_mtime")
    context.bot_data["zameni_mtime"] = mtime
    if last is None or mtime <= last:
        return
    d = schedule.replacements_date()
    when = f" на {DAY_NAMES[d.weekday()].lower()}, {d:%d.%m}" if d else ""
    text = f"🔔 <b>Вышли замены{when}!</b>\nПроверьте расписание."
    await broadcast(context.bot, ((uid, text) for uid in load_users()), "замены обновились")


# --- РЕГИСТРАЦИЯ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    group = load_users().get(str(user_id))
    if group and schedule.find_group(group):
        await update.message.reply_text(f"Группа: {group}.", reply_markup=get_main_menu(user_id))
        return ConversationHandler.END
    if group:
        await update.message.reply_text(f"Группы «{group}» нет в текущем расписании.")
    await update.message.reply_text("Введите название группы (напр. ПР-1-24):", reply_markup=ReplyKeyboardRemove())
    return CHOOSING_GROUP


async def save_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if text in MENU_BUTTONS:
        await update.message.reply_text("Это кнопка меню, а нужно название группы. Например: ПР-1-24")
        return CHOOSING_GROUP

    try:
        title = schedule.find_group(text)
        known = schedule.all_group_titles()
    except Exception:
        log.exception("Не читается Excel при регистрации")
        title, known = None, []

    if known and not title:
        await update.message.reply_text(
            f"Группы «{text}» нет в расписании. Проверьте написание и введите ещё раз "
            f"(например ПР-1-24, ФК-1-25 гос) или /cancel."
        )
        return CHOOSING_GROUP

    group = title or text.upper()
    try:
        set_user_value(USER_DATA_FILE, user_id, group)
    except StorageError:
        await update.message.reply_text("⚠️ Не удалось сохранить группу, попробуйте позже.")
        return ConversationHandler.END
    await update.message.reply_text(f"✅ Сохранено: {group}", reply_markup=get_main_menu(user_id))
    return ConversationHandler.END


async def change_group_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите новую группу (или /cancel):", reply_markup=ReplyKeyboardRemove())
    return CHOOSING_GROUP


# --- ЗАПУСК ---

def main():
    if not TOKEN:
        raise SystemExit(f"Нет BOT_TOKEN в {os.path.join(PROJECT_ROOT, '.env')}")
    application = Application.builder().token(TOKEN).build()

    application.job_queue.run_daily(morning_job, time(hour=7, minute=30, tzinfo=KZ_TZ))
    application.job_queue.run_repeating(check_file_update_job, interval=60, first=1)

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start, filters=filters.ChatType.PRIVATE),
            MessageHandler(filters.ChatType.PRIVATE & filters.Regex(f"^{BTN_CHANGE}$"), change_group_request),
        ],
        states={CHOOSING_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_group)]},
        fallbacks=[CommandHandler("start", start), CommandHandler("cancel", cancel_action)],
    )

    admin_msg_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(f"^{BTN_MESSAGE_ALL}$"), admin_broadcast_request)],
        states={ADMIN_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_send)]},
        fallbacks=[CommandHandler("cancel", cancel_action)],
    )

    # Порядок важен: диалоги раньше общего обработчика текста.
    application.add_handler(CommandHandler("get_users", get_users_list_handler))
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_USERS}$"), get_users_list_handler))
    application.add_handler(MessageHandler(filters.Regex(f"^{BTN_LESSONS_BROADCAST}$"), manual_broadcast_handler))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(conv_handler)
    application.add_handler(admin_msg_handler)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_schedule_request))

    print("Бот запущен...")
    application.run_polling()


if __name__ == "__main__":
    main()
