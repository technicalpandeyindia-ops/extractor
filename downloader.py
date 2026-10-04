import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from typing import List, Tuple

import aiohttp
from pyrogram import Client, filters
from pyrogram.types import Message

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

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', '', name).strip()
    return name[:120] if len(name) > 120 else name

async def download_file_http(url: str, output_path: str, editable: Message, title: str) -> bool:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        async with aiohttp.ClientSession(headers=headers) as session:
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
                        
                        # Throttle status updates
                        now = time.time()
                        if now - last_update > 4:
                            last_update = now
                            pct = (downloaded / total_size * 100) if total_size > 0 else 0
                            speed = downloaded / (now - start_time + 0.001)
                            await editable.edit(
                                f"📥 **Downloading PDF:** `{title[:40]}`\n\n"
                                f"📊 **Progress:** `{pct:.1f}%` ({format_bytes(downloaded)} / {format_bytes(total_size) if total_size else '?'})\n"
                                f"⚡ **Speed:** `{format_bytes(speed)}/s`\n\n"
                                f"<blockquote>❌ Send `/cancel` to abort.</blockquote>"
                            )
                return True
    except Exception as e:
        logger.error(f"Download HTTP error: {e}")
        return False

async def download_video_stream(url: str, output_path: str, editable: Message, title: str) -> bool:
    """Downloads HLS/M3U8 or MP4 using yt-dlp / ffmpeg."""
    clean_url = clean_appx_url(url)
    
    # Try yt-dlp first
    try:
        await editable.edit(f"📥 **Downloading Video:** `{title[:40]}`\n⏳ *Initializing stream fetch...*")
        
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--no-check-certificates",
            "--concurrent-fragments", "8",
            "-o", output_path,
            clean_url
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            return True
    except Exception as e:
        logger.warning(f"yt-dlp download failed: {e}, falling back to ffmpeg/direct")

    # Fallback to ffmpeg
    try:
        cmd_ffmpeg = [
            "ffmpeg", "-y",
            "-headers", "User-Agent: Mozilla/5.0\r\n",
            "-i", clean_url,
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
        logger.warning(f"ffmpeg download failed: {e}")

    # Fallback to direct HTTP download if it's an MP4
    if ".mp4" in clean_url or not clean_url.endswith(".m3u8"):
        return await download_file_http(clean_url, output_path, editable, title)
        
    return False

def parse_link_lines(raw_text: str) -> List[Tuple[str, str, str]]:
    """
    Parses lines of format:
    'Title (PDF):https://...' or 'Title:https://...'
    Returns list of tuples: (clean_title, url, file_type) where file_type in ['PDF', 'VIDEO', 'FILE']
    """
    items = []
    lines = raw_text.strip().splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        
        # Split by last colon that precedes http
        match = re.search(r'^(.*?)\s*:\s*(https?://.*)$', line)
        if match:
            title = match.group(1).strip()
            url = match.group(2).strip()
            
            # Determine type
            if "(PDF)" in title or url.endswith(".pdf") or "subject/" in url or "paid_course4" in url:
                file_type = "PDF"
            elif any(ext in url for ext in [".m3u8", ".mp4", "transcoded-videos", "vodclasses", "liveclasses", "hls"]):
                file_type = "VIDEO"
            else:
                file_type = "FILE"
                
            items.append((title, clean_appx_url(url), file_type))
    return items

async def process_batch_downloader(bot: Client, m: Message, user_id: int):
    editable = await m.reply_text("**Wait initializing Appx V2 Downloader... ⏳**")
    temp_dir = f"dl_temp_{user_id}_{int(time.time())}"
    os.makedirs(temp_dir, exist_ok=True)
    
    try:
        prompt_text = (
            "🚀 **Appx V2 Video & PDF Downloader**\n\n"
            "📥 **Send the extracted links:**\n"
            "• You can **paste links directly** here.\n"
            "• Or **upload a `.txt` file** containing the extracted links.\n\n"
            "*(Format: `Title (PDF):URL` or `Title:URL`)*"
        )
        user_input = await prompt_user(bot, m, editable, prompt_text, user_id)
        
        raw_text = ""
        # Check if user sent document / file
        if m.reply_to_message and m.reply_to_message.document:
            doc_file = await m.reply_to_message.download(file_name=os.path.join(temp_dir, "input.txt"))
            with open(doc_file, "r", encoding="utf-8", errors="ignore") as f:
                raw_text = f.read()
        else:
            raw_text = user_input
            
        items = parse_link_lines(raw_text)
        if not items:
            await editable.edit("**No valid download links found in input! ❌**")
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
            await editable.edit(
                f"⚙️ **Processing Item [{idx}/{total}]:**\n\n"
                f"📌 **Title:** `{title}`\n"
                f"📂 **Type:** `{ftype}`\n"
                f"📊 **Overall:** `{(idx-1)/total*100:.1f}%` ({idx-1}/{total})\n\n"
                f"<blockquote>❌ Send `/cancel` to stop batch.</blockquote>"
            )
            
            if ftype == "PDF":
                pdf_path = os.path.join(temp_dir, f"{clean_title}.pdf")
                ok = await download_file_http(url, pdf_path, editable, title)
                if ok and os.path.exists(pdf_path):
                    await editable.edit(f"📤 **Uploading PDF:** `{clean_title}.pdf`...")
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
                            os.remove(pdf_path)
                else:
                    fail_count += 1
                    
            elif ftype == "VIDEO":
                vid_path = os.path.join(temp_dir, f"{clean_title}.mp4")
                ok = await download_video_stream(url, vid_path, editable, title)
                if ok and os.path.exists(vid_path):
                    await editable.edit(f"📤 **Uploading Video:** `{clean_title}.mp4`...")
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
                            os.remove(vid_path)
                else:
                    fail_count += 1
            else:
                # Generic file
                gen_path = os.path.join(temp_dir, f"{clean_title}.bin")
                ok = await download_file_http(url, gen_path, editable, title)
                if ok and os.path.exists(gen_path):
                    await bot.send_document(chat_id=target_chat_id, document=gen_path, caption=f"📁 **{title}**")
                    success_count += 1
                    os.remove(gen_path)
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
        await editable.edit(final_msg)
        
    except ProcessCancelledException:
        pass
    except Exception as e:
        logger.exception("Error during batch download:")
        if editable:
            await editable.edit(f"**Batch Downloader Error:** `{e}`")
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
