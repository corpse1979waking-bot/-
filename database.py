"""
Модуль работы с базой данных SQLite для хранения звонков.
"""

import sqlite3
from datetime import datetime
from difflib import SequenceMatcher
from config import DB_PATH


def get_connection():
    """Создаёт и возвращает соединение с БД."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Создаёт таблицы БД, если они не существуют."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            phone       TEXT NOT NULL,
            name        TEXT NOT NULL,
            call_date   TEXT NOT NULL,
            remind_date TEXT,
            notes       TEXT,
            reminded    INTEGER DEFAULT 0,
            created_at  TEXT NOT NULL,
            status      TEXT DEFAULT 'Новый',
            created_by  INTEGER,
            assigned_to INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            full_name TEXT,
            role TEXT DEFAULT 'guest',
            status TEXT DEFAULT 'pending'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (call_id) REFERENCES calls (id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    conn.close()


def add_call(phone: str, name: str, call_date: str,
             remind_date: str | None, notes: str | None,
             created_by: int | None = None, status: str = 'Новый') -> int:
    """
    Добавляет новый звонок в базу.
    Возвращает ID созданной записи.
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
    return call_id


def get_all_calls() -> list[dict]:
    """Возвращает все звонки, отсортированные по дате создания (новые первые)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM calls ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_recent_calls(limit: int = 10) -> list[dict]:
    """Возвращает последние N звонков."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM calls ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_pending_reminders() -> list[dict]:
    """
    Возвращает звонки, по которым пора напомнить:
    remind_date <= текущее время и ещё не напоминали.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = get_connection()
    rows = conn.execute(
        """SELECT * FROM calls
           WHERE reminded = 0
             AND remind_date IS NOT NULL
             AND remind_date <= ?
           ORDER BY remind_date""",
        (now,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def mark_reminded(call_id: int):
    """Отмечает что напоминание по звонку уже отправлено."""
    conn = get_connection()
    conn.execute("UPDATE calls SET reminded = 1 WHERE id = ?", (call_id,))
    conn.commit()
    conn.close()


def mark_acknowledged(call_id: int):
    """Отмечает что пользователь обработал напоминание (нажал ОК/Слился)."""
    conn = get_connection()
    conn.execute("UPDATE calls SET reminded = 2 WHERE id = ?", (call_id,))
    conn.commit()
    conn.close()


def get_unacknowledged_reminders() -> list[dict]:
    """Возвращает напоминания, которые были отправлены, но не обработаны.
    reminded=1 означает отправлено, но пользователь не нажал ОК/не перенёс.
    """
    conn = get_connection()
    rows = conn.execute(
        """SELECT * FROM calls
           WHERE reminded = 1
             AND remind_date IS NOT NULL
           ORDER BY remind_date DESC"""
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_call_by_id(call_id: int) -> dict | None:
    """Возвращает звонок по ID или None если не найден."""
    conn = get_connection()
    row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_call(call_id: int, **fields) -> bool:
    """
    Обновляет указанные поля звонка.
    Пример: update_call(5, phone="79991112233", name="Петров")
    Возвращает True если запись существовала.
    """
    if not fields:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [call_id]
    conn = get_connection()
    cursor = conn.execute(
        f"UPDATE calls SET {set_clause} WHERE id = ?", values
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated



def search_calls(query: str, limit: int = 20) -> list[dict]:
    """
    Умный поиск: сначала точное вхождение (LIKE, без учёта регистра),
    затем нечёткий поиск (совпадение >= 80%).
    """
    query_lower = query.lower()
    conn = get_connection()

    # 1. Точное вхождение подстроки (без учёта регистра)
    pattern = f"%{query_lower}%"
    exact_rows = conn.execute(
        """SELECT * FROM calls
           WHERE LOWER(phone) LIKE ? OR LOWER(name) LIKE ?
           ORDER BY id DESC""",
        (pattern, pattern)
    ).fetchall()
    exact_ids = {row["id"] for row in exact_rows}
    results = [dict(row) for row in exact_rows]

    # 2. Нечёткий поиск по всем записям (>= 80% совпадения)
    all_rows = conn.execute("SELECT * FROM calls ORDER BY id DESC").fetchall()
    conn.close()

    for row in all_rows:
        if row["id"] in exact_ids:
            continue
        # Проверяем похожесть по каждому слову в ФИО и по телефону
        name_lower = (row["name"] or "").lower()
        phone = row["phone"] or ""
        # Сравниваем с полным именем
        ratio_name = SequenceMatcher(None, query_lower, name_lower).ratio()
        ratio_phone = SequenceMatcher(None, query_lower, phone).ratio()
        # Также сравниваем с отдельными словами в ФИО
        words = name_lower.split()
        ratio_words = max(
            (SequenceMatcher(None, query_lower, w).ratio() for w in words),
            default=0
        )
        best = max(ratio_name, ratio_phone, ratio_words)
        if best >= 0.8:
            results.append(dict(row))

    return results[:limit]


def delete_call(call_id: int) -> bool:
    """Удаляет звонок по ID. Возвращает True если запись существовала."""
    conn = get_connection()
    cursor = conn.execute("DELETE FROM calls WHERE id = ?", (call_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

# ==============================================================================
# Users
# ==============================================================================

def add_user(telegram_id: int, username: str | None, full_name: str | None, role: str = 'guest', status: str = 'pending') -> int:
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO users (telegram_id, username, full_name, role, status) VALUES (?, ?, ?, ?, ?)",
        (telegram_id, username, full_name, role, status)
    )
    user_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return user_id

def get_user_by_telegram_id(telegram_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_all_users() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(row) for row in rows]

def update_user(user_id: int, **fields) -> bool:
    if not fields:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [user_id]
    conn = get_connection()
    cursor = conn.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated

# ==============================================================================
# Comments
# ==============================================================================

def add_comment(call_id: int, user_id: int, content: str) -> int:
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO comments (call_id, user_id, content, created_at) VALUES (?, ?, ?, ?)",
        (call_id, user_id, content, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    comment_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return comment_id

def get_call_comments(call_id: int) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        """SELECT c.*, u.full_name as author_name 
           FROM comments c
           LEFT JOIN users u ON c.user_id = u.id
           WHERE c.call_id = ? ORDER BY c.id ASC""",
        (call_id,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]

# ==============================================================================
# Extended Call Logic
# ==============================================================================

def assign_call(call_id: int, user_id: int) -> bool:
    return update_call(call_id, assigned_to=user_id, status='В работе')

def update_call_status(call_id: int, status: str) -> bool:
    return update_call(call_id, status=status)
