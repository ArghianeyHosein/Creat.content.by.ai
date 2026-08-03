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
import re
import json
import uuid
import base64
import subprocess
import mimetypes
from pathlib import Path
from typing import Optional

import boto3
from botocore.client import Config as BotoConfig
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks, Query
from fastapi.responses import FileResponse

app = FastAPI(title="Media Downloader (YouTube + Instagram)")

# کلید امنیتی از متغیر محیطی خونده می‌شه (تو تنظیمات Render ست می‌کنیم)
API_KEY = os.environ.get("API_KEY", "")

# اطلاعات بک‌بلیز — برای ساخت لینک موقت دانلود (presigned URL) از فایل‌های
# private. این‌ها رو باید توی Environment Variables سرویس Render ست کنی:
#   B2_KEY_ID          -> Access Key ID بک‌بلیز
#   B2_APPLICATION_KEY  -> Secret Access Key بک‌بلیز
#   B2_ENDPOINT         -> مثلا https://s3.us-east-005.backblazeb2.com
#   B2_BUCKET_NAME      -> مثلا Creator-content-by-AI
B2_KEY_ID = os.environ.get("B2_KEY_ID", "")
B2_APPLICATION_KEY = os.environ.get("B2_APPLICATION_KEY", "")
B2_ENDPOINT = os.environ.get("B2_ENDPOINT", "")
B2_BUCKET_NAME = os.environ.get("B2_BUCKET_NAME", "")

_s3_client = None


def get_s3_client():
    """S3 client رو فقط یه‌بار می‌سازه (نه هر درخواست) و برای درخواست‌های بعدی
    از همون استفاده می‌کنه."""
    global _s3_client
    if _s3_client is None:
        if not (B2_KEY_ID and B2_APPLICATION_KEY and B2_ENDPOINT):
            raise HTTPException(
                status_code=500,
                detail="Backblaze credentials not configured (B2_KEY_ID / B2_APPLICATION_KEY / B2_ENDPOINT)",
            )
        _s3_client = boto3.client(
            "s3",
            endpoint_url=B2_ENDPOINT,
            aws_access_key_id=B2_KEY_ID,
            aws_secret_access_key=B2_APPLICATION_KEY,
            config=BotoConfig(signature_version="s3v4"),
        )
    return _s3_client

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


def cookies_for_platform(platform: str) -> Optional[str]:
    if platform == "youtube":
        return YOUTUBE_COOKIES_PATH
    if platform == "instagram":
        return INSTAGRAM_COOKIES_PATH
    return None


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


def build_info_command(platform: str, url: str) -> list:
    """
    مثل build_command ولی به‌جای دانلود واقعی فایل، فقط متادیتا (کپشن،
    هشتگ و ...) رو به‌صورت JSON از yt-dlp می‌گیره. --skip-download یعنی
    هیچ فایلی دانلود نمی‌شه، فقط اطلاعاتش استخراج می‌شه (سریع و کم‌مصرف).
    """
    if platform not in ("youtube", "instagram"):
        raise HTTPException(status_code=400, detail="Unsupported URL (only YouTube and Instagram)")

    cmd = [
        "yt-dlp",
        "--skip-download",
        "--dump-json",
        "--no-playlist",
        url,
    ]
    cookies_path = cookies_for_platform(platform)
    if cookies_path:
        cmd += ["--cookies", cookies_path]
    return cmd


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


@app.get("/info")
def get_info(
    url: str = Query(...),
    x_api_key: str = Header(default=""),
):
    """
    فقط متادیتای محتوا (کپشن، هشتگ) رو برمی‌گردونه، بدون دانلود فایل.
    برای اضافه‌کردن این اطلاعات به data_collection توی Supabase استفاده می‌شه.
    """
    # چک کلید امنیتی — دقیقاً همون منطق /download
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    platform = detect_platform(url)
    if platform == "unknown":
        raise HTTPException(status_code=400, detail="Only YouTube and Instagram URLs are supported")

    cmd = build_info_command(platform, url)

    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=502, detail=f"yt-dlp failed: {e.stderr[-500:]}")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Fetching info timed out")

    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Could not parse yt-dlp output")

    caption = info.get("description") or ""
    hashtags = re.findall(r"#(\w+)", caption)

    return {
        "platform": platform,
        "caption": caption,
        "hashtags": hashtags,
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
    }


