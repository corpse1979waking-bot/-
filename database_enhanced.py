"""
Модуль расширенных функций для многопользовательской работы
и улучшения эффективности бота "База звонков"
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Optional
from config import DB_PATH


def get_connection():
    """Создаёт и возвращает соединение с БД."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# =============================================================================
# Улучшенная система пользователей
# =============================================================================

def register_user(telegram_id: int, username: str, full_name: str) -> dict:
    """
    Регистрирует нового пользователя или обновляет данные существующего.
    Возвращает информацию о пользователе.
    """
    conn = get_connection()
    
    # Проверяем существующего пользователя
    existing = conn.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    
    if existing:
        # Обновляем имя и username если изменились
        conn.execute(
            "UPDATE users SET username = ?, full_name = ? WHERE telegram_id = ?",
            (username, full_name, telegram_id)
        )
        conn.commit()
        user = dict(conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone())
    else:
        # Создаем нового пользователя со статусом 'pending'
        cursor = conn.execute(
            "INSERT INTO users (telegram_id, username, full_name, role, status) VALUES (?, ?, ?, ?, ?)",
            (telegram_id, username, full_name, 'guest', 'pending')
        )
        conn.commit()
        user = dict(conn.execute(
            "SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)
        ).fetchone())
    
    conn.close()
    return user


def approve_user(user_id: int, role: str = 'operator') -> bool:
    """
    Одобрляет регистрацию пользователя и назначает роль.
    Роли: admin, operator, guest
    """
    if role not in ('admin', 'operator', 'guest'):
        return False
    
    return update_user(user_id, role=role, status='active')


def reject_user(user_id: int) -> bool:
    """Отклоняет регистрацию пользователя."""
    return update_user(user_id, status='rejected')


def update_user(user_id: int, **fields) -> bool:
    """
    Обновляет поля пользователя.
    Поля: role, status, username, full_name
    """
    if not fields:
        return False
    
    # Проверяем что user_id существует
    conn = get_connection()
    existing = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not existing:
        # Пробуем найти по telegram_id
        existing = conn.execute("SELECT id FROM users WHERE telegram_id = ?", (user_id,)).fetchone()
        if existing:
            user_id = existing['id']
        else:
            conn.close()
            return False
    
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [user_id]
    cursor = conn.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def get_pending_users() -> list[dict]:
    """Возвращает список пользователей, ожидающих одобрения."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM users WHERE status = 'pending' ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_active_users() -> list[dict]:
    """Возвращает список активных пользователей."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM users WHERE status = 'active' ORDER BY role, full_name"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_user_stats(user_id: int) -> dict:
    """
    Возвращает статистику пользователя:
    - количество созданных звонков
    - количество завершенных звонков
    - среднее время обработки
    """
    conn = get_connection()
    
    # Количество созданных звонков
    created = conn.execute(
        "SELECT COUNT(*) as count FROM calls WHERE created_by = ?", (user_id,)
    ).fetchone()['count']
    
    # Количество назначенных звонков
    assigned = conn.execute(
        "SELECT COUNT(*) as count FROM calls WHERE assigned_to = ?", (user_id,)
    ).fetchone()['count']
    
    # Завершенные (статус не 'Новый' и не 'В работе')
    completed = conn.execute(
        """SELECT COUNT(*) as count FROM calls 
           WHERE assigned_to = ? AND status NOT IN ('Новый', 'В работе')""",
        (user_id,)
    ).fetchone()['count']
    
    conn.close()
    
    return {
        'created': created,
        'assigned': assigned,
        'completed': completed,
        'completion_rate': round(completed / assigned * 100, 1) if assigned > 0 else 0
    }


# =============================================================================
# Улучшенная система статусов и воронки
# =============================================================================

STATUS_FLOW = {
    'Новый': ['В работе', 'Отложен', 'Закрыт'],
    'В работе': ['На связи', 'Отложен', 'Закрыт', 'Не заинтересован'],
    'На связи': ['В работе', 'Отложен', 'Закрыт', 'Не заинтересован'],
    'Отложен': ['В работе', 'Закрыт', 'Не заинтересован'],
    'Закрыт': [],
    'Не заинтересован': []
}

