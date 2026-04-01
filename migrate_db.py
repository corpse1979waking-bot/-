import sqlite3
import os

from config import DB_PATH

def migrate():
    # Проверяем, существует ли БД
    if not os.path.exists(DB_PATH):
        print(f"База данных {DB_PATH} не найдена. Нечего мигрировать.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        # Проверяем, есть ли новые колонки в таблице calls
        cursor.execute("PRAGMA table_info(calls)")
        columns = [row[1] for row in cursor.fetchall()]

        if "status" not in columns:
            print("Добавление colonok status, created_by, assigned_to...")
            cursor.execute("ALTER TABLE calls ADD COLUMN status TEXT DEFAULT 'Новый'")
            cursor.execute("ALTER TABLE calls ADD COLUMN created_by INTEGER")
            cursor.execute("ALTER TABLE calls ADD COLUMN assigned_to INTEGER")
        else:
            print("Таблица calls уже мигрирована.")

        # Создаём новые таблицы
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                full_name TEXT,
                role TEXT DEFAULT 'guest',
                status TEXT DEFAULT 'pending'
            )
        """)

        cursor.execute("""
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
        
        # Обновляем все старые звонки (например, если status NULL)
        cursor.execute("UPDATE calls SET status = 'Новый' WHERE status IS NULL")

        conn.commit()
        print("Миграция успешно завершена!")

    except Exception as e:
        print(f"Ошибка миграции: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == "__main__":
    migrate()
