import asyncio
import base64
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

try:
    asyncio.get_event_loop()
except RuntimeError:
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

import aiohttp
from pyrogram import Client, filters
from pyrogram.types import Message

from helpers import appx_decrypt, clean_appx_url, ask_user, is_authorized

SEMAPHORE = asyncio.Semaphore(15)

class ProcessCancelledException(Exception):
    """Custom exception raised when a process is cancelled by the user."""
    pass

def format_time(seconds: float) -> str:
    """Format seconds into a human-readable HH:MM:SS or MM:SS string."""
    seconds = int(seconds)
    mins, secs = divmod(seconds, 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs:02d}h {mins:02d}m {secs:02d}s"
    return f"{mins:02d}m {secs:02d}s"

async def prompt_user(bot: Client, message: Message, editable: Message, text: str, user_id: int) -> str:
    """Helper wrapper to ask user input with built-in /cancel check."""
    cancel_notice = "\n\n<blockquote>❌ **Send `/cancel` at any time to abort this process.**</blockquote>"
    full_text = text + cancel_notice
    
    response = await ask_user(bot, message, editable, full_text, user_id)
    
    if response is None or response.strip().lower() == "/cancel":
        await editable.edit("**Process Cancelled by User ❌**")
        raise ProcessCancelledException("User requested cancellation.")
    
    return response.strip()

async def update_status_card(editable: Message, task_name: str, current: int, total: int, start_time: float, activity: str):
    """Formats and updates a progress tracking message card."""
    percentage = (current / total * 100) if total > 0 else 0
    elapsed = time.time() - start_time
    
    if current > 0 and total > 0:
        avg_time_per_unit = elapsed / current
        remaining_units = total - current
        eta = avg_time_per_unit * remaining_units
        eta_str = format_time(eta)
    else:
        eta_str = "Calculating..."

    filled_blocks = int(percentage // 10)
    progress_bar = "▓" * filled_blocks + "░" * (10 - filled_blocks)

    status_text = (
        f"<blockquote>⚙️ **Processing Task:** `{task_name}`</blockquote>\n\n"
        f"📊 **Progress:** [{progress_bar}] `{percentage:.1f}%` ({current}/{total})\n"
        f"📌 **Current Activity:** {activity}\n"
        f"⏱️ **Time Elapsed:** `{format_time(elapsed)}`\n"
        f"⏳ **Time Left (ETA):** `{eta_str}`\n\n"
        f"<blockquote>❌ **Send `/cancel` to abort.**</blockquote>"
    )
    
    try:
        await editable.edit(status_text)
    except Exception:
        pass

async def fetch_appx_html_to_json(session: aiohttp.ClientSession, url: str, headers: Dict = None, data: Any = None) -> Any:
    async with SEMAPHORE:
        for attempt in range(3):
            try:
                if data:
                    async with session.post(url, headers=headers, data=data) as response:
                        text = await response.text()
                else:
                    async with session.get(url, headers=headers) as response:
                        text = await response.text()

                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    match = re.search(r'\{"status":', text, re.DOTALL)
                    if match:
                        json_str = text[match.start():]
                        open_brace_count = 0
                        close_brace_count = 0
                        json_end = -1

                        for i, char in enumerate(json_str):
                            if char == '{':
                                open_brace_count += 1
                            elif char == '}':
                                close_brace_count += 1

                            if open_brace_count > 0 and open_brace_count == close_brace_count:
                                json_end = i + 1
                                break

                        if json_end != -1:
                            return json.loads(json_str[:json_end])
            except aiohttp.ClientError as e:
                logging.error(f"Appx Attempt {attempt + 1} failed for {url}: {e}")
            except Exception as e:
                logging.exception(f"Appx Unexpected error for {url}: {e}")
            if attempt < 2:
                await asyncio.sleep(1.5 ** attempt)
        return None

def extract_user_id_from_jwt(token: str) -> str:
    try:
        parts = token.split(".")
        if len(parts) == 3:
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload_json = base64.urlsafe_b64decode(payload_b64).decode("utf-8")
            data = json.loads(payload_json)
            return str(data.get("id") or data.get("user_id") or data.get("sub") or "0")
    except Exception as e:
        logging.warning(f"Failed to parse user-id from token: {e}")
    return "0"

def find_appx_matching_apis(search_api: List[str], appxapis_file=None) -> List[Dict]:
    matched_apis = []
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        appxapis_file,
        os.path.join(base_dir, "threeapis.json"),
        os.path.join(base_dir, "appxapis.json"),
        "threeapis.json",
        "appxapis.json"
    ]
    api_data = []
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            try:
                with open(candidate, 'r', encoding='utf-8') as f:
                    api_data = json.load(f)
                    if api_data:
                        break
            except Exception as e:
                logging.error(f"Error reading appxapis file {candidate}: {e}")

    if not api_data:
        logging.error("No valid appxapis JSON database found.")
        return matched_apis

    for item in api_data:
        for term in search_api:
            term = term.strip().lower()
            if term in item.get("name", "").lower() or term in item.get("api", "").lower():
                matched_apis.append(item)

    unique_apis = []
    seen_apis = set()
    for item in matched_apis:
        api_url = item.get("api")
        if api_url and api_url not in seen_apis:
            unique_apis.append(item)
            seen_apis.add(api_url)

    return unique_apis

