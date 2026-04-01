"""
Модуль интеграции расширенных функций для бота "База звонков"
Добавляет команды для многопользовательской работы
"""

import logging
import sqlite3
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters, ContextTypes
from config import DB_PATH, BOT_TOKEN
import database as db
import database_enhanced as de

logger = logging.getLogger(__name__)

# Состояния для ConversationHandler
ASSIGN_CALL_ID, ASSIGN_USER_ID = range(100, 102)
CHANGE_STATUS_ID, CHANGE_STATUS_VALUE = range(102, 104)
ADD_COMMENT_ID, ADD_COMMENT_TEXT = range(104, 106)
HISTORY_CALL_ID = 106
OVERDUE_CALLS = 107
BULK_ASSIGN_IDS, BULK_ASSIGN_USER = range(108, 110)
BULK_STATUS_IDS, BULK_STATUS_VALUE = range(110, 112)


# =============================================================================
# Вспомогательные функции
# =============================================================================

def is_admin_or_operator(update: Update) -> tuple[bool, str]:
    """Проверяет что пользователь админ или оператор."""
    user = update.effective_user
    if not user:
        return False, ""
    
    db_user = db.get_user_by_telegram_id(user.id)
    if not db_user:
        return False, "❌ Пользователь не найден. Используйте /start"
    
    if db_user.get('status') != 'active':
        return False, "⏳ Ваша заявка ожидает одобрения"
    
    if db_user.get('role') not in ('admin', 'operator'):
        return False, "❌ Только операторы и администраторы могут выполнять это действие"
    
    return True, ""


def get_call_inline_keyboard(call: dict, user_role: str = 'guest') -> InlineKeyboardMarkup:
    """Создает inline-клавиатуру для звонка."""
    buttons = []
    
    # Кнопки статусов (только допустимые переходы)
    if user_role in ('admin', 'operator'):
        current_status = call.get('status', 'Новый')
        transitions = de.get_valid_transitions(current_status)
        
        if transitions:
            status_buttons = []
            for status in transitions[:3]:
                emoji = {'В работе': '⏳', 'На связи': '📞', 'Закрыт': '✅', 
                        'Не заинтересован': '❌', 'Отложен': '⏸️'}.get(status, '📝')
                status_buttons.append(InlineKeyboardButton(f"{emoji} {status}", callback_data=f"status:{call['id']}:{status}"))
            
            if status_buttons:
                buttons.append(status_buttons)
    
    # Кнопка назначения (если не назначен или для админа)
    if user_role == 'admin' or not call.get('assigned_to'):
        assign_text = "👤 Назначить" if not call.get('assigned_to') else "🔄 Переназначить"
        buttons.append([InlineKeyboardButton(assign_text, callback_data=f"assign:{call['id']}")])
    
    # Кнопка комментариев
    buttons.append([InlineKeyboardButton("💬 Комментарии", callback_data=f"comments:{call['id']}")])
    
    # Кнопка истории
    buttons.append([InlineKeyboardButton("📜 История", callback_data=f"history:{call['id']}")])
    
    return InlineKeyboardMarkup(buttons)


def format_comment(comment: dict) -> str:
    """Форматирует комментарий для вывода."""
    author = comment.get('author_name') or comment.get('author_username') or 'Аноним'
    role = comment.get('author_role', '')
    role_badge = {'admin': '👑', 'operator': '👤', 'guest': '👁️'}.get(role, '')
    
    created = comment.get('created_at', '')
    if created:
        try:
            dt = datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
            created = dt.strftime("%d.%m %H:%M")
        except:
            pass
    
    return f"<b>{role_badge} {author}</b> <i>({created})</i>:\n{comment.get('content', '')}"


# =============================================================================
# Команда /assign - Назначение звонка
# =============================================================================

