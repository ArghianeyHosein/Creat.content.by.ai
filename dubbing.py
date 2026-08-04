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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")  # اختیاری - اگه ست بشه، به‌جای whisper+Mistral استفاده میشه (تست)
WORK_DIR = Path("/tmp/dubbing")
WORK_DIR.mkdir(exist_ok=True)

# سقف‌های ایمنی برای جلوگیری از OOM روی پلن رایگان Render (۵۱۲ مگابایت RAM).
# این عددها رو می‌تونی با env var عوض کنی، بدون نیاز به تغییر کد.
MAX_FILE_SIZE_MB = float(os.environ.get("DUB_MAX_FILE_SIZE_MB", "30"))
MAX_DURATION_SECONDS = float(os.environ.get("DUB_MAX_DURATION_SECONDS", "180"))  # ۳ دقیقه

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
    original_transcript: str | None = None   # متن اصلی (قبل از ترجمه) - برای عیب‌یابی دقت whisper
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


def download_and_validate_video(req: "DubRequest", video_path: Path) -> tuple["DubResult | None", float]:
    """
    دانلود (streaming، با سقف حجم) + اعتبارسنجی ویدیو + چک صدا + سقف طول زمانی.
    مشترک بین پایپ‌لاین whisper و پایپ‌لاین Gemini.
    خروجی: (DubResult در صورت skip شدن یا None اگه همه‌چی اوکی بود, طول ویدیو به ثانیه)
    """
    import httpx

    logger.info(f"[{req.content_id}] دانلود ویدیو (سقف حجم: {MAX_FILE_SIZE_MB}MB)")
    max_bytes = int(MAX_FILE_SIZE_MB * 1024 * 1024)
    with httpx.Client(follow_redirects=True) as client:
        with client.stream("GET", req.video_url, timeout=60) as resp:
            resp.raise_for_status()
            content_length = resp.headers.get("content-length")
            if content_length and int(content_length) > max_bytes:
                return DubResult(
                    content_id=req.content_id, skipped=True,
                    skip_reason=f"حجم فایل بیشتر از سقف مجاز ({MAX_FILE_SIZE_MB} مگابایت) است",
                    skip_reason_en=f"file size exceeds the {MAX_FILE_SIZE_MB}MB limit",
                ), 0.0
            total = 0
            with open(video_path, "wb") as f:
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        return DubResult(
                            content_id=req.content_id, skipped=True,
                            skip_reason=f"حجم فایل بیشتر از سقف مجاز ({MAX_FILE_SIZE_MB} مگابایت) است",
                            skip_reason_en=f"file size exceeds the {MAX_FILE_SIZE_MB}MB limit",
                        ), 0.0
                    f.write(chunk)
    logger.info(f"[{req.content_id}] دانلود تموم شد، حجم: {video_path.stat().st_size} بایت")

    if not has_video_stream(video_path):
        return DubResult(
            content_id=req.content_id, skipped=True,
            skip_reason="فایل دانلودشده یک ویدیوی معتبر نیست (احتمالا لینک مستقیم فایل نبوده)",
            skip_reason_en="downloaded file has no valid video stream (link may not be a direct file link)",
        ), 0.0

    total_duration = ffprobe_duration(video_path)
    if total_duration > MAX_DURATION_SECONDS:
        return DubResult(
            content_id=req.content_id, skipped=True,
            skip_reason=f"طول ویدیو ({total_duration:.0f} ثانیه) بیشتر از سقف مجاز ({MAX_DURATION_SECONDS:.0f} ثانیه) است",
            skip_reason_en=f"video duration ({total_duration:.0f}s) exceeds the {MAX_DURATION_SECONDS:.0f}s limit",
        ), 0.0

    if not has_audio_stream(video_path):
        return DubResult(
            content_id=req.content_id, skipped=True,
            skip_reason="ویدیو استریم صوتی ندارد",
            skip_reason_en="video has no audio stream",
        ), 0.0

    return None, total_duration


