import os

# All credentials MUST be set via environment variables.
# Never hardcode tokens or API keys here.
API_ID = api_id = int(os.environ.get("API_ID", os.environ.get("api_id", 0)))
API_HASH = api_hash = os.environ.get("API_HASH", os.environ.get("api_hash", ""))
BOT_TOKEN = bot_token = os.environ.get("BOT_TOKEN", os.environ.get("bot_token", ""))

if not all([API_ID, API_HASH, BOT_TOKEN]):
    import logging
    logging.warning(
        "MISSING CREDENTIALS: Set API_ID, API_HASH, BOT_TOKEN environment variables "
        "before running the bot."
    )

# List of authorized Telegram User IDs (comma-separated in env)
_auth = os.environ.get("AUTH_USERS", os.environ.get("auth_users", ""))
if _auth:
    AUTH_USERS = auth_users = [int(u.strip()) for u in str(_auth).split(",") if u.strip().isdigit()]
else:
    AUTH_USERS = auth_users = []
