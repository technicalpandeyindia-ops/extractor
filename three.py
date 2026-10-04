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

from helpers import appx_decrypt, ask_user, clean_appx_url, is_authorized, send_extracted_text_file

logger = logging.getLogger("AppxExtractor")

# Throttle API requests to prevent Appx rate-limiting
SEMAPHORE = asyncio.Semaphore(15)

class ProcessCancelledException(Exception):
    pass

def format_time(seconds: float) -> str:
    seconds = int(seconds)
    mins, secs = divmod(seconds, 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs:02d}h {mins:02d}m {secs:02d}s"
    return f"{mins:02d}m {secs:02d}s"

async def prompt_user(bot: Client, message: Message, editable: Message, text: str, user_id: int) -> str:
    cancel_notice = "\n\n<blockquote>❌ **Send `/cancel` at any time to abort this process.**</blockquote>"
    full_text = text + cancel_notice
    response = await ask_user(bot, message, editable, full_text, user_id)
    if response is None or response.strip().lower() == "/cancel":
        await editable.edit("**Process Cancelled by User ❌**")
        raise ProcessCancelledException("User requested cancellation.")
    return response.strip()

async def update_status_card(editable: Message, task_name: str, current: int, total: int, start_time: float, activity: str):
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
                logger.error(f"Appx Attempt {attempt + 1} failed for {url}: {e}")
            except Exception as e:
                logger.exception(f"Appx Unexpected error for {url}: {e}")
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
        logger.warning(f"Failed to parse user-id from token: {e}")
    return "0"

BUILTIN_APPX_APIS = [
    {"name": "Adhyayan Mantra", "api": "https://adhyayanmantraapi.classx.co.in", "website": "https://live.adhyayanmantra.com"},
    {"name": "Akash Institute", "api": "https://akashapi.classx.co.in", "website": "https://akash.classx.co.in"},
    {"name": "Allen", "api": "https://allenapi.classx.co.in", "website": "https://allen.classx.co.in"},
    {"name": "Apna Kaksha", "api": "https://apnakakshaapi.classx.co.in", "website": "https://apnakaksha.classx.co.in"},
    {"name": "Careerwill", "api": "https://careerwillapi.classx.co.in", "website": "https://careerwill.com"},
    {"name": "Competition Wallah", "api": "https://competitionwallahapi.classx.co.in", "website": "https://competitionwallah.classx.co.in"},
    {"name": "Drishti IAS", "api": "https://drishtiiasapi.classx.co.in", "website": "https://drishtiias.classx.co.in"},
    {"name": "Exampur", "api": "https://exampurapi.classx.co.in", "website": "https://exampur.com"},
    {"name": "Khan Global Studies", "api": "https://khanglobalstudiesapi.classx.co.in", "website": "https://khanglobalstudies.com"},
    {"name": "MD Classes", "api": "https://mdclassesapi.classx.co.in", "website": "https://mdclasses.classx.co.in"},
    {"name": "Next IAS", "api": "https://nextiasapi.classx.co.in", "website": "https://nextias.com"},
    {"name": "Pariksha App", "api": "https://parikshaapi.classx.co.in", "website": "https://pariksha.co"},
    {"name": "PW Appx", "api": "https://physicswallahapi.classx.co.in", "website": "https://physicswallah.classx.co.in"},
    {"name": "Rajasthan Gyan", "api": "https://rajasthangyanapi.classx.co.in", "website": "https://rajasthangyan.classx.co.in"},
    {"name": "Rojgar With Ankit", "api": "https://rojgarwithankitapi.classx.co.in", "website": "https://rojgarwithankit.co.in"},
    {"name": "Sankalp Bharat", "api": "https://sankalpbharatapi.classx.co.in", "website": "https://sankalpbharat.com"},
    {"name": "Sanskrit Ganga", "api": "https://sanskritgangaapi.classx.co.in", "website": "https://sanskritganga.classx.co.in"},
    {"name": "Target with Alok", "api": "https://targetwithalokapi.classx.co.in", "website": "https://targetwithalok.classx.co.in"},
    {"name": "Utkarsh Classes", "api": "https://utkarshapi.classx.co.in", "website": "https://utkarsh.com"},
    {"name": "Vedantu", "api": "https://vedantuapi.classx.co.in", "website": "https://vedantu.com"},
    {"name": "Winners Institute", "api": "https://winnersinstituteapi.classx.co.in", "website": "https://winnersinstitute.in"},
    {"name": "Wi-Fi Study", "api": "https://wifistudyapi.classx.co.in", "website": "https://wifistudy.com"}
]

