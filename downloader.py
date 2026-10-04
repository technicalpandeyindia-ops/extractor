import asyncio
import hashlib
import logging
import os
import re
import shutil
import subprocess
import time
from typing import List, Tuple
from urllib.parse import quote, urlparse

try:
    asyncio.get_event_loop()
except RuntimeError:
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

import aiohttp
from pyrogram import Client, filters
from pyrogram.types import Message
from pyromod.exceptions import ListenerTimeout

from helpers import ask_user, clean_appx_url, is_authorized

logger = logging.getLogger("Downloader")

APPX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
    "Referer": "https://appx-play.akamai.net.in/",
    "Origin": "https://appx-play.akamai.net.in",
}

class ProcessCancelledException(Exception):
    pass

def format_bytes(size: float) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"

def format_time(seconds: float) -> str:
    seconds = int(seconds)
    mins, secs = divmod(seconds, 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs:02d}h {mins:02d}m {secs:02d}s"
    return f"{mins:02d}m {secs:02d}s"

async def prompt_user(bot: Client, message: Message, editable: Message, text: str, user_id: int) -> str:
    cancel_notice = "\n\n<blockquote>❌ **Send `/cancel` at any time to abort.**</blockquote>"
    response = await ask_user(bot, message, editable, text + cancel_notice, user_id)
    if response is None or response.strip().lower() == "/cancel":
        await editable.edit("**Process Cancelled by User ❌**")
        raise ProcessCancelledException("User requested cancellation.")
    return response.strip()

