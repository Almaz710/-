# -*- coding: utf-8 -*-
import logging
import sqlite3
import re
from datetime import datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters,
    ConversationHandler, CallbackContext
)

# -------- Настройки --------
TOKEN = "7581280110:AAHnqkCVJGjqBvHD1gU4dl8CsSA0eHOPsRg"  # <-- ВСТАВЬ СВОЙ ТОКЕН
DB_PATH = "finance.db"

# -------- Состояния --------
EXPENSE, INCOME = range(2)

# -------- Логирование --------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# -------- Клавиатуры --------
main_keyboard = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📩 Add income"), KeyboardButton("📤 Add expense")],
        [KeyboardButton("💰 Balance"), KeyboardButton("📅 Today")],
        [KeyboardButton("🧠 Analyze"), KeyboardButton("📊 Categories")],
        [KeyboardButton("❌ Cancel")],
    ],
    resize_keyboard=True
)

conv_cancel_keyboard = ReplyKeyboardMarkup(
    [[KeyboardButton("❌ Cancel")]],
    resize_keyboard=True, one_time_keyboard=True
)

# -------- База данных (инициализация + миграция) --------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # создаём актуальные таблицы (если их нет)
    c.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER NOT NULL,
            category TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS income (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER NOT NULL,
            category TEXT NOT NULL,
            date TEXT NOT NULL
        )
    """)

    # --- миграция старой схемы expenses (без user_id/timestamp) ---
    try:
        cols = {row[1] for row in c.execute("PRAGMA table_info(expenses)")}
        if "user_id" not in cols:
            c.execute("ALTER TABLE expenses ADD COLUMN user_id INTEGER")
        if "timestamp" not in cols:
            c.execute("ALTER TABLE expenses ADD COLUMN timestamp TEXT")
    except Exception as e:
        log.exception("Migration check failed: %s", e)

    conn.commit()
    conn.close()

# -------- Вспомогательное --------
def _normalize_spaces(s: str) -> str:
    # заменяем неразрывные/узкие пробелы на обычный
    return (s or "").replace("\u00A0", " ").replace("\u202F", " ").replace("\u2009", " ")

# -------- Хэндлеры --------
def start(update: Update, context: CallbackContext):
    update.message.reply_text("Добро пожаловать! Выберите действие:", reply_markup=main_keyboard)

# ---- Расходы ----
def add_expense(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Введите расход в формате: 500 пятерочка",
        reply_markup=conv_cancel_keyboard
    )
    return EXPENSE

def save_expense(update: Update, context: CallbackContext):
    text = _normalize_spaces(update.message.text).strip()
    # Разрешаем: "500 лента", "500.00 лента", "500,50 лента"
    m = re.match(r"^\s*(\d+)(?:[.,](\d{1,2}))?\s+(.+?)\s*$", text)
    if not m:
        update.message.reply_text("⚠️ Неверный формат. Пример: 500 пятерочка")
        return EXPENSE

    amount = int(m.group(1))  # копейки игнорируем; при желании можно хранить как целые копейки
    category = m.group(3).lower()

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO expenses (user_id, amount, category, timestamp) VALUES (?, ?, ?, ?)",
            (update.effective_user.id, amount, category, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.exception("DB error on save_expense: %s", e)
        update.message.reply_text("💥 Ошибка базы данных. Попробуйте ещё раз.")
        return EXPENSE

    update.message.reply_text(
        f"✅ Расход {amount} ₽ на «{category}» сохранён.",
        reply_markup=main_keyboard
    )
    return ConversationHandler.END

# ---- Доходы ----
def add_income(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Введите сумму и категорию дохода. Пример: 10000 работа",
        reply_markup=conv_cancel_keyboard
    )
    return INCOME

def save_income(update: Update, context: CallbackContext):
    text = _normalize_spaces(update.message.text).strip()
    parts = text.split(maxsplit=1)

    if len(parts) != 2 or not parts[0].isdigit():
        update.message.reply_text("❌ Неверный формат. Пример: 10000 работа")
        return INCOME

    amount = int(parts[0])
    category = parts[1].lower()
    date = datetime.now().strftime("%Y-%m-%d")

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO income (user_id, amount, category, date) VALUES (?, ?, ?, ?)",
            (update.effective_user.id, amount, category, date)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.exception("DB error on save_income: %s", e)
        update.message.reply_text("💥 Ошибка базы данных. Попробуйте ещё раз.")
        return INCOME

    update.message.reply_text(
        f"✅ Доход {amount} ₽ от «{category}» сохранён.",
        reply_markup=main_keyboard
    )
    return ConversationHandler.END

# ---- Отмена ----
def cancel(update: Update, context: CallbackContext):
    update.message.reply_text("🚫 Действие отменено.", reply_markup=main_keyboard)
    return ConversationHandler.END

# ---- Категории/статистика ----
def get_stats(period_days: int, user_id: int) -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    since = datetime.now() - timedelta(days=period_days)
    c.execute(
        "SELECT category, SUM(amount) FROM expenses WHERE user_id = ? AND timestamp > ? GROUP BY category",
        (user_id, since.isoformat())
    )
    rows = c.fetchall()
    conn.close()
    if not rows:
        return "Нет трат за выбранный период."
    return "\n".join([f"{(cat or '').capitalize()}: {amt} ₽" for cat, amt in rows])

def categories(update: Update, context: CallbackContext):
    kb = ReplyKeyboardMarkup(
        [
            [KeyboardButton("📅 Today"), KeyboardButton("🗓 Week")],
            [KeyboardButton("📆 Month"), KeyboardButton("⬅️ Back")],
            [KeyboardButton("❌ Cancel")],
        ],
        resize_keyboard=True
    )
    update.message.reply_text("Выберите период:", reply_markup=kb)

def today_categories(update: Update, context: CallbackContext):
    update.message.reply_text(f"📅 Сегодня:\n{get_stats(1, update.effective_user.id)}")

def week_categories(update: Update, context: CallbackContext):
    update.message.reply_text(f"🗓 За неделю:\n{get_stats(7, update.effective_user.id)}")

def month_categories(update: Update, context: CallbackContext):
    update.message.reply_text(f"📆 За месяц:\n{get_stats(30, update.effective_user.id)}")

# ---- Баланс ----
def balance(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE user_id = ?", (user_id,))
    total_expenses = c.fetchone()[0] or 0
    c.execute("SELECT COALESCE(SUM(amount),0) FROM income WHERE user_id = ?", (user_id,))
    total_income = c.fetchone()[0] or 0
    conn.close()

    net = total_income - total_expenses
    update.message.reply_text(f"💰 Доходы: {total_income} ₽\n💸 Расходы: {total_expenses} ₽\n🧾 Баланс: {net} ₽")

# ---- Анализ (заглушка) ----
def analyze(update: Update, context: CallbackContext):
    update.message.reply_text("🧠 Анализ пока в разработке.", reply_markup=main_keyboard)

# ---- Фолбэк ----
def handle_text(update: Update, context: CallbackContext):
    update.message.reply_text("❓ Команда не распознана. Выберите из кнопок ниже.", reply_markup=main_keyboard)

# -------- main --------
def main():
    init_db()

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # Конверсейшн: расходы
    expense_conv = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex(r"(?i)^📤 Add expense$"), add_expense)],
        states={EXPENSE: [MessageHandler(Filters.text & ~Filters.command, save_expense)]},
        fallbacks=[
            MessageHandler(Filters.regex(r"(?i)^❌ Cancel$"), cancel),
            CommandHandler("cancel", cancel)
        ],
        per_chat=True
    )

    # Конверсейшн: доходы
    income_conv = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex(r"(?i)^📩 Add income$"), add_income)],
        states={INCOME: [MessageHandler(Filters.text & ~Filters.command, save_income)]},
        fallbacks=[
            MessageHandler(Filters.regex(r"(?i)^❌ Cancel$"), cancel),
            CommandHandler("cancel", cancel)
        ],
        per_chat=True
    )

    # Команды/кнопки
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.regex(r"(?i)^📊 Categories$"), categories))
    dp.add_handler(MessageHandler(Filters.regex(r"(?i)^📅 Today$"), today_categories))
    dp.add_handler(MessageHandler(Filters.regex(r"(?i)^🗓 Week$"), week_categories))
    dp.add_handler(MessageHandler(Filters.regex(r"(?i)^📆 Month$"), month_categories))
    dp.add_handler(MessageHandler(Filters.regex(r"(?i)^💰 Balance$"), balance))
    dp.add_handler(MessageHandler(Filters.regex(r"(?i)^🧠 Analyze$"), analyze))

    # Cancel как глобальная кнопка/команда
    dp.add_handler(MessageHandler(Filters.regex(r"(?i)^❌ Cancel$"), cancel))
    dp.add_handler(CommandHandler("cancel", cancel))

    # Конверсейшны
    dp.add_handler(expense_conv)
    dp.add_handler(income_conv)

    # Фолбэк
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