async def resolve_api_and_app_name(bot: Client, m: Message, editable: Message, raw_input_text: str, user_id: int):
    raw_input_text = raw_input_text.strip()
    
    if raw_input_text.startswith(("http://", "https://")):
        clean_url = raw_input_text.replace("https://", "").replace("http://", "").rstrip("/")
        api_url = f"https://{clean_url}"
        return api_url, api_url

    search_terms = [term.strip() for term in raw_input_text.split()]
    matches = find_appx_matching_apis(search_terms)

    if not matches:
        await editable.edit("**No matches found! Enter Correct App Starting Word ❌**")
        return None, None

    if len(matches) > 35:
        matches = matches[:35]
        truncated_note = "\n\n⚠️ *Too many matches, showing first 35.*"
    else:
        truncated_note = ""

    text = ""
    for cnt, item in enumerate(matches):
        text += f"<blockquote>**{cnt + 1}.** `{item['name']}:{item['api']}`</blockquote>\n"

    selection_text = await prompt_user(bot, m, editable, f"**Select Index Number Of App API:**\n\n{text}{truncated_note}", user_id)

    if selection_text.isdigit() and 1 <= int(selection_text) <= len(matches):
        selected_item = matches[int(selection_text) - 1]
        return selected_item['api'], selected_item['name']
    else:
        await editable.edit("**Error: Wrong Index Number ❌**")
        return None, None