VALID_STATUSES = list(STATUS_FLOW.keys())


def get_valid_transitions(current_status: str) -> list[str]:
    """Возвращает список допустимых переходов для текущего статуса."""
    return STATUS_FLOW.get(current_status, [])


def can_transition(from_status: str, to_status: str) -> bool:
    """Проверяет возможен ли переход между статусами."""
    return to_status in STATUS_FLOW.get(from_status, [])


def get_calls_by_status(status: str, limit: int = 50) -> list[dict]:
    """Возвращает звонки с указанным статусом."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM calls WHERE status = ? ORDER BY id DESC LIMIT ?",
        (status, limit)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_overdue_calls() -> list[dict]:
    """
    Возвращает просроченные звонки:
    - remind_date < сейчас и reminded != 2
    - или статус 'В работе' без изменений более 3 дней
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    three_days_ago = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM calls 
        WHERE (remind_date IS NOT NULL AND remind_date <= ? AND reminded != 2)
           OR (status = 'В работе' AND created_at <= ?)
        ORDER BY remind_date ASC, created_at ASC
    """, (now, three_days_ago)).fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


# =============================================================================
# Расширенная работа с комментариями
# =============================================================================

def add_comment(call_id: int, user_id: int, content: str) -> int:
    """Добавляет комментарий к звонку."""
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO comments (call_id, user_id, content, created_at) VALUES (?, ?, ?, ?)",
        (call_id, user_id, content, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    comment_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return comment_id


def get_call_comments_with_details(call_id: int) -> list[dict]:
    """Возвращает комментарии с подробной информацией о пользователе."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT c.id, c.call_id, c.user_id, c.content, c.created_at,
               u.full_name as author_name, u.username as author_username, u.role as author_role
        FROM comments c
        LEFT JOIN users u ON c.user_id = u.id
        WHERE c.call_id = ?
        ORDER BY c.created_at ASC
    """, (call_id,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def delete_comment(comment_id: int, user_id: int) -> bool:
    """
    Удаляет комментарий.
    Пользователь может удалить только свой комментарий.
    """
    conn = get_connection()
    cursor = conn.execute(
        "DELETE FROM comments WHERE id = ? AND user_id = ?",
        (comment_id, user_id)
    )
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


# =============================================================================
# История изменений (Audit Log)
# =============================================================================

def init_audit_log():
    """Создает таблицу истории изменений если она не существует."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id INTEGER NOT NULL,
            user_id INTEGER,
            action TEXT NOT NULL,
            field TEXT,
            old_value TEXT,
            new_value TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def log_change(call_id: int, user_id: int, action: str, 
               field: str = None, old_value: str = None, new_value: str = None):
    """
    Логирует изменение записи.
    action: 'create', 'update', 'delete', 'status_change', 'assign'
    """
    conn = get_connection()
    conn.execute("""
        INSERT INTO audit_log (call_id, user_id, action, field, old_value, new_value, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (call_id, user_id, action, field, 
          str(old_value) if old_value is not None else None,
          str(new_value) if new_value is not None else None,
          datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()


def get_call_history(call_id: int) -> list[dict]:
    """Возвращает историю изменений звонка."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT a.*, u.full_name as user_name
        FROM audit_log a
        LEFT JOIN users u ON a.user_id = u.id
        WHERE a.call_id = ?
        ORDER BY a.created_at DESC
    """, (call_id,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# =============================================================================
# Назначение исполнителей
# =============================================================================

def assign_call(call_id: int, user_id: int, assigned_by: int = None) -> bool:
    """
    Назначает звонок на пользователя.
    Автоматически меняет статус на 'В работе'.
    """
    conn = get_connection()
    
    # Получаем текущий статус
    call = conn.execute("SELECT status FROM calls WHERE id = ?", (call_id,)).fetchone()
    if not call:
        conn.close()
        return False
    
    old_status = call['status']
    
    # Обновляем назначение
    conn.execute("""
        UPDATE calls 
        SET assigned_to = ?, status = 'В работе', 
            updated_at = ?
        WHERE id = ?
    """, (user_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), call_id))
    
    conn.commit()
    conn.close()
    
    # Логируем
    log_change(call_id, assigned_by or user_id, 'assign', 
               'assigned_to', None, user_id)
    
    return True


def unassign_call(call_id: int) -> bool:
    """Снимает назначение со звонка."""
    return update_call(call_id, assigned_to=None, status='Новый')


def get_my_calls(user_id: int, status: str = None, limit: int = 50) -> list[dict]:
    """
    Возвращает звонки, назначенные на пользователя.
    Если status указан — фильтрует по статусу.
    """
    conn = get_connection()
    
    if status:
        rows = conn.execute("""
            SELECT * FROM calls 
            WHERE assigned_to = ? AND status = ?
            ORDER BY id DESC LIMIT ?
        """, (user_id, status, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM calls 
            WHERE assigned_to = ?
            ORDER BY id DESC LIMIT ?
        """, (user_id, limit)).fetchall()
    
    conn.close()
    return [dict(row) for row in rows]