@app.get("/presign")
def get_presigned_url(
    key: str = Query(..., description="مسیر فایل داخل باکت، مثلا instagram/197373549-123.mp4"),
    expires_in: int = Query(3600, ge=60, le=604800, description="مدت اعتبار لینک به ثانیه (پیش‌فرض ۱ ساعت، حداکثر ۷ روز)"),
    x_api_key: str = Header(default=""),
):
    """
    برای فایل‌های private روی بک‌بلیز، یه لینک موقت (presigned URL) می‌سازه
    که تا expires_in ثانیه دیگه معتبره و بدون نیاز به احراز هویت اضافه
    قابل دانلوده. مقدار key همون file_key ایه که توی data_collection
    ذخیره کردیم.
    """
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    if not B2_BUCKET_NAME:
        raise HTTPException(status_code=500, detail="B2_BUCKET_NAME is not configured")

    client = get_s3_client()

    try:
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": B2_BUCKET_NAME, "Key": key},
            ExpiresIn=expires_in,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not generate presigned URL: {e}")

    return {"url": url, "expires_in": expires_in}

"""
ماژول دوبله خودکار ویدیو
مراحل: جدا کردن صدا -> تشخیص گوینده و جنسیت -> تبدیل به متن -> ترجمه -> TTS -> ترکیب نهایی

نحوه‌ی اضافه کردن به سرویس فعلی:
    from dubbing import router as dubbing_router
    app.include_router(dubbing_router, prefix="/dub")

Environment Variables مورد نیاز (باید روی Render ست بشن):
    HF_TOKEN        - توکن Hugging Face برای دانلود مدل‌های pyannote
    MISTRAL_API_KEY - برای ترجمه متن (همونی که از قبل داری)
"""

import os
import uuid
import shutil
import subprocess
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

HF_TOKEN = os.environ.get("HF_TOKEN")
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")
WORK_DIR = Path("/tmp/dubbing")
WORK_DIR.mkdir(exist_ok=True)

# ---------- مدل‌های ورودی/خروجی ----------

class DubRequest(BaseModel):
    video_url: str          # لینک موقت presign شده از بک‌بلیز
    content_id: str         # شناسه‌ی رکورد مربوطه در data_collection
    original_language: str | None = None   # اگه از قبل مشخصه (مثلا "en")، حدس زدن خودکار رو رد کن


class DubResult(BaseModel):
    content_id: str
    dubbed_transcript: str | None = None
    original_language: str | None = None
    local_output_path: str | None = None   # مسیر فایل نهایی روی دیسک سرویس؛ آپلود به بک‌بلیز جدا انجام میشه
    skipped: bool = False
    skip_reason: str | None = None


# ---------- توابع کمکی هر مرحله ----------

def has_audio_stream(video_path: Path) -> bool:
    """چک می‌کنه ویدیو اصلاً استریم صوتی داره یا نه (با ffprobe)"""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "csv=p=0",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return bool(result.stdout.strip())


def extract_audio(video_path: Path, audio_path: Path) -> None:
    """جدا کردن صدا از ویدیو با ffmpeg"""
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(audio_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg extract failed: {result.stderr}")


def diarize_and_detect_gender(audio_path: Path) -> list[dict]:
    """
    خروجی: لیستی از segmentها به شکل
    [{"start": 0.0, "end": 3.2, "speaker": "SPEAKER_00", "gender": "male"}, ...]
    """
    from pyannote.audio import Pipeline
    import numpy as np
    import librosa

    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1", use_auth_token=HF_TOKEN
    )
    diarization = pipeline(str(audio_path))

    y, sr = librosa.load(str(audio_path), sr=16000)
    segments = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        start_sample = int(turn.start * sr)
        end_sample = int(turn.end * sr)
        chunk = y[start_sample:end_sample]
        if len(chunk) < sr * 0.1:  # قطعه‌ی خیلی کوتاه، رد شو
            continue
        f0 = librosa.yin(chunk, fmin=65, fmax=400, sr=sr)
        f0 = f0[f0 > 0]
        pitch = float(np.median(f0)) if len(f0) else 165.0
        gender = "male" if pitch < 165 else "female"
        segments.append({
            "start": turn.start,
            "end": turn.end,
            "speaker": speaker,
            "gender": gender,
        })
    return segments


def transcribe(audio_path: Path) -> tuple[str, str]:
    """تبدیل صدا به متن با faster-whisper. خروجی: (متن کامل, زبان تشخیص داده‌شده)"""
    from faster_whisper import WhisperModel

    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(audio_path))
    full_text = " ".join(seg.text.strip() for seg in segments)
    return full_text, info.language