async def cmd_assign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начать назначение звонка на пользователя."""
    has_access, error = is_admin_or_operator(update)
    if not has_access:
        await update.message.reply_text(error or "❌ Нет доступа")
        return
    
    args = context.args
    
    if len(args) >= 2:
        # Быстрое назначение: /assign call_id user_id
        try:
            call_id = int(args[0])
            user_id = int(args[1])
        except ValueError:
            await update.message.reply_text("❌ ID должны быть числами\nИспользование: /assign &lt;call_id&gt; &lt;user_id&gt;")
            return
        
        success = de.assign_call(call_id, user_id, db.get_user_by_telegram_id(update.effective_user.id)['id'])
        
        if success:
            # Получаем данные для уведомления
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            assigned_user = conn.execute("SELECT telegram_id, full_name FROM users WHERE id = ?", (user_id,)).fetchone()
            call = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
            conn.close()
            
            text = f"✅ Звонок #{call_id} назначен на пользователя #{user_id}"
            if assigned_user and call:
                text += f"\n\n<b>Звонок:</b>\n📞 {call['phone']}\n👤 {call['name']}"
                
                # Уведомляем назначенного пользователя
                try:
                    await context.bot.send_message(
                        chat_id=assigned_user['telegram_id'],
                        text=f"🔔 <b>Вам назначен новый звонок!</b>\n\n"
                             f"📞 {call['phone']}\n"
                             f"👤 {call['name']}\n"
                             f"📅 {call['call_date']}\n\n"
                             f"Используйте /my_calls для просмотра ваших звонков.",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"Ошибка уведомления: {e}")
            
            await update.message.reply_text(text, parse_mode="HTML")
        else:
            await update.message.reply_text(f"❌ Не удалось назначить звонок #{call_id}")
        return
    
    # Пошаговый режим
    await update.message.reply_text(
        "📋 <b>Назначение звонка</b>\n\n"
        "Введите ID звонка для назначения:",
        parse_mode="HTML"
    )
    return ASSIGN_CALL_ID


async def assign_receive_call_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение ID звонка для назначения."""
    try:
        call_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом. Попробуйте еще раз или /cancel")
        return ASSIGN_CALL_ID
    
    # Проверяем существование звонка
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    call = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    conn.close()
    
    if not call:
        await update.message.reply_text(f"❌ Звонок #{call_id} не найден.\nПопробуйте другой ID или /cancel")
        return ASSIGN_CALL_ID
    
    context.user_data['assign_call_id'] = call_id
    
    # Показываем список активных пользователей для назначения
    users = de.get_active_users()
    operators = [u for u in users if u.get('role') in ('admin', 'operator')]
    
    if not operators:
        await update.message.reply_text("❌ Нет активных операторов для назначения")
        return ConversationHandler.END
    
    text = f"📋 Звонок #{call_id}: {call['phone']} - {call['name']}\n\n"
    text += "<b>Выберите оператора для назначения:</b>\n\n"
    
    keyboard = []
    for i in range(0, len(operators), 2):
        row = []
        for user in operators[i:i+2]:
            name = user.get('full_name') or user.get('username') or f"User #{user['id']}"
            row.append(InlineKeyboardButton(name, callback_data=f"assign_to:{call_id}:{user['id']}"))
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=f"assign_cancel:{call_id}")])
    
    await update.message.reply_text(
        text + "\n".join(f"• {u.get('full_name') or u.get('username')} (ID: {u['id']})" for u in operators[:10]),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    return ASSIGN_USER_ID


async def handle_assign_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора пользователя для назначения."""
    query = update.callback_query
    await query.answer()
    
    if not query.data.startswith("assign_to:"):
        return
    
    parts = query.data.split(":")
    call_id = int(parts[1])
    user_id = int(parts[2])
    
    assigner = db.get_user_by_telegram_id(update.effective_user.id)
    success = de.assign_call(call_id, user_id, assigner['id'] if assigner else None)
    
    if success:
        await query.edit_message_text(f"✅ Звонок #{call_id} назначен на пользователя #{user_id}")
        
        # Уведомление назначенному пользователю
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        assigned_user = conn.execute("SELECT telegram_id FROM users WHERE id = ?", (user_id,)).fetchone()
        call = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        conn.close()
        
        if assigned_user and call:
            try:
                await context.bot.send_message(
                    chat_id=assigned_user['telegram_id'],
                    text=f"🔔 <b>Вам назначен звонок!</b>\n\n"
                         f"📞 {call['phone']}\n👤 {call['name']}\n\n"
                         f"Используйте /my_calls",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления: {e}")
    else:
        await query.edit_message_text(f"❌ Не удалось назначить звонок #{call_id}")
    
    return ConversationHandler.END


async def assign_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена назначения."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Назначение отменено")
    return ConversationHandler.END


# =============================================================================
# Команда /status - Изменение статуса звонка
# =============================================================================

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Изменить статус звонка."""
    has_access, error = is_admin_or_operator(update)
    if not has_access:
        await update.message.reply_text(error or "❌ Нет доступа")
        return
    
    args = context.args
    
    if len(args) >= 2:
        # Быстрая смена: /status call_id new_status
        try:
            call_id = int(args[0])
            new_status = ' '.join(args[1:])
        except ValueError:
            await update.message.reply_text("❌ ID должен быть числом")
            return
        
        # Проверяем текущий статус
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        call = conn.execute("SELECT status FROM calls WHERE id = ?", (call_id,)).fetchone()
        conn.close()
        
        if not call:
            await update.message.reply_text(f"❌ Звонок #{call_id} не найден")
            return
        
        if not de.can_transition(call['status'], new_status):
            valid = de.get_valid_transitions(call['status'])
            await update.message.reply_text(
                f"❌ Нельзя перейти из '{call['status']}' в '{new_status}'\n"
                f"Доступные переходы: {', '.join(valid) if valid else 'нет доступных'}"
            )
            return
        
        user = db.get_user_by_telegram_id(update.effective_user.id)
        success = de.update_call(call_id, user_id=user['id'] if user else None, status=new_status)
        
        if success:
            await update.message.reply_text(f"✅ Статус звонка #{call_id} изменен на '{new_status}'")
        else:
            await update.message.reply_text(f"❌ Не удалось изменить статус")
        return
    
    # Пошаговый режим
    await update.message.reply_text(
        "📊 <b>Изменение статуса</b>\n\n"
        "Введите ID звонка:",
        parse_mode="HTML"
    )
    return CHANGE_STATUS_ID


async def status_receive_call_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение ID звонка для смены статуса."""
    try:
        call_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом. Попробуйте еще раз или /cancel")
        return CHANGE_STATUS_ID
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    call = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    conn.close()
    
    if not call:
        await update.message.reply_text(f"❌ Звонок #{call_id} не найден.\nПопробуйте другой ID или /cancel")
        return CHANGE_STATUS_ID
    
    context.user_data['status_call_id'] = call_id
    current_status = call['status']
    
    transitions = de.get_valid_transitions(current_status)
    
    if not transitions:
        await update.message.reply_text(f"ℹ️ Для статуса '{current_status}' нет доступных переходов")
        return ConversationHandler.END
    
    keyboard = []
    for status in transitions:
        emoji = {'В работе': '⏳', 'На связи': '📞', 'Закрыт': '✅', 
                'Не заинтересован': '❌', 'Отложен': '⏸️', 'Новый': '🆕'}.get(status, '📝')
        keyboard.append([InlineKeyboardButton(f"{emoji} {status}", callback_data=f"setstatus:{call_id}:{status}")])
    
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=f"status_cancel:{call_id}")])
    
    await update.message.reply_text(
        f"📋 Звонок #{call_id}: {call['phone']}\n"
        f"Текущий статус: <b>{current_status}</b>\n\n"
        f"Выберите новый статус:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    return CHANGE_STATUS_VALUE


async def handle_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора нового статуса."""
    query = update.callback_query
    await query.answer()
    
    if not query.data.startswith("setstatus:"):
        return
    
    parts = query.data.split(":")
    call_id = int(parts[1])
    new_status = parts[2]
    
    user = db.get_user_by_telegram_id(update.effective_user.id)
    success = de.update_call(call_id, user_id=user['id'] if user else None, status=new_status)
    
    if success:
        await query.edit_message_text(f"✅ Статус звонка #{call_id} изменен на '<b>{new_status}</b>'", parse_mode="HTML")
    else:
        await query.edit_message_text(f"❌ Не удалось изменить статус звонка #{call_id}")
    
    return ConversationHandler.END


async def status_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена смены статуса."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Изменение статуса отменено")
    return ConversationHandler.END


