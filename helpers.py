import asyncio
import logging
import re
from base64 import b64decode
from typing import Dict, Optional

try:
    asyncio.get_event_loop()
except RuntimeError:
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from pyrogram import Client, filters
from pyrogram.types import Message
from pyromod.exceptions import ListenerTimeout
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

try:
    from config import auth_users
except ImportError:
    auth_users = []

def is_authorized(user_id: int) -> bool:
    if not auth_users:
        return True
    return user_id in auth_users

async def ask_user(bot: Client, m: Message, editable: Message, text: str, user_id: int, timeout: int = 120) -> Optional[str]:
    try:
        await editable.edit(text)
    except Exception:
        pass
    try:
        msg = await bot.listen(chat_id=m.chat.id, filters=filters.user(user_id), timeout=timeout)
        val = (msg.text or "").strip()
        try:
            await msg.delete()
        except Exception:
            pass
        return val
    except ListenerTimeout:
        try:
            await editable.edit("**Timeout! You took too long to respond.**")
        except Exception:
            pass
        return None
    except Exception as e:
        logging.exception("Error during input listener:")
        try:
            await editable.edit(f"**Error:** `{e}`")
        except Exception:
            pass
        return None

def clean_appx_url(url: str) -> str:
    """Normalizes Appx / Classx CDN URLs, converts live stream IDs to permanent VOD playlist_eof, and strips expired signatures and query params."""
    if not url:
        return ""
    url = str(url).strip()
    
    # Strip ?bitrate= and trailing query parameters from .m3u8 playlists
    if ".m3u8?" in url:
        url = url.split("?")[0]

    # Handle static assets & PDF rewrites
    if "appx.co.in" in url and ("subject/" in url or "paid_course" in url or "image/" in url or url.endswith(".pdf")):
        url = re.sub(r"https?://[^/]+\.appx\.co\.in", "https://appx-content-v2.classx.co.in", url)
        if "?" in url and any(param in url for param in ("URLPrefix=", "Expires=", "KeyName=", "Signature=")):
            url = url.split("?")[0]
            
    # Handle vodclasses & live stream conversions
    # Convert ANY liveclasses / expired live link containing stream ID T_\d+ to permanent VOD playlist_eof
    if "liveclasses" in url or ("classx.co.in" in url and "/live/" in url):
        match = re.search(r'/(T_\d+|\d+)/', url) or re.search(r'(T_\d+)', url)
        if match:
            stream_id = match.group(1)
            url = f"https://vodclasses.classx.co.in/live/{stream_id}/playlist_eof.m3u8"
                
    if "vodclasses.classx.co.in" in url:
        # Strip query parameters from vodclasses playlist_eof for clean, permanent download/playback
        if "?" in url:
            url = url.split("?")[0]

    return url


def extract_url_from_video_details(item: Dict) -> str:
    if not isinstance(item, dict):
        return ""
    v_details = item.get("videoDetails") or {}
    if not isinstance(v_details, dict):
        v_details = {}
    url = (
        v_details.get("videoUrl") or v_details.get("embedCode") or v_details.get("mediaUrl") or
        v_details.get("streamUrl") or v_details.get("hlsUrl") or v_details.get("mpdUrl") or
        v_details.get("downloadUrl") or v_details.get("url") or v_details.get("fileUrl") or
        item.get("videoUrl") or item.get("mediaUrl") or item.get("streamUrl") or
        item.get("hlsUrl") or item.get("mpdUrl") or item.get("url") or ""
    )
    if url and ("<" in url or "iframe" in url.lower() or "src=" in url.lower()):
        match = re.search(r'src=["\'](https?://[^"\']+)["\']', url)
        if match:
            url = match.group(1)
        else:
            match_any = re.search(r'https?://[^"\'\s<>]+', url)
            url = match_any.group(0) if match_any else url
    return clean_appx_url(url)

def appx_decrypt(enc: str) -> str:
    if not enc:
        return ""
    try:
        cleaned_enc = enc.split(":")[0]
        enc_bytes = b64decode(cleaned_enc)
        if not enc_bytes or len(enc_bytes) % 16 != 0:
            return clean_appx_url(enc)
        key = b"638udh3829162018"
        iv = b"fedcba9876543210"
        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(enc_bytes)
        dec_str = unpad(decrypted, AES.block_size).decode("utf-8")
        return clean_appx_url(dec_str)
    except Exception:
        return clean_appx_url(enc)

async def send_extracted_text_file(bot: Client, m: Message, editable: Message, file_path: str, app_name: str, batch_name: str, total_links: int, elapsed_time: str):
    try:
        await editable.delete()
    except Exception:
        pass
    caption = (
        f"🎯 **Extraction Completed Successfully!**\n\n"
        f"📌 **App:** `{app_name}`\n"
        f"📂 **Batch:** `{batch_name}`\n"
        f"🔗 **Total Links:** `{total_links}`\n"
        f"⏱️ **Time Taken:** `{elapsed_time}`\n\n"
        f"<blockquote>Use Downloader to download videos & notes directly.</blockquote>"
    )
    await bot.send_document(
        chat_id=m.chat.id,
        document=file_path,
        caption=caption
    )

