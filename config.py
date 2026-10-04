import os

api_id = int(os.environ.get("API_ID", 33466201))
api_hash = os.environ.get("API_HASH", "c487cc22d111e6febcbf1e9c2b088e9a")
bot_token = os.environ.get("BOT_TOKEN", "8623292536:AAGeZ7fa1jG4l9f08Nq7l_XNreyHn3RI6Zs")

# List of authorized Telegram User IDs (can be set via env var comma-separated, e.g. "5324130644,12345678")
_auth = os.environ.get("AUTH_USERS", "5324130644")
if _auth:
    auth_users = [int(u.strip()) for u in _auth.split(",") if u.strip().isdigit()]
else:
    auth_users = [5324130644]