# =============================================================================
# Команда /comment - Добавление комментария
# =============================================================================

async def cmd_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавить комментарий к звонку."""
    has_access, error = check_user_access(update)
    if not has_access:
        await update.message.reply_text(error or "❌ Нет доступа")
        return
    
    args = context.args
    
    if len(args) >= 1:
        # Быстрый режим: /comment call_id текст комментария
        try:
            call_id = int(args[0])
        except ValueError:
            await update.message.reply_text("❌ ID звонка должен быть числом")
            return
        
        comment_text = ' '.join(args[1:]) if len(args) > 1 else None
        
        if not comment_text:
            await update.message.reply_text("❌ Введите текст комментария после ID звонка")
            return
        
        user = db.get_user_by_telegram_id(update.effective_user.id)
        comment_id = de.add_comment(call_id, user['id'] if user else None, comment_text)
        
        if comment_id:
            await update.message.reply_text(f"✅ Комментарий добавлен к звонку #{call_id}")
        else:
            await update.message.reply_text(f"❌ Звонок #{call_id} не найден")
        return
    
    # Пошаговый режим
    await update.message.reply_text(
        "💬 <b>Добавление комментария</b>\n\n"
        "Введите ID звонка:",
        parse_mode="HTML"
    )
    return ADD_COMMENT_ID


async def comment_receive_call_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение ID звонка для комментария."""
    try:
        call_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом. Попробуйте еще раз или /cancel")
        return ADD_COMMENT_ID
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    call = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    conn.close()
    
    if not call:
        await update.message.reply_text(f"❌ Звонок #{call_id} не найден.\nПопробуйте другой ID или /cancel")
        return ADD_COMMENT_ID
    
    context.user_data['comment_call_id'] = call_id
    
    await update.message.reply_text(
        f"📋 Звонок #{call_id}: {call['phone']} - {call['name']}\n\n"
        f"Введите текст комментария:"
    )
    return ADD_COMMENT_TEXT