def find_appx_matching_apis(search_api: List[str], appxapis_file="threeapis.json") -> List[Dict]:
    matched_apis = []
    api_data = BUILTIN_APPX_APIS.copy()
    if os.path.exists(appxapis_file):
        try:
            with open(appxapis_file, 'r', encoding='utf-8') as f:
                extra = json.load(f)
                if isinstance(extra, list):
                    api_data.extend(extra)
        except Exception:
            pass

    for item in api_data:
        for term in search_api:
            term = term.strip().lower()
            if term in item.get("name", "").lower() or term in item.get("api", "").lower():
                matched_apis.append(item)

    unique_apis = []
    seen_apis = set()
    for item in matched_apis:
        if item["api"] not in seen_apis:
            unique_apis.append(item)
            seen_apis.add(item["api"])

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

    text = ""
    for cnt, item in enumerate(matches):
        text += f"<blockquote>**{cnt + 1}.** `{item['name']}:{item['api']}`</blockquote>\n"

    selection_text = await prompt_user(bot, m, editable, f"**Select Index Number Of App API:**\n\n{text}", user_id)

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

async def fetch_appx_video_id_details_v2(session: aiohttp.ClientSession, api: str, selected_batch_id: str, video_id: str, ytFlag: str, headers: Dict, folder_wise_course: Any, user_id: int) -> List[str]:
    try:
        headers_noauth = {k: v for k, v in headers.items() if k.lower() not in ('authorization', 'user-id')}
        
        res = await fetch_appx_html_to_json(session, f"{api}/get/fetchVideoDetailsById?course_id={selected_batch_id}&folder_wise_course={folder_wise_course}&ytflag={ytFlag}&video_id={video_id}", headers)
        if not res or res.get('status') != 200:
            res = await fetch_appx_html_to_json(session, f"{api}/get/fetchVideoDetailsById?course_id={selected_batch_id}&folder_wise_course={folder_wise_course}&ytflag={ytFlag}&video_id={video_id}", headers_noauth)

        output = []
        if res and res.get('data'):
            data = res.get('data')
            Title = data.get("Title", f"Video {video_id}").strip()
            
            direct_video_url = (
                data.get('video_url') or data.get('videoUrl') or data.get('hls_url')
                or data.get('hlsUrl') or data.get('stream_url') or data.get('streamUrl')
                or data.get('media_url') or data.get('mediaUrl') or data.get('mpd_url')
                or data.get('mpdUrl') or data.get('download_url') or data.get('downloadUrl')
                or data.get('url') or data.get('file_url') or data.get('fileUrl')
                or data.get('source_url') or data.get('sourceUrl') or data.get('path')
                or data.get('link') or data.get('src') or data.get('video_link') or data.get('videoLink') or ""
            )
            
            if direct_video_url:
                output.append(f"{Title}:{clean_appx_url(direct_video_url)}\n")
            else:
                res_drm = await fetch_appx_html_to_json(session, f"{api}/get/get_mpd_drm_links?videoid={video_id}&folder_wise_course={folder_wise_course}", headers)
                if not res_drm or res_drm.get('status') != 200:
                    res_drm = await fetch_appx_html_to_json(session, f"{api}/get/get_mpd_drm_links?videoid={video_id}&folder_wise_course={folder_wise_course}", headers_noauth)
                
                if res_drm:
                    drm_data = res_drm.get('data', [])
                    if drm_data and isinstance(drm_data, list) and len(drm_data) > 0:
                        path = appx_decrypt(drm_data[0].get("path", "")) if drm_data[0].get("path") else None
                        if path:
                            output.append(f"{Title}:{clean_appx_url(path)}\n")
                    
                    if not output and drm_data and isinstance(drm_data, list):
                        for item in drm_data:
                            for field in ['path', 'url', 'videoUrl', 'hlsUrl', 'mpdUrl', 'streamUrl', 'mediaUrl', 'downloadUrl', 'fileUrl', 'link', 'src']:
                                val = item.get(field)
                                if val:
                                    try:
                                        decrypted = appx_decrypt(val) if val else None
                                        if decrypted and (decrypted.startswith('http') or decrypted.startswith('//')):
                                            if decrypted.startswith('//'):
                                                decrypted = f"https:{decrypted}"
                                            output.append(f"{Title}:{clean_appx_url(decrypted)}\n")
                                            break
                                    except Exception:
                                        if val.startswith('http') or val.startswith('//'):
                                            if val.startswith('//'):
                                                val = f"https:{val}"
                                            output.append(f"{Title}:{clean_appx_url(val)}\n")
                                            break
            
            pdf_link = appx_decrypt(data.get("pdf_link", "")) if data.get("pdf_link", "") and appx_decrypt(data.get("pdf_link", "")).endswith(".pdf") else None
            if pdf_link:
                if str(data.get("is_pdf_encrypted", 0)) == "1":
                    key = appx_decrypt(data.get("pdf_encryption_key", "")) if data.get("pdf_encryption_key") else None
                    output.append(f"{Title}:{clean_appx_url(pdf_link)}*{key}\n" if key else f"{Title}:{clean_appx_url(pdf_link)}\n")
                else:
                    output.append(f"{Title}:{clean_appx_url(pdf_link)}\n")

            pdf_link2 = appx_decrypt(data.get("pdf_link2", "")) if data.get("pdf_link2", "") and appx_decrypt(data.get("pdf_link2", "")).endswith(".pdf") else None
            if pdf_link2:
                if str(data.get("is_pdf2_encrypted", 0)) == "1":
                    key = appx_decrypt(data.get("pdf2_encryption_key", "")) if data.get("pdf2_encryption_key") else None
                    output.append(f"{Title}:{clean_appx_url(pdf_link2)}*{key}\n" if key else f"{Title}:{clean_appx_url(pdf_link2)}\n")
                else:
                    output.append(f"{Title}:{clean_appx_url(pdf_link2)}\n")
        return output
    except Exception as e:
        return [f"User ID: {user_id} - Error fetching details for Course_id : {selected_batch_id}, video ID {video_id}: {str(e)}\n"]