async def gemini_live_dub(pcm_audio_path: Path, output_wav_path: Path, expected_speech_duration: float = 0.0) -> None:
    """
    نسخه‌ی آزمایشی (Preview گوگل): صدا رو مستقیم به Gemini Live API می‌فرستیم و
    ازش می‌خوایم بلافاصله صدای ترجمه‌شده به فارسی رو - با لحن و آهنگ مشابه گوینده‌ی
    اصلی - برگردونه. برخلاف مسیر whisper+edge-tts، اینجا نیازی به تفکیک جمله،
    ترجمه‌ی جدا، یا هماهنگ‌سازی دستی زمان‌بندی نیست - خودِ مدل انجامش می‌ده.
    ورودی باید PCM خام ۱۶بیتی، ۱۶kHz، تک‌کاناله باشه (بدون هدر WAV).

    expected_speech_duration: زمان واقعی آخرین گفتار (از whisper VAD، فقط برای
    تخمین زمان‌بندی - نه ترجمه) - برای محاسبه‌ی یه سقف زمانی هوشمند به‌جای عدد
    ثابت حدسی. اگه صفر بود، از یه پیش‌فرض محافظه‌کارانه استفاده می‌کنیم.

    هشدار: این قابلیت preview هست و ممکنه اسم مدل، پارامترها، یا فرمت پاسخ
    بین نسخه‌های SDK فرق کنه - در صورت خطا، لاگ دقیق خطا رو چک کن.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    model = "gemini-2.5-flash-native-audio-preview-12-2025"
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=(
            "تو یک مترجم صوتی همزمان هستی. هرچی که می‌شنوی رو بلافاصله و با لحن، "
            "احساس و سرعت گفتار مشابه گوینده‌ی اصلی، به زبان فارسی روان ترجمه کن و بگو."
        ),
        # تنظیم قبلی (فقط disabled=True) هیچ تأثیری نداشت - نتیجه‌ی تست عیناً
        # همون قبلی بود. این‌بار علاوه بر خاموش کردن تشخیص خودکار، صریحاً هم
        # می‌گیم even اگه فعالیت جدید تشخیص داده شد، جواب رو قطع نکن
        # (activity_handling=NO_INTERRUPTION) - این فیلد جداست و شاید غایب بودنش
        # دلیل بی‌اثر بودن اصلاح قبلی بوده.
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True),
            activity_handling=types.ActivityHandling.NO_INTERRUPTION,
        ),
    )
    # لاگ می‌کنیم تا مطمئن بشیم این تنظیمات واقعاً روی آبجکت config نشستن،
    # نه اینکه بی‌صدا نادیده گرفته شده باشن (همون چیزی که دفعه‌ی قبل رخ داد)
    logger.info(f"[gemini_live] realtime_input_config ساخته‌شده: {config.realtime_input_config!r}")

    # سقف زمانی هوشمند: دو برابر طول واقعی گفتار (چون تولید صدا معمولاً کندتر از
    # پخش بلادرنگه) + یه حاشیه‌ی امن، به‌جای عدد ثابت حدسی
    overall_timeout = max(60.0, expected_speech_duration * 2.5 + 30.0)
    logger.info(f"[gemini_live] سقف زمانی کلی: {overall_timeout:.0f} ثانیه (بر اساس {expected_speech_duration:.0f}s گفتار واقعی)")

    pcm_bytes = pcm_audio_path.read_bytes()
    chunk_size = 4096
    out_chunks: list[bytes] = []
    message_count = 0

    async def drain_session(session) -> None:
        nonlocal message_count
        turn_iter = session.receive().__aiter__()
        while True:
            try:
                response = await turn_iter.__anext__()
            except StopAsyncIteration:
                logger.info("[gemini_live] session.receive() طبیعی تموم شد (StopAsyncIteration)")
                break

            message_count += 1
            server_content = getattr(response, "server_content", None)
            turn_complete = getattr(server_content, "turn_complete", None)
            interrupted = getattr(server_content, "interrupted", None)
            generation_complete = getattr(server_content, "generation_complete", None)
            has_data = bool(getattr(response, "data", None))

            logger.info(
                f"[gemini_live] پیام #{message_count}: data={has_data}, "
                f"turn_complete={turn_complete}, generation_complete={generation_complete}, "
                f"interrupted={interrupted}"
            )

            if has_data:
                out_chunks.append(response.data)

            # اگه سیگنال صریح "تموم شدن نوبت" اومد و دیگه چیزی نمونده، خارج شو
            if turn_complete or generation_complete:
                logger.info("[gemini_live] سیگنال پایان دریافت شد، خروج از حلقه")
                break

    async with client.aio.live.connect(model=model, config=config) as session:
        # چون تشخیص خودکار مکالمه رو خاموش کردیم، باید خودمون صریح بگیم
        # "نوبت کاربر شروع شد" و "نوبت کاربر تموم شد"
        await session.send_realtime_input(activity_start=types.ActivityStart())
        for i in range(0, len(pcm_bytes), chunk_size):
            chunk = pcm_bytes[i:i + chunk_size]
            await session.send_realtime_input(
                audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
            )
        await session.send_realtime_input(activity_end=types.ActivityEnd())
        await session.send_realtime_input(audio_stream_end=True)

        # به‌جای حدس زدن با timeout کور، منتظر سیگنال واقعی پایان (turn_complete /
        # generation_complete) می‌مونیم؛ فقط برای جلوگیری از هنگ کامل سرویس، یه سقف
        # کلی (نه به‌ازای هر پیام) می‌ذاریم که اگه هیچ‌کدوم نیومد، خطای واضح بده.
        try:
            await asyncio.wait_for(drain_session(session), timeout=overall_timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[gemini_live] سقف زمانی {overall_timeout:.0f}s رسید؛ {len(out_chunks)} تکه صدا تا الان جمع شده")

    logger.info(f"[gemini_live] مجموع پیام‌های دریافتی: {message_count}, تکه‌های صدا: {len(out_chunks)}")

    if not out_chunks:
        raise RuntimeError("Gemini Live API هیچ صدایی برنگردوند")

    raw_out_path = pcm_audio_path.with_suffix(".gemini_raw.pcm")
    raw_out_path.write_bytes(b"".join(out_chunks))

    # خروجی Live API معمولاً PCM ۲۴kHz تک‌کاناله‌ست؛ تبدیلش می‌کنیم به wav معمولی
    cmd = [
        "ffmpeg", "-y", "-f", "s16le", "-ar", "24000", "-ac", "1",
        "-i", str(raw_out_path), str(output_wav_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg convert gemini output failed: {result.stderr}")


def transcribe_segments(audio_path: Path) -> tuple[list[dict], str]:
    """
    تبدیل صدا به متن **به‌تفکیک جمله**، با timestamp دقیق هر جمله.
    خروجی: (لیست {"start","end","text"}, زبان تشخیص داده‌شده)
    این جایگزین نسخه‌ی قبلی شد که کل صدا رو یک‌جا (بدون هماهنگی زمانی) ترجمه می‌کرد.
    """
    from faster_whisper import WhisperModel

    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments_iter, info = model.transcribe(
        str(audio_path), beam_size=1, vad_filter=True,
        vad_parameters=dict(
            speech_pad_ms=100,           # پیش‌فرض ~۴۰۰ms بود؛ باعث میشد دوبله زودتر از موقع پخش بشه
            min_silence_duration_ms=300,  # پیش‌فرض ~۲۰۰۰ms بود؛ مکث‌های کوتاه بین جمله‌ها رو نادیده می‌گرفت
        ),
    )

    segments = []
    for seg in segments_iter:
        text = seg.text.strip()
        if text:
            segments.append({"start": seg.start, "end": seg.end, "text": text})
    return segments, info.language


def translate_to_persian(text: str, max_syllables: int | None = None) -> str:
    """
    ترجمه متن به فارسی با فراخوانی مستقیم REST API میسترال (بدون نیاز به SDK رسمی).
    اگه max_syllables داده بشه، از مدل می‌خوایم ترجمه رو تا حد امکان هم‌طول با
    متن اصلی نگه داره - تا کمتر لازم بشه صدای TTS رو بعداً فشرده کنیم (که باعث
    غیرطبیعی سریع شدن صدا میشه).
    """
    import httpx

    length_hint = ""
    if max_syllables:
        length_hint = (
            f"\n\nمهم: ترجمه باید کوتاه و فشرده باشه، تقریباً هم‌طول با جمله‌ی اصلی "
            f"(حدود {max_syllables} هجا یا کمتر)، طوری که با همون سرعت طبیعی گفتار "
            f"قابل خوندن باشه. از کلمات اضافی و پرکننده پرهیز کن، ولی معنی رو کامل حفظ کن."
        )

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
                "content": f"این متن رو فقط ترجمه کن به فارسی روان، بدون هیچ توضیح اضافه:{length_hint}\n\n{text}",
            }],
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


async def synthesize_speech(text: str, gender: str, output_path: Path, max_retries: int = 3) -> None:
    """
    تولید صدا با edge-tts بر اساس جنسیت گوینده.
    - timeout تا در صورت مسدود بودن شبکه، بی‌نهایت هنگ نکنه
    - retry با backoff چون edge-tts گاهی به‌خاطر یه محدودیت یا قطعی موقتی
      شبکه، خطای NoAudioReceived می‌ده که با یه تلاش دوباره معمولاً حل میشه
    """
    import edge_tts

    voice = "fa-IR-DilaraNeural" if gender == "female" else "fa-IR-FaridNeural"
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            communicate = edge_tts.Communicate(text, voice)
            await asyncio.wait_for(communicate.save(str(output_path)), timeout=45)
            return
        except Exception as e:
            last_error = e
            logger.warning(f"[edge-tts] تلاش {attempt}/{max_retries} ناموفق بود: {e}")
            if attempt < max_retries:
                await asyncio.sleep(1.5 * attempt)  # backoff کوتاه قبل از تلاش بعدی
    raise last_error


def fit_segment_to_window(raw_audio_path: Path, target_duration: float, out_path: Path) -> None:
    """
    اگه صدای TTS تولیدشده از بازه‌ی زمانی جمله‌ی اصلی بلندتر بود، کمی سریع‌ترش می‌کنیم
    (حداکثر ۲ برابر، محدودیت خودِ فیلتر atempo) تا داخل زمان جمله جا بشه و با جمله‌ی
    بعدی قاطی نشه. همچنین به مونو/۲۴کیلوهرتز نرمالایز می‌شه تا mix بعدی یکدست باشه.
    """
    actual = ffprobe_duration(raw_audio_path)
    if target_duration > 0.05 and actual > target_duration * 1.15:
        # حداکثر ۱.۳ برابر سریع‌تر - ترجیح میدیم صدا کمی از بازه‌ی زمانی جمله
        # بیرون بزنه تا اینکه غیرطبیعی و عجول به‌نظر برسه
        factor = min(actual / target_duration, 1.3)
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
    پایپ‌لاین اصلی (whisper + Mistral + edge-tts، segment به segment).
    کل پایپ‌لاین سنگین (I/O + CPU-bound) اینجا به‌صورت synchronous اجرا میشه
    و از endpoint با asyncio.to_thread صدا زده میشه، تا event loop اصلی
    (و health-check های Render) در طول پردازش بلاک نشن.
    """
    video_path = job_dir / "input.mp4"
    audio_path = job_dir / "audio.wav"
    dubbed_track_path = job_dir / "dubbed_track.wav"
    output_path = job_dir / "output.mp4"

    logger.info(f"[{req.content_id}] مرحله ۱-۳: دانلود و اعتبارسنجی")
    early_result, total_duration = download_and_validate_video(req, video_path)
    if early_result is not None:
        return early_result

    logger.info(f"[{req.content_id}] مرحله ۴: جدا کردن صدا با ffmpeg")
    extract_audio(video_path, audio_path)

    logger.info(f"[{req.content_id}] مرحله ۵: تشخیص جنسیت غالب")
    gender = detect_dominant_gender(audio_path)
    logger.info(f"[{req.content_id}] جنسیت تشخیص داده شده: {gender}")

    logger.info(f"[{req.content_id}] مرحله ۶: تبدیل صدا به متن به‌تفکیک جمله (whisper base)")
    raw_segments, detected_lang = transcribe_segments(audio_path)
    original_language = req.original_language or detected_lang
    logger.info(f"[{req.content_id}] زبان: {original_language}, تعداد جمله: {len(raw_segments)}")

    dubbed_segments = []
    persian_full_text_parts = []
    original_full_text_parts = []
    cursor = 0.0  # پایان زمانی آخرین قطعه‌ی دوبله‌شده - برای جلوگیری از هم‌پوشانی صداها

    for i, seg in enumerate(raw_segments):
        logger.info(f"[{req.content_id}] جمله {i+1}/{len(raw_segments)}: {seg['text']}")
        original_full_text_parts.append(seg["text"])

        window = max(0.3, seg["end"] - seg["start"])
        syllable_budget = max(3, int(window * 4.5))  # تخمین تقریبی: ~۴.۵ هجا در ثانیه گفتار طبیعی
        persian_text = translate_to_persian(seg["text"], max_syllables=syllable_budget)
        persian_full_text_parts.append(persian_text)

        raw_tts_path = job_dir / f"seg_{i}_raw.mp3"
        fitted_path = job_dir / f"seg_{i}.wav"
        logger.info(f"[{req.content_id}] جمله {i+1}/{len(raw_segments)}: TTS")
        asyncio.run(synthesize_speech(persian_text, gender, raw_tts_path))

        fit_segment_to_window(raw_tts_path, window, fitted_path)

        # اگه جمله‌ی قبلی تا بعد از شروع این جمله ادامه داشته، این یکی رو عقب می‌ندازیم
        # تا صداها روی هم نیفتن (حتی اگه یعنی یه‌کم دیرتر از زمان اصلی whisper پخش بشه)
        actual_start = max(seg["start"], cursor)
        fitted_duration = ffprobe_duration(fitted_path)
        cursor = actual_start + fitted_duration

        dubbed_segments.append({"start": actual_start, "path": fitted_path})

    logger.info(f"[{req.content_id}] مرحله ۷: چیدن قطعات صوتی سر زمان درستشون")
    build_timed_audio_track(dubbed_segments, total_duration, dubbed_track_path)

    logger.info(f"[{req.content_id}] مرحله ۸: ترکیب نهایی با ویدیو")
    merge_audio_with_video(video_path, dubbed_track_path, output_path)

    logger.info(f"[{req.content_id}] تمام شد ✅")
    return DubResult(
        content_id=req.content_id,
        original_transcript=" ".join(original_full_text_parts),
        dubbed_transcript=" ".join(persian_full_text_parts),
        original_language=original_language,
        local_output_path=str(output_path),
    )