async def comment_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение текста комментария."""
    call_id = context.user_data.get('comment_call_id')
    comment_text = update.message.text.strip()
    
    if not comment_text:
        await update.message.reply_text("❌ Комментарий не может быть пустым. Попробуйте еще раз или /cancel")
        return ADD_COMMENT_TEXT
    
    user = db.get_user_by_telegram_id(update.effective_user.id)
    comment_id = de.add_comment(call_id, user['id'] if user else None, comment_text)
    
    if comment_id:
        await update.message.reply_text(f"✅ Комментарий добавлен к звонку #{call_id}")
    else:
        await update.message.reply_text(f"❌ Не удалось добавить комментарий")
    
    return ConversationHandler.END


async def comment_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена добавления комментария."""
    await update.message.reply_text("❌ Добавление комментария отменено")
    return ConversationHandler.END


# =============================================================================
# Команда /history - История изменений звонка
# =============================================================================

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать историю изменений звонка."""
    has_access, error = check_user_access(update)
    if not has_access:
        await update.message.reply_text(error or "❌ Нет доступа")
        return
    
    args = context.args
    
    if not args:
        await update.message.reply_text("Использование: /history &lt;call_id&gt;")
        return
    
    try:
        call_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")
        return
    
    history = de.get_call_history(call_id)
    
    if not history:
        await update.message.reply_text(f"📜 У звонка #{call_id} нет истории изменений")
        return
    
    text = f"📜 <b>История звонка #{call_id}</b>\n\n"
    
    for record in history[:20]:
        action = record.get('action', '')
        action_emoji = {'create': '➕', 'update': '✏️', 'delete': '🗑️', 
                       'status_change': '📊', 'assign': '👤'}.get(action, '📝')
        
        user_name = record.get('user_name') or 'Система'
        created = record.get('created_at', '')
        field = record.get('field', '')
        old_val = record.get('old_value', '')
        new_val = record.get('new_value', '')
        
        text += f"{action_emoji} <b>{user_name}</b> <i>({created})</i>\n"
        text += f"   Действие: {action}"
        
        if field and field != 'None':
            text += f", Поле: {field}"
            if old_val and old_val != 'None':
                text += f"\n   Было: {old_val}"
            if new_val and new_val != 'None':
                text += f"\n   Стало: {new_val}"
        
        text += "\n\n"
    
    if len(history) > 20:
        text += f"<i>Показаны первые 20 из {len(history)} записей</i>"
    
    await update.message.reply_text(text, parse_mode="HTML")


# =============================================================================
# Команда /overdue - Просроченные звонки
# =============================================================================

async def cmd_overdue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать просроченные звонки."""
    has_access, error = is_admin_or_operator(update)
    if not has_access:
        await update.message.reply_text(error or "❌ Нет доступа")
        return
    
    overdue = de.get_overdue_calls()
    
    if not overdue:
        await update.message.reply_text("✅ Все звонки в порядке! Нет просроченных задач.")
        return
    
    text = f"⚠️ <b>Просроченные звонки ({len(overdue)})</b>\n\n"
    
    for call in overdue[:10]:
        status = call.get('status', '')
        phone = call.get('phone', '')
        name = call.get('name', '')
        remind = call.get('remind_date', '')
        assigned = call.get('assigned_to', '')
        
        # Получаем имя назначенного
        assigned_name = ''
        if assigned:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            user = conn.execute("SELECT full_name FROM users WHERE id = ?", (assigned,)).fetchone()
            conn.close()
            assigned_name = user['full_name'] if user else f"#{assigned}"
        
        text += f"📞 {phone} - {name}\n"
        text += f"   Статус: {status}"
        if assigned_name:
            text += f", Ответственный: {assigned_name}"
        if remind:
            text += f", Напоминание: {remind}"
        text += "\n\n"
    
    if len(overdue) > 10:
        text += f"<i>Показаны первые 10 из {len(overdue)}</i>"
    
    await update.message.reply_text(text, parse_mode="HTML")


