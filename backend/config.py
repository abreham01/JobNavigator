"""Loads .env secrets only. All other config lives in the settings DB table."""
import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GMAIL_CLIENT_ID = os.getenv("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET", "")
GMAIL_REFRESH_TOKEN = os.getenv("GMAIL_REFRESH_TOKEN", "")

# Optimize to accept standard remote database URLs (like Neon)
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://jobnavigator:password@db:5432/jobnavigator")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

INITIAL_API_KEY = os.getenv("INITIAL_API_KEY", "change-me-on-first-login")

# Decoupled frontend configuration
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

# Decoupled cookie configuration
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "lax")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in ("true", "1", "yes")