def _run_gemini_dubbing_pipeline(req: "DubRequest", job_dir: Path) -> "DubResult":
    """
    پایپ‌لاین آزمایشی (Preview): بجای whisper+Mistral+edge-tts segment-by-segment،
    صدای کامل مستقیم به Gemini Live API فرستاده میشه و صدای ترجمه‌شده (با لحن
    مشابه) مستقیم ازش گرفته میشه - بدون نیاز به تفکیک جمله یا هماهنگ‌سازی دستی.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY تنظیم نشده روی سرویس")

    video_path = job_dir / "input.mp4"
    pcm_audio_path = job_dir / "audio_16k.pcm"
    dubbed_audio_path = job_dir / "gemini_dubbed.wav"
    output_path = job_dir / "output.mp4"

    logger.info(f"[{req.content_id}] (Gemini) مرحله ۱-۳: دانلود و اعتبارسنجی")
    early_result, total_duration = download_and_validate_video(req, video_path)
    if early_result is not None:
        return early_result

    logger.info(f"[{req.content_id}] (Gemini) مرحله ۴: استخراج PCM خام ۱۶kHz")
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-f", "s16le", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(pcm_audio_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg pcm extract failed: {result.stderr}")

    # مرحله‌ی کمکی: با VAD خودِ whisper (سبک، فقط برای زمان‌بندی - نه ترجمه)
    # می‌فهمیم گفتار واقعی تا کجای فایل ادامه داره، تا سقف زمانی صبر برای
    # Gemini رو هوشمندانه تنظیم کنیم (نه یه عدد ثابت حدسی)
    logger.info(f"[{req.content_id}] (Gemini) مرحله ۴.۵: تخمین طول گفتار واقعی با VAD")
    wav_for_vad = job_dir / "audio_for_vad.wav"
    extract_audio(video_path, wav_for_vad)
    try:
        vad_segments, _ = transcribe_segments(wav_for_vad)
        expected_speech_duration = vad_segments[-1]["end"] if vad_segments else total_duration
    except Exception:
        logger.warning(f"[{req.content_id}] (Gemini) تخمین VAD ناموفق بود، از طول کل ویدیو استفاده میشه")
        expected_speech_duration = total_duration
    logger.info(f"[{req.content_id}] (Gemini) آخرین گفتار تشخیص‌داده‌شده تا ثانیه‌ی {expected_speech_duration:.1f}")

    logger.info(f"[{req.content_id}] (Gemini) مرحله ۵: ارسال صدا به Gemini Live API و دریافت دوبله")
    asyncio.run(gemini_live_dub(pcm_audio_path, dubbed_audio_path, expected_speech_duration))

    logger.info(f"[{req.content_id}] (Gemini) مرحله ۶: ترکیب نهایی با ویدیو")
    merge_audio_with_video(video_path, dubbed_audio_path, output_path)

    logger.info(f"[{req.content_id}] (Gemini) تمام شد ✅")
    return DubResult(
        content_id=req.content_id,
        original_transcript=None,  # این مسیر transcript جدا برنمی‌گردونه
        dubbed_transcript=None,
        original_language=None,
        local_output_path=str(output_path),
    )



    logger.info(f"[{req.content_id}] مرحله ۵: تشخیص جنسیت غالب")
    gender = detect_dominant_gender(audio_path)
    logger.info(f"[{req.content_id}] جنسیت تشخیص داده شده: {gender}")

    logger.info(f"[{req.content_id}] مرحله ۶: تبدیل صدا به متن به‌تفکیک جمله (whisper tiny)")
    raw_segments, detected_lang = transcribe_segments(audio_path)
    original_language = req.original_language or detected_lang
    logger.info(f"[{req.content_id}] زبان: {original_language}, تعداد جمله: {len(raw_segments)}")

    dubbed_segments = []
    persian_full_text_parts = []
    original_full_text_parts = []
    cursor = 0.0  # پایان زمانی آخرین قطعه‌ی دوبله‌شده - برای جلوگیری از هم‌پوشانی صداها

    for i, seg in enumerate(raw_segments):
        logger.info(f"[{req.content_id}] جمله {i+1}/{len(raw_segments)}: {seg['text']}")
        original_full_text_parts.append(seg["text"])

        window = max(0.3, seg["end"] - seg["start"])
        syllable_budget = max(3, int(window * 4.5))  # تخمین تقریبی: ~۴.۵ هجا در ثانیه گفتار طبیعی
        persian_text = translate_to_persian(seg["text"], max_syllables=syllable_budget)
        persian_full_text_parts.append(persian_text)

        raw_tts_path = job_dir / f"seg_{i}_raw.mp3"
        fitted_path = job_dir / f"seg_{i}.wav"
        logger.info(f"[{req.content_id}] جمله {i+1}/{len(raw_segments)}: TTS")
        asyncio.run(synthesize_speech(persian_text, gender, raw_tts_path))

        fit_segment_to_window(raw_tts_path, window, fitted_path)

        # اگه جمله‌ی قبلی تا بعد از شروع این جمله ادامه داشته، این یکی رو عقب می‌ندازیم
        # تا صداها روی هم نیفتن (حتی اگه یعنی یه‌کم دیرتر از زمان اصلی whisper پخش بشه)
        actual_start = max(seg["start"], cursor)
        fitted_duration = ffprobe_duration(fitted_path)
        cursor = actual_start + fitted_duration

        dubbed_segments.append({"start": actual_start, "path": fitted_path})

    logger.info(f"[{req.content_id}] مرحله ۷: چیدن قطعات صوتی سر زمان درستشون")
    build_timed_audio_track(dubbed_segments, total_duration, dubbed_track_path)

    logger.info(f"[{req.content_id}] مرحله ۸: ترکیب نهایی با ویدیو")
    merge_audio_with_video(video_path, dubbed_track_path, output_path)

    logger.info(f"[{req.content_id}] تمام شد ✅")
    return DubResult(
        content_id=req.content_id,
        original_transcript=" ".join(original_full_text_parts),
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


def _run_gemini_dubbing_job(job_id: str, req: "DubRequest", job_dir: Path) -> None:
    """نسخه‌ی پس‌زمینه‌ی پایپ‌لاین آزمایشی Gemini - همون الگوی _run_dubbing_job"""
    try:
        result = _run_gemini_dubbing_pipeline(req, job_dir)
        JOBS[job_id] = JobStatus(job_id=job_id, content_id=req.content_id, status="done", result=result)
    except Exception as e:
        logger.exception(f"[{req.content_id}] خطا در فرآیند دوبله (Gemini)")
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


@router.post("/process-gemini", response_model=JobAccepted, status_code=202)
async def process_dubbing_gemini(req: DubRequest):
    """
    نسخه‌ی آزمایشی (Preview): مسیر Gemini Live API - صدا-به-صدا با لحن مشابه،
    بدون تفکیک جمله یا هماهنگ‌سازی دستی. همون الگوی job نامتقارن /process رو داره.
    نیاز به GEMINI_API_KEY روی سرویس.
    """
    if not GEMINI_API_KEY:
        raise HTTPException(500, "GEMINI_API_KEY تنظیم نشده روی سرویس")

    job_id = str(uuid.uuid4())
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    JOBS[job_id] = JobStatus(job_id=job_id, content_id=req.content_id, status="processing")
    asyncio.create_task(asyncio.to_thread(_run_gemini_dubbing_job, job_id, req, job_dir))

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