# =============================================================================
# Команда /view_comments - Просмотр комментариев
# =============================================================================

async def cmd_view_comments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотреть комментарии к звонку."""
    has_access, error = check_user_access(update)
    if not has_access:
        await update.message.reply_text(error or "❌ Нет доступа")
        return
    
    args = context.args
    
    if not args:
        await update.message.reply_text("Использование: /view_comments &lt;call_id&gt;")
        return
    
    try:
        call_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")
        return
    
    comments = de.get_call_comments_with_details(call_id)
    
    if not comments:
        await update.message.reply_text(f"💬 У звонка #{call_id} пока нет комментариев")
        return
    
    text = f"💬 <b>Комментарии к звонку #{call_id}</b>\n\n"
    text += "\n\n".join(format_comment(c) for c in comments)
    
    await update.message.reply_text(text, parse_mode="HTML")


# =============================================================================
# Команда /bulk_assign - Массовое назначение
# =============================================================================

async def cmd_bulk_assign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Массовое назначение звонков."""
    if not is_admin(update):
        await update.message.reply_text("❌ Только администраторы могут выполнять массовые операции")
        return
    
    args = context.args
    
    if len(args) < 2:
        await update.message.reply_text(
            "Использование: /bulk_assign &lt;user_id&gt; &lt;call_id1,call_id2,...&gt;\n"
            "Пример: /bulk_assign 5 10,15,20"
        )
        return
    
    try:
        user_id = int(args[0])
        call_ids = [int(x.strip()) for x in args[1].split(',')]
    except ValueError:
        await update.message.reply_text("❌ Ошибка в формате ID. Используйте числа, разделенные запятыми")
        return
    
    count = de.bulk_assign(call_ids, user_id)
    
    await update.message.reply_text(f"✅ Назначено {count} из {len(call_ids)} звонков на пользователя #{user_id}")


# =============================================================================
# Команда /bulk_status - Массовая смена статуса
# =============================================================================

