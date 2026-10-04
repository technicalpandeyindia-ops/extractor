import logging
import re
from base64 import b64decode
from typing import Dict, Optional
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from pyrogram import Client, filters
from pyrogram.types import Message
from pyromod.exceptions import ListenerTimeout

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
    """Normalizes Appx / Classx CDN URLs and strips expired CloudFront signatures."""
    if not url:
        return ""
    url = str(url).strip()
    if "appx.co.in" in url:
        url = re.sub(r"https?://[^/]+\.appx\.co\.in", "https://appx-content-v2.classx.co.in", url)
        if "?" in url and any(param in url for param in ("URLPrefix=", "Expires=", "KeyName=", "Signature=")):
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
