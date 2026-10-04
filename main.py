# language: Python, file: main.py, target: Linux / Windows / Render, Python 3.10-3.14
import asyncio
import logging
import os
import sys
import threading
import time

# Initialize asyncio event loop immediately before any pyrogram imports
try:
    loop = asyncio.get_event_loop()
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

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

from config import API_ID, API_HASH, BOT_TOKEN, auth_users
from helpers import is_authorized
from one import process_pw
from two import process_cp
from three import process_appxwp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("ExtBot")

bot = Client(
    "bot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True
)

active_tasks = {}

def start_health_server():
    """Runs a tiny standalone HTTP server in the background for Render/Railway 24/7 pings."""
    try:
        from flask import Flask
        app = Flask("HealthServer")

        @app.route("/")
        @app.route("/health")
        def health():
            return "Bot is running 24/7!", 200

        port = int(os.environ.get("PORT", 8080))
        threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False), daemon=True).start()
        logger.info(f"Health check server active on port {port}")
    except Exception as e:
        logger.warning(f"Failed to start health server: {e}")

@bot.on_message(filters.command(["start", "help"]) & filters.private)
async def start_handler(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else message.chat.id
    if not is_authorized(user_id):
        await message.reply_text("⛔ **Access Denied!** You are not authorized to use this bot.")
        return

    welcome_text = (
        "👋 **Welcome to All-In-One Course Extractor Bot!**\n\n"
        "⚡ **Supported Platforms:**\n"
        "1️⃣ **Physics Wallah (PW)** — Batches, Videos & Notes\n"
        "2️⃣ **Classplus (CP)** — Master OTP/Token/Sub Extraction\n"
        "3️⃣ **Appx / Akamai / Classx** — Credential/JWT Token Course Extractor\n\n"
        "👇 **Select a platform below to begin:**"
    )

    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣ Physics Wallah", callback_data="ext_pw")],
        [InlineKeyboardButton("2️⃣ Classplus", callback_data="ext_cp")],
        [InlineKeyboardButton("3️⃣ Appx / Akamai", callback_data="ext_appx")],
        [InlineKeyboardButton("❌ Cancel Active Process", callback_data="ext_cancel")]
    ])
    await message.reply_text(welcome_text, reply_markup=buttons)

@bot.on_message(filters.command("cancel") & filters.private)
async def cancel_handler(client: Client, message: Message):
    user_id = message.from_user.id if message.from_user else message.chat.id
    if user_id in active_tasks:
        task = active_tasks.pop(user_id)
        task.cancel()
        await message.reply_text("🛑 **Active extraction process cancelled successfully.**")
    else:
        await message.reply_text("ℹ️ **No active extraction process running.**")

@bot.on_callback_query()
async def callback_handler(client: Client, query):
    user_id = query.from_user.id
    if not is_authorized(user_id):
        await query.answer("⛔ Access Denied!", show_alert=True)
        return

    data = query.data

    if data == "ext_cancel":
        if user_id in active_tasks:
            task = active_tasks.pop(user_id)
            task.cancel()
            await query.message.edit_text("🛑 **Process cancelled.**")
        else:
            await query.answer("No active process to cancel.", show_alert=True)
        return

    if user_id in active_tasks and not active_tasks[user_id].done():
        await query.answer("⚠️ You already have an active extraction in progress! Send /cancel to restart.", show_alert=True)
        return

    await query.answer()

    if data == "ext_pw":
        task = asyncio.create_task(process_pw(client, query.message, user_id))
        active_tasks[user_id] = task
    elif data == "ext_cp":
        task = asyncio.create_task(process_cp(client, query.message, user_id))
        active_tasks[user_id] = task
    elif data == "ext_appx":
        task = asyncio.create_task(process_appxwp(client, query.message, user_id))
        active_tasks[user_id] = task

async def main():
    start_health_server()
    logger.info("Starting Telegram Bot...")
    await bot.start()
    me = await bot.get_me()
    logger.info(f"Bot started successfully as @{me.username} [{me.id}]")
    
    # Run forever
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
