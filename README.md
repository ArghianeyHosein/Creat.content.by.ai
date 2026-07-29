# YouTube CC Downloader — راهنمای راه‌اندازی

## مرحله ۱: ساخت ریپو روی GitHub
1. یه ریپو جدید (خصوصی یا عمومی، فرقی نمی‌کنه) بساز.
2. سه‌تا فایل `main.py`، `requirements.txt`، و همین `README.md` رو توش آپلود کن.

## مرحله ۲: دیپلوی روی Render
1. برو به [render.com](https://render.com) و با گیت‌هاب ثبت‌نام/لاگین کن.
2. **New +** → **Web Service** رو بزن.
3. ریپویی که ساختی رو انتخاب کن.
4. تنظیمات:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: Free
5. تو بخش **Environment Variables**، یه متغیر بساز:
   - Key: `API_KEY`
   - Value: یه رشته‌ی تصادفی و طولانی خودت انتخاب کن (مثلاً از یه پسورد-جنریتور). این کلید امنیتی سرویس‌ته — جایی محرمانه نگهش دار.
6. **Create Web Service** رو بزن و صبر کن دیپلوی تموم بشه (چند دقیقه طول می‌کشه).
7. بعد از اتمام، یه آدرس شبیه این بهت می‌ده: `https://your-service-name.onrender.com`

## مرحله ۳: تست دستی (قبل از وصل‌کردن به n8n)
تو ترمینال یا Postman امتحان کن:
```bash
curl -H "X-API-Key: همون-کلیدی-که-ساختی" \
  "https://your-service-name.onrender.com/download?url=https://www.youtube.com/watch?v=VIDEO_ID" \
  --output test.mp4
```
اگه یه فایل mp4 معتبر دانلود شد (نه یه پیام خطا)، یعنی کار می‌کنه.

## نکته‌ی مهم درباره‌ی پلن رایگان Render
سرویس‌های رایگان Render بعد از ~۱۵ دقیقه بی‌کاری «می‌خوابن» و اولین درخواست بعدش کند جواب می‌ده (Cold Start، تا ۱ دقیقه طول بکشه). برای اتوماسیونی که هر چند ساعت یه‌بار اجرا می‌شه (نه لحظه‌ای)، این مشکلی نیست — فقط تو n8n روی نود HTTP Request که این سرویس رو صدا می‌زنه، تایم‌اوت رو حداقل ۹۰-۱۲۰ ثانیه بذار.

## مرحله ۴: اتصال به n8n (بعد از تست موفق)
وقتی مطمئن شدی سرویس درست کار می‌کنه، بگو تا با هم:
1. جستجوی YouTube رو با `videoLicense=creativeCommon` بسازیم (فقط ویدیوهای قابل‌بازنشر).
2. یه نود HTTP Request به این سرویس اضافه کنیم که `url=<لینک ویدیوی CC>` رو با هدر `X-API-Key` بفرسته و فایل رو با `responseFormat=file` بگیره.
3. بقیه‌ی زنجیره (Upload to Backblaze + Insert to downloaded_files) رو دقیقاً مثل چیزی که برای Pexels/Pixabay ساختیم وصل کنیم.
