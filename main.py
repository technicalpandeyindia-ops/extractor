import asyncio
import logging
import os
import sys
import threading
import time

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

import config
api_id = getattr(config, "api_id", getattr(config, "API_ID", 33466201))
api_hash = getattr(config, "api_hash", getattr(config, "API_HASH", "c487cc22d111e6febcbf1e9c2b088e9a"))
bot_token = getattr(config, "bot_token", getattr(config, "BOT_TOKEN", "8623292536:AAGeZ7fa1jG4l9f08Nq7l_XNreyHn3RI6Zs"))
from helpers import is_authorized
from one import register_pwwp_handlers
from two import register_cpwp_handlers
from three import register_appxwp_handlers
from downloader import process_batch_downloader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def run_web():
    port = int(os.environ.get("PORT", 8080))
    start_time = time.time()
    logging.info(f"Starting KeepAlive Web Server on 0.0.0.0:{port}...")
    
    try:
        from flask import Flask, jsonify
        app = Flask(__name__)

        # Disable werkzeug access logs to keep terminal clean
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)

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

        logging.info(f"Flask Web Server successfully bound to port {port}")
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    except Exception as e:
        logging.error(f"Flask runner error: {e}, falling back to builtin http.server")
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
            socketserver.TCPServer.allow_reuse_address = True
            with socketserver.TCPServer(("0.0.0.0", port), HealthCheckHandler) as httpd:
                logging.info(f"Built-in HTTP server listening on 0.0.0.0:{port}")
                httpd.serve_forever()
        except Exception as server_err:
            logging.error(f"Critical Web server error: {server_err}")

def run_self_pinger():
    """Pings own service URL periodically to prevent Render / Railway free tier sleep."""
    import urllib.request
    
    # Wait for web server to spin up
    time.sleep(15)
    
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
    in_memory=True,
    api_id=api_id,
    api_hash=api_hash,
    bot_token=bot_token
)

START_IMAGE = "https://files.catbox.moe/vg3vae.jpg"

START_CAPTION = (
    "<blockquote>\n"
    "╭━━━ ✦ <b>Zx Extractor & Downloader</b> ✦ ━━━╮\n\n"
    "⚡ <b>Your Content. One Platform.</b>\n"
    "🔐 <b>Smart • Reliable • Efficient</b>\n"
    "📚 <b>PW • Classplus • Appx • Downloader</b>\n\n"
    "<i>Select a provider or tool below to begin...</i>\n"
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
    ],
    [
        InlineKeyboardButton(
            "📥 𝗩𝗶𝗱𝗲𝗼 & 𝗣𝗗𝗙 𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱𝗲𝗿 (𝗔𝗽𝗽𝘅 𝗩𝟮) 📥",
            callback_data="dl_appx_v2"
        )
    ]
])

@bot.on_message(filters.command(["start"]))
async def start(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else 0
    logging.info(f"Incoming /start command from user_id: {user_id} (@{message.from_user.username if message.from_user else 'unknown'})")
    if not is_authorized(user_id):
        logging.warning(f"User {user_id} rejected: Not in authorized users list.")
        await message.reply_text(f"⛔ **Access Denied:** Your User ID (`{user_id}`) is not authorized to use this bot.")
        return
    try:
        await message.reply_photo(
            photo=START_IMAGE,
            caption=START_CAPTION,
            reply_markup=START_KEYBOARD,
            parse_mode=ParseMode.HTML
        )
    except Exception as e1:
        logging.warning(f"Failed to send start photo: {e1}, attempting text-only fallback...")
        try:
            await message.reply_text(
                text=START_CAPTION,
                reply_markup=START_KEYBOARD,
                parse_mode=ParseMode.HTML
            )
        except Exception as e2:
            logging.error(f"Failed to send HTML start message: {e2}, sending plain text...")
            await message.reply_text(
                text="⚡ **Zx Extractor & Downloader**\n\nSelect an option below to begin:",
                reply_markup=START_KEYBOARD
            )


@bot.on_message(filters.command(["help"]))
async def help_cmd(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else 0
    if not is_authorized(user_id):
        await message.reply_text("⛔ **Access Denied.**")
        return
    await message.reply_text(
        "✦ <b>Zx Extractor & Downloader</b>\n\n"
        "• Use /start to open the extractor and downloader menu.\n"
        "• Click <b>Video & PDF Downloader (Appx V2)</b> to download and batch-upload extracted links directly to your Telegram chat/channel.",
        parse_mode=ParseMode.HTML
    )

@bot.on_callback_query(filters.regex("^dl_appx_v2$"))
async def dl_appx_callback(client: Client, callback_query):
    user_id = callback_query.from_user.id if callback_query.from_user else 0
    if not is_authorized(user_id):
        await callback_query.answer("⛔ Access Denied! You are not authorized.", show_alert=True)
        return
    await callback_query.answer()
    asyncio.create_task(process_batch_downloader(client, callback_query.message, user_id))

register_pwwp_handlers(bot)
register_cpwp_handlers(bot)
register_appxwp_handlers(bot)

if __name__ == "__main__":
    bot.run()
