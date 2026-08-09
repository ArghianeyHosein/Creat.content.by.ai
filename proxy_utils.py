"""
ابزار مشترک برای گرفتن پروکسی فعال از جدول proxies توی Supabase.
هم main.py (برای yt-dlp) و هم instagram_publish.py / instagram_test_login.py
(برای instagrapi) از همین تابع استفاده می‌کنن تا هر دو مسیر (دانلود و
لاگین/پابلیش) از یک پروکسی هماهنگ عبور کنن.
"""
import os
import requests
from typing import Optional

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
PROXY_MAX_AGE_SECONDS = int(os.environ.get("PROXY_MAX_AGE_SECONDS", "86400"))


def get_active_proxy_url() -> Optional[str]:
    """
    پروکسی فعال رو (با در نظر گرفتن چرخش دوره‌ای) از Supabase می‌گیره و به
    فرمت URL آماده (مثلا http://user:pass@ip:port) برمی‌گردونه.

    اگه Supabase تنظیم نشده باشه، پروکسی‌ای فعال نباشه، یا هر خطای دیگه‌ای
    پیش بیاد، None برمی‌گردونه — یعنی بدون پروکسی تلاش می‌کنیم (سرویس نباید
    به‌خاطر این قطع بشه).
    """
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return None
    try:
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/rpc/get_or_rotate_active_proxy",
            json={"p_max_age_seconds": PROXY_MAX_AGE_SECONDS},
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return None
        row = rows[0]
        protocol = row.get("protocol") or "http"
        ip = row.get("ip")
        port = row.get("port")
        username = row.get("username")
        password = row.get("password")
        if not (ip and port):
            return None
        if username and password:
            return f"{protocol}://{username}:{password}@{ip}:{port}"
        return f"{protocol}://{ip}:{port}"
    except Exception:
        return None


def log_error(source: str, message: str, job_id: str = None, details: dict = None) -> None:
    """
    خطا رو مستقیم و بی‌درنگ (synchronous) توی errors_notifications ثبت می‌کنه —
    این کار قبل از هر تلاش/retry دیگه‌ای انجام می‌شه، چون اگه منتظر برگشتن
    جواب HTTP به n8n بمونیم، ممکنه n8n زودتر timeout بزنه و خطا هیچ‌وقت
    توی هیچ لاگی (جز لاگ خام Render) ثبت نشه.

    اگه Supabase تنظیم نشده باشه یا خودِ این درخواست fail بشه، بی‌سروصدا رد
    می‌شه — این نباید جریان اصلی رو مختل کنه.
    """
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/errors_notifications",
            json={
                "job_id": job_id,
                "level": "error",
                "source": source,
                "message": message,
                "details": details or {},
            },
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
    except Exception:
        pass


def rotate_proxy(status: str = "failed") -> None:
    """
    پروکسی فعال فعلی رو غیرفعال می‌کنه و پروکسی بعدی توی صف رو فعال می‌کنه
    (همون تابع rotate_proxy توی Supabase که برای منطق چرخش دانلود هم
    استفاده می‌شه). برای مواردی مثل خطای لاگین/JSONDecodeError که نشون‌دهنده‌ی
    مشکل خودِ پروکسیه، نه صرفاً یه خطای گذرا.

    اگه Supabase تنظیم نشده باشه یا خطایی پیش بیاد، بی‌سروصدا رد می‌شه —
    این نباید جلوی جریان اصلی رو بگیره.
    """
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        return
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/rpc/rotate_proxy",
            json={"p_status": status},
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
    except Exception:
        pass
