"""
سرویس دانلود ویدیو/عکس (یوتیوب + اینستاگرام) — برای اجرا روی Render
--------------------------------------------------------------------
این سرویس یه لینک (یوتیوب یا اینستاگرام) می‌گیره، با yt-dlp خودش دانلودش
می‌کنه، و مستقیم بایت‌های فایل رو برمی‌گردونه. این‌جوری n8n Cloud دیگه لازم
نیست به لینک مستقیم CDN (که قفل IP/سشنه) وصل بشه؛ فقط به این سرور خودت
وصل می‌شه.

نکته‌ی امنیتی: یه کلید ساده (X-API-Key) گذاشتیم تا هرکسی که آدرس این سرویس
رو پیدا کنه نتونه ازش سوءاستفاده کنه (چون هر دانلود، پهنای‌باند/زمان مصرف
می‌کنه).

نکته‌ی کوکی: برای هر پلتفرم، یه Environment Variable جدا باید ست بشه
(چون کوکی‌های یوتیوب و اینستاگرام کاملاً مستقل و مربوط به دو دامنه‌ی
متفاوتن):
  - YOUTUBE_COOKIES_B64
  - INSTAGRAM_COOKIES_B64
هرکدوم که ست نشده باشن، سرویس بدون کوکی (anonymous) تلاش می‌کنه —
یعنی اگه فقط کوکی اینستاگرام رو داری، یوتیوب همچنان (با همون مشکل قبلی)
بدون‌کوکی امتحان می‌شه.
"""

import os
import uuid
import base64
import subprocess
import mimetypes
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from fastapi.responses import FileResponse

app = FastAPI(title="Media Downloader (YouTube + Instagram)")

# کلید امنیتی از متغیر محیطی خونده می‌شه (تو تنظیمات Render ست می‌کنیم)
API_KEY = os.environ.get("API_KEY", "")

DOWNLOAD_DIR = Path("/tmp/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

COOKIES_DIR = Path("/tmp/cookies")
COOKIES_DIR.mkdir(parents=True, exist_ok=True)


def setup_cookies(env_var_name: str, filename: str) -> Optional[str]:
    """
    کوکی base64-شده رو از Environment Variable می‌خونه، دیکد می‌کنه، و
    توی /tmp به یه فایل کوکی (فرمت Netscape) می‌نویسه.
    اگه متغیر ست نشده باشه، None برمی‌گردونه (یعنی بدون کوکی تلاش می‌کنیم).
    """
    b64_value = os.environ.get(env_var_name, "")
    if not b64_value:
        return None
    try:
        raw = base64.b64decode(b64_value)
    except Exception:
        return None
    path = COOKIES_DIR / filename
    path.write_bytes(raw)
    return str(path)


# کوکی‌ها فقط یه‌بار، موقع بالا اومدن سرویس آماده می‌شن (نه هر درخواست —
# چون دیکد/نوشتن فایل کار اضافیه که نیازی نیست هر بار تکرار بشه)
YOUTUBE_COOKIES_PATH = setup_cookies("YOUTUBE_COOKIES_B64", "youtube_cookies.txt")
INSTAGRAM_COOKIES_PATH = setup_cookies("INSTAGRAM_COOKIES_B64", "instagram_cookies.txt")


def cleanup_file(path: Path):
    """بعد از ارسال فایل به n8n، از دیسک موقت پاکش می‌کنیم (چون فضای Render محدوده)"""
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def detect_platform(url: str) -> str:
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if "instagram.com" in url:
        return "instagram"
    return "unknown"


def build_command(platform: str, url: str, output_template: str) -> list:
    if platform == "youtube":
        cmd = [
            "yt-dlp",
            "-f", "best[ext=mp4][filesize<50M]/best[ext=mp4]/best",
            "-o", output_template,
            "--no-playlist",
            "--max-filesize", "50M",
            "--extractor-args", "youtube:player_client=android,web",
            url,
        ]
        if YOUTUBE_COOKIES_PATH:
            cmd += ["--cookies", YOUTUBE_COOKIES_PATH]
        return cmd

    if platform == "instagram":
        # پست اینستاگرام می‌تونه ویدیو یا عکس باشه، پس فرمت رو به mp4 محدود نمی‌کنیم
        cmd = [
            "yt-dlp",
            "-f", "best[filesize<50M]/best",
            "-o", output_template,
            "--no-playlist",
            "--max-filesize", "50M",
            url,
        ]
        if INSTAGRAM_COOKIES_PATH:
            cmd += ["--cookies", INSTAGRAM_COOKIES_PATH]
        return cmd

    raise HTTPException(status_code=400, detail="Unsupported URL (only YouTube and Instagram)")


def guess_media_type(file_path: Path) -> str:
    """از روی پسوند واقعی فایل دانلودشده، media_type درست رو حدس می‌زنه
    (چون دیگه فرض ثابت 'همه‌چیز ویدیوعه' درست نیست — اینستاگرام می‌تونه عکس بده)"""
    mime, _ = mimetypes.guess_type(file_path.name)
    return mime or "application/octet-stream"


@app.get("/")
def health_check():
    # Render برای بیدار نگه‌داشتن سرویس، این مسیر رو پینگ می‌کنه
    return {"status": "ok"}


@app.get("/download")
def download_video(
    url: str,
    background_tasks: BackgroundTasks,
    x_api_key: str = Header(default=""),
):
    # چک کلید امنیتی
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    platform = detect_platform(url)
    if platform == "unknown":
        raise HTTPException(status_code=400, detail="Only YouTube and Instagram URLs are supported")

    file_id = str(uuid.uuid4())
    output_template = str(DOWNLOAD_DIR / f"{file_id}.%(ext)s")

    cmd = build_command(platform, url, output_template)

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=502, detail=f"yt-dlp failed: {e.stderr[-500:]}")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Download timed out")

    # پیدا کردن فایل واقعی دانلودشده (پسوندش رو yt-dlp خودش تعیین می‌کنه)
    matches = list(DOWNLOAD_DIR.glob(f"{file_id}.*"))
    if not matches:
        raise HTTPException(status_code=500, detail="Downloaded file not found")

    file_path = matches[0]
    background_tasks.add_task(cleanup_file, file_path)

    return FileResponse(
        path=file_path,
        media_type=guess_media_type(file_path),
        filename=file_path.name,
        background=background_tasks,
    )