async def cmd_bulk_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Массовая смена статуса звонков."""
    if not is_admin(update):
        await update.message.reply_text("❌ Только администраторы могут выполнять массовые операции")
        return
    
    args = context.args
    
    if len(args) < 2:
        await update.message.reply_text(
            "Использование: /bulk_status &lt;new_status&gt; &lt;call_id1,call_id2,...&gt;\n"
            "Пример: /bulk_status Закрыт 10,15,20"
        )
        return
    
    new_status = args[0]
    if new_status not in de.VALID_STATUSES:
        await update.message.reply_text(f"❌ Недопустимый статус. Доступные: {', '.join(de.VALID_STATUSES)}")
        return
    
    try:
        call_ids = [int(x.strip()) for x in args[1].split(',')]
    except ValueError:
        await update.message.reply_text("❌ Ошибка в формате ID. Используйте числа, разделенные запятыми")
        return
    
    user = db.get_user_by_telegram_id(update.effective_user.id)
    count = de.bulk_status_change(call_ids, new_status, user['id'] if user else None)
    
    await update.message.reply_text(f"✅ Изменен статус у {count} из {len(call_ids)} звонков на '{new_status}'")


# =============================================================================
# Интеграция с существующими командами
# =============================================================================

def check_user_access(update: Update) -> tuple[bool, str]:
    """Проверяет доступ пользователя (копия из bot.py)."""
    user = update.effective_user
    if not user:
        return False, "❌ Не удалось определить пользователя"
    
    db_user = db.get_user_by_telegram_id(user.id)
    if not db_user:
        return False, "❌ Пользователь не найден. Используйте /start"
    
    if db_user.get('status') == 'pending':
        return False, "⏳ Ваша заявка ожидает одобрения"
    
    if db_user.get('status') == 'rejected':
        return False, "❌ Ваша заявка была отклонена"
    
    return True, ""


def is_admin(update: Update) -> bool:
    """Проверяет что пользователь админ (копия из bot.py)."""
    user = update.effective_user
    if not user:
        return False
    
    db_user = db.get_user_by_telegram_id(user.id)
    return db_user and db_user.get('role') == 'admin' and db_user.get('status') == 'active'


# =============================================================================
# Функция для получения всех обработчиков
# =============================================================================

def get_enhanced_handlers():
    """Возвращает список всех обработчиков для интеграции в bot.py."""
    
    handlers = [
        # Команды
        CommandHandler("assign", cmd_assign),
        CommandHandler("status", cmd_status),
        CommandHandler("comment", cmd_comment),
        CommandHandler("history", cmd_history),
        CommandHandler("overdue", cmd_overdue),
        CommandHandler("view_comments", cmd_view_comments),
        CommandHandler("bulk_assign", cmd_bulk_assign),
        CommandHandler("bulk_status", cmd_bulk_status),
        
        # ConversationHandler для назначения
        ConversationHandler(
            entry_points=[CommandHandler("assign", cmd_assign)],
            states={
                ASSIGN_CALL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, assign_receive_call_id)],
                ASSIGN_USER_ID: [],  # Обрабатывается callback
            },
            fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
        ),
        
        # ConversationHandler для статуса
        ConversationHandler(
            entry_points=[CommandHandler("status", cmd_status)],
            states={
                CHANGE_STATUS_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, status_receive_call_id)],
                CHANGE_STATUS_VALUE: [],  # Обрабатывается callback
            },
            fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
        ),
        
        # ConversationHandler для комментариев
        ConversationHandler(
            entry_points=[CommandHandler("comment", cmd_comment)],
            states={
                ADD_COMMENT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, comment_receive_call_id)],
                ADD_COMMENT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, comment_receive_text)],
            },
            fallbacks=[CommandHandler("cancel", comment_cancel)],
        ),
        
        # Callback query handlers
        CallbackQueryHandler(handle_assign_callback, pattern=r"^assign_to:"),
        CallbackQueryHandler(assign_cancel_callback, pattern=r"^assign_cancel:"),
        CallbackQueryHandler(handle_status_callback, pattern=r"^setstatus:"),
        CallbackQueryHandler(status_cancel_callback, pattern=r"^status_cancel:"),
    ]
    
    return handlers
