import base64
import os
import tempfile
import requests
from fastapi import APIRouter, HTTPException, Header, Request
from instagrapi import Client

API_KEY = os.environ.get("API_KEY")

router = APIRouter()


def parse_netscape_cookies(cookie_text: str) -> dict:
    cookies = {}
    for line in cookie_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            name, value = parts[5], parts[6]
            cookies[name] = value
    return cookies


def download_to_temp(url: str, suffix: str = ".mp4") -> str:
    resp = requests.get(url, stream=True, timeout=180)
    resp.raise_for_status()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    with tmp as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
    return tmp.name


@router.post("/instagram/publish")
async def publish_instagram(request: Request, x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    cookie_b64 = body.get("cookie_b64")
    presigned_url = body.get("presigned_url")
    caption = body.get("caption", "")
    post_feed = bool(body.get("post_feed", False))
    post_story = bool(body.get("post_story", False))

    if not cookie_b64 or not presigned_url:
        raise HTTPException(status_code=400, detail="cookie_b64 و presigned_url الزامی هستن")

    if not post_feed and not post_story:
        return {"success": False, "stage": "input", "error": "نه post_feed نه post_story فعال نیست"}

    # مرحله ۱: دیکود کوکی و لاگین
    try:
        cookie_text = base64.b64decode(cookie_b64).decode("utf-8")
        cookies = parse_netscape_cookies(cookie_text)
        sessionid = cookies.get("sessionid")
        if not sessionid:
            return {"success": False, "stage": "parse", "error": "sessionid توی کوکی‌ها پیدا نشد"}
        cl = Client()
        cl.login_by_sessionid(sessionid)
    except Exception as e:
        return {"success": False, "stage": "login", "error": str(e)}

    # مرحله ۲: دانلود فایل ویدیو از presigned_url
    try:
        video_path = download_to_temp(presigned_url)
    except Exception as e:
        return {"success": False, "stage": "download", "error": str(e)}

    result = {"success": True, "feed": None, "story": None}

    # مرحله ۳: آپلود به فید (اگه فعال باشه)
    if post_feed:
        try:
            media = cl.clip_upload(video_path, caption)
            result["feed"] = {"success": True, "media_id": str(media.pk), "code": media.code}
        except Exception as e:
            result["feed"] = {"success": False, "error": str(e)}

    # مرحله ۴: آپلود به استوری (اگه فعال باشه)
    if post_story:
        try:
            story = cl.video_upload_to_story(video_path, caption)
            result["story"] = {"success": True, "media_id": str(story.pk)}
        except Exception as e:
            result["story"] = {"success": False, "error": str(e)}

    # پاکسازی فایل موقت
    try:
        os.remove(video_path)
    except Exception:
        pass

    # اگه هرکدوم که فعال بوده شکست خورده باشه، success کلی رو false کن
    if post_feed and result["feed"] and not result["feed"]["success"]:
        result["success"] = False
    if post_story and result["story"] and not result["story"]["success"]:
        result["success"] = False

    return result