def get_unassigned_calls(limit: int = 50) -> list[dict]:
    """Возвращает звонки без назначения."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM calls 
        WHERE assigned_to IS NULL AND status = 'Новый'
        ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# =============================================================================
# Статистика и аналитика
# =============================================================================

def get_dashboard_stats() -> dict:
    """
    Возвращает общую статистику для дашборда:
    - всего звонков
    - по статусам
    - просроченные
    - за сегодня/неделю/месяц
    """
    conn = get_connection()
    
    today = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    
    # Всего звонков
    total = conn.execute("SELECT COUNT(*) as count FROM calls").fetchone()['count']
    
    # По статусам
    status_counts = {}
    for status in VALID_STATUSES:
        count = conn.execute(
            "SELECT COUNT(*) as count FROM calls WHERE status = ?", (status,)
        ).fetchone()['count']
        status_counts[status] = count
    
    # Просроченные
    overdue = len(get_overdue_calls())
    
    # За сегодня
    today_count = conn.execute("""
        SELECT COUNT(*) as count FROM calls 
        WHERE DATE(created_at) = ?
    """, (today,)).fetchone()['count']
    
    # За неделю
    week_count = conn.execute("""
        SELECT COUNT(*) as count FROM calls 
        WHERE DATE(created_at) >= ?
    """, (week_ago,)).fetchone()['count']
    
    # За месяц
    month_count = conn.execute("""
        SELECT COUNT(*) as count FROM calls 
        WHERE DATE(created_at) >= ?
    """, (month_ago,)).fetchone()['count']
    
    # Конверсия (Закрыт + Не заинтересован) / Всего
    converted = status_counts.get('Закрыт', 0) + status_counts.get('Не заинтересован', 0)
    conversion_rate = round(converted / total * 100, 1) if total > 0 else 0
    
    conn.close()
    
    return {
        'total': total,
        'by_status': status_counts,
        'overdue': overdue,
        'today': today_count,
        'week': week_count,
        'month': month_count,
        'conversion_rate': conversion_rate
    }


def get_conversion_stats(days: int = 30) -> dict:
    """
    Статистика конверсии за период.
    """
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    
    conn = get_connection()
    
    # Звонки за период
    total = conn.execute("""
        SELECT COUNT(*) as count FROM calls 
        WHERE DATE(created_at) >= ?
    """, (start_date,)).fetchone()['count']
    
    # Успешные (Закрыт)
    closed = conn.execute("""
        SELECT COUNT(*) as count FROM calls 
        WHERE DATE(created_at) >= ? AND status = 'Закрыт'
    """, (start_date,)).fetchone()['count']
    
    # Отказы
    rejected = conn.execute("""
        SELECT COUNT(*) as count FROM calls 
        WHERE DATE(created_at) >= ? AND status = 'Не заинтересован'
    """, (start_date,)).fetchone()['count']
    
    # В работе
    in_progress = conn.execute("""
        SELECT COUNT(*) as count FROM calls 
        WHERE DATE(created_at) >= ? AND status IN ('Новый', 'В работе', 'На связи', 'Отложен')
    """, (start_date,)).fetchone()['count']
    
    conn.close()
    
    return {
        'total': total,
        'closed': closed,
        'rejected': rejected,
        'in_progress': in_progress,
        'close_rate': round(closed / total * 100, 1) if total > 0 else 0,
        'reject_rate': round(rejected / total * 100, 1) if total > 0 else 0
    }


# =============================================================================
# Массовые операции
# =============================================================================

def bulk_assign(call_ids: list[int], user_id: int) -> int:
    """
    Массово назначает звонки на пользователя.
    Возвращает количество успешно назначенных.
    """
    conn = get_connection()
    count = 0
    
    for call_id in call_ids:
        result = conn.execute("""
            UPDATE calls 
            SET assigned_to = ?, status = 'В работе'
            WHERE id = ? AND assigned_to IS NULL
        """, (user_id, call_id))
        
        if result.rowcount > 0:
            count += 1
    
    conn.commit()
    conn.close()
    return count


def bulk_status_change(call_ids: list[int], new_status: str, user_id: int) -> int:
    """
    Массово меняет статус звонков.
    Возвращает количество успешно измененных.
    """
    if new_status not in VALID_STATUSES:
        return 0
    
    conn = get_connection()
    placeholders = ','.join('?' * len(call_ids))
    
    result = conn.execute(f"""
        UPDATE calls 
        SET status = ?, updated_at = ?
        WHERE id IN ({placeholders})
    """, [new_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S")] + call_ids)
    
    count = result.rowcount
    conn.commit()
    conn.close()
    
    # Логируем изменения
    for call_id in call_ids[:count]:
        log_change(call_id, user_id, 'status_change', 'status', None, new_status)
    
    return count


# =============================================================================
# Доработки для существующих функций
# =============================================================================

def update_call(call_id: int, user_id: int = None, **fields) -> bool:
    """
    Обновляет звонок с логированием изменений.
    """
    conn = get_connection()
    
    # Получаем текущие значения
    current = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    if not current:
        conn.close()
        return False
    
    current_dict = dict(current)
    
    # Формируем UPDATE
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [call_id]
    
    cursor = conn.execute(f"UPDATE calls SET {set_clause}, updated_at = ? WHERE id = ?",
                         list(fields.values()) + [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), call_id])
    
    updated = cursor.rowcount > 0
    
    if updated and user_id:
        conn.commit()
        # Логируем каждое измененное поле
        for field, new_value in fields.items():
            old_value = current_dict.get(field)
            if old_value != new_value:
                log_change(call_id, user_id, 'update', field, old_value, new_value)
    else:
        conn.commit()
    
    conn.close()
    return updated


def delete_call(call_id: int, user_id: int = None) -> bool:
    """Удаляет звонок с логированием."""
    conn = get_connection()
    
    # Проверяем существование
    call = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    if not call:
        conn.close()
        return False
    
    cursor = conn.execute("DELETE FROM calls WHERE id = ?", (call_id,))
    deleted = cursor.rowcount > 0
    
    if deleted and user_id:
        conn.commit()
        log_change(call_id, user_id, 'delete')
    else:
        conn.commit()
    
    conn.close()
    return deleted


def add_call(phone: str, name: str, call_date: str,
             remind_date: str | None, notes: str | None,
             created_by: int | None = None, status: str = 'Новый') -> int:
    """
    Добавляет звонок с логированием.
    """
    conn = get_connection()
    cursor = conn.execute(
        """INSERT INTO calls (phone, name, call_date, remind_date, notes, created_at, status, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (phone, name, call_date, remind_date, notes,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"), status, created_by)
    )
    call_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    # Логируем создание
    if created_by:
        log_change(call_id, created_by, 'create')
    
    return call_id


# Инициализация при импорте
init_audit_log()