async def login_appx_user(session: aiohttp.ClientSession, bot: Client, m: Message, editable: Message, user_id: int):
    app_input = await prompt_user(bot, m, editable, "**Enter App Name or API URL to login:**", user_id)
    api, app_name = await resolve_api_and_app_name(bot, m, editable, app_input, user_id)
    if not api or not app_name:
        return None, None, None

    mobile = await prompt_user(bot, m, editable, "**Enter Mobile Number:**", user_id)
    password = await prompt_user(bot, m, editable, "**Enter Password:**", user_id)

    await editable.edit("🔑 **Authenticating with Appx servers...**")

    headers = {
        "Client-Service": "Appx",
        "Auth-Key": "appxapi",
        "source": "website",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    login_url = f"{api}/post/userlogin"
    login_data = {
        "email": mobile,
        "password": password
    }

    res = await fetch_appx_html_to_json(session, login_url, headers=headers, data=login_data)

    if not res or res.get("status") != 200 or not res.get("data"):
        msg = res.get("message", "Invalid credentials or login API endpoint mismatch.") if res else "No response from server."
        await editable.edit(f"**Login Failed! ❌**\n`Reason: {msg}`")
        return None, None, None

    data = res["data"]
    token = data.get("token") or data.get("jwt_token") or data.get("user_token")

    if not token:
        await editable.edit("**Login successful, but token could not be found in response! ❌**")
        return None, None, None

    token_msg = (
        f"🔑 **Appx Token Generated Successfully!**\n\n"
        f"**App Name:** `{app_name}`\n"
        f"**Mobile:** `{mobile}`\n"
        f"**Token:**\n`{token}`\n\n"
        f"<blockquote>Tap token to copy it for future use.</blockquote>"
    )
    await bot.send_message(chat_id=m.chat.id, text=token_msg)

    return api, token, app_name

def extract_appx_item_links(item: Dict[str, Any], api: str) -> List[str]:
    """
    Extracts all valid permanent video streams and PDF notes from an Appx item.
    - Decrypts AES ciphertext
    - Extracts multi-bitrate VOD master streams (720p/480p) to avoid expiring live broadcast tokens
    - Rewrites dead CloudFront tokens to permanent 200 OK CDN endpoints
    - Extracts PDFs independently without skipping videos
    """
    outputs = []
    Title = (item.get("Title") or item.get("title") or item.get("name") or "Item").strip()

    # 1. PDF 1 Extraction
    p1 = item.get("pdf_link")
    if p1:
        dec_p1 = appx_decrypt(str(p1))
        if dec_p1 and dec_p1.endswith(".pdf"):
            clean_p = clean_appx_url(dec_p1)
            if str(item.get("is_pdf_encrypted")) == "1":
                key = appx_decrypt(str(item.get("pdf_encryption_key", ""))) if item.get("pdf_encryption_key") else None
                outputs.append(f"{Title} (PDF):{clean_p}*{key}\n" if key else f"{Title} (PDF):{clean_p}\n")
            else:
                outputs.append(f"{Title} (PDF):{clean_p}\n")

    # 2. PDF 2 Extraction
    p2 = item.get("pdf_link2")
    if p2:
        dec_p2 = appx_decrypt(str(p2))
        if dec_p2 and dec_p2.endswith(".pdf"):
            clean_p = clean_appx_url(dec_p2)
            if str(item.get("is_pdf2_encrypted")) == "1":
                key = appx_decrypt(str(item.get("pdf2_encryption_key", ""))) if item.get("pdf2_encryption_key") else None
                outputs.append(f"{Title} (PDF 2):{clean_p}*{key}\n" if key else f"{Title} (PDF 2):{clean_p}\n")
            else:
                outputs.append(f"{Title} (PDF 2):{clean_p}\n")

    # 3. Image / Thumbnail
    material_type = str(item.get("material_type") or item.get("type") or "").upper()
    if material_type == "IMAGE":
        thumbnail = item.get("thumbnail") or item.get("imageUrl")
        if thumbnail:
            outputs.append(f"{Title}:{clean_appx_url(thumbnail)}\n")

    # 4. Video Extraction (VOD playlist_eof -> download_links -> download_link -> recording_hls -> file_link -> video_url)
    video_url = None
    if item.get("download_links") and isinstance(item["download_links"], list):
        for dl in item["download_links"]:
            path = dl.get("path")
            if path:
                dec = appx_decrypt(str(path))
                if dec and (dec.startswith("http") or dec.startswith("//")):
                    video_url = f"https:{dec}" if dec.startswith("//") else clean_appx_url(dec)
                    break

    if not video_url and item.get("download_link"):
        dec = appx_decrypt(str(item["download_link"]))
        if dec and (dec.startswith("http") or dec.startswith("//")):
            video_url = f"https:{dec}" if dec.startswith("//") else clean_appx_url(dec)

    if not video_url and item.get("recording_hls"):
        dec = appx_decrypt(str(item["recording_hls"]))
        if dec and (dec.startswith("http") or dec.startswith("//")):
            video_url = f"https:{dec}" if dec.startswith("//") else clean_appx_url(dec)

    if not video_url and item.get("file_link"):
        dec = appx_decrypt(str(item["file_link"]))
        if dec and (dec.startswith("http") or dec.startswith("//")):
            video_url = f"https:{dec}" if dec.startswith("//") else clean_appx_url(dec)

    if not video_url:
        for field in [
            "video_url", "videoUrl", "hls_url", "hlsUrl", "stream_url", "streamUrl",
            "media_url", "mediaUrl", "mpd_url", "mpdUrl", "download_url", "downloadUrl",
            "url", "file_url", "fileUrl", "video_link", "videoLink", "source_url", "sourceUrl"
        ]:
            val = item.get(field)
            if val:
                dec = appx_decrypt(str(val))
                if dec and (dec.startswith("http") or dec.startswith("//")):
                    video_url = f"https:{dec}" if dec.startswith("//") else clean_appx_url(dec)
                    break
                elif val and (str(val).startswith("http") or str(val).startswith("//")):
                    video_url = f"https:{val}" if str(val).startswith("//") else clean_appx_url(str(val))
                    break

    if video_url:
        outputs.append(f"{Title}:{video_url}\n")

    return outputs


async def fetch_appx_video_id_details_v2(session: aiohttp.ClientSession, api: str, selected_batch_id: str, video_id: str, ytFlag: str, headers: Dict, folder_wise_course: Any, user_id: int) -> List[str]:
    try:
        headers_noauth = {k: v for k, v in headers.items() if k.lower() not in ('authorization', 'user-id')}
        res = await fetch_appx_html_to_json(session, f"{api}/get/fetchVideoDetailsById?course_id={selected_batch_id}&folder_wise_course={folder_wise_course}&ytflag={ytFlag}&video_id={video_id}", headers)
        if not res or res.get('status') != 200 or not res.get('data'):
            res = await fetch_appx_html_to_json(session, f"{api}/get/fetchVideoDetailsById?course_id={selected_batch_id}&folder_wise_course={folder_wise_course}&ytflag={ytFlag}&video_id={video_id}", headers_noauth)

        if res and isinstance(res.get('data'), dict):
            extracted = extract_appx_item_links(res['data'], api)
            if extracted:
                return extracted

        # Fallback to DRM endpoint
        res_drm = await fetch_appx_html_to_json(session, f"{api}/get/get_mpd_drm_links?videoid={video_id}&folder_wise_course={folder_wise_course}", headers)
        if not res_drm or res_drm.get('status') != 200:
            res_drm = await fetch_appx_html_to_json(session, f"{api}/get/get_mpd_drm_links?videoid={video_id}&folder_wise_course={folder_wise_course}", headers_noauth)
        
        output = []
        if res_drm and res_drm.get('data') and isinstance(res_drm['data'], list):
            Title = f"Video {video_id}"
            for item in res_drm['data']:
                if isinstance(item, dict):
                    for field in ['path', 'url', 'videoUrl', 'hlsUrl', 'mpdUrl', 'streamUrl', 'mediaUrl', 'downloadUrl', 'fileUrl', 'link', 'src']:
                        val = item.get(field)
                        if val:
                            dec = appx_decrypt(str(val))
                            if dec and (dec.startswith('http') or dec.startswith('//')):
                                output.append(f"{Title}:{f'https:{dec}' if dec.startswith('//') else clean_appx_url(dec)}\n")
                                break
                    if output:
                        break
        return output
    except Exception as e:
        return [f"User ID: {user_id} - Error fetching details for Course_id : {selected_batch_id}, video ID {video_id}: {str(e)}\n"]

async def fetch_appx_folder_contents_v2(session: aiohttp.ClientSession, api: str, selected_batch_id: str, folder_id: str, headers: Dict, folder_wise_course: Any, user_id: int) -> List[str]:
    try:
        res = await fetch_appx_html_to_json(session, f"{api}/get/folder_contentsv2?course_id={selected_batch_id}&parent_id={folder_id}", headers)
        tasks, output = [], []
        if res and "data" in res and isinstance(res["data"], list):
            for item in res["data"]:
                video_id = item.get("id") or item.get("_id")
                ytFlag = item.get("ytFlag", "0")
                material_type = str(item.get("material_type") or item.get("type") or "").upper()

                if material_type == "FOLDER" or (not material_type and not item.get("pdf_link") and not item.get("download_links") and not item.get("video_url")):
                    tasks.append(fetch_appx_folder_contents_v2(session, api, selected_batch_id, video_id, headers, folder_wise_course, user_id))
                else:
                    item_links = extract_appx_item_links(item, api)
                    if item_links:
                        output.extend(item_links)
                    elif video_id:
                        tasks.append(fetch_appx_video_id_details_v2(session, api, selected_batch_id, str(video_id), str(ytFlag), headers, folder_wise_course, user_id))

        if tasks:
            results = await asyncio.gather(*tasks)
            for r in results:
                if isinstance(r, list):
                    output.extend(r)
                else:
                    output.append(r)
        return output
    except Exception as e:
        return [f"User ID: {user_id} - Error fetching folder contents: {e}\n"]

async def fetch_appx_video_id_details_v3(session: aiohttp.ClientSession, api: str, selected_batch_id: str, video_id: str, ytFlag: str, headers: Dict, user_id: int) -> List[str]:
    return await fetch_appx_video_id_details_v2(session, api, selected_batch_id, video_id, ytFlag, headers, 0, user_id)

async def process_folder_wise_course_0(session: aiohttp.ClientSession, api: str, selected_batch_id: str, headers: Dict, user_id: int) -> List[str]:
    res = await fetch_appx_html_to_json(session, f"{api}/get/allsubjectfrmlivecourseclass?courseid={selected_batch_id}&start=-1", headers)
    all_outputs, tasks = [], []
    
    if res and "data" in res and isinstance(res["data"], list):
        for subject in res["data"]:
            subjectid = subject.get("subjectid")
            res2 = await fetch_appx_html_to_json(session, f"{api}/get/alltopicfrmlivecourseclass?courseid={selected_batch_id}&subjectid={subjectid}&start=-1", headers)
            if res2 and "data" in res2 and isinstance(res2["data"], list):
                for topic in res2["data"]:
                    topicid = topic.get("topicid")
                    res3 = await fetch_appx_html_to_json(session, f"{api}/get/livecourseclassbycoursesubtopconceptapiv3?topicid={topicid}&start=-1&courseid={selected_batch_id}&subjectid={subjectid}", headers)
                    if res3 and "data" in res3 and isinstance(res3["data"], list):
                        for item in res3["data"]:
                            item_links = extract_appx_item_links(item, api)
                            if item_links:
                                all_outputs.extend(item_links)
                            else:
                                video_id = item.get("id") or item.get("_id")
                                ytFlag = item.get("ytFlag", "0")
                                if selected_batch_id is not None and video_id is not None:
                                    tasks.append(fetch_appx_video_id_details_v3(session, api, selected_batch_id, str(video_id), str(ytFlag), headers, user_id))

    if tasks:
        results = await asyncio.gather(*tasks)
        for res in results:
            if isinstance(res, list):
                all_outputs.extend(res)

    return all_outputs

async def process_folder_wise_course_1(session: aiohttp.ClientSession, api: str, selected_batch_id: str, headers: Dict, user_id: int) -> List[str]:
    # Try parent_id=-1 first, if empty try parent_id=0 for Classx/Appx compatibility
    res = await fetch_appx_html_to_json(session, f"{api}/get/folder_contentsv2?course_id={selected_batch_id}&parent_id=-1", headers)
    if not res or not res.get("data"):
        res = await fetch_appx_html_to_json(session, f"{api}/get/folder_contentsv2?course_id={selected_batch_id}&parent_id=0", headers)

    all_outputs, tasks = [], []
    
    if res and "data" in res and isinstance(res["data"], list):
        for item in res["data"]:
            video_id = item.get("id") or item.get("_id")
            ytFlag = item.get("ytFlag", "0")
            material_type = str(item.get("material_type") or item.get("type") or "").upper()
            
            if material_type == "FOLDER" or (not material_type and not item.get("pdf_link") and not item.get("download_links") and not item.get("video_url")):
                tasks.append(fetch_appx_folder_contents_v2(session, api, selected_batch_id, video_id, headers, 1, user_id))
            else:
                item_links = extract_appx_item_links(item, api)
                if item_links:
                    all_outputs.extend(item_links)
                elif video_id:
                    tasks.append(fetch_appx_video_id_details_v2(session, api, selected_batch_id, str(video_id), str(ytFlag), headers, 1, user_id))

    if tasks:
        results = await asyncio.gather(*tasks)
        for res in results:
            if isinstance(res, list):
                all_outputs.extend(res)

    return all_outputs


async def process_appxwp(bot: Client, m: Message, user_id: int):
    editable = await m.reply_text("**Wait initializing process... ⏳**")
    clean_file_name = None

    try:
        connector = aiohttp.TCPConnector(limit=100)
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=60)) as session:
            
            auth_prompt = (
                "**Select Appx Authentication Option:**\n\n"
                "**1. 🔑 Login with Credentials (Mobile & Password)**\n"
                "**2. 🎫 Enter JWT/Access Token or App Name Directly**"
            )
            auth_mode = await prompt_user(bot, m, editable, auth_prompt, user_id)

            api = None
            token = None
            selected_app_name = None

            if auth_mode == "1":
                api, token, selected_app_name = await login_appx_user(session, bot, m, editable, user_id)
                if not api or not token:
                    return
            else:
                first_input = await prompt_user(bot, m, editable, "**Enter App Name, API Url, or JWT/Access Token:**", user_id)

                if first_input.count('.') == 2 and len(first_input) > 100:
                    token = first_input
                    extracted_jwt_userid = extract_user_id_from_jwt(token)
                    
                    second_input = await prompt_user(
                        bot, m, editable,
                        f"**Token received! (Detected User ID: `{extracted_jwt_userid}`)\nNow enter App Name or API URL:**",
                        user_id
                    )
                    api, selected_app_name = await resolve_api_and_app_name(bot, m, editable, second_input, user_id)
                else:
                    api, selected_app_name = await resolve_api_and_app_name(bot, m, editable, first_input, user_id)

            if not api or not selected_app_name:
                return

            extracted_jwt_userid = extract_user_id_from_jwt(token) if token else "0"
            formatted_token = f"{token}" if token and not token.startswith("Bearer ") else token

            headers = {
                "Client-Service": "Appx",
                "Auth-Key": "appxapi",
                "source": "website",
            }
            if token:
                headers['Authorization'] = formatted_token
                headers['User-ID'] = extracted_jwt_userid

            try: await editable.delete()
            except: pass
            editable = await m.reply_text("**Fetching Available Courses... 🔍**")
            res1 = await fetch_appx_html_to_json(session, f"{api}/get/courselist", headers)
            res2 = await fetch_appx_html_to_json(session, f"{api}/get/courselistnewv2", headers)

            courses1 = res1.get("data", []) if res1 and res1.get('status') == 200 else []
            courses2 = res2.get("data", []) if res2 and res2.get('status') == 200 else []
            courses3 = []

            if token:
                res3 = await fetch_appx_html_to_json(session, f"{api}/get/mycourse", headers)
                if res3 and res3.get('status') == 200:
                    courses3 = res3.get("data", [])

            courses = courses3 + courses1 + courses2
            if not courses:
                await editable.edit("**Did not find any course! ❌\nCheck if token is expired or if the App API endpoint is valid.**")
                return

            total = len(courses)
            if total > 50:
                text = ""
                for cnt, course in enumerate(courses):
                    text += f"{cnt + 1}. {course.get('course_name', 'Unnamed Course')} 💵₹{course.get('price', '0')}\n"

                course_details_file = f"{user_id}_paid_course_details.txt"
                with open(course_details_file, 'w', encoding='utf-8') as f:
                    f.write(text)

                caption = f"**App Name:** `{selected_app_name}`\n**Batch Name:** `Course Details`"

                try:
                    with open(course_details_file, 'rb') as f:
                        await m.reply_document(document=f, caption=caption, file_name="course_details.txt")
                    
                    await editable.delete()
                    editable = await m.reply_text("📂 **Course list sent above as document. Send index number here:**")
                finally:
                    if os.path.exists(course_details_file):
                        os.remove(course_details_file)

                selection_course = await prompt_user(bot, m, editable, "**Send index number from the course details file which i just send you:**", user_id)
            else:
                text = ""
                for cnt, course in enumerate(courses):
                    text += f"<blockquote>**{cnt + 1}.** `{course.get('course_name', 'Unnamed Course')} 💵₹{course.get('price', '0')}`</blockquote>\n"
                selection_course = await prompt_user(bot, m, editable, f"**Send index number of the course to download:**\n\n{text}", user_id)

            if selection_course.isdigit() and 1 <= int(selection_course) <= len(courses):
                selected_course_index = int(selection_course) - 1
                course = courses[selected_course_index]
                selected_batch_id = course['id']
                selected_batch_name = course.get('course_name', 'Batch')
                folder_wise_course = course.get("folder_wise_course", "")
                
                clean_batch_name = selected_batch_name.replace('/', '-').replace('|', '-')[:244]
                clean_file_name = f"{user_id}_{clean_batch_name}"
            else:
                await editable.edit("**Invalid Selection Index! ❌**")
                return

            start_time = time.time()
            await update_status_card(editable, f"Extracting: {selected_batch_name}", 0, 100, start_time, "Initializing extraction...")

            extraction_headers = {
                "Client-Service": "Appx",
                "Auth-Key": "appxapi",
                "source": "website",
            }
            if token:
                extraction_headers["Authorization"] = formatted_token
                extraction_headers["User-ID"] = extracted_jwt_userid

            all_outputs = []

            # Always try folder structure first for Classx apps, then live course items if needed
            await update_status_card(editable, f"Extracting: {selected_batch_name}", 20, 100, start_time, "Extracting folder contents...")
            all_outputs = await process_folder_wise_course_1(session, api, selected_batch_id, extraction_headers, user_id)
            
            if not all_outputs:
                await update_status_card(editable, f"Extracting: {selected_batch_name}", 40, 100, start_time, "Extracting live/subject course items...")
                all_outputs = await process_folder_wise_course_0(session, api, selected_batch_id, extraction_headers, user_id)

            if all_outputs:
                output_txt_path = f"{clean_file_name}.txt"
                with open(output_txt_path, 'w', encoding='utf-8') as f:
                    for output_line in all_outputs:
                        f.write(output_line)

                time_taken = format_time(time.time() - start_time)
                caption = f"**App Name:** `{selected_app_name}`\n**Batch Name:** `{selected_batch_name}`\n**Time Taken:** `{time_taken}`"

                await editable.edit("📤 **Uploading generated document to Telegram...**")

                if os.path.exists(output_txt_path) and os.path.getsize(output_txt_path) > 0:
                    try:
                        with open(output_txt_path, "rb") as doc:
                            await m.reply_document(doc, caption=caption, file_name=f"{clean_batch_name}.txt")
                        await editable.delete()
                    except Exception as upload_err:
                        logging.error(f"Failed to upload output file: {upload_err}")
                    finally:
                        if os.path.exists(output_txt_path):
                            os.remove(output_txt_path)
                else:
                    if os.path.exists(output_txt_path):
                        os.remove(output_txt_path)
                    await editable.edit("**Extraction completed, but no content was found (0 Bytes output). ❌**")
            else:
                await editable.edit("**Didn't Find Any Content In The Course! ❌**")

    except ProcessCancelledException:
        if clean_file_name:
            f_path = f"{clean_file_name}.txt"
            if os.path.exists(f_path):
                try:
                    os.remove(f_path)
                except Exception:
                    pass
    except Exception as e:
        logging.exception("Error in process_appxwp:")
        if editable:
            await editable.edit(f"**Error : {e}**")
        if clean_file_name:
            f_path = f"{clean_file_name}.txt"
            if os.path.exists(f_path):
                try:
                    os.remove(f_path)
                except Exception:
                    pass

def register_appxwp_handlers(bot: Client):
    @bot.on_callback_query(filters.regex("^appxwp$"))
    async def appxwp_callback(client: Client, callback_query):
        user_id = callback_query.from_user.id if callback_query.from_user else 0
        if not is_authorized(user_id):
            await callback_query.answer("⛔ Access Denied! You are not authorized.", show_alert=True)
            return
        await callback_query.answer()
        asyncio.create_task(process_appxwp(client, callback_query.message, user_id))
