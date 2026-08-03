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
    skip_reason_en: str | None = None   # نسخه انگلیسی، برای دیدن راحت توی ترمینال‌هایی که فونت فارسی ندارن


# ---------- توابع کمکی هر مرحله ----------

def has_video_stream(video_path: Path) -> bool:
    """چک می‌کنه فایل دانلودشده واقعاً استریم تصویری داره (یعنی یه ویدیوی معتبره، نه HTML/خطا)"""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v",
        "-show_entries", "stream=index",
        "-of", "csv=p=0",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return bool(result.stdout.strip())


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
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(req.video_url, timeout=60)
            resp.raise_for_status()
            video_path.write_bytes(resp.content)

        # ۲. اعتبارسنجی اینکه چیزی که دانلود شده واقعاً یه فایل ویدیوییه
        if not has_video_stream(video_path):
            return DubResult(
                content_id=req.content_id,
                skipped=True,
                skip_reason="فایل دانلودشده یک ویدیوی معتبر نیست (احتمالا لینک مستقیم فایل نبوده)",
                skip_reason_en="downloaded file has no valid video stream (link may not be a direct file link)",
            )

        # ۳. چک اینکه اصلا صدا داره یا نه؛ اگه نداشت، دوبله بی‌معنیه، skip کن
        if not has_audio_stream(video_path):
            return DubResult(
                content_id=req.content_id,
                skipped=True,
                skip_reason="ویدیو استریم صوتی ندارد",
                skip_reason_en="video has no audio stream",
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
