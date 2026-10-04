import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from typing import List, Tuple

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
    """Listens for either text or uploaded document file from user."""
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

    # 1. If user sent a document (.txt)
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

    # 2. If user sent text or caption
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

async def download_file_http(url: str, output_path: str, editable: Message, title: str) -> bool:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        conn = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(headers=headers, connector=conn) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                if resp.status != 200:
                    logger.warning(f"HTTP {resp.status} downloading {url}")
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
                return True
    except Exception as e:
        logger.error(f"Download HTTP error: {e}")
        return False

async def download_video_stream(url: str, output_path: str, editable: Message, title: str) -> bool:
    """Downloads HLS/M3U8 or MP4 using yt-dlp / ffmpeg / direct stream with candidate fallbacks."""
    clean_url = clean_appx_url(url)
    
    candidates = [clean_url]
    if "?" in clean_url:
        candidates.append(clean_url.split("?")[0])
    if "/480p/" in clean_url:
        candidates.append(clean_url.replace("/480p/", "/720p/"))
    if "transcoded-videos-v2" in clean_url:
        candidates.append(clean_url.replace("transcoded-videos-v2", "transcoded-videos"))
        
    for target_url in candidates:
        # 1. Try yt-dlp
        try:
            try:
                await editable.edit(f"📥 **Downloading Video:** `{title[:40]}`\n⏳ *Fetching stream...*")
            except Exception:
                pass
            
            cmd = [
                "yt-dlp",
                "--no-warnings",
                "--no-check-certificates",
                "--concurrent-fragments", "8",
                "--add-header", "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "-o", output_path,
                target_url
            ]
            
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            await proc.communicate()
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                return True
        except Exception as e:
            logger.warning(f"yt-dlp attempt failed: {e}")

        # 2. Try ffmpeg
        try:
            cmd_ffmpeg = [
                "ffmpeg", "-y",
                "-headers", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n",
                "-reconnect", "1",
                "-reconnect_at_eof", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-i", target_url,
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                output_path
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd_ffmpeg,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                return True
        except Exception as e:
            logger.warning(f"ffmpeg attempt failed: {e}")

    # Fallback to direct HTTP download if it's an MP4
    if ".mp4" in clean_url or not clean_url.endswith(".m3u8"):
        return await download_file_http(clean_url, output_path, editable, title)
        
    return False

def parse_link_lines(raw_text: str) -> List[Tuple[str, str, str]]:
    """
    Parses lines of format:
    'Title (PDF):https://...' or 'Title:https://...' or 'Title : https://...*KEY'
    Returns list of tuples: (clean_title, url, file_type) where file_type in ['PDF', 'VIDEO', 'FILE']
    """
    items = []
    lines = raw_text.strip().splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        
        # Strip encryption key if present (*key)
        if "*" in line and ("http://" in line or "https://" in line):
            parts = line.split("*")
            line = parts[0].strip()

        # Match Title:URL
        if "://" in line:
            http_pos = line.find("http://")
            if http_pos == -1:
                http_pos = line.find("https://")
            
            if http_pos > 0:
                title = line[:http_pos].rstrip(": \t-").strip()
                url = line[http_pos:].strip()
                
                # Determine type
                if "(PDF)" in title.upper() or url.lower().endswith(".pdf") or "/subject/" in url or "/paid_course" in url:
                    file_type = "PDF"
                elif any(ext in url.lower() for ext in [".m3u8", ".mp4", "transcoded-videos", "vodclasses", "liveclasses", "hls", "/videos/"]):
                    file_type = "VIDEO"
                else:
                    file_type = "FILE"
                    
                items.append((title or "File", clean_appx_url(url), file_type))
    return items

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
            "*(Format: `Title (PDF):URL` or `Title:URL`)*"
        )
        raw_text = await get_user_input_or_doc(bot, m, editable, prompt_text, user_id, temp_dir)
        
        items = parse_link_lines(raw_text)
        if not items:
            await editable.edit("**No valid download links found in input! ❌**\nMake sure the file or text contains lines with URLs.")
            return
            
        dest_prompt = (
            f"✅ **Found `{len(items)}` items to download!**\n\n"
            "📤 **Where do you want to upload?**\n"
            "• Send `0` or `me` to upload in **this private chat**.\n"
            "• Or send **Channel / Group ID** (e.g. `-1001234567890`) where the bot is Admin."
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
        
        for idx, (title, url, ftype) in enumerate(items, 1):
            clean_title = sanitize_filename(title)
            try:
                await editable.edit(
                    f"⚙️ **Processing Item [{idx}/{total}]:**\n\n"
                    f"📌 **Title:** `{title}`\n"
                    f"📂 **Type:** `{ftype}`\n"
                    f"📊 **Overall:** `{(idx-1)/total*100:.1f}%` ({idx-1}/{total})\n\n"
                    f"<blockquote>❌ Send `/cancel` to stop batch.</blockquote>"
                )
            except Exception:
                pass
            
            if ftype == "PDF":
                pdf_path = os.path.join(temp_dir, f"{clean_title}.pdf")
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
                        logger.error(f"Failed to upload PDF: {e}")
                        fail_count += 1
                    finally:
                        if os.path.exists(pdf_path):
                            try: os.remove(pdf_path)
                            except: pass
                else:
                    fail_count += 1
                    
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
                        logger.error(f"Failed to upload Video: {e}")
                        fail_count += 1
                    finally:
                        if os.path.exists(vid_path):
                            try: os.remove(vid_path)
                            except: pass
                else:
                    fail_count += 1
            else:
                gen_path = os.path.join(temp_dir, f"{clean_title}.bin")
                ok = await download_file_http(url, gen_path, editable, title)
                if ok and os.path.exists(gen_path):
                    try:
                        await bot.send_document(chat_id=target_chat_id, document=gen_path, caption=f"📁 **{title}**")
                        success_count += 1
                    except Exception as e:
                        fail_count += 1
                    finally:
                        if os.path.exists(gen_path):
                            try: os.remove(gen_path)
                            except: pass
                else:
                    fail_count += 1
                    
        total_time = format_time(time.time() - start_all_time)
        final_msg = (
            f"🎉 **Batch Download & Upload Completed!**\n\n"
            f"✅ **Successfully Uploaded:** `{success_count}` items\n"
            f"❌ **Failed:** `{fail_count}` items\n"
            f"⏱️ **Total Time:** `{total_time}`\n\n"
            f"<blockquote>All files sent to destination chat.</blockquote>"
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
