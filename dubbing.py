"""
ماژول دوبله خودکار ویدیو
مراحل: جدا کردن صدا -> تشخیص جنسیت غالب صدا -> تبدیل به متن -> ترجمه -> TTS -> ترکیب نهایی

نسخه‌ی سبک (بدون pyannote/torch) برای سازگاری با پلن رایگان Render (۵۱۲ مگابایت RAM).
برای فعال کردن تشخیص چند-گوینده/چند-جنسیت در آینده (وقتی پلن آپگرید شد)،
به فایل dubbing_prompt.md مراجعه کن.

نحوه‌ی اضافه کردن به سرویس فعلی:
    from dubbing import router as dubbing_router
    app.include_router(dubbing_router, prefix="/dub")

Environment Variables مورد نیاز (باید روی Render ست بشن):
    MISTRAL_API_KEY - برای ترجمه متن (همونی که از قبل داری)
"""

import os
import uuid
import shutil
import logging
import asyncio
import subprocess
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger("dubbing")
logging.basicConfig(level=logging.INFO)

HF_TOKEN = os.environ.get("HF_TOKEN")
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")
WORK_DIR = Path("/tmp/dubbing")
WORK_DIR.mkdir(exist_ok=True)

# وضعیت job ها توی حافظه نگه‌داری میشه (چون فرکانس اجرا کمه - حدود ۱ در ساعت -
# نیازی به دیتابیس جدا برای این نیست؛ سرویس تا وقتی جواب نهایی رو نگرفتی نمی‌خوابه)
JOBS: dict[str, "JobStatus"] = {}

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


class JobStatus(BaseModel):
    job_id: str
    content_id: str
    status: str   # "processing" | "done" | "failed"
    result: DubResult | None = None
    error: str | None = None


class JobAccepted(BaseModel):
    job_id: str
    content_id: str
    status: str = "processing"


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


