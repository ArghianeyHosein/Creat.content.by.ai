import base64
import os
import subprocess
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


def generate_thumbnail(video_path: str) -> str:
    """با ffmpeg (که قبلاً برای دابینگ روی سرویس نصبه) یک فریم از ویدیو رو به‌عنوان thumbnail می‌گیره."""
    thumb_path = video_path + ".jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-ss", "00:00:01", "-vframes", "1", thumb_path],
        check=True,
        capture_output=True,
    )
    return thumb_path


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
    # پروکسی دیگه از داخل Python انتخاب/چرخش نمی‌شه — n8n (تنها جایی که حق
    # تغییر/خواندن دیتابیس رو داره) از قبل پروکسی فعال رو تعیین و تست کرده
    # و اینجا مستقیم توی بدنه‌ی درخواست می‌فرسته. اگه فرستاده نشه، بدون
    # پروکسی (مستقیم) تلاش می‌کنیم.
    proxy_url = body.get("proxy_url")
    # تنظیمات دستگاه (device ID, UUID و غیره) که n8n از دفعه‌ی قبل ذخیره
    # کرده — اگه بفرسته، همون رو لود می‌کنیم تا از دید اینستاگرام همیشه
    # همون «دستگاه» باشیم (نه هر بار یه دستگاه تصادفی تازه، که خودش یکی از
    # محرک‌های چالش امنیتیه). در پایان، تنظیمات فعلی رو توی پاسخ برمی‌گردونیم
    # تا n8n ذخیره‌ش کنه (چه بار اول باشه چه به‌روزرسانی).
    device_settings = body.get("device_settings")

    if not cookie_b64 or not presigned_url:
        raise HTTPException(status_code=400, detail="cookie_b64 و presigned_url الزامی هستن")

    if not post_feed and not post_story:
        return {"success": False, "stage": "input", "error": "نه post_feed نه post_story فعال نیست"}

    try:
        cookie_text = base64.b64decode(cookie_b64).decode("utf-8")
        cookies = parse_netscape_cookies(cookie_text)
        sessionid = cookies.get("sessionid")
        if not sessionid:
            return {"success": False, "stage": "parse", "error": "sessionid توی کوکی‌ها پیدا نشد"}
    except Exception as e:
        return {"success": False, "stage": "parse", "error": str(e)}

    # لاگین دیگه retry یا چرخش پروکسی نمی‌کنه (نه خودکار، نه با دسترسی مستقیم
    # به دیتابیس). تجربه نشون داد وقتی اینستاگرام یه چالش امنیتی سطح اکانت
    # (ChallengeRequired) می‌ذاره، عوض کردن IP بین تلاش‌ها کمکی نمی‌کنه و
    # می‌تونه خودش الگوی مشکوکی بسازه. فقط یه‌بار با پروکسی‌ای که n8n فرستاده
    # امتحان می‌کنیم؛ نتیجه (موفق/ناموفق) رو کامل توی پاسخ برمی‌گردونیم تا
    # n8n خودش تصمیم بگیره لاگ کنه یا چرخش پروکسی رو صدا بزنه.
    try:
        cl = Client()
        if device_settings:
            cl.set_settings(device_settings)
        if proxy_url:
            cl.set_proxy(proxy_url)
        cl.login_by_sessionid(sessionid)
    except Exception as e:
        # حتی وقتی لاگین fail می‌شه، تنظیمات دستگاه رو برمی‌گردونیم — چون
        # instagrapi موقع ساخت Client() این تنظیمات (device_id, uuid و غیره)
        # رو تولید می‌کنه، حتی اگه لاگین بعدش fail بشه. اگه این‌ها رو دور
        # بریزیم، دفعه‌ی بعد یه دستگاه کاملاً جدید می‌سازیم که دقیقاً همون
        # الگوی مشکوکیه که می‌خوایم جلوش رو بگیریم.
        return {
            "success": False,
            "stage": "login",
            "error": str(e),
            "device_settings": cl.get_settings(),
        }

    try:
        video_path = download_to_temp(presigned_url)
    except Exception as e:
        return {"success": False, "stage": "download", "error": str(e), "device_settings": cl.get_settings()}

    thumb_path = None
    try:
        thumb_path = generate_thumbnail(video_path)
    except Exception:
        thumb_path = None  # اگه ساخت thumbnail شکست خورد، بدون اون امتحان می‌کنیم

    result = {"success": True, "feed": None, "story": None, "device_settings": cl.get_settings()}

    if post_feed:
        try:
            if thumb_path:
                media = cl.clip_upload(video_path, caption, thumbnail=thumb_path)
            else:
                media = cl.clip_upload(video_path, caption)
            result["feed"] = {"success": True, "media_id": str(media.pk), "code": media.code}
        except Exception as e:
            result["feed"] = {"success": False, "error": str(e)}

    if post_story:
        # آپلود استوری هم دیگه retry نمی‌کنه — فقط یه‌بار امتحان می‌کنیم،
        # نتیجه رو (موفق یا خطای کامل) توی پاسخ برمی‌گردونیم.
        try:
            if thumb_path:
                story = cl.video_upload_to_story(video_path, caption, thumbnail=thumb_path)
            else:
                story = cl.video_upload_to_story(video_path, caption)
            result["story"] = {"success": True, "media_id": str(story.pk)}
        except Exception as e:
            result["story"] = {"success": False, "error": str(e)}

    for p in (video_path, thumb_path):
        if p:
            try:
                os.remove(p)
            except Exception:
                pass

    if post_feed and result["feed"] and not result["feed"]["success"]:
        result["success"] = False
    if post_story and result["story"] and not result["story"]["success"]:
        result["success"] = False

    return result
