# language: Python, file: config.py, target: Linux / Windows / Render
import os

API_ID = api_id = int(os.environ.get("API_ID", os.environ.get("api_id", 33466201)))
API_HASH = api_hash = os.environ.get("API_HASH", os.environ.get("api_hash", "c487cc22d111e6febcbf1e9c2b088e9a"))
BOT_TOKEN = bot_token = os.environ.get("BOT_TOKEN", os.environ.get("bot_token", "8623292536:AAGeZ7fa1jG4l9f08Nq7l_XNreyHn3RI6Zs"))

# List of authorized Telegram User IDs (comma-separated or default list)
_auth = os.environ.get("AUTH_USERS", os.environ.get("auth_users", "5324130644"))
if _auth:
    AUTH_USERS = auth_users = [int(u.strip()) for u in str(_auth).split(",") if u.strip().isdigit()]
else:
    AUTH_USERS = auth_users = [5324130644]