async def fetch_appx_folder_contents_v2(session: aiohttp.ClientSession, api: str, selected_batch_id: str, folder_id: str, headers: Dict, folder_wise_course: Any, user_id: int) -> List[str]:
    try:
        res = await fetch_appx_html_to_json(session, f"{api}/get/folder_contentsv2?course_id={selected_batch_id}&parent_id={folder_id}", headers)
        tasks, output = [], []
        if res and "data" in res and isinstance(res["data"], list):
            for item in res["data"]:
                video_id = item.get("id")
                ytFlag = item.get("ytFlag", "0")
                mat_type = str(item.get("material_type") or "").upper()
                Title = (item.get("Title") or item.get("title") or f"Item {video_id}").strip()

                if mat_type in ("PDF", "TEST"):
                    pdf_link = appx_decrypt(item.get("pdf_link", "")) if item.get("pdf_link", "") and appx_decrypt(item.get("pdf_link", "")).endswith(".pdf") else None
                    if pdf_link:
                        if str(item.get("is_pdf_encrypted")) == "1":
                            key = appx_decrypt(item.get("pdf_encryption_key", ""))
                            output.append(f"{Title}:{clean_appx_url(pdf_link)}*{key}\n" if key else f"{Title}:{clean_appx_url(pdf_link)}\n")
                        else:
                            output.append(f"{Title}:{clean_appx_url(pdf_link)}\n")
                elif mat_type == "VIDEO":
                    tasks.append(fetch_appx_video_id_details_v2(session, api, selected_batch_id, str(video_id), str(ytFlag), headers, folder_wise_course, user_id))
                elif mat_type == "FOLDER":
                    tasks.append(fetch_appx_folder_contents_v2(session, api, selected_batch_id, str(video_id), headers, folder_wise_course, user_id))

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
                            Title = (item.get("Title") or item.get("title") or "").strip()
                            video_id = item.get("id")
                            ytFlag = item.get("ytFlag", "0")
                            mat_type = str(item.get("material_type") or "").upper()

                            if mat_type in ("PDF", "TEST"):
                                pdf_link = appx_decrypt(item.get("pdf_link", "")) if item.get("pdf_link", "") and appx_decrypt(item.get("pdf_link", "")).endswith(".pdf") else None
                                if pdf_link:
                                    if str(item.get("is_pdf_encrypted")) == "1":
                                        key = appx_decrypt(item.get("pdf_encryption_key", ""))
                                        all_outputs.append(f"{Title}:{clean_appx_url(pdf_link)}*{key}\n" if key else f"{Title}:{clean_appx_url(pdf_link)}\n")
                                    else:
                                        all_outputs.append(f"{Title}:{clean_appx_url(pdf_link)}\n")
                                        
                                pdf_link2 = appx_decrypt(item.get("pdf_link2", "")) if item.get("pdf_link2", "") and appx_decrypt(item.get("pdf_link2", "")).endswith(".pdf") else None
                                if pdf_link2:
                                    if str(item.get("is_pdf2_encrypted")) == "1":
                                        key = appx_decrypt(item.get("pdf2_encryption_key", ""))
                                        all_outputs.append(f"{Title}:{clean_appx_url(pdf_link2)}*{key}\n" if key else f"{Title}:{clean_appx_url(pdf_link2)}\n")
                                    else:
                                        all_outputs.append(f"{Title}:{clean_appx_url(pdf_link2)}\n")

                            elif mat_type == "IMAGE":
                                thumbnail = item.get("thumbnail")
                                if thumbnail:
                                    all_outputs.append(f"{Title}:{clean_appx_url(thumbnail)}\n")
                                    
                            elif mat_type == "VIDEO":
                                direct_video_url = (
                                    item.get('video_url') or item.get('videoUrl') or item.get('hls_url')
                                    or item.get('hlsUrl') or item.get('stream_url') or item.get('streamUrl')
                                    or item.get('media_url') or item.get('mediaUrl') or item.get('mpd_url')
                                    or item.get('mpdUrl') or item.get('download_url') or item.get('downloadUrl')
                                    or item.get('url') or item.get('file_url') or item.get('fileUrl')
                                    or item.get('video_link') or item.get('videoLink') or item.get('source_url')
                                    or item.get('sourceUrl') or item.get('path') or item.get('link') or item.get('src')
                                )
                                if not direct_video_url:
                                    direct_video_url = (
                                        appx_decrypt(item.get("pdf_link", "")) if item.get("pdf_link") and not appx_decrypt(item.get("pdf_link", "")).endswith(".pdf") else None
                                        or appx_decrypt(item.get("pdf_link2", "")) if item.get("pdf_link2") and not appx_decrypt(item.get("pdf_link2", "")).endswith(".pdf") else None
                                        or appx_decrypt(item.get("file_link", "")) if item.get("file_link") else None
                                    )
                                
                                if direct_video_url:
                                    all_outputs.append(f"{Title}:{clean_appx_url(direct_video_url)}\n")
                                else:
                                    if selected_batch_id is not None and video_id is not None:
                                        tasks.append(fetch_appx_video_id_details_v3(session, api, selected_batch_id, str(video_id), str(ytFlag), headers, user_id))

    if tasks:
        results = await asyncio.gather(*tasks)
        for res in results:
            if isinstance(res, list):
                all_outputs.extend(res)

    return all_outputs

