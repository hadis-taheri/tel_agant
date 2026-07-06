# Podcast → Persian Telegram Agent

ایجنتی که به‌صورت دوره‌ای اپیزودهای جدید دو پادکست را پیدا می‌کند، صدا را به متن تبدیل می‌کند،
متن را به فارسی ترجمه/خلاصه می‌کند و خلاصه را در یک کانال تلگرام منتشر می‌کند.

منابع فعلی:
- [crossingpodcast.com](https://crossingpodcast.com) — از طریق API داخلی سایت (tRPC)
- [sv101.fireside.fm](https://sv101.fireside.fm) — از طریق فید RSS استاندارد Fireside

## معماری

```
scraper.py       -> پیدا کردن اپیزودهای جدید (API سایت اول + RSS سایت دوم)
transcriber.py   -> دانلود فایل صوتی + تبدیل صدا به متن (Groq Whisper, رایگان)
summarizer.py    -> ترجمه و خلاصه‌سازی ساختاریافته به فارسی (Groq Llama 3.3, رایگان) + فرمت HTML تلگرام
telegram_bot.py  -> ارسال خلاصه به کانال تلگرام
database.py      -> ثبت اپیزودهای پردازش‌شده در Supabase (جلوگیری از تکرار)
config.py        -> بارگذاری تنظیمات از .env
main.py          -> هماهنگ‌کننده‌ی کل فرایند
```

## پیش‌نیازها

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/download.html) نصب‌شده و در PATH (برای `pydub`، جهت برش فایل‌های صوتی طولانی)
- یک پروژه‌ی [Supabase](https://supabase.com) (رایگان)
- یک API key رایگان از [Groq Console](https://console.groq.com/keys) (هم برای Whisper و هم برای LLM)
- یک ربات تلگرام ساخته‌شده با [@BotFather](https://t.me/BotFather) که به‌عنوان ادمین به کانال شما اضافه شده باشد

## راه‌اندازی

1. نصب وابستگی‌ها:
   ```bash
   pip install -r requirements.txt
   ```

2. ساخت جدول در Supabase: فایل [`supabase_schema.sql`](supabase_schema.sql) را در بخش
   **SQL Editor** پروژه‌ی Supabase خود اجرا کنید.

3. فایل `.env` را از روی نمونه بسازید و مقادیر را پر کنید:
   ```bash
   cp .env.example .env
   ```
   - `SUPABASE_URL` / `SUPABASE_KEY`: از Project Settings → API در Supabase (از **service_role key** استفاده کنید چون از سمت سرور insert/update انجام می‌شود، نه از مرورگر)
   - `GROQ_API_KEY`: از console.groq.com
   - `TELEGRAM_BOT_TOKEN`: از BotFather
   - `TELEGRAM_CHANNEL_ID`: یوزرنیم کانال (مثل `@my_channel`) یا شناسه عددی کانال (مثل `-1001234567890`)

4. **مهم — قبل از اولین اجرای واقعی**: منابعی مثل `sv101` ده‌ها اپیزود قدیمی دارند. اگر
   نمی‌خواهید ایجنت همه‌ی تاریخچه را پردازش کند، یک‌بار این دستور را بزنید تا همه‌ی اپیزودهای
   فعلی به‌عنوان «قبلاً دیده‌شده» ثبت شوند (بدون پردازش) و از این به بعد فقط اپیزودهای واقعاً
   جدید پردازش شوند:
   ```bash
   python main.py --seed-only
   ```

5. اجرای یک‌بارِ ایجنت:
   ```bash
   python main.py
   ```

6. اجرای مداوم (بررسی دوره‌ای هر `CHECK_INTERVAL_MINUTES` دقیقه):
   ```bash
   python main.py --loop
   ```

   برای اجرای production، بهتر است به‌جای `--loop` از **Windows Task Scheduler** یا **cron** (روی
   لینوکس) برای اجرای دوره‌ای `python main.py` استفاده کنید — این‌طوری اگر پروسه کرش کند، خودش
   دوباره در سیکل بعدی اجرا می‌شود.

## نکات مهم

- **دانلود تکراری نمی‌شود**: هر اپیزود بر اساس `(source, external_id)` در جدول `episodes` در
  Supabase یکتا ثبت می‌شود، پس اجراهای بعدی فقط اپیزودهای جدید را پردازش می‌کنند.
- **مدیریت خطا**: اگر دانلود، STT، یا ارسال تلگرام برای یک اپیزود شکست بخورد، وضعیت آن اپیزود
  `failed` ثبت می‌شود و بقیه‌ی اپیزودها همچنان پردازش می‌شوند (فایل `agent.log` را برای جزئیات
  خطا ببینید). برای retry کردن یک اپیزود شکست‌خورده، ردیف آن را در جدول Supabase حذف کنید تا
  دوباره به‌عنوان اپیزود «جدید» شناسایی شود.
- **محدودیت نرخ (Rate limit)**: تماس‌های Groq (هم STT و هم LLM) با backoff نمایی retry می‌شوند.
  متغیر `MAX_EPISODES_PER_RUN` در `.env` تعداد اپیزودهایی که در هر اجرا پردازش می‌شوند را محدود
  می‌کند تا به سقف رایگان Groq نخورید.
- **اپیزودهای طولانی**: فایل‌های صوتی بزرگ‌تر از ~۲۰ مگابایت به تکه‌های ۱۰ دقیقه‌ای تقسیم و جدا
  جدا تبدیل به متن می‌شوند، سپس متن نهایی به هم چسبانده می‌شود.
- فایل `.env` هرگز نباید commit شود (در `.gitignore` قرار دارد).

## توسعه‌ی بعدی (اختیاری)

- اضافه کردن منابع جدید فقط با نوشتن یک تابع `fetch_*_episodes` در `scraper.py` که لیستی از
  `RawEpisode` برمی‌گرداند و اضافه‌کردن آن به `fetch_all_episodes`.
- اگر خواستید از مدل‌های دیگر Groq یا حتی OpenAI استفاده کنید، فقط کافی‌ست `GROQ_LLM_MODEL` /
  `GROQ_STT_MODEL` در `.env` را عوض کنید یا کلاینت را در `transcriber.py`/`summarizer.py` عوض کنید.
