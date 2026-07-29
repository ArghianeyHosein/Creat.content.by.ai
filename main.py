"""
سرویس کوچیک دانلود ویدیو (برای اجرا روی Render)
------------------------------------------------
این سرویس یه لینک یوتیوب می‌گیره، با yt-dlp خودش دانلودش می‌کنه، و مستقیم
بایت‌های فایل رو برمی‌گردونه. این‌جوری n8n Cloud دیگه لازم نیست به لینک
مستقیم CDN گوگل (که قفل IP/سشنه) وصل بشه؛ فقط به این سرور خودت وصل می‌شه.

نکته‌ی امنیتی: یه کلید ساده (X-API-Key) گذاشتم تا هرکسی که آدرس این سرویس رو
پیدا کنه نتونه ازش سوءاستفاده کنه (چون هر دانلود، پهنای‌باند/زمان مصرف می‌کنه).
"""

import os
import uuid
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from fastapi.responses import FileResponse

app = FastAPI(title="YouTube CC Downloader")

# کلید امنیتی از متغیر محیطی خونده می‌شه (تو تنظیمات Render ست می‌کنیم)
API_KEY = os.environ.get("API_KEY", "")

DOWNLOAD_DIR = Path("/tmp/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def cleanup_file(path: Path):
    """بعد از ارسال فایل به n8n، از دیسک موقت پاکش می‌کنیم (چون فضای Render محدوده)"""
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


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

    if not url or ("youtube.com" not in url and "youtu.be" not in url):
        raise HTTPException(status_code=400, detail="Only YouTube URLs are supported")

    file_id = str(uuid.uuid4())
    output_template = str(DOWNLOAD_DIR / f"{file_id}.%(ext)s")

    # فرمت: یه فایل mp4 که هم تصویر هم صدا توش mux شده (تا نیازی به ffmpeg نداشته باشیم)
    # محدودیت حجم: حداکثر ۵۰ مگابایت، تا با پلن رایگان Render/Backblaze سازگار بمونه
    cmd = [
        "yt-dlp",
        "-f", "best[ext=mp4][filesize<50M]/best[ext=mp4]/best",
        "-o", output_template,
        "--no-playlist",
        "--max-filesize", "50M",
        "--extractor-args", "youtube:player_client=android,web",
        url,
    ]

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
        media_type="video/mp4",
        filename=file_path.name,
        background=background_tasks,
    )