async def get_user_input_or_doc(bot: Client, m: Message, editable: Message, text: str, user_id: int, temp_dir: str) -> str:
    cancel_notice = "\n\n<blockquote>❌ **Send `/cancel` at any time to abort.**</blockquote>"
    try:
        await editable.edit(text + cancel_notice)
    except Exception:
        pass
    try:
        msg: Message = await bot.listen(chat_id=m.chat.id, filters=filters.user(user_id), timeout=300)
    except ListenerTimeout:
        try:
            await editable.edit("**Timeout! You took too long to send links or file.**")
        except Exception:
            pass
        raise ProcessCancelledException("Timeout.")
    except Exception as e:
        logger.error(f"Listener error: {e}")
        raise ProcessCancelledException(str(e))

    if not msg:
        raise ProcessCancelledException("No message received.")

    if msg.document:
        await editable.edit("📂 **Downloading and parsing uploaded document... ⏳**")
        file_path = os.path.join(temp_dir, msg.document.file_name or "extracted.txt")
        try:
            downloaded_file = await msg.download(file_name=file_path)
            with open(downloaded_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            try:
                await msg.delete()
            except Exception:
                pass
            return content
        except Exception as e:
            logger.error(f"Failed to download/read document: {e}")
            raise ProcessCancelledException(f"Failed to read document: {e}")

    val = (msg.text or msg.caption or "").strip()
    try:
        await msg.delete()
    except Exception:
        pass

    if val.lower() == "/cancel":
        await editable.edit("**Process Cancelled by User ❌**")
        raise ProcessCancelledException("Cancelled by user.")

    return val

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', '', name).strip()
    return name[:120] if len(name) > 120 else name

def extract_content_id(url: str) -> str:
    if 'contentId=' in url:
        parts = url.split('contentId=')
        if len(parts) > 1:
            cid = parts[1]
            for char in ['?', '&']:
                if char in cid:
                    cid = cid.split(char)[0]
            if cid.endswith('.m3u8'):
                cid = cid[:-5]
            return cid
    return ""

# ─── URL type detection ────────────────────────────────────────────────────────
# VIDEO signal patterns — URL substrings that mean "this is a video stream"
_VIDEO_URL_SIGNALS = [
    ".m3u8", ".mp4", ".ts", ".mkv",
    "transcoded-videos", "transcoded_videos",
    "vodclasses", "liveclasses",
    "classx.co.in/live", "classx.co.in/vod",
    "appx-play.akamai", "akamaized.net", "akamai.net",
    "hls/", "/hls", "playlist_eof",
    "cloudfront.net",
    "youtube.com/watch", "youtu.be",
    "/video/", "/videos/", "video_url",
    "stream", "media/",
    "jwplatform", "jwplayer",
    "brightcove",
]

# PDF signal patterns — these OVERRIDE video signals
_PDF_URL_SIGNALS = [
    ".pdf",
]

# Title signals that force PDF type
_PDF_TITLE_SIGNALS = ["(pdf)", "(note)", "(notes)", "(dpp)", "(ncert)"]

def _classify_type(title: str, url: str, enc_key: str) -> str:
    """
    Classify a link as VIDEO, PDF, or FILE.
    Rule order (highest priority first):
      1. Title explicitly says (PDF)/(Notes) → PDF
      2. URL ends with .pdf → PDF
      3. URL contains any VIDEO signal → VIDEO
      4. enc_key present + no video signal → PDF (encrypted PDF pattern)
      5. /paid_course/ or /subject/ in URL alone is NOT enough for PDF —
         those paths host both videos and PDFs; fall through to FILE
         which the downloader will attempt as generic HTTP.
    """
    url_lower = url.lower()
    title_lower = title.lower()

    # 1. Title says PDF
    if any(s in title_lower for s in _PDF_TITLE_SIGNALS):
        return "PDF"

    # 2. URL ends with .pdf
    if url_lower.split("?")[0].endswith(".pdf"):
        return "PDF"

    # 3. URL contains explicit .pdf anywhere
    if ".pdf" in url_lower:
        return "PDF"

    # 4. URL has a video signal
    if any(sig in url_lower for sig in _VIDEO_URL_SIGNALS):
        return "VIDEO"

    # 5. enc_key present → encrypted PDF
    if enc_key:
        return "PDF"

    # 6. Default — let downloader try HTTP
    return "FILE"

# ─── JW signed URL ─────────────────────────────────────────────────────────────
async def get_jw_signed_url(content_id: str, access_token: str = "") -> str:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://web.classplusapp.com",
        "Referer": "https://web.classplusapp.com/",
        "User-Agent": "Mozilla/5.0",
    }
    if access_token:
        headers["X-Access-Token"] = access_token

    content_api = f"https://api.classplusapp.com/cams/uploader/video/jw-signed-url?contentId={quote(content_id, safe='')}"
    try:
        conn = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(headers=headers, connector=conn) as session:
            async with session.get(content_api, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    data = await r.json()
                    signed = data.get("url")
                    if signed:
                        return signed
            live_api = f"https://api.classplusapp.com/cams/uploader/video/jw-signed-url?liveSessionId={quote(content_id, safe='')}&isAgora=2"
            async with session.get(live_api, timeout=aiohttp.ClientTimeout(total=15)) as r2:
                if r2.status == 200:
                    data = await r2.json()
                    return data.get("url", "")
    except Exception as e:
        logger.warning(f"Error fetching JW signed URL: {e}")
    return ""

# ─── HTTP file download ─────────────────────────────────────────────────────────
async def download_file_http(url: str, output_path: str, editable: Message, title: str) -> bool:
    try:
        conn = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(headers=APPX_HEADERS, connector=conn) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                if resp.status != 200:
                    logger.warning(f"HTTP {resp.status} downloading: {url}")
                    return False

                total_size = int(resp.headers.get('content-length', 0))
                downloaded = 0
                start_time = time.time()
                last_update = 0

                with open(output_path, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(1024 * 64):
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        if now - last_update > 4:
                            last_update = now
                            pct = (downloaded / total_size * 100) if total_size > 0 else 0
                            speed = downloaded / (now - start_time + 0.001)
                            try:
                                await editable.edit(
                                    f"📥 **Downloading File:** `{title[:40]}`\n\n"
                                    f"📊 **Progress:** `{pct:.1f}%` ({format_bytes(downloaded)} / {format_bytes(total_size) if total_size else '?'})\n"
                                    f"⚡ **Speed:** `{format_bytes(speed)}/s`\n\n"
                                    f"<blockquote>❌ Send `/cancel` to abort.</blockquote>"
                                )
                            except Exception:
                                pass
                return os.path.exists(output_path) and os.path.getsize(output_path) > 100
    except Exception as e:
        logger.error(f"Download HTTP error: {e}")
        return False

# ─── Encrypted PDF ──────────────────────────────────────────────────────────────
async def download_appx_encrypted_pdf(url: str, output_path: str, enc_key: str) -> bool:
    temp_raw = f"{output_path}.raw"
    try:
        conn = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(headers=APPX_HEADERS, connector=conn) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status != 200:
                    return False
                with open(temp_raw, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(1024 * 64):
                        f.write(chunk)

        if not os.path.exists(temp_raw) or os.path.getsize(temp_raw) < 32:
            if os.path.exists(temp_raw): os.remove(temp_raw)
            return False

        with open(temp_raw, 'rb') as f:
            header = f.read(4)
            if header == b'%PDF':
                shutil.move(temp_raw, output_path)
                return True

        with open(temp_raw, 'rb') as f:
            file_bytes = f.read()

        iv_bytes = file_bytes[:16]
        ciphertext = file_bytes[16:]
        key_bytes = enc_key.encode('utf-8')
        candidates = [
            key_bytes[:16].ljust(16, b'\x00'),
            hashlib.md5(key_bytes).digest(),
            hashlib.sha256(key_bytes).digest()[:16]
        ]

        try:
            from Crypto.Cipher import AES
            for cand in candidates:
                for current_iv in [iv_bytes, b'\x00' * 16]:
                    cipher = AES.new(cand, AES.MODE_CBC, current_iv)
                    decrypted = cipher.decrypt(ciphertext)
                    if decrypted.startswith(b'%PDF'):
                        pad_len = decrypted[-1]
                        if 1 <= pad_len <= 16:
                            decrypted = decrypted[:-pad_len]
                        with open(output_path, 'wb') as out_f:
                            out_f.write(decrypted)
                        if os.path.exists(temp_raw): os.remove(temp_raw)
                        return True
        except ImportError:
            pass

        for cand in candidates:
            hex_key = cand.hex()
            hex_iv = iv_bytes.hex()
            temp_plain = f"{output_path}.plain"
            temp_cipher = f"{output_path}.cipher"
            with open(temp_cipher, 'wb') as cf:
                cf.write(ciphertext)
            cmd = ["openssl", "aes-128-cbc", "-d", "-K", hex_key, "-iv", hex_iv, "-in", temp_cipher, "-out", temp_plain]
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if os.path.exists(temp_plain):
                with open(temp_plain, 'rb') as pf:
                    if pf.read(4) == b'%PDF':
                        shutil.move(temp_plain, output_path)
                        if os.path.exists(temp_cipher): os.remove(temp_cipher)
                        if os.path.exists(temp_raw): os.remove(temp_raw)
                        return True
                if os.path.exists(temp_plain): os.remove(temp_plain)
            if os.path.exists(temp_cipher): os.remove(temp_cipher)

        if os.path.exists(temp_raw): os.remove(temp_raw)
        return False
    except Exception as e:
        logger.error(f"Encrypted PDF decryption error: {e}")
        if os.path.exists(temp_raw): os.remove(temp_raw)
        return False

# ─── Pure Python HLS downloader ────────────────────────────────────────────────
async def download_hls_stream_pure_python(m3u8_url: str, output_path: str, editable: Message, title: str) -> bool:
    import urllib.parse
    conn = aiohttp.TCPConnector(ssl=False)
    try:
        async with aiohttp.ClientSession(connector=conn, headers=APPX_HEADERS) as session:
            async with session.get(m3u8_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    logger.warning(f"HLS master fetch HTTP {resp.status}: {m3u8_url}")
                    return False
                m3u8_content = await resp.text()

            lines = [l.strip() for l in m3u8_content.splitlines() if l.strip()]
            variant_playlists = [l for l in lines if not l.startswith("#") and ".m3u8" in l]
            if variant_playlists:
                target_variant = variant_playlists[-1]  # default: last = highest bitrate
                for v in variant_playlists:
                    if "1080" in v:
                        target_variant = v
                        break
                    elif "720" in v:
                        target_variant = v
                    elif "480" in v and "720" not in target_variant and "1080" not in target_variant:
                        target_variant = v

                sub_url = urllib.parse.urljoin(m3u8_url, target_variant)
                async with session.get(sub_url, timeout=aiohttp.ClientTimeout(total=20)) as sub_resp:
                    if sub_resp.status == 200:
                        m3u8_content = await sub_resp.text()
                        m3u8_url = sub_url

            segments = []
            for line in m3u8_content.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    seg_url = urllib.parse.urljoin(m3u8_url, line)
                    segments.append(seg_url)

            if not segments:
                logger.warning(f"No segments found in HLS playlist: {m3u8_url}")
                return False

            total_segs = len(segments)
            start_time = time.time()
            last_update = 0

            with open(output_path, "wb") as out_file:
                for idx, seg_url in enumerate(segments, 1):
                    seg_ok = False
                    for attempt in range(4):
                        try:
                            async with session.get(seg_url, timeout=aiohttp.ClientTimeout(total=45)) as seg_resp:
                                if seg_resp.status == 200:
                                    chunk = await seg_resp.read()
                                    out_file.write(chunk)
                                    seg_ok = True
                                    break
                                else:
                                    logger.warning(f"Segment {idx} HTTP {seg_resp.status}, retry {attempt+1}")
                        except Exception as e:
                            logger.warning(f"Segment {idx} error (attempt {attempt+1}): {e}")
                            await asyncio.sleep(1 * (attempt + 1))

                    if not seg_ok:
                        logger.error(f"Segment {idx} failed after 4 attempts, aborting")
                        return False

                    now = time.time()
                    if now - last_update > 4 or idx == total_segs:
                        last_update = now
                        pct = (idx / total_segs) * 100
                        try:
                            await editable.edit(
                                f"📥 **Downloading Video:** `{title[:40]}`\n\n"
                                f"📊 **Segments:** `[{idx}/{total_segs}]` ({pct:.1f}%)\n"
                                f"⏱️ **Elapsed:** `{format_time(now - start_time)}`\n\n"
                                f"<blockquote>❌ Send `/cancel` to abort.</blockquote>"
                            )
                        except Exception:
                            pass

            return os.path.exists(output_path) and os.path.getsize(output_path) > 1000
    except Exception as e:
        logger.error(f"Pure Python HLS downloader error: {e}")
        return False

# ─── Main video stream downloader ──────────────────────────────────────────────
async def download_video_stream(url: str, output_path: str, editable: Message, title: str) -> bool:
    """
    Download any video URL: HLS/M3U8, MP4, or generic stream.
    Strategy: Pure-Python HLS → yt-dlp → ffmpeg → direct HTTP.
    Logs every failure so silent skips never happen.
    """
    clean_url = clean_appx_url(url)

    # Classplus JW contentId resolution
    if "contentId=" in clean_url:
        cid = extract_content_id(clean_url)
        if cid:
            jw_url = await get_jw_signed_url(cid)
            if jw_url:
                logger.info(f"Resolved JW signed URL for contentId={cid}")
                clean_url = jw_url

    # Build candidate URL list: original + stripped query + quality variants
    candidates = [clean_url]
    if "?" in clean_url:
        candidates.append(clean_url.split("?")[0])
    if "/480p/" in clean_url:
        candidates.append(clean_url.replace("/480p/", "/720p/"))
        candidates.append(clean_url.replace("/480p/", "/1080p/"))
    if "/360p/" in clean_url:
        candidates.append(clean_url.replace("/360p/", "/720p/"))
    if "transcoded-videos-v2" in clean_url:
        candidates.append(clean_url.replace("transcoded-videos-v2", "transcoded-videos"))
    # De-duplicate preserving order
    seen = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    for target_url in candidates:
        logger.info(f"Trying video URL: {target_url}")

        # ── 1. Pure Python HLS (fastest, no binaries) ──────────────────────
        if ".m3u8" in target_url or "playlist_eof" in target_url:
            ok = await download_hls_stream_pure_python(target_url, output_path, editable, title)
            if ok:
                logger.info(f"HLS pure-python success: {target_url}")
                return True
            logger.warning(f"HLS pure-python failed: {target_url}")

        # ── 2. yt-dlp ──────────────────────────────────────────────────────
        try:
            ytdlp_base = output_path.replace(".mp4", "")
            ytdlp_out_tmpl = f"{ytdlp_base}.%(ext)s"
            cmd = [
                "yt-dlp",
                "--no-warnings",
                "--no-check-certificates",
                "--concurrent-fragments", "8",
                "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
                "--merge-output-format", "mp4",
                "--add-header", f"User-Agent:{APPX_HEADERS['User-Agent']}",
                "--add-header", f"Referer:{APPX_HEADERS['Referer']}",
                "--add-header", f"Origin:{APPX_HEADERS['Origin']}",
                "-o", ytdlp_out_tmpl,
                target_url
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if stderr:
                logger.warning(f"yt-dlp stderr: {stderr.decode(errors='ignore')[-400:]}")
            for ext in ["mp4", "mkv", "webm", "ts"]:
                candidate = f"{ytdlp_base}.{ext}"
                if os.path.exists(candidate) and os.path.getsize(candidate) > 1000:
                    if candidate != output_path:
                        os.rename(candidate, output_path)
                    logger.info(f"yt-dlp success: {target_url}")
                    return True
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                return True
            logger.warning(f"yt-dlp produced no output for: {target_url}")
        except FileNotFoundError:
            logger.warning("yt-dlp not found, skipping")
        except Exception as e:
            logger.error(f"yt-dlp exception: {e}")

        # ── 3. ffmpeg ───────────────────────────────────────────────────────
        try:
            cmd_ffmpeg = [
                "ffmpeg", "-y",
                "-loglevel", "warning",
                "-user_agent", APPX_HEADERS["User-Agent"],
                "-headers", f"Referer: {APPX_HEADERS['Referer']}\r\nOrigin: {APPX_HEADERS['Origin']}\r\n",
                "-reconnect", "1",
                "-reconnect_at_eof", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "10",
                "-timeout", "30000000",
                "-i", target_url,
                "-c", "copy",
                "-movflags", "+faststart",
                "-bsf:a", "aac_adtstoasc",
                output_path
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd_ffmpeg,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await proc.communicate()
            if stderr:
                logger.warning(f"ffmpeg stderr: {stderr.decode(errors='ignore')[-400:]}")
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                logger.info(f"ffmpeg success: {target_url}")
                return True
            logger.warning(f"ffmpeg produced no output for: {target_url}")
        except FileNotFoundError:
            logger.warning("ffmpeg not found, skipping")
        except Exception as e:
            logger.error(f"ffmpeg exception: {e}")

        # ── 4. Direct HTTP (MP4 / generic) ─────────────────────────────────
        base_check = target_url.split('?')[0].lower()
        if any(ext in base_check for ext in [".mp4", ".mkv", ".ts", ".webm"]):
            ok = await download_file_http(target_url, output_path, editable, title)
            if ok:
                logger.info(f"Direct HTTP success: {target_url}")
                return True
            logger.warning(f"Direct HTTP failed: {target_url}")

    # Last resort: try direct HTTP on the original clean URL regardless of extension
    logger.warning(f"All engines failed, last-resort HTTP: {clean_url}")
    ok = await download_file_http(clean_url, output_path, editable, title)
    if ok:
        return True

    logger.error(f"COMPLETE FAIL for video: {url}")
    return False

# ─── Link parser ───────────────────────────────────────────────────────────────
def parse_link_lines(raw_text: str) -> List[Tuple[str, str, str, str]]:
    """
    Parses lines of format:
    'Title (PDF):https://...' or 'Title:https://...*KEY' or 'Title:https://...'
    Returns list of (clean_title, url, file_type, enc_key).
    """
    items = []
    for line in raw_text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        enc_key = ""
        # Extract encryption key after *
        if "*" in line and ("http://" in line or "https://" in line):
            star_pos = line.rfind("*")
            url_start = line.find("http")
            if star_pos > url_start:
                enc_key = line[star_pos + 1:].strip()
                line = line[:star_pos].strip()

        if "://" not in line:
            continue

        http_pos = line.find("https://")
        if http_pos == -1:
            http_pos = line.find("http://")
        if http_pos <= 0:
            continue

        title = line[:http_pos].rstrip(": \t-|>").strip()
        url = line[http_pos:].strip()
        if not url:
            continue

        url = clean_appx_url(url)
        file_type = _classify_type(title, url, enc_key)
        items.append((title or "File", url, file_type, enc_key))

    return items

# ─── Batch downloader ───────────────────────────────────────────────────────────
async def process_batch_downloader(bot: Client, m: Message, user_id: int):
    editable = await m.reply_text("**Wait initializing Appx V2 Downloader... ⏳**")
    temp_dir = f"dl_temp_{user_id}_{int(time.time())}"
    os.makedirs(temp_dir, exist_ok=True)

    try:
        prompt_text = (
            "🚀 **Appx V2 Video & PDF Downloader**\n\n"
            "📥 **Send the extracted links:**\n"
            "• You can **upload a `.txt` file** containing the links.\n"
            "• Or **paste links directly** here.\n\n"
            "*(Format: `Title (PDF):URL` or `Title:URL*KEY` or `Title:URL`)*"
        )
        raw_text = await get_user_input_or_doc(bot, m, editable, prompt_text, user_id, temp_dir)

        items = parse_link_lines(raw_text)
        if not items:
            await editable.edit("**No valid download links found in input! ❌**\nMake sure the file or text contains lines with URLs.")
            return

        # Show type breakdown so user can verify detection
        v_count = sum(1 for _, _, t, _ in items if t == "VIDEO")
        p_count = sum(1 for _, _, t, _ in items if t == "PDF")
        f_count = sum(1 for _, _, t, _ in items if t == "FILE")

        dest_prompt = (
            f"✅ **Found `{len(items)}` items:**\n"
            f"🎥 Videos: `{v_count}` | 📄 PDFs: `{p_count}` | 📁 Files: `{f_count}`\n\n"
            "📤 **Where to upload?**\n"
            "• Send `0` or `me` → this private chat\n"
            "• Or Channel/Group ID (e.g. `-1001234567890`)"
        )
        dest_choice = await prompt_user(bot, m, editable, dest_prompt, user_id)

        target_chat_id = m.chat.id
        if dest_choice not in ["0", "me", "here", ""]:
            try:
                target_chat_id = int(dest_choice)
            except ValueError:
                target_chat_id = dest_choice

        total = len(items)
        success_count = 0
        fail_count = 0
        start_all_time = time.time()

        for idx, (title, url, ftype, enc_key) in enumerate(items, 1):
            clean_title = sanitize_filename(title)
            try:
                await editable.edit(
                    f"⚙️ **Processing [{idx}/{total}]:**\n\n"
                    f"📌 **Title:** `{title[:60]}`\n"
                    f"📂 **Type:** `{ftype}`{' (Encrypted)' if enc_key else ''}\n"
                    f"🔗 **URL:** `{url[:60]}...`\n"
                    f"📊 **Overall:** `{(idx-1)/total*100:.1f}%` ({idx-1}/{total})\n\n"
                    f"<blockquote>❌ Send `/cancel` to stop.</blockquote>"
                )
            except Exception:
                pass

            # ── PDF ─────────────────────────────────────────────────────────
            if ftype == "PDF":
                pdf_path = os.path.join(temp_dir, f"{clean_title}.pdf")
                if enc_key:
                    ok = await download_appx_encrypted_pdf(url, pdf_path, enc_key)
                else:
                    ok = await download_file_http(url, pdf_path, editable, title)

                if ok and os.path.exists(pdf_path):
                    try:
                        await editable.edit(f"📤 **Uploading PDF:** `{clean_title}.pdf`...")
                    except Exception:
                        pass
                    try:
                        await bot.send_document(
                            chat_id=target_chat_id,
                            document=pdf_path,
                            caption=f"📄 **{title}**",
                            file_name=f"{clean_title}.pdf"
                        )
                        success_count += 1
                    except Exception as e:
                        logger.error(f"Upload PDF failed: {e}")
                        fail_count += 1
                    finally:
                        if os.path.exists(pdf_path):
                            try: os.remove(pdf_path)
                            except: pass
                else:
                    fail_count += 1
                    try:
                        await editable.edit(f"⚠️ **PDF Failed:** `{title[:50]}`\n`URL: {url[:80]}`\n*Moving to next...*")
                        await asyncio.sleep(1.5)
                    except Exception:
                        pass

            # ── VIDEO ────────────────────────────────────────────────────────
            elif ftype == "VIDEO":
                vid_path = os.path.join(temp_dir, f"{clean_title}.mp4")
                ok = await download_video_stream(url, vid_path, editable, title)

                if ok and os.path.exists(vid_path):
                    try:
                        await editable.edit(f"📤 **Uploading Video:** `{clean_title}.mp4`...")
                    except Exception:
                        pass
                    try:
                        await bot.send_video(
                            chat_id=target_chat_id,
                            video=vid_path,
                            caption=f"🎥 **{title}**",
                            file_name=f"{clean_title}.mp4",
                            supports_streaming=True
                        )
                        success_count += 1
                    except Exception as e:
                        logger.error(f"Upload video failed: {e}")
                        fail_count += 1
                    finally:
                        if os.path.exists(vid_path):
                            try: os.remove(vid_path)
                            except: pass
                else:
                    fail_count += 1
                    try:
                        await editable.edit(
                            f"⚠️ **Video Failed:** `{title[:50]}`\n"
                            f"`URL: {url[:80]}`\n"
                            f"*Check logs — all 4 engines tried. Moving to next...*"
                        )
                        await asyncio.sleep(1.5)
                    except Exception:
                        pass

            # ── FILE (generic) ───────────────────────────────────────────────
            else:
                gen_path = os.path.join(temp_dir, f"{clean_title}.bin")
                ok = await download_file_http(url, gen_path, editable, title)
                if ok and os.path.exists(gen_path):
                    try:
                        await bot.send_document(chat_id=target_chat_id, document=gen_path, caption=f"📁 **{title}**")
                        success_count += 1
                    except Exception:
                        fail_count += 1
                    finally:
                        if os.path.exists(gen_path):
                            try: os.remove(gen_path)
                            except: pass
                else:
                    fail_count += 1

        total_time = format_time(time.time() - start_all_time)
        final_msg = (
            f"🎉 **Batch Complete!**\n\n"
            f"✅ **Uploaded:** `{success_count}` items\n"
            f"⚠️ **Failed:** `{fail_count}` items\n"
            f"⏱️ **Total Time:** `{total_time}`\n\n"
            f"<blockquote>All active files sent to destination.</blockquote>"
        )
        try:
            await editable.edit(final_msg)
        except Exception:
            pass

    except ProcessCancelledException:
        pass
    except Exception as e:
        logger.exception("Error during batch download:")
        if editable:
            try:
                await editable.edit(f"**Batch Downloader Error:** `{e}`")
            except Exception:
                pass
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
