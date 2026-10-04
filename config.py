import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]
# Канал можна задати тут або командою /channel у боті
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Kyiv")
