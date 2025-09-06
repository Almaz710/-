# -*- coding: utf-8 -*-
import os
import re
import logging
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters,
    ConversationHandler, CallbackContext
)

# -------- Настройки --------
TOKEN = os.environ["TOKEN"]                 # хранится в Railway Variables
DATABASE_URL = os.environ["DATABASE_URL"]   # строка подключения к Postgres на Railway

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
        [KeyboardButton("🗂 History")],  # <-- добавили кнопку истории
        [KeyboardButton("❌ Cancel")],
    ],
    resize_keyboard=True
)

conv_cancel_keyboard = ReplyKeyboardMarkup(
    [[KeyboardButton("❌ Cancel")]],
    resize_keyboard=True, one_time_keyboard=True
)

# -------- Подключение к БД --------
def pg_conn():
    # Railway требует SSL
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    with pg_conn() as conn, conn.cursor() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                amount INTEGER NOT NULL,
                category TEXT NOT NULL,
                timestamp TIMESTAMPTZ NOT NULL
            );
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS income (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                amount INTEGER NOT NULL,
                category TEXT NOT NULL,
                date DATE NOT NULL
            );
        """)
        conn.commit()

# -------- Утилиты --------
def _normalize_spaces(s: str) -> str:
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
    # Опциональная подстраховка от служебных нажатий
    text = _normalize_spaces(update.message.text).strip()
    if text in ("📤 Add expense", "📩 Add income", "❌ Cancel", "🗂 History"):
        return EXPENSE

    m = re.match(r"^\s*(\d+)(?:[.,](\d{1,2}))?\s+(.+?)\s*$", text)
    if not m:
        update.message.reply_text("⚠️ Неверный формат. Пример: 500 пятерочка")
        return EXPENSE

    amount = int(m.group(1))
    category = m.group(3).lower()
    now = datetime.now(timezone.utc)

    try:
        with pg_conn() as conn, conn.cursor() as c:
            c.execute(
                "INSERT INTO expenses (user_id, amount, category, timestamp) VALUES (%s, %s, %s, %s)",
                (update.effective_user.id, amount, category, now)
            )
            conn.commit()
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
    # Опциональная подстраховка от служебных нажатий
    text = _normalize_spaces(update.message.text).strip()
    if text in ("📤 Add expense", "📩 Add income", "❌ Cancel", "🗂 History"):
        return INCOME

    parts = text.split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        update.message.reply_text("❌ Неверный формат. Пример: 10000 работа")
        return INCOME

    amount = int(parts[0])
    category = parts[1].lower()
    date = datetime.now(timezone.utc).date()

    try:
        with pg_conn() as conn, conn.cursor() as c:
            c.execute(
                "INSERT INTO income (user_id, amount, category, date) VALUES (%s, %s, %s, %s)",
                (update.effective_user.id, amount, category, date)
            )
            conn.commit()
    except Exception as e:
        log.exception("DB error on save_income: %s", e)
        update.message.reply_text("💥 Ошибка базы данных. Попробуйте ещё раз.")
        return INCOME

    update.message.reply_text(
        f"✅ Доход {amount} ₽ от «{category}» сохранён.",
        reply_markup=main_keyboard
    )
    return ConversationHandler.END

# ---- История и удаление ----
def history(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    limit = 20  # сколько последних записей показывать

    with pg_conn() as conn, conn.cursor() as c:
        c.execute(
            """
            SELECT t, id, amount, category, ts
            FROM (
                SELECT 'E'::text AS t, id, amount, category, timestamp AS ts
                FROM expenses
                WHERE user_id = %s
                UNION ALL
                SELECT 'I'::text AS t, id, amount, category, date::timestamptz AS ts
                FROM income
                WHERE user_id = %s
            ) AS u
            ORDER BY ts DESC
            LIMIT %s
            """,
            (user_id, user_id, limit)
        )
        rows = c.fetchall()

    if not rows:
        update.message.reply_text("🗂 История пуста.")
        return

    lines = []
    for t, _id, amount, category, ts in rows:
        # показываем в локальном UTC — при желании можно сменить на локальный TZ
        dt_str = ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
        if t == 'E':
            lines.append(f"🧾 E{_id} · {dt_str} · -{amount} ₽ · {category}")
        else:
            lines.append(f"💵 I{_id} · {dt_str} · +{amount} ₽ · {category}")

    msg = "🗂 Последние записи (удаление: /delete <код>):\n" + "\n".join(lines)
    update.message.reply_text(msg)

def delete_entry(update: Update, context: CallbackContext):
    # ожидаем аргумент вида E123 или I45
    if not context.args:
        update.message.reply_text("Укажи код записи. Пример: /delete E123")
        return

    code = context.args[0].strip().upper()
    m = re.match(r"^([EI])(\d+)$", code)
    if not m:
        update.message.reply_text("Неверный формат. Пример: /delete E123 или /delete I45")
        return

    t, id_str = m.group(1), m.group(2)
    rec_id = int(id_str)
    user_id = update.effective_user.id

    with pg_conn() as conn, conn.cursor() as c:
        if t == 'E':
            c.execute(
                "DELETE FROM expenses WHERE id = %s AND user_id = %s RETURNING amount, category",
                (rec_id, user_id)
            )
        else:
            c.execute(
                "DELETE FROM income WHERE id = %s AND user_id = %s RETURNING amount, category",
                (rec_id, user_id)
            )
        row = c.fetchone()
        conn.commit()

    if row:
        amount, category = row
        sign = "-" if t == "E" else "+"
        emoji = "🧾" if t == "E" else "💵"
        update.message.reply_text(f"{emoji} Запись {code} удалена: {sign}{amount} ₽ · {category}")
    else:
        update.message.reply_text("❌ Запись не найдена или не принадлежит вам.")

# ---- Отмена ----
def cancel(update: Update, context: CallbackContext):
    update.message.reply_text("🚫 Действие отменено.", reply_markup=main_keyboard)
    return ConversationHandler.END

# ---- Статистика ----
def get_stats(period_days: int, user_id: int) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=period_days)
    with pg_conn() as conn, conn.cursor() as c:
        c.execute(
            """
            SELECT category, SUM(amount)::INT
            FROM expenses
            WHERE user_id = %s AND timestamp > %s
            GROUP BY category
            ORDER BY SUM(amount) DESC
            """,
            (user_id, since)
        )
        rows = c.fetchall()
    if not rows:
        return "Нет трат за выбранный период."
    return "\n".join([f"{(cat or '').capitalize()}: {amt} ₽" for (cat, amt) in rows])

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
    with pg_conn() as conn, conn.cursor() as c:
        c.execute("SELECT COALESCE(SUM(amount),0)::INT FROM expenses WHERE user_id = %s", (user_id,))
        total_expenses = c.fetchone()[0] or 0
        c.execute("SELECT COALESCE(SUM(amount),0)::INT FROM income   WHERE user_id = %s", (user_id,))
        total_income = c.fetchone()[0] or 0
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

    # --- фильтры состояний принимают только строки, начинающиеся с цифры + обработчик Cancel ---
    expense_conv = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex(r"(?i)^📤 Add expense$"), add_expense)],
        states={
            EXPENSE: [
                MessageHandler(Filters.regex(r"^\s*\d") & ~Filters.command, save_expense),
                MessageHandler(Filters.regex(r"(?i)^❌ Cancel$"), cancel),
            ],
        },
        fallbacks=[
            MessageHandler(Filters.regex(r"(?i)^❌ Cancel$"), cancel),
            CommandHandler("cancel", cancel),
        ],
        per_chat=True,
    )

    income_conv = ConversationHandler(
        entry_points=[MessageHandler(Filters.regex(r"(?i)^📩 Add income$"), add_income)],
        states={
            INCOME: [
                MessageHandler(Filters.regex(r"^\s*\d") & ~Filters.command, save_income),
                MessageHandler(Filters.regex(r"(?i)^❌ Cancel$"), cancel),
            ],
        },
        fallbacks=[
            MessageHandler(Filters.regex(r"(?i)^❌ Cancel$"), cancel),
            CommandHandler("cancel", cancel),
        ],
        per_chat=True,
    )

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.regex(r"(?i)^📊 Categories$"), categories))
    dp.add_handler(MessageHandler(Filters.regex(r"(?i)^📅 Today$"), today_categories))
    dp.add_handler(MessageHandler(Filters.regex(r"(?i)^🗓 Week$"), week_categories))
    dp.add_handler(MessageHandler(Filters.regex(r"(?i)^📆 Month$"), month_categories))
    dp.add_handler(MessageHandler(Filters.regex(r"(?i)^💰 Balance$"), balance))
    dp.add_handler(MessageHandler(Filters.regex(r"(?i)^🧠 Analyze$"), analyze))

    # История и удаление
    dp.add_handler(CommandHandler("history", history))
    dp.add_handler(MessageHandler(Filters.regex(r"(?i)^🗂 History$"), history))
    dp.add_handler(CommandHandler("delete", delete_entry))

    dp.add_handler(expense_conv)
    dp.add_handler(income_conv)

    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
