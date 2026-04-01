"""
Telegram-бот «База звонков» — быстрая фиксация входящих звонков
с напоминаниями и выгрузкой в Excel.

Запуск: python bot.py
"""

import logging
import os
import re
from datetime import datetime, timedelta

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from config import BOT_TOKEN, ADMIN_USER_IDS, PROXY_URL
import database as db

# ── Логирование ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Состояния ConversationHandler ────────────────────────────
PHONE, NAME, CALL_DATE, REMIND_DATE, NOTES = range(5)

# Состояния для редактирования
EDIT_ID, EDIT_SELECT, EDIT_VALUE = range(10, 13)

# Состояния для поиска и удаления
SEARCH_QUERY = 20
DELETE_ID = 21

# ── Вспомогательные функции ──────────────────────────────────

def is_admin(update: Update) -> bool:
    """Проверяет что сообщение от администратора."""
    if not ADMIN_USER_IDS:
        return True  # если ID не задан — доступ всем (для первой настройки)
    return update.effective_user.id in ADMIN_USER_IDS


def parse_date(text: str) -> str | None:
    """
    Парсит дату из текста. Поддерживает форматы:
    - 2026-03-09
    - 09.03.2026
    - 09.03.26
    - 09/03/2026
    Для времени напоминания также поддерживает:
    - 2026-03-09 14:00
    - 09.03.2026 14:00
    """
    text = text.strip()

    # Формат YYYY-MM-DD HH:MM
    for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%d/%m/%Y %H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass

    # Формат только даты
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d/%m/%y"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None


def normalize_phone(raw: str) -> str:
    """
    Нормализует номер телефона в формат 89991234567.
    Если начинается с @ — возвращает как есть (тег Telegram).

    Поддерживаемые форматы ввода:
      +7 (999) 123-12-12  → 89991231212
      +7-999-123-12-12    → 89991231212
      +79991231212        → 89991231212
      8 (999) 123-12-12   → 89991231212
      8-999-123-12-12     → 89991231212
      89991231212         → 89991231212
      7 999 123 12 12     → 89991231212
      79991231212         → 89991231212
      9991231212          → 89991231212
      999-123-12-12       → 89991231212
      @username           → @username
    """
    raw = raw.strip()

    # Telegram-тег — не трогаем
    if raw.startswith("@"):
        return raw

    # Убираем все кроме цифр
    digits = re.sub(r"\D", "", raw)

    if not digits:
        return raw

    # +7... или 7... (11 цифр, начинается с 7) → заменить 7 на 8
    if len(digits) == 11 and digits[0] == "7":
        digits = "8" + digits[1:]

    # 10 цифр без кода страны (9xx...) → добавить 8
    if len(digits) == 10 and digits[0] == "9":
        digits = "8" + digits

    return digits


def format_call(call: dict) -> str:
    """Форматирует запись звонка для отображения в Telegram."""
    remind = call.get("remind_date") or "—"
    notes = call.get("notes") or "—"
    reminded_mark = " ✅" if call.get("reminded") else ""
    return (
        f"📞 <b>#{call['id']}</b>\n"
        f"  Телефон: <code>{call['phone']}</code>\n"
        f"  ФИО: {call['name']}\n"
        f"  Дата звонка: {call['call_date']}\n"
        f"  Напомнить: {remind}{reminded_mark}\n"
        f"  Примечание: {notes}"
    )


# ── Команды ──────────────────────────────────────────────────

