"""
instagram_login.py — endpoint لاگین واقعی اینستاگرام (یوزرنیم/پسورد) از طریق instagrapi

این فایل رو کنار instagram_test_login.py و instagram_publish.py توی ریشه‌ی
پروژه بذارید، و توی main.py دو خط زیر رو اضافه کنید (کنار همون importهای
مشابه که از قبل برای instagram_test_login و instagram_publish هست):

    from instagram_login import router as instagram_login_router
    app.include_router(instagram_login_router)

چرا این فایل لازم بود:
  همه‌ی مسیرهای فعلی (test-login و publish) فرض می‌کنن یه cookie_b64 از قبل
  از یه‌جای دیگه (مثلاً مرورگر شخصی) ساخته شده. این فایل خودِ لاگین اولیه رو
  از طریق instagrapi و مستقیم از IP همین سرویس Render انجام می‌ده، و در پایان
  دقیقاً همون فرمت cookie_b64 (Netscape، base64-شده) رو برمی‌گردونه — یعنی
  خروجیش مستقیم قابل‌استفاده برای /instagram/test-login و /instagram/publish
  و همچنین برای INSTAGRAM_COOKIES_B64 (که main.py برای yt-dlp استفاده می‌کنه).

نحوه‌ی استفاده (از طریق curl یا n8n):

  مرحله ۱:
    POST /instagram/login
    { "username": "...", "password": "...", "proxy_url": "..." (اختیاری) }

  اگه پاسخ status == "success" بود، فیلدهای cookie_b64 و device_settings رو
  بگیرید و توی Supabase ذخیره کنید (device_settings رو برای درخواست‌های بعدی
  instagram_publish.py هم لازم دارید).

  اگه status == "two_factor_required" بود، کد رو از پیامک/اپ احراز هویت
  بگیرید و دوباره دقیقاً همون درخواست رو با فیلد اضافه‌ی verification_code
  بفرستید:

    POST /instagram/login
    { "username": "...", "password": "...", "verification_code": "123456" }

  اگه status == "challenge_required" بود، یعنی اینستاگرام یه چالش امنیتی
  (checkpoint) گذاشته که این endpoint نمی‌تونه خودکار حلش کنه — این حالت
  نیاز به رسیدگی دستی‌تر داره (پیام خطا توضیح می‌ده چی شده).
"""

import base64
import os
import time

from fastapi import APIRouter, HTTPException, Header, Request
from instagrapi import Client
from instagrapi.exceptions import (
    TwoFactorRequired,
    ChallengeRequired,
    BadPassword,
    PleaseWaitFewMinutes,
)

API_KEY = os.environ.get("API_KEY")

router = APIRouter()


def safe_get_settings(client):
    try:
        return client.get_settings() if client is not None else None
    except Exception:
        return None


def cookies_dict_to_netscape_b64(cookies: dict) -> str:
    """
    instagrapi از get_settings()["cookies"] یه دیکشنری ساده‌ی name->value
    برمی‌گردونه. اینجا به همون فرمت Netscape (۷ ستون تب‌جدا) تبدیلش می‌کنیم
    که parse_netscape_cookies توی instagram_test_login.py و
    instagram_publish.py انتظارش رو داره، بعد base64 می‌کنیم.
    """
    lines = ["# Netscape HTTP Cookie File"]
    expiry = int(time.time()) + 180 * 24 * 60 * 60  # ۱۸۰ روز اعتبار فرضی
    for name, value in (cookies or {}).items():
        # domain, includeSubdomains, path, secure, expiry, name, value
        lines.append(f".instagram.com\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}")
    cookie_text = "\n".join(lines) + "\n"
    return base64.b64encode(cookie_text.encode("utf-8")).decode("ascii")


@router.post("/instagram/login")
async def instagram_login(request: Request, x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    username = body.get("username")
    password = body.get("password")
    verification_code = body.get("verification_code")  # کد ۲مرحله‌ای، اختیاری
    proxy_url = body.get("proxy_url")  # همون الگوی proxy_url که n8n جاهای دیگه هم می‌فرسته
    # اگه از یه لاگین قبلی (حتی ناموفق) device_settings دارید، بفرستید تا
    # همون «دستگاه» حفظ بشه، نه یه دستگاه تصادفی جدید.
    device_settings = body.get("device_settings")

    if not username or not password:
        raise HTTPException(status_code=400, detail="username و password الزامی هستن")

    cl = Client()
    if device_settings:
        cl.set_settings(device_settings)
    if proxy_url:
        cl.set_proxy(proxy_url)

    try:
        if verification_code:
            cl.login(username, password, verification_code=verification_code)
        else:
            cl.login(username, password)

        settings = safe_get_settings(cl)
        cookie_b64 = cookies_dict_to_netscape_b64((settings or {}).get("cookies", {}))

        return {
            "status": "success",
            "cookie_b64": cookie_b64,
            "device_settings": settings,
        }

    except TwoFactorRequired:
        return {
            "status": "two_factor_required",
            "message": "اینستاگرام یه کد تأیید دو مرحله‌ای خواسته. کد رو از پیامک/اپ "
            "احراز هویت بگیرید و دوباره همین درخواست رو با فیلد verification_code بفرستید.",
            "device_settings": safe_get_settings(cl),
        }

    except ChallengeRequired as e:
        return {
            "status": "challenge_required",
            "message": "اینستاگرام یه چالش امنیتی (checkpoint) گذاشته که این endpoint "
            "نمی‌تونه خودکار حلش کنه — معمولاً یعنی باید یه‌بار با مرورگر واقعی این "
            "حساب رو تأیید کنید.",
            "error": str(e),
            "device_settings": safe_get_settings(cl),
        }

    except BadPassword:
        return {"status": "error", "message": "یوزرنیم یا پسورد اشتباهه."}

    except PleaseWaitFewMinutes as e:
        return {
            "status": "error",
            "message": "اینستاگرام موقتاً محدودمون کرده (rate limit) — چند دقیقه صبر کنید و دوباره امتحان کنید.",
            "error": str(e),
        }

    except Exception as e:  # noqa: BLE001 - هر خطای دیگه رو هم به‌جای ۵۰۰ خام برگردون
        return {
            "status": "error",
            "message": f"خطای غیرمنتظره: {e}",
            "device_settings": safe_get_settings(cl),
        }
