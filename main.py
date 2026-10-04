import logging
import os
import threading

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from pyromod import listen

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
    try:
        from flask import Flask
        app = Flask(__name__)

        @app.route("/")
        def health():
            return "Bot running OK", 200

        app.run(host="0.0.0.0", port=port)
    except Exception as e:
        logging.info(f"Flask runner notice: {e}")
        import http.server
        import socketserver

        class HealthCheckHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Bot running OK")

            def log_message(self, format, *args):
                pass

        try:
            with socketserver.TCPServer(("", port), HealthCheckHandler) as httpd:
                httpd.serve_forever()
        except Exception as server_err:
            logging.error(f"Health server error: {server_err}")

threading.Thread(target=run_web, daemon=True).start()

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
