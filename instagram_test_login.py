import base64
import os
from fastapi import APIRouter, HTTPException, Header, Request
from instagrapi import Client

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


@router.post("/instagram/test-login")
async def test_instagram_login(request: Request, x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    cookie_b64 = body.get("cookie_b64")
    # پروکسی از داخل Python انتخاب نمی‌شه؛ n8n (تنها جایی که با دیتابیس
    # کار می‌کنه) پروکسی فعال رو مستقیم توی بدنه‌ی درخواست می‌فرسته.
    proxy_url = body.get("proxy_url")
    device_settings = body.get("device_settings")
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

    def safe_get_settings(client):
        try:
            return client.get_settings() if client is not None else None
        except Exception:
            return None

    cl = None
    try:
        cl = Client()
        if device_settings:
            cl.set_settings(device_settings)
        if proxy_url:
            cl.set_proxy(proxy_url)
        cl.login_by_sessionid(sessionid)
        account = cl.account_info()
        return {
            "success": True,
            "stage": "login",
            "username": account.username,
            "user_id": str(account.pk),
            "full_name": account.full_name,
            "device_settings": safe_get_settings(cl),
        }
    except Exception as e:
        return {
            "success": False,
            "stage": "login",
            "error": str(e),
            "device_settings": safe_get_settings(cl),
        }
