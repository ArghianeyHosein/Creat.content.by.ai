import base64
import os
import subprocess
import tempfile
import requests
from fastapi import APIRouter, HTTPException, Header, Request
from instagrapi import Client

from proxy_utils import get_active_proxy_url, log_error

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
    job_id = body.get("job_id")  # اختیاریه؛ اگه دیسپچر n8n بفرستدش، لاگ‌ها بهش وصل می‌شن
    cookie_b64 = body.get("cookie_b64")
    presigned_url = body.get("presigned_url")
    caption = body.get("caption", "")
    post_feed = bool(body.get("post_feed", False))
    post_story = bool(body.get("post_story", False))

    if not cookie_b64 or not presigned_url:
        raise HTTPException(status_code=400, detail="cookie_b64 و presigned_url الزامی هستن")

    if not post_feed and not post_story:
        return {"success": False, "stage": "input", "error": "نه post_feed نه post_story فعال نیست"}

    try:
        cookie_text = base64.b64decode(cookie_b64).decode("utf-8")
        cookies = parse_netscape_cookies(cookie_text)
        sessionid = cookies.get("sessionid")
        if not sessionid:
            msg = "sessionid توی کوکی‌ها پیدا نشد"
            log_error("Publishing - Instagram", msg, job_id=job_id, details={"stage": "parse"})
            return {"success": False, "stage": "parse", "error": msg}
    except Exception as e:
        log_error("Publishing - Instagram", str(e), job_id=job_id, details={"stage": "parse"})
        return {"success": False, "stage": "parse", "error": str(e)}

    # نکته‌ی مهم: لاگین دیگه retry یا چرخش پروکسی نمی‌کنه. تجربه نشون داد وقتی
    # اینستاگرام یه چالش امنیتی سطح اکانت (ChallengeRequired) می‌ذاره، عوض کردن
    # IP بین تلاش‌ها نه‌تنها کمکی نمی‌کنه، بلکه خودش الگوی مشکوکیه (چند IP
    # پشت‌سرهم برای یه اکانت) که می‌تونه باعث تشدید همون چالش بشه. پس فقط یه‌بار
    # با همون پروکسی فعلی (ثابت) امتحان می‌کنیم؛ اگه fail شد، بلافاصله و کامل
    # لاگ می‌کنیم و برمی‌گردیم — بدون تلاش مجدد خودکار.
    try:
        cl = Client()
        proxy_url = get_active_proxy_url()
        if proxy_url:
            cl.set_proxy(proxy_url)
        cl.login_by_sessionid(sessionid)
    except Exception as e:
        error_msg = str(e)
        # لاگ رو همین‌جا و بی‌درنگ ثبت می‌کنیم — نه بعد از return — چون اگه
        # n8n زودتر از رسیدن جواب HTTP timeout بزنه، این تنها جایی می‌مونه
        # که خطای واقعی ثبت شده باشه (به‌جای اینکه فقط توی لاگ خام Render گم بشه).
        log_error(
            "Publishing - Instagram",
            error_msg,
            job_id=job_id,
            details={"stage": "login", "proxy": proxy_url},
        )
        return {"success": False, "stage": "login", "error": error_msg}

    try:
        video_path = download_to_temp(presigned_url)
    except Exception as e:
        log_error("Publishing - Instagram", str(e), job_id=job_id, details={"stage": "download"})
        return {"success": False, "stage": "download", "error": str(e)}

    thumb_path = None
    try:
        thumb_path = generate_thumbnail(video_path)
    except Exception:
        thumb_path = None  # اگه ساخت thumbnail شکست خورد، بدون اون امتحان می‌کنیم

    result = {"success": True, "feed": None, "story": None}

    if post_feed:
        try:
            if thumb_path:
                media = cl.clip_upload(video_path, caption, thumbnail=thumb_path)
            else:
                media = cl.clip_upload(video_path, caption)
            result["feed"] = {"success": True, "media_id": str(media.pk), "code": media.code}
        except Exception as e:
            result["feed"] = {"success": False, "error": str(e)}
            log_error("Publishing - Instagram", str(e), job_id=job_id, details={"stage": "feed"})

    if post_story:
        # آپلود استوری هم دیگه retry نمی‌کنه (همون منطق: تکرار خودکار می‌تونه
        # به‌جای رفع مشکل گذرا، رفتار مشکوک اضافه‌ای برای اکانت بسازه). فقط
        # یه‌بار امتحان می‌کنیم و هر نتیجه‌ای (موفق/ناموفق) رو همون لحظه لاگ می‌کنیم.
        try:
            if thumb_path:
                story = cl.video_upload_to_story(video_path, caption, thumbnail=thumb_path)
            else:
                story = cl.video_upload_to_story(video_path, caption)
            result["story"] = {"success": True, "media_id": str(story.pk)}
        except Exception as e:
            result["story"] = {"success": False, "error": str(e)}
            log_error("Publishing - Instagram", str(e), job_id=job_id, details={"stage": "story"})

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