def get_main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📝 Новая запись", "📋 Список"],
            ["📊 Выгрузить в Excel", "⚡ Быстрая запись"],
            ["🔍 Поиск", "✏️ Редактировать", "🗑 Удалить"],
            ["📋 Пропущенные"]
        ],
        resize_keyboard=True
    )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик /start — приветствие и инструкция."""
    if not is_admin(update):
        return

    await update.message.reply_text(
        "👋 <b>База звонков</b>\n\n"
        "Выбери действие в меню или используй команды:\n"
        "🗑 /delete <code>ID</code> — удалить запись\n\n"
        "<b>Быстрая запись:</b>\n"
        "<code>/quick 79991234567 | Иванов Иван | 09.03.2026 | 12.03.2026 14:00 | Перезвонить по КП</code>\n\n"
        "Поля разделяй символом <code>|</code>",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


# ── Пошаговая запись (ConversationHandler) ───────────────────

async def new_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало пошаговой записи звонка."""
    if not is_admin(update):
        return ConversationHandler.END

    keyboard = [["❌ Отмена"]]
    await update.message.reply_text(
        "📞 <b>Новый звонок</b>\n\nВведи номер телефона:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    return PHONE


async def new_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение номера телефона."""
    phone = normalize_phone(update.message.text)
    if not phone.startswith("@") and len(phone) < 5:
        await update.message.reply_text("❌ Слишком короткий номер. Попробуй ещё раз:")
        return PHONE

    context.user_data["phone"] = phone
    keyboard = [["❌ Отмена"]]
    await update.message.reply_text(
        "👤 Введи ФИО:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )
    return NAME


async def new_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение ФИО."""
    context.user_data["name"] = update.message.text.strip()

    today = datetime.now().strftime("%d.%m.%Y")
    keyboard = [["📅 Сегодня", "❌ Отмена"]]
    await update.message.reply_text(
        f"📅 Дата звонка?\n(Например: <code>{today}</code> или <code>2026-03-09</code>)\n\n"
        f"Можешь нажать «📅 Сегодня»",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )
    return CALL_DATE


async def new_call_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение даты звонка."""
    text = update.message.text.strip()

    if text.lower() in ("/today", "сегодня", "📅 сегодня"):
        date_str = datetime.now().strftime("%Y-%m-%d")
    else:
        date_str = parse_date(text)
        if not date_str:
            await update.message.reply_text("❌ Не могу распознать дату. Попробуй формат ДД.ММ.ГГГГ:")
            return CALL_DATE

    context.user_data["call_date"] = date_str

    keyboard = [
        ["⏰ Через 2ч", "⏰ Через 4ч", "⏰ Через сутки"],
        ["⏭ Пропустить", "❌ Отмена"],
    ]
    await update.message.reply_text(
        "🔔 Когда напомнить?\n"
        "Выбери кнопку или введи дату вручную:\n"
        "(Например: <code>12.03.2026 14:00</code>)\n\n"
        "Или нажми «⏭ Пропустить»",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    return REMIND_DATE


async def new_remind_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение даты напоминания."""
    text = update.message.text.strip()

    if text.lower() in ("/skip", "нет", "-", "пропустить", "⏭ пропустить"):
        context.user_data["remind_date"] = None
    elif text in ("⏰ Через 2ч", "⏰ через 2ч"):
        context.user_data["remind_date"] = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    elif text in ("⏰ Через 4ч", "⏰ через 4ч"):
        context.user_data["remind_date"] = (datetime.now() + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M")
    elif text in ("⏰ Через сутки", "⏰ через сутки"):
        context.user_data["remind_date"] = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    else:
        date_str = parse_date(text)
        if not date_str:
            await update.message.reply_text("❌ Не могу распознать дату. Попробуй формат ДД.ММ.ГГГГ ЧЧ:ММ:")
            return REMIND_DATE
        context.user_data["remind_date"] = date_str

    keyboard = [["⏭ Пропустить", "❌ Отмена"]]
    await update.message.reply_text(
        "📝 Примечания?\n\nИли нажми «⏭ Пропустить»",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    return NOTES


async def new_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение примечаний и сохранение записи."""
    text = update.message.text.strip()

    notes = None if text.lower() in ("/skip", "-", "пропустить", "⏭ пропустить") else text

    call_id = db.add_call(
        phone=context.user_data["phone"],
        name=context.user_data["name"],
        call_date=context.user_data["call_date"],
        remind_date=context.user_data.get("remind_date"),
        notes=notes,
    )

    await update.message.reply_text(
        f"✅ <b>Звонок #{call_id} записан!</b>\n\n"
        f"📞 {context.user_data['phone']}\n"
        f"👤 {context.user_data['name']}\n"
        f"📅 {context.user_data['call_date']}\n"
        f"🔔 {context.user_data.get('remind_date') or '—'}\n"
        f"📝 {notes or '—'}",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )
    context.user_data.clear()
    return ConversationHandler.END


async def new_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена пошаговой записи."""
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Запись отменена.",
        reply_markup=get_main_keyboard(),
    )
    return ConversationHandler.END


async def parse_forwarded_application(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Парсинг пересланной заявки."""
    if not is_admin(update):
        return ConversationHandler.END

    text = update.message.text or update.message.caption or ""

    phone_match = re.search(r"• Телефон:\s*([^\n]+)", text)
    fio_match = re.search(r"• ФИО:\s*([^\n]+)", text)
    user_match = re.search(r"👤 Пользователь:\s*([^\n]+)", text)
    date_match = re.search(r"📅 Дата подачи:\s*([\d\.]+)", text)

    phone = phone_match.group(1).strip() if phone_match else "Неизвестно"
    phone = normalize_phone(phone)

    name = fio_match.group(1).strip() if fio_match else ""
    if not name or name.startswith("+") or name.isdigit() or name == "—" or name == "Нет":
        name = user_match.group(1).strip() if user_match else "Неизвестно"
        name = re.sub(r"\(.*?\)", "", name).strip()

    if date_match:
        call_date = parse_date(date_match.group(1)) or datetime.now().strftime("%Y-%m-%d")
    else:
        call_date = datetime.now().strftime("%Y-%m-%d")

    context.user_data["phone"] = phone
    context.user_data["name"] = name
    context.user_data["call_date"] = call_date

    keyboard = [
        ["⏰ Через 2ч", "⏰ Через 4ч", "⏰ Через сутки"],
        ["⏭ Пропустить", "❌ Отмена"],
    ]
    await update.message.reply_text(
        f"✅ <b>Заявка распознана!</b>\n"
        f"📞 {phone}\n"
        f"👤 {name}\n"
        f"📅 {call_date}\n\n"
        "🔔 Когда напомнить?\n"
        "Выбери кнопку или введи дату вручную:\n"
        "(Например: <code>12.03.2026 14:00</code>)\n\n"
        "Или нажми «⏭ Пропустить»",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    return REMIND_DATE


# ── Быстрая запись ───────────────────────────────────────────

async def cmd_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Быстрая запись одной строкой.
    Формат: /quick телефон | ФИО | дата звонка | дата напоминания | примечания
    Дата напоминания и примечания — необязательны.
    """
    if not is_admin(update):
        return

    text = update.message.text.replace("/quick", "", 1).strip()
    if not text or text == "⚡ Быстрая запись":
        await update.message.reply_text(
            "⚡ <b>Быстрая запись</b>\n\n"
            "Формат:\n"
            "<code>/quick телефон | ФИО | дата звонка | напомнить | примечания</code>\n\n"
            "Пример:\n"
            "<code>/quick 79991234567 | Иванов Иван | 09.03.2026 | 12.03.2026 14:00 | Перезвонить</code>\n\n"
            "Напоминание и примечания можно пропустить:\n"
            "<code>/quick 79991234567 | Иванов Иван | 09.03.2026</code>",
            parse_mode="HTML",
        )
        return

    parts = [p.strip() for p in text.split("|")]

    if len(parts) < 3:
        await update.message.reply_text(
            "❌ Минимум 3 поля: телефон | ФИО | дата звонка\n"
            "Разделяй поля символом <code>|</code>",
            parse_mode="HTML",
        )
        return

    phone = normalize_phone(parts[0])
    name = parts[1]
    call_date = parse_date(parts[2])

    if not call_date:
        await update.message.reply_text("❌ Не могу распознать дату звонка. Используй формат ДД.ММ.ГГГГ")
        return

    remind_date = None
    if len(parts) >= 4 and parts[3] not in ("-", ""):
        remind_date = parse_date(parts[3])
        if not remind_date:
            await update.message.reply_text("❌ Не могу распознать дату напоминания. Используй формат ДД.ММ.ГГГГ ЧЧ:ММ")
            return

    notes = parts[4] if len(parts) >= 5 else None

    call_id = db.add_call(phone, name, call_date, remind_date, notes)

    await update.message.reply_text(
        f"✅ <b>Звонок #{call_id} записан!</b>\n\n"
        f"📞 {phone}\n"
        f"👤 {name}\n"
        f"📅 {call_date}\n"
        f"🔔 {remind_date or '—'}\n"
        f"📝 {notes or '—'}",
        parse_mode="HTML",
    )


# ── Список записей ───────────────────────────────────────────

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать последние 10 записей."""
    if not is_admin(update):
        return

    calls = db.get_recent_calls(10)
    if not calls:
        await update.message.reply_text("📋 Записей пока нет.")
        return

    text = "📋 <b>Последние записи:</b>\n\n"
    text += "\n\n".join(format_call(c) for c in calls)
    await update.message.reply_text(text, parse_mode="HTML")


# ── Поиск записей ────────────────────────────────────────────

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Поиск по ФИО или телефону."""
    if not is_admin(update):
        return ConversationHandler.END

    text = update.message.text
    query = text.replace("/search", "", 1).replace("🔍 Поиск", "").strip()

    if not query:
        keyboard = [["❌ Отмена"]]
        await update.message.reply_text(
            "🔍 <b>Поиск</b>\n\nВведи ФИО или номер телефона:",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        )
        return SEARCH_QUERY

    await _do_search(update, query)
    return ConversationHandler.END


async def search_query_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователь ввёл запрос для поиска."""
    query = update.message.text.strip()
    await _do_search(update, query)
    return ConversationHandler.END


async def search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Поиск отменён.", reply_markup=get_main_keyboard())
    return ConversationHandler.END


async def _do_search(update: Update, query: str):
    """Выполняет поиск и отправляет результаты."""
    results = db.search_calls(query)

    if not results:
        await update.message.reply_text(
            f"🔍 По запросу <code>{query}</code> ничего не найдено.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )
        return

    header = f"🔍 Найдено по <code>{query}</code>: <b>{len(results)}</b>\n\n"
    text_out = header + "\n\n".join(format_call(c) for c in results)

    if len(text_out) > 4000:
        text_out = header + "\n\n".join(format_call(c) for c in results[:10])
        text_out += f"\n\n<i>Показаны первые 10 из {len(results)}</i>"

    await update.message.reply_text(text_out, parse_mode="HTML", reply_markup=get_main_keyboard())


# ── Удаление записи ──────────────────────────────────────────

async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удалить запись по ID."""
    if not is_admin(update):
        return ConversationHandler.END

    text = update.message.text.replace("/delete", "", 1).replace("🗑 Удалить", "").strip()

    if not text:
        keyboard = [["❌ Отмена"]]
        await update.message.reply_text(
            "🗑 <b>Удаление</b>\n\nВведи ID записи для удаления:",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        )
        return DELETE_ID

    return await _do_delete(update, text)


async def delete_receive_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователь ввёл ID для удаления."""
    return await _do_delete(update, update.message.text.strip())


async def delete_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Удаление отменено.", reply_markup=get_main_keyboard())
    return ConversationHandler.END


async def _do_delete(update: Update, text: str) -> int:
    """Выполняет удаление записи."""
    try:
        call_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом.", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    if db.delete_call(call_id):
        await update.message.reply_text(f"🗑 Запись #{call_id} удалена.", reply_markup=get_main_keyboard())
    else:
        await update.message.reply_text(f"❌ Запись #{call_id} не найдена.", reply_markup=get_main_keyboard())
    return ConversationHandler.END


# ── Редактирование записи ─────────────────────────────────────

EDIT_FIELD_MAP = {
    "📞 Телефон": "phone",
    "👤 ФИО": "name",
    "📅 Дата звонка": "call_date",
    "🔔 Напоминание": "remind_date",
    "📝 Примечания": "notes",
}


def get_edit_keyboard():
    """Клавиатура выбора поля для редактирования."""
    return ReplyKeyboardMarkup(
        [
            ["📞 Телефон", "👤 ФИО"],
            ["📅 Дата звонка", "🔔 Напоминание"],
            ["📝 Примечания"],
            ["✅ Готово", "❌ Отмена"],
        ],
        resize_keyboard=True,
    )


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Начало редактирования: /edit ID или кнопка «✏️ Редактировать».
    """
    if not is_admin(update):
        return ConversationHandler.END

    text = update.message.text.replace("/edit", "", 1).replace("✏️ Редактировать", "").strip()

    if not text:
        keyboard = [["❌ Отмена"]]
        await update.message.reply_text(
            "✏️ <b>Редактирование</b>\n\nВведи ID записи:",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        )
        return EDIT_ID

    try:
        call_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом.")
        return ConversationHandler.END

    call = db.get_call_by_id(call_id)
    if not call:
        await update.message.reply_text(f"❌ Запись #{call_id} не найдена.")
        return ConversationHandler.END

    context.user_data["edit_id"] = call_id

    await update.message.reply_text(
        f"✏️ <b>Редактирование #{call_id}</b>\n\n"
        + format_call(call)
        + "\n\nВыбери поле для изменения:",
        parse_mode="HTML",
        reply_markup=get_edit_keyboard(),
    )
    return EDIT_SELECT


async def edit_receive_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователь ввёл ID для редактирования."""
    text = update.message.text.strip()
    try:
        call_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом. Попробуй ещё:")
        return EDIT_ID

    call = db.get_call_by_id(call_id)
    if not call:
        await update.message.reply_text(f"❌ Запись #{call_id} не найдена. Попробуй другой ID:")
        return EDIT_ID

    context.user_data["edit_id"] = call_id

    await update.message.reply_text(
        f"✏️ <b>Редактирование #{call_id}</b>\n\n"
        + format_call(call)
        + "\n\nВыбери поле для изменения:",
        parse_mode="HTML",
        reply_markup=get_edit_keyboard(),
    )
    return EDIT_SELECT

async def edit_select_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователь выбрал поле для редактирования."""
    text = update.message.text.strip()

    if text in ("✅ Готово", "✅ готово"):
        return await edit_done(update, context)

    field = EDIT_FIELD_MAP.get(text)
    if not field:
        await update.message.reply_text(
            "❌ Выбери поле из клавиатуры ниже:",
            reply_markup=get_edit_keyboard(),
        )
        return EDIT_SELECT

    context.user_data["edit_field"] = field
    context.user_data["edit_field_label"] = text

    call = db.get_call_by_id(context.user_data["edit_id"])
    current_value = call.get(field) or "—"

    hints = {
        "phone": "Введи новый номер телефона:",
        "name": "Введи новое ФИО:",
        "call_date": "Введи новую дату звонка (ДД.ММ.ГГГГ):",
        "remind_date": "Введи новую дату напоминания (ДД.ММ.ГГГГ ЧЧ:ММ)\nили <code>-</code> чтобы убрать:",
        "notes": "Введи новое примечание\nили <code>-</code> чтобы убрать:",
    }

    keyboard = [["❌ Отмена"]]
    await update.message.reply_text(
        f"{text}: <code>{current_value}</code>\n\n{hints[field]}",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    return EDIT_VALUE


async def edit_set_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение нового значения поля."""
    text = update.message.text.strip()
    field = context.user_data["edit_field"]
    call_id = context.user_data["edit_id"]

    # Валидация и подготовка значения
    if field == "phone":
        new_value = normalize_phone(text)
        if not new_value.startswith("@") and len(new_value) < 5:
            await update.message.reply_text("❌ Слишком короткий номер. Попробуй ещё раз:")
            return EDIT_VALUE

    elif field in ("call_date", "remind_date"):
        if text in ("-", "нет", "убрать") and field == "remind_date":
            new_value = None
        else:
            new_value = parse_date(text)
            if not new_value:
                await update.message.reply_text("❌ Не могу распознать дату. Попробуй формат ДД.ММ.ГГГГ:")
                return EDIT_VALUE

    elif field == "notes":
        new_value = None if text in ("-", "нет", "убрать") else text

    else:  # name
        new_value = text

    # Обновляем в БД
    # При обнулении remind_date также сбрасываем флаг reminded
    update_fields = {field: new_value}
    if field == "remind_date" and new_value is not None:
        update_fields["reminded"] = 0

    db.update_call(call_id, **update_fields)

    call = db.get_call_by_id(call_id)
    label = context.user_data["edit_field_label"]

    await update.message.reply_text(
        f"✅ Поле {label} обновлено!\n\n"
        + format_call(call)
        + "\n\nВыбери ещё поле или нажми «✅ Готово»:",
        parse_mode="HTML",
        reply_markup=get_edit_keyboard(),
    )
    return EDIT_SELECT


async def edit_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершение редактирования."""
    call_id = context.user_data.get("edit_id")
    context.user_data.clear()

    msg = f"✅ Редактирование #{call_id} завершено." if call_id else "✅ Готово."
    await update.message.reply_text(msg, reply_markup=get_main_keyboard())
    return ConversationHandler.END


async def edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена редактирования."""
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Редактирование отменено.",
        reply_markup=get_main_keyboard(),
    )
    return ConversationHandler.END


# ── Выгрузка в Excel ─────────────────────────────────────────

async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выгрузить все записи в Excel и отправить файлом."""
    if not is_admin(update):
        return

    calls = db.get_all_calls()
    if not calls:
        await update.message.reply_text("📋 Записей пока нет — нечего выгружать.")
        return

    # Создаём Excel
    wb = Workbook()
    ws = wb.active
    ws.title = "Звонки"

    # Заголовки
    headers = ["ID", "Телефон", "ФИО", "Дата звонка", "Напомнить", "Примечания", "Создано"]
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
        cell.border = thin_border

    # Данные
    for row_idx, call in enumerate(calls, 2):
        values = [
            call["id"],
            call["phone"],
            call["name"],
            call["call_date"],
            call.get("remind_date") or "",
            call.get("notes") or "",
            call.get("created_at") or "",
        ]
        for col_idx, value in enumerate(values, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.border = thin_border
            if col_idx == 2:  # Телефон — текст
                cell.number_format = "@"

    # Автоширина колонок
    for col in ws.columns:
        max_length = 0
        col_letter = col[0].column_letter
        for cell in col:
            if cell.value:
                max_length = max(max_length, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max_length + 4, 40)

    # Сохраняем и отправляем
    filename = f"звонки_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    filepath = os.path.join(os.path.dirname(__file__) or ".", filename)
    wb.save(filepath)

    await update.message.reply_document(
        document=open(filepath, "rb"),
        filename=filename,
        caption=f"📊 Выгружено записей: {len(calls)}",
    )

    # Удаляем временный файл
    try:
        os.remove(filepath)
    except OSError:
        pass


# ── Напоминания ───────────────────────────────────────────────

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    """
    Периодическая проверка напоминаний.
    Вызывается каждые 60 секунд через JobQueue.
    """
    pending = db.get_pending_reminders()
    for call in pending:
        text = (
            "🔔 <b>НАПОМИНАНИЕ!</b>\n\n"
            + format_call(call)
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⏰ +1ч", callback_data=f"remind:{call['id']}:1h"),
                InlineKeyboardButton("⏰ +3ч", callback_data=f"remind:{call['id']}:3h"),
                InlineKeyboardButton("⏰ +1д", callback_data=f"remind:{call['id']}:1d"),
                InlineKeyboardButton("⏰ +3д", callback_data=f"remind:{call['id']}:3d"),
            ],
            [
                InlineKeyboardButton("📝 Примечание", callback_data=f"remind:{call['id']}:note"),
                InlineKeyboardButton("❌ Слился", callback_data=f"remind:{call['id']}:lost"),
            ],
            [
                InlineKeyboardButton("✅ ОК", callback_data=f"remind:{call['id']}:ok"),
            ],
        ])
        try:
            await context.bot.send_message(
                chat_id=ADMIN_USER_IDS[0],
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            db.mark_reminded(call["id"])
            logger.info(f"Напоминание отправлено: звонок #{call['id']}")
        except Exception as e:
            logger.error(f"Ошибка отправки напоминания #{call['id']}: {e}")


async def handle_remind_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка инлайн-кнопок напоминания."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != "remind":
        return

    call_id = int(parts[1])
    action = parts[2]

    call = db.get_call_by_id(call_id)
    if not call:
        await query.edit_message_text(f"❌ Запись #{call_id} не найдена.")
        return

    if action == "ok":
        db.mark_acknowledged(call_id)
        await query.edit_message_text(
            "✅ Принято.\n\n" + format_call(call),
            parse_mode="HTML",
        )
        return

    if action == "lost":
        db.update_call(call_id, notes="Не интересно")
        db.mark_acknowledged(call_id)
        call = db.get_call_by_id(call_id)
        await query.edit_message_text(
            "❌ <b>Слился</b>\n\n" + format_call(call),
            parse_mode="HTML",
        )
        return

    if action == "note":
        context.user_data["pending_note_edit"] = call_id
        await query.edit_message_text(
            f"📝 <b>Редактирование примечания #{call_id}</b>\n\n"
            + format_call(call)
            + "\n\nВведи новое примечание:",
            parse_mode="HTML",
        )
        return

    # Перенос напоминания
    now = datetime.now()
    delays = {
        "1h": timedelta(hours=1),
        "3h": timedelta(hours=3),
        "1d": timedelta(days=1),
        "3d": timedelta(days=3),
    }
    delta = delays.get(action)
    if not delta:
        return

    new_dt = now + delta
    new_remind = new_dt.strftime("%Y-%m-%d %H:%M")

    db.update_call(call_id, remind_date=new_remind, reminded=0)

    labels = {"1h": "1 час", "3h": "3 часа", "1d": "1 день", "3d": "3 дня"}
    call = db.get_call_by_id(call_id)

    await query.edit_message_text(
        f"⏰ Напоминание перенесено на <b>+{labels[action]}</b>\n"
        f"📅 {new_remind}\n\n"
        + format_call(call),
        parse_mode="HTML",
    )


async def quick_note_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода примечания из напоминания."""
    call_id = context.user_data.pop("pending_note_edit", None)
    if not call_id:
        return  # не для нас, пропускаем

    text = update.message.text.strip()
    new_notes = None if text in ("-", "нет", "убрать") else text

    db.update_call(call_id, notes=new_notes)
    call = db.get_call_by_id(call_id)

    # Показываем результат с кнопкой для перехода к полному редактированию
    await update.message.reply_text(
        f"✅ Примечание #{call_id} обновлено!\n\n"
        + format_call(call)
        + f"\n\nДля редактирования других полей: /edit {call_id}",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )


async def cmd_missed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать пропущенные/необработанные напоминания."""
    if not is_admin(update):
        return

    calls = db.get_unacknowledged_reminders()
    if not calls:
        await update.message.reply_text("✅ Нет пропущенных напоминаний!")
        return

    header = f"📋 <b>Пропущенные напоминания: {len(calls)}</b>\n\n"
    text = header + "\n\n".join(format_call(c) for c in calls)

    if len(text) > 4000:
        text = header + "\n\n".join(format_call(c) for c in calls[:10])
        text += f"\n\n<i>Показаны первые 10 из {len(calls)}</i>"

    await update.message.reply_text(text, parse_mode="HTML")


# ── Запуск бота ───────────────────────────────────────────────

def main():
    """Инициализация и запуск бота."""
    # Инициализация базы данных
    db.init_db()
    logger.info("База данных инициализирована")

    # Создание приложения
    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
    )

    # SOCKS5 прокси
    if PROXY_URL:
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)
        logger.info(f"Прокси: {PROXY_URL}")

    app = builder.build()

    # ConversationHandler для пошаговой записи
    text_filter = filters.TEXT & ~filters.COMMAND & ~filters.Regex("^❌ Отмена$")
    
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("new", new_start),
            MessageHandler(filters.Regex("^📝 Новая запись$"), new_start),
            MessageHandler(filters.Regex("НОВАЯ ЗАЯВКА"), parse_forwarded_application),
        ],
        states={
            PHONE: [MessageHandler(text_filter, new_phone)],
            NAME: [MessageHandler(text_filter, new_name)],
            CALL_DATE: [
                CommandHandler("today", new_call_date),
                MessageHandler(text_filter, new_call_date),
            ],
            REMIND_DATE: [
                CommandHandler("skip", new_remind_date),
                MessageHandler(text_filter, new_remind_date),
            ],
            NOTES: [
                CommandHandler("skip", new_notes),
                MessageHandler(text_filter, new_notes),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", new_cancel),
            MessageHandler(filters.Regex("^❌ Отмена$"), new_cancel),
        ],
    )

    # Регистрация обработчиков
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", cmd_start))
    
    # ConversationHandler для редактирования
    edit_text_filter = filters.TEXT & ~filters.COMMAND & ~filters.Regex("^❌ Отмена$")

    edit_handler = ConversationHandler(
        entry_points=[
            CommandHandler("edit", cmd_edit),
            MessageHandler(filters.Regex("^✏️ Редактировать$"), cmd_edit),
        ],
        states={
            EDIT_ID: [MessageHandler(edit_text_filter, edit_receive_id)],
            EDIT_SELECT: [MessageHandler(edit_text_filter, edit_select_field)],
            EDIT_VALUE: [MessageHandler(edit_text_filter, edit_set_value)],
        },
        fallbacks=[
            CommandHandler("cancel", edit_cancel),
            MessageHandler(filters.Regex("^❌ Отмена$"), edit_cancel),
        ],
    )
    app.add_handler(edit_handler)

    # ConversationHandler для поиска
    search_text_filter = filters.TEXT & ~filters.COMMAND & ~filters.Regex("^❌ Отмена$")

    search_handler = ConversationHandler(
        entry_points=[
            CommandHandler("search", cmd_search),
            MessageHandler(filters.Regex("^🔍 Поиск$"), cmd_search),
        ],
        states={
            SEARCH_QUERY: [MessageHandler(search_text_filter, search_query_received)],
        },
        fallbacks=[
            CommandHandler("cancel", search_cancel),
            MessageHandler(filters.Regex("^❌ Отмена$"), search_cancel),
        ],
    )
    app.add_handler(search_handler)

    # ConversationHandler для удаления
    delete_text_filter = filters.TEXT & ~filters.COMMAND & ~filters.Regex("^❌ Отмена$")

    delete_handler = ConversationHandler(
        entry_points=[
            CommandHandler("delete", cmd_delete),
            MessageHandler(filters.Regex("^🗑 Удалить$"), cmd_delete),
        ],
        states={
            DELETE_ID: [MessageHandler(delete_text_filter, delete_receive_id)],
        },
        fallbacks=[
            CommandHandler("cancel", delete_cancel),
            MessageHandler(filters.Regex("^❌ Отмена$"), delete_cancel),
        ],
    )
    app.add_handler(delete_handler)

    # Кнопки главного меню
    app.add_handler(MessageHandler(filters.Regex("^📋 Список$"), cmd_list))
    app.add_handler(MessageHandler(filters.Regex("^📊 Выгрузить в Excel$"), cmd_export))
    app.add_handler(MessageHandler(filters.Regex("^⚡ Быстрая запись$"), cmd_quick))
    app.add_handler(MessageHandler(filters.Regex("^📋 Пропущенные$"), cmd_missed))
    
    # Обычные команды
    app.add_handler(CommandHandler("quick", cmd_quick))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("missed", cmd_missed))
    app.add_handler(CommandHandler("export", cmd_export))

    # Инлайн-кнопки напоминаний
    app.add_handler(CallbackQueryHandler(handle_remind_callback, pattern=r"^remind:"))

    # Быстрое редактирование примечания из напоминания (низкий приоритет, группа 1)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, quick_note_handler),
        group=1
    )

    # Запуск проверки напоминаний каждые 60 секунд
    app.job_queue.run_repeating(
        check_reminders,
        interval=60,
        first=10,  # первая проверка через 10 секунд после старта
    )

    logger.info("Бот запущен! Нажми Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