async def process_folder_wise_course_1(session: aiohttp.ClientSession, api: str, selected_batch_id: str, headers: Dict, user_id: int) -> List[str]:
    res = await fetch_appx_html_to_json(session, f"{api}/get/folder_contentsv2?course_id={selected_batch_id}&parent_id=-1", headers)
    if not res or not res.get("data"):
        res = await fetch_appx_html_to_json(session, f"{api}/get/folder_contentsv2?course_id={selected_batch_id}&parent_id=0", headers)

    all_outputs, tasks = [], []
    
    if res and "data" in res and isinstance(res["data"], list):
        for item in res["data"]:
            Title = (item.get("Title") or item.get("title") or "").strip()
            video_id = item.get("id")
            ytFlag = item.get("ytFlag", "0")
            mat_type = str(item.get("material_type") or "").upper()
            
            if mat_type in ("PDF", "TEST"):
                pdf_link = appx_decrypt(item.get("pdf_link", "")) if item.get("pdf_link", "") and appx_decrypt(item.get("pdf_link", "")).endswith(".pdf") else None
                if pdf_link:
                    if str(item.get("is_pdf_encrypted")) == "1":
                        key = appx_decrypt(item.get("pdf_encryption_key", ""))
                        all_outputs.append(f"{Title}:{clean_appx_url(pdf_link)}*{key}\n" if key else f"{Title}:{clean_appx_url(pdf_link)}\n")
                    else:
                        all_outputs.append(f"{Title}:{clean_appx_url(pdf_link)}\n")
                        
                pdf_link2 = appx_decrypt(item.get("pdf_link2", "")) if item.get("pdf_link2", "") and appx_decrypt(item.get("pdf_link2", "")).endswith(".pdf") else None
                if pdf_link2:
                    if str(item.get("is_pdf2_encrypted")) == "1":
                        key = appx_decrypt(item.get("pdf2_encryption_key", ""))
                        all_outputs.append(f"{Title}:{clean_appx_url(pdf_link2)}*{key}\n" if key else f"{Title}:{clean_appx_url(pdf_link2)}\n")
                    else:
                        all_outputs.append(f"{Title}:{clean_appx_url(pdf_link2)}\n")

            elif mat_type == "IMAGE":
                thumbnail = item.get("thumbnail")
                if thumbnail:
                    all_outputs.append(f"{Title}:{clean_appx_url(thumbnail)}\n")
                   
            elif mat_type == "VIDEO":
                direct_video_url = (
                    item.get('video_url') or item.get('videoUrl') or item.get('hls_url')
                    or item.get('hlsUrl') or item.get('stream_url') or item.get('streamUrl')
                    or item.get('media_url') or item.get('mediaUrl') or item.get('mpd_url')
                    or item.get('mpdUrl') or item.get('download_url') or item.get('downloadUrl')
                    or item.get('url') or item.get('file_url') or item.get('fileUrl')
                    or item.get('video_link') or item.get('videoLink') or item.get('source_url')
                    or item.get('sourceUrl') or item.get('path') or item.get('link') or item.get('src')
                )
                if not direct_video_url:
                    direct_video_url = (
                        appx_decrypt(item.get("pdf_link", "")) if item.get("pdf_link") and not appx_decrypt(item.get("pdf_link", "")).endswith(".pdf") else None
                        or appx_decrypt(item.get("pdf_link2", "")) if item.get("pdf_link2") and not appx_decrypt(item.get("pdf_link2", "")).endswith(".pdf") else None
                        or appx_decrypt(item.get("file_link", "")) if item.get("file_link") else None
                    )
                if direct_video_url:
                    all_outputs.append(f"{Title}:{clean_appx_url(direct_video_url)}\n")
                else:
                    tasks.append(fetch_appx_video_id_details_v2(session, api, selected_batch_id, str(video_id), str(ytFlag), headers, 1, user_id))

            elif mat_type == "FOLDER":
                tasks.append(fetch_appx_folder_contents_v2(session, api, selected_batch_id, str(item.get("id")), headers, 1, user_id))

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

            headers = {
                "Client-Service": "Appx",
                "Auth-Key": "appxapi",
                "source": "website",
                "User-ID": str(extracted_jwt_userid)
            }
            if token:
                headers["Authorization"] = token

            course_type = await prompt_user(
                bot, m, editable,
                f"**Connected to:** `{selected_app_name}`\n\n"
                f"Select Course Extraction Mode:\n\n"
                f"**1. 📚 My / Purchased Courses**\n"
                f"**2. 🌐 All / Public Courses (No Login Required)**",
                user_id
            )

            if course_type == "1":
                if not token:
                    token = await prompt_user(bot, m, editable, "**Send Token for Purchased Courses:**", user_id)
                    extracted_jwt_userid = extract_user_id_from_jwt(token)
                    headers["Authorization"] = token
                    headers["User-ID"] = str(extracted_jwt_userid)
                api_endpoint = f"{api}/get/mycourse"
            else:
                api_endpoint = f"{api}/get/allcourse2"

            await editable.edit("🔍 **Fetching courses list... ⏳**")
            res = await fetch_appx_html_to_json(session, api_endpoint, headers=headers)

            if not res or not res.get("data"):
                if course_type == "1":
                    res = await fetch_appx_html_to_json(session, f"{api}/get/allcourse2", headers=headers)

            if not res or not res.get("data"):
                await editable.edit("**No courses found or Token/API expired! ❌**")
                return

            course_data = res["data"]
            if not isinstance(course_data, list):
                course_data = [course_data]

            courses_text = f"**Available Courses for {selected_app_name}:**\n\n"
            for cnt, item in enumerate(course_data):
                c_name = item.get("course_name") or item.get("name") or "Unnamed Course"
                c_id = item.get("id") or item.get("course_id")
                courses_text += f"<blockquote>**{cnt + 1}.** `{c_name}` (ID: `{c_id}`)</blockquote>\n"

            selection_idx = await prompt_user(bot, m, editable, f"{courses_text}\n**Send Course Index Number to Extract:**", user_id)
            if not selection_idx.isdigit() or int(selection_idx) < 1 or int(selection_idx) > len(course_data):
                await editable.edit("**Invalid course selection! ❌**")
                return

            selected_course = course_data[int(selection_idx) - 1]
            selected_batch_id = str(selected_course.get("id") or selected_course.get("course_id"))
            selected_course_name = selected_course.get("course_name") or selected_course.get("name") or "Course"
            folder_wise_course = selected_course.get("folder_wise_course", 0)

            clean_file_name = re.sub(r'[\\/*?:"<>|]', "", selected_course_name).strip().replace(" ", "_")
            if not clean_file_name:
                clean_file_name = f"Appx_Course_{selected_batch_id}"

            await editable.edit(f"⏳ **Extracting contents for:** `{selected_course_name}`\n*Resolving video streams & notes...*")
            start_time = time.time()

            if str(folder_wise_course) == "1":
                outputs = await process_folder_wise_course_1(session, api, selected_batch_id, headers, user_id)
            else:
                outputs = await process_folder_wise_course_0(session, api, selected_batch_id, headers, user_id)

            if not outputs:
                # Fallback to alternate folder structure if empty
                if str(folder_wise_course) == "1":
                    outputs = await process_folder_wise_course_0(session, api, selected_batch_id, headers, user_id)
                else:
                    outputs = await process_folder_wise_course_1(session, api, selected_batch_id, headers, user_id)

            if not outputs:
                await editable.edit("**No lessons, videos, or PDFs found in this course! ❌**")
                return

            txt_file_path = f"{clean_file_name}.txt"
            with open(txt_file_path, "w", encoding="utf-8") as f:
                for line in outputs:
                    f.write(line)

            elapsed_str = format_time(time.time() - start_time)
            await send_extracted_text_file(
                bot, m, editable, txt_file_path,
                app_name=f"Appx - {selected_app_name}",
                batch_name=selected_course_name,
                total_links=len(outputs),
                elapsed_time=elapsed_str
            )

    except ProcessCancelledException:
        pass
    except Exception as e:
        logger.exception("Error during Appx extraction:")
        if editable:
            try:
                await editable.edit(f"**Appx Extraction Failed:** `{e}`")
            except Exception:
                pass
    finally:
        if clean_file_name:
            txt_file_path = f"{clean_file_name}.txt"
            if os.path.exists(txt_file_path):
                try: os.remove(txt_file_path)
                except: pass

def register_appxwp_handlers(bot: Client):
    @bot.on_callback_query(filters.regex("^appxwp$"))
    async def appx_callback(client: Client, callback_query):
        user_id = callback_query.from_user.id if callback_query.from_user else 0
        if not is_authorized(user_id):
            await callback_query.answer("⛔ Access Denied! You are not authorized.", show_alert=True)
            return
        await callback_query.answer()
        asyncio.create_task(process_appxwp(client, callback_query.message, user_id))

    @bot.on_message(filters.command(["three", "appx", "appxwp"]) & filters.private)
    async def appx_cmd_handler(client: Client, message: Message):
        user_id = message.from_user.id
        if not is_authorized(user_id):
            await message.reply_text("**You are not authorized to use this bot! ❌**")
            return
        await process_appxwp(client, message, user_id)

process_appx = process_appxwp
