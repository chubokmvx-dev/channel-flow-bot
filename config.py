import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]
# Канал можна задати тут або командою /channel у боті
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Kyiv")

# Публикация от имени твоего аккаунта (нужна, чтобы работали премиум-эмодзи в канале). Необязательно.
TG_API_ID = int(os.getenv("TG_API_ID", "0") or 0)
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_SESSION = os.getenv("TG_SESSION", "")