def detect_dominant_gender(audio_path: Path) -> str:
    """
    نسخه‌ی فوق‌سبک (بدون librosa/numba/pyannote/torch) برای پلن رایگان Render.
    به‌جای librosa.yin (که به‌خاطر کامپایلر numba حافظه‌ی زیادی حین import مصرف می‌کنه)،
    اینجا با خود numpy یه تشخیص pitch ساده با autocorrelation انجام می‌دیم.
    محدودیت: یک جنسیت غالب برای کل فایل، نه به‌ازای هر گوینده — برای چند-گوینده
    به فایل dubbing_prompt.md مراجعه کن.
    """
    import wave
    import numpy as np

    with wave.open(str(audio_path), "rb") as wf:
        sr = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
    y = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    frame_len = int(sr * 0.04)   # پنجره‌ی ۴۰ میلی‌ثانیه‌ای
    hop = frame_len // 2
    min_lag = int(sr / 400)      # سقف فرکانس ۴۰۰ هرتز
    max_lag = int(sr / 65)       # کف فرکانس ۶۵ هرتز

    pitches = []
    for start in range(0, max(0, len(y) - frame_len), hop):
        frame = y[start:start + frame_len]
        frame = frame - frame.mean()
        if np.max(np.abs(frame)) < 0.01:   # سکوت، رد شو
            continue
        corr = np.correlate(frame, frame, mode="full")
        corr = corr[len(corr) // 2:]
        if max_lag >= len(corr):
            continue
        segment = corr[min_lag:max_lag]
        if len(segment) == 0:
            continue
        peak_lag = int(np.argmax(segment)) + min_lag
        if corr[peak_lag] <= 0:
            continue
        pitches.append(sr / peak_lag)

    pitch = float(np.median(pitches)) if pitches else 165.0
    return "male" if pitch < 165 else "female"


def ffprobe_duration(path: Path) -> float:
    """طول (ثانیه) یه فایل صوتی/ویدیویی رو با ffprobe برمی‌گردونه"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise RuntimeError(f"ffprobe duration failed: {result.stderr}")


def transcribe_segments(audio_path: Path) -> tuple[list[dict], str]:
    """
    تبدیل صدا به متن **به‌تفکیک جمله**، با timestamp دقیق هر جمله.
    خروجی: (لیست {"start","end","text"}, زبان تشخیص داده‌شده)
    این جایگزین نسخه‌ی قبلی شد که کل صدا رو یک‌جا (بدون هماهنگی زمانی) ترجمه می‌کرد.
    """
    from faster_whisper import WhisperModel

    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    segments_iter, info = model.transcribe(str(audio_path), beam_size=1, vad_filter=True)

    segments = []
    for seg in segments_iter:
        text = seg.text.strip()
        if text:
            segments.append({"start": seg.start, "end": seg.end, "text": text})
    return segments, info.language


def translate_to_persian(text: str) -> str:
    """ترجمه متن به فارسی با فراخوانی مستقیم REST API میسترال (بدون نیاز به SDK رسمی)"""
    import httpx

    resp = httpx.post(
        "https://api.mistral.ai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {MISTRAL_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "mistral-small-latest",
            "messages": [{
                "role": "user",
                "content": f"این متن رو فقط ترجمه کن به فارسی روان، بدون هیچ توضیح اضافه:\n\n{text}",
            }],
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


async def synthesize_speech(text: str, gender: str, output_path: Path) -> None:
    """تولید صدا با edge-tts بر اساس جنسیت گوینده (با timeout تا در صورت مسدود بودن شبکه، بی‌نهایت هنگ نکنه)"""
    import edge_tts

    voice = "fa-IR-DilaraNeural" if gender == "female" else "fa-IR-FaridNeural"
    communicate = edge_tts.Communicate(text, voice)
    await asyncio.wait_for(communicate.save(str(output_path)), timeout=45)


def fit_segment_to_window(raw_audio_path: Path, target_duration: float, out_path: Path) -> None:
    """
    اگه صدای TTS تولیدشده از بازه‌ی زمانی جمله‌ی اصلی بلندتر بود، کمی سریع‌ترش می‌کنیم
    (حداکثر ۲ برابر، محدودیت خودِ فیلتر atempo) تا داخل زمان جمله جا بشه و با جمله‌ی
    بعدی قاطی نشه. همچنین به مونو/۲۴کیلوهرتز نرمالایز می‌شه تا mix بعدی یکدست باشه.
    """
    actual = ffprobe_duration(raw_audio_path)
    if target_duration > 0.05 and actual > target_duration * 1.1:
        factor = min(actual / target_duration, 2.0)
        cmd = [
            "ffmpeg", "-y", "-i", str(raw_audio_path),
            "-filter:a", f"atempo={factor:.3f}",
            "-ac", "1", "-ar", "24000",
            str(out_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", str(raw_audio_path),
            "-ac", "1", "-ar", "24000",
            str(out_path),
        ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg fit segment failed: {result.stderr}")


def build_timed_audio_track(dubbed_segments: list[dict], total_duration: float, output_path: Path) -> None:
    """
    یه ترک صوتی به طول کامل ویدیو می‌سازه و هر قطعه‌ی دوبله‌شده رو دقیقاً سر
    زمان شروع جمله‌ی اصلیش (نه پشت سر هم از اول) قرار می‌ده.
    dubbed_segments: هر عنصر {"start": float, "path": Path} (فایل صوتی نرمال‌شده)
    """
    n = len(dubbed_segments)
    if n == 0:
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi", "-t", f"{total_duration}",
            "-i", "anullsrc=r=24000:cl=mono", "-ac", "1", str(output_path),
        ]
        subprocess.run(cmd, capture_output=True, text=True)
        return

    inputs = []
    delay_filters = []
    for i, seg in enumerate(dubbed_segments):
        inputs += ["-i", str(seg["path"])]
        delay_ms = max(0, int(seg["start"] * 1000))
        delay_filters.append(f"[{i}:a]adelay={delay_ms}|{delay_ms}[a{i}]")

    mix_labels = "".join(f"[a{i}]" for i in range(n)) + f"[{n}:a]"
    filter_complex = (
        ";".join(delay_filters)
        + f";{mix_labels}amix=inputs={n + 1}:duration=longest:dropout_transition=0,volume={n + 1}[aout]"
    )

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-f", "lavfi", "-t", f"{total_duration}", "-i", "anullsrc=r=24000:cl=mono",
        "-filter_complex", filter_complex,
        "-map", "[aout]",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg build timed track failed: {result.stderr}")


def merge_audio_with_video(video_path: Path, audio_path: Path, output_path: Path) -> None:
    """جایگزینی صدای اصلی ویدیو با صدای دوبله‌شده (هر دو از قبل هم‌طول ویدیو ساخته شدن)"""
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path), "-i", str(audio_path),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy", "-c:a", "aac",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg merge failed: {result.stderr}")


# ---------- endpoint اصلی ----------

def _run_dubbing_pipeline(req: "DubRequest", job_dir: Path) -> "DubResult":
    """
    کل پایپ‌لاین سنگین (I/O + CPU-bound) اینجا به‌صورت synchronous اجرا میشه
    و از endpoint با asyncio.to_thread صدا زده میشه، تا event loop اصلی
    (و health-check های Render) در طول پردازش بلاک نشن.
    """
    import httpx

    video_path = job_dir / "input.mp4"
    audio_path = job_dir / "audio.wav"
    dubbed_track_path = job_dir / "dubbed_track.wav"
    output_path = job_dir / "output.mp4"

    logger.info(f"[{req.content_id}] مرحله ۱: دانلود ویدیو")
    with httpx.Client(follow_redirects=True) as client:
        resp = client.get(req.video_url, timeout=60)
        resp.raise_for_status()
        video_path.write_bytes(resp.content)
    logger.info(f"[{req.content_id}] دانلود تموم شد، حجم: {video_path.stat().st_size} بایت")

    logger.info(f"[{req.content_id}] مرحله ۲: اعتبارسنجی استریم ویدیو")
    if not has_video_stream(video_path):
        return DubResult(
            content_id=req.content_id,
            skipped=True,
            skip_reason="فایل دانلودشده یک ویدیوی معتبر نیست (احتمالا لینک مستقیم فایل نبوده)",
            skip_reason_en="downloaded file has no valid video stream (link may not be a direct file link)",
        )

    logger.info(f"[{req.content_id}] مرحله ۳: چک وجود صدا")
    if not has_audio_stream(video_path):
        return DubResult(
            content_id=req.content_id,
            skipped=True,
            skip_reason="ویدیو استریم صوتی ندارد",
            skip_reason_en="video has no audio stream",
        )

    logger.info(f"[{req.content_id}] مرحله ۴: جدا کردن صدا با ffmpeg")
    extract_audio(video_path, audio_path)

    logger.info(f"[{req.content_id}] مرحله ۵: تشخیص جنسیت غالب")
    gender = detect_dominant_gender(audio_path)
    logger.info(f"[{req.content_id}] جنسیت تشخیص داده شده: {gender}")

    logger.info(f"[{req.content_id}] مرحله ۶: تبدیل صدا به متن به‌تفکیک جمله (whisper tiny)")
    raw_segments, detected_lang = transcribe_segments(audio_path)
    original_language = req.original_language or detected_lang
    logger.info(f"[{req.content_id}] زبان: {original_language}, تعداد جمله: {len(raw_segments)}")

    total_duration = ffprobe_duration(video_path)
    dubbed_segments = []
    persian_full_text_parts = []

    for i, seg in enumerate(raw_segments):
        logger.info(f"[{req.content_id}] جمله {i+1}/{len(raw_segments)}: ترجمه")
        persian_text = translate_to_persian(seg["text"])
        persian_full_text_parts.append(persian_text)

        raw_tts_path = job_dir / f"seg_{i}_raw.mp3"
        fitted_path = job_dir / f"seg_{i}.wav"
        logger.info(f"[{req.content_id}] جمله {i+1}/{len(raw_segments)}: TTS")
        asyncio.run(synthesize_speech(persian_text, gender, raw_tts_path))

        window = max(0.3, seg["end"] - seg["start"])
        fit_segment_to_window(raw_tts_path, window, fitted_path)
        dubbed_segments.append({"start": seg["start"], "path": fitted_path})

    logger.info(f"[{req.content_id}] مرحله ۷: چیدن قطعات صوتی سر زمان درستشون")
    build_timed_audio_track(dubbed_segments, total_duration, dubbed_track_path)

    logger.info(f"[{req.content_id}] مرحله ۸: ترکیب نهایی با ویدیو")
    merge_audio_with_video(video_path, dubbed_track_path, output_path)

    logger.info(f"[{req.content_id}] تمام شد ✅")
    return DubResult(
        content_id=req.content_id,
        dubbed_transcript=" ".join(persian_full_text_parts),
        original_language=original_language,
        local_output_path=str(output_path),
    )


def _run_dubbing_job(job_id: str, req: "DubRequest", job_dir: Path) -> None:
    """
    این تابع توی یه ترد پس‌زمینه (background thread) اجرا میشه، کاملاً جدا از
    درخواست HTTP اصلی. نتیجه رو توی JOBS[job_id] ذخیره می‌کنه تا endpoint وضعیت
    بتونه بعداً بخونتش. چون هیچ اتصال HTTPای رو باز نگه نمی‌داره، محدودیت
    timeout پروکسی Render دیگه مشکلی ایجاد نمی‌کنه.
    """
    try:
        result = _run_dubbing_pipeline(req, job_dir)
        JOBS[job_id] = JobStatus(job_id=job_id, content_id=req.content_id, status="done", result=result)
    except Exception as e:
        logger.exception(f"[{req.content_id}] خطا در فرآیند دوبله")
        JOBS[job_id] = JobStatus(job_id=job_id, content_id=req.content_id, status="failed", error=str(e))


@router.post("/process", response_model=JobAccepted, status_code=202)
async def process_dubbing(req: DubRequest):
    """
    این endpoint فوراً جواب می‌ده (بدون معطلی) و یه job_id برمی‌گردونه.
    پردازش واقعی توی پس‌زمینه ادامه پیدا می‌کنه؛ برای دیدن نتیجه، هرچند ثانیه
    یه‌بار GET /dub/status/{job_id} رو صدا بزن.

    نکته: نگه‌داشتن یه اتصال HTTP باز برای چند دقیقه (تا کل پردازش تموم بشه)
    باعث می‌شد Render خودش اتصال رو قطع کنه (به‌خاطر محدودیت timeout پروکسی).
    با این الگو، دیگه هیچ اتصالی طولانی باز نمی‌مونه.
    """
    if not MISTRAL_API_KEY:
        raise HTTPException(500, "MISTRAL_API_KEY تنظیم نشده روی سرویس")

    job_id = str(uuid.uuid4())
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    JOBS[job_id] = JobStatus(job_id=job_id, content_id=req.content_id, status="processing")

    # اجرا در پس‌زمینه، بدون منتظر موندن endpoint برای تموم شدنش
    asyncio.create_task(asyncio.to_thread(_run_dubbing_job, job_id, req, job_dir))

    return JobAccepted(job_id=job_id, content_id=req.content_id)


@router.get("/status/{job_id}", response_model=JobStatus)
async def get_dubbing_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "job_id پیدا نشد")
    return job


@router.get("/download/{job_id}")
async def download_dubbed_video(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "job_id پیدا نشد")
    if job.status != "done" or not job.result or not job.result.local_output_path:
        raise HTTPException(409, f"فایل هنوز آماده نیست (وضعیت فعلی: {job.status})")
    path = Path(job.result.local_output_path)
    if not path.exists():
        raise HTTPException(410, "فایل روی دیسک سرویس دیگه وجود نداره (احتمالا سرویس ری‌استارت شده)")
    return FileResponse(path, media_type="video/mp4", filename=f"dubbed_{job.content_id}.mp4")
    # توجه: job_dir رو عمداً پاک نکردیم چون آپلود به بک‌بلیز در مرحله‌ی بعدی (n8n)
    # از local_output_path استفاده می‌کنه. باید بعد از آپلود موفق، یه endpoint یا
    # cronjob جداگانه برای پاک‌سازی /tmp/dubbing و JOBS اضافه بشه.
