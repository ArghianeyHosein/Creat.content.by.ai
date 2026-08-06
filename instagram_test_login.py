# این کد رو به فایل اصلی سرویس FastAPI (همون‌جایی که بقیه‌ی اندپوینت‌ها مثل /dub هستن) اضافه کن.
# اگه instagrapi توی requirements.txt نیست، این خط رو بهش اضافه کن: instagrapi

import base64
from fastapi import HTTPException, Header, Request
from instagrapi import Client


def parse_netscape_cookies(cookie_text: str) -> dict:
    """فایل کوکی به فرمت Netscape (خط به خط، tab-separated) رو به دیکشنری تبدیل می‌کنه."""
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


@app.post("/instagram/test-login")
async def test_instagram_login(request: Request, x_api_key: str = Header(None)):
    # همون الگوی احراز هویت بقیه‌ی اندپوینت‌های سرویس
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    cookie_b64 = body.get("cookie_b64")
    if not cookie_b64:
        raise HTTPException(status_code=400, detail="cookie_b64 الزامیه")

    try:
        cookie_text = base64.b64decode(cookie_b64).decode("utf-8")
    except Exception as e:
        return {"success": False, "stage": "decode", "error": str(e)}

    cookies = parse_netscape_cookies(cookie_text)
    sessionid = cookies.get("sessionid")
    if not sessionid:
        return {
            "success": False,
            "stage": "parse",
            "error": "sessionid توی کوکی‌ها پیدا نشد",
            "found_cookie_names": list(cookies.keys()),
        }

    try:
        cl = Client()
        cl.login_by_sessionid(sessionid)
        account = cl.account_info()
        return {
            "success": True,
            "stage": "login",
            "username": account.username,
            "user_id": str(account.pk),
            "full_name": account.full_name,
        }
    except Exception as e:
        return {"success": False, "stage": "login", "error": str(e)}
