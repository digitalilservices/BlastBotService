import os
from dotenv import load_dotenv

load_dotenv()  # ✅ загрузит .env из текущей папки

BOT_TOKEN = os.getenv("BOT_TOKEN")

API_ID = os.getenv("API_ID")
API_ID = int(API_ID) if API_ID else None

API_HASH = os.getenv("API_HASH")

DIGIBOT_BASE_URL = os.getenv("DIGIBOT_BASE_URL")
DIGIBOT_INTERNAL_API_KEY = os.getenv("DIGIBOT_INTERNAL_API_KEY")

ADMIN_CHANNEL_ID = os.getenv("ADMIN_CHANNEL_ID")
ADMIN_CHANNEL_ID = int(ADMIN_CHANNEL_ID) if ADMIN_CHANNEL_ID else None