def translate_to_persian(text: str) -> str:
    """ترجمه متن به فارسی با Mistral"""
    from mistralai import Mistral

    client = Mistral(api_key=MISTRAL_API_KEY)
    resp = client.chat.complete(
        model="mistral-small-latest",
        messages=[{
            "role": "user",
            "content": f"این متن رو فقط ترجمه کن به فارسی روان، بدون هیچ توضیح اضافه:\n\n{text}",
        }],
    )
    return resp.choices[0].message.content.strip()


# ---------- dubbing ----------

async def synthesize_speech(text: str, gender: str, output_path: Path) -> None:
    """تولید صدا با edge-tts بر اساس جنسیت گوینده"""
    import edge_tts

    voice = "fa-IR-DilaraNeural" if gender == "female" else "fa-IR-FaridNeural"
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(output_path))


def merge_audio_with_video(video_path: Path, audio_path: Path, output_path: Path) -> None:
    """جایگزینی صدای اصلی ویدیو با صدای دوبله‌شده"""
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path), "-i", str(audio_path),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy", "-c:a", "aac",
        "-shortest", str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg merge failed: {result.stderr}")


# ---------- endpoint اصلی ----------

@router.post("/process", response_model=DubResult)
async def process_dubbing(req: DubRequest):
    """
    نکته: چون ویدیوها حداکثر یک دقیقه‌ان و فرکانس اجرا حدود یک بار در ساعت،
    این endpoint به‌صورت synchronous (بدون صف جدا) کل کار رو انجام می‌ده.
    """
    if not HF_TOKEN:
        raise HTTPException(500, "HF_TOKEN تنظیم نشده روی سرویس")
    if not MISTRAL_API_KEY:
        raise HTTPException(500, "MISTRAL_API_KEY تنظیم نشده روی سرویس")

    job_dir = WORK_DIR / str(uuid.uuid4())
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        video_path = job_dir / "input.mp4"
        audio_path = job_dir / "audio.wav"
        dubbed_audio_path = job_dir / "dubbed.mp3"
        output_path = job_dir / "output.mp4"

        # ۱. دانلود ویدیو از لینک presign شده
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(req.video_url, timeout=60)
            resp.raise_for_status()
            video_path.write_bytes(resp.content)

        # ۲. چک اینکه اصلا صدا داره یا نه؛ اگه نداشت، دوبله بی‌معنیه، skip کن
        if not has_audio_stream(video_path):
            return DubResult(
                content_id=req.content_id,
                skipped=True,
                skip_reason="ویدیو استریم صوتی ندارد",
            )

        # ۳. جدا کردن صدا
        extract_audio(video_path, audio_path)

        # ۴. تشخیص گوینده‌ها و جنسیت (فعلاً برای انتخاب صدای TTS غالب استفاده می‌شه)
        segments = diarize_and_detect_gender(audio_path)
        # اگه چند گوینده داشتیم، جنسیت غالب (بر اساس مجموع مدت زمان) رو انتخاب می‌کنیم
        gender = "male"
        if segments:
            from collections import defaultdict
            durations = defaultdict(float)
            for s in segments:
                durations[s["gender"]] += s["end"] - s["start"]
            gender = max(durations, key=durations.get)

        # ۵. تبدیل صدا به متن
        original_text, detected_lang = transcribe(audio_path)
        original_language = req.original_language or detected_lang

        # ۶. ترجمه به فارسی
        persian_text = translate_to_persian(original_text)

        # ۷. تولید صدای دوبله
        await synthesize_speech(persian_text, gender, dubbed_audio_path)

        # ۸. ترکیب نهایی
        merge_audio_with_video(video_path, dubbed_audio_path, output_path)

        return DubResult(
            content_id=req.content_id,
            dubbed_transcript=persian_text,
            original_language=original_language,
            local_output_path=str(output_path),
        )

    except Exception as e:
        raise HTTPException(500, f"خطا در فرآیند دوبله: {e}")
    # توجه: job_dir رو عمداً پاک نکردیم چون آپلود به بک‌بلیز در مرحله‌ی بعدی (n8n)
    # از local_output_path استفاده می‌کنه. باید بعد از آپلود موفق، یه endpoint یا
    # cronjob جداگانه برای پاک‌سازی /tmp/dubbing اضافه بشه.

