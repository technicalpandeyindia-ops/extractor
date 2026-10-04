import asyncio
import logging
import os
import threading

# Initialize asyncio event loop for Python 3.10+ / 3.12+ / 3.14+ compatibility
try:
    asyncio.get_event_loop()
except RuntimeError:
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from pyromod import listen
import pyromod.types.identifier as _pti

# Python 3.14 PEP 649 compatibility patch for pyromod Identifier
def _patched_matches(self, update: "_pti.Identifier") -> bool:
    fields = getattr(self, '__annotations__', None) or getattr(type(self), '__annotations__', None) or ['inline_message_id', 'chat_id', 'message_id', 'from_user_id']
    for field in fields:
        pattern_value = getattr(self, field)
        update_value = getattr(update, field)
        if pattern_value is not None:
            if isinstance(update_value, list):
                if isinstance(pattern_value, list):
                    if not set(update_value).intersection(set(pattern_value)):
                        return False
                elif pattern_value not in update_value:
                    return False
            elif isinstance(pattern_value, list):
                if update_value not in pattern_value:
                    return False
            elif update_value != pattern_value:
                return False
    return True

def _patched_count_populated(self):
    non_null_count = 0
    fields = getattr(self, '__annotations__', None) or getattr(type(self), '__annotations__', None) or ['inline_message_id', 'chat_id', 'message_id', 'from_user_id']
    for attr in fields:
        if getattr(self, attr) is not None:
            non_null_count += 1
    return non_null_count

_pti.Identifier.matches = _patched_matches
_pti.Identifier.count_populated = _patched_count_populated

from config import api_id, api_hash, bot_token
from helpers import is_authorized
from one import register_pwwp_handlers
from two import register_cpwp_handlers
from three import register_appxwp_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def run_web():
    port = int(os.environ.get("PORT", 8080))
    start_time = time.time()
    
    try:
        from flask import Flask, jsonify
        app = Flask(__name__)

        @app.route("/")
        def health():
            uptime_sec = int(time.time() - start_time)
            mins, secs = divmod(uptime_sec, 60)
            hrs, mins = divmod(mins, 60)
            days, hrs = divmod(hrs, 24)
            return jsonify({
                "status": "online",
                "service": "Zx Extractor Bot",
                "uptime": f"{days}d {hrs}h {mins}m {secs}s",
                "uptime_seconds": uptime_sec
            }), 200

        @app.route("/ping")
        def ping():
            return "pong", 200

        app.run(host="0.0.0.0", port=port)
    except Exception as e:
        logging.info(f"Flask runner notice: {e}")
        import http.server
        import socketserver

        class HealthCheckHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "online", "message": "Bot running OK"}')

            def log_message(self, format, *args):
                pass

        try:
            with socketserver.TCPServer(("", port), HealthCheckHandler) as httpd:
                httpd.serve_forever()
        except Exception as server_err:
            logging.error(f"Health server error: {server_err}")

def run_self_pinger():
    """Pings own service URL periodically to prevent Render / Railway free tier sleep."""
    import time
    import urllib.request
    
    # Wait for web server to spin up
    time.sleep(10)
    
    app_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("RAILWAY_STATIC_URL") or os.environ.get("APP_URL")
    if app_url:
        if not app_url.startswith("http"):
            app_url = f"https://{app_url}"
        logging.info(f"Uptime KeepAlive Self-Pinger active for: {app_url}")
        
        while True:
            try:
                req = urllib.request.Request(f"{app_url.rstrip('/')}/ping", headers={"User-Agent": "UptimeKeepAlive/1.0"})
                with urllib.request.urlopen(req, timeout=15) as res:
                    logging.info(f"Self-ping successful: HTTP {res.status}")
            except Exception as e:
                logging.warning(f"Self-ping notice: {e}")
            # Ping every 10 minutes (600s) before Render's 15-min idle sleep
            time.sleep(600)

threading.Thread(target=run_web, daemon=True).start()
threading.Thread(target=run_self_pinger, daemon=True).start()

bot = Client(
    "techvjbot",
    api_id=api_id,
    api_hash=api_hash,
    bot_token=bot_token
)

START_IMAGE = "https://files.catbox.moe/vg3vae.jpg"

START_CAPTION = (
    "<blockquote>\n"
    "╭━━━ ✦ <b>Zx Extractor</b> ✦ ━━━╮\n\n"
    "⚡ <b>Your Content. One Extractor.</b>\n"
    "🔐 <b>Smart • Reliable • Efficient</b>\n"
    "📚 <b>PW • Classplus • Appx</b>\n\n"
    "<i>Select a platform to begin...</i>\n"
    "╰━━━━━━━━━━━━━━━━━━━━╯\n"
    "</blockquote>"
)

START_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton(
            "👨‍💻 𝗗𝗲𝘃𝗲𝗹𝗼𝗽𝗲𝗿 🇮🇳",
            url="https://t.me/SumitTripathi"
        )
    ],
    [
        InlineKeyboardButton(
            "🚀 𝗣𝗵𝘆𝘀𝗶𝗰𝘀 𝗪𝗮𝗹𝗹𝗮𝗵 🚀",
            callback_data="pwwp"
        )
    ],
    [
        InlineKeyboardButton(
            "📘 𝗖𝗹𝗮𝘀𝘀𝗽𝗹𝘂𝘀 📘",
            callback_data="cpwp"
        )
    ],
    [
        InlineKeyboardButton(
            "📒 𝗔𝗽𝗽𝘅 📒",
            callback_data="appxwp"
        )
    ]
])

@bot.on_message(filters.command(["start"]))
async def start(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else 0
    if not is_authorized(user_id):
        await message.reply_text("⛔ **Access Denied:** You are not authorized to use this bot.")
        return
    try:
        await message.reply_photo(
            photo=START_IMAGE,
            caption=START_CAPTION,
            reply_markup=START_KEYBOARD,
            parse_mode=ParseMode.HTML
        )
    except Exception:
        await message.reply_text(
            text=START_CAPTION,
            reply_markup=START_KEYBOARD,
            parse_mode=ParseMode.HTML
        )

@bot.on_message(filters.command(["help"]))
async def help_cmd(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else 0
    if not is_authorized(user_id):
        await message.reply_text("⛔ **Access Denied.**")
        return
    await message.reply_text(
        "✦ <b>Zx Extractor</b>\n\n"
        "Use /start to open the extractor menu and select your provider.",
        parse_mode=ParseMode.HTML
    )

register_pwwp_handlers(bot)
register_cpwp_handlers(bot)
register_appxwp_handlers(bot)

if __name__ == "__main__":
    bot.run()
