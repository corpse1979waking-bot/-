import os
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# Конфигурация бота "База звонков"
# ============================================================

# Токен бота
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Твой главный Telegram User ID (первый админ)
# Остальные будут загружаться из базы данных
MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", 0))

# Старый список, оставляем для обратной совместимости или удаляем
# Мы удаляем ADMIN_USER_IDS, теперь берем из БД или MAIN_ADMIN_ID для первого старта

# Путь к файлу базы данных
DB_PATH = os.getenv("DB_PATH", "calls.db")

# SOCKS5 прокси
PROXY_URL = os.getenv("PROXY_URL", "")
