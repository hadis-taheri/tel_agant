"""Translates and summarizes an English/Chinese transcript into an engaging
Persian Telegram post, using a free Groq-hosted LLM.
"""
import logging

from groq import Groq, APIStatusError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

# Keep well under Groq's free-tier tokens-per-minute limits for a single call.
MAX_TRANSCRIPT_CHARS = 90_000

# Only tags Telegram's HTML parse_mode actually supports.
SYSTEM_PROMPT = """\
تو یک ویراستار حرفه‌ای فارسی‌زبان هستی که برای یک کانال تلگرامی پادکست، خلاصه می‌نویسی.
ورودی تو رونوشت (transcript) یک اپیزود پادکست است که ممکن است به زبان انگلیسی یا چینی باشد.

وظایف تو:
۱. مفاهیم و اصطلاحات کلیدی را دقیق و بدون تحریف به فارسی ترجمه و منتقل کن (اصطلاحات تخصصی AI/تک را در صورت نیاز داخل پرانتز به انگلیسی هم بیاور).
۲. یک خروجی ساختاریافته و جذاب برای مخاطب فارسی‌زبان بنویس، شامل:
   - یک عنوان کوتاه، جذاب و کنجکاوی‌برانگیز (بدون گیومه و بدون هشتگ)
   - یک خلاصه روایی و درگیرکننده (نه فهرست خشک) در ۴ تا ۸ پاراگراف کوتاه که مهم‌ترین نکات، بحث‌ها و ایده‌های اپیزود را پوشش می‌دهد و خواننده را کنجکاو نگه می‌دارد
۳. خروجی را فقط با تگ‌های HTML مجاز در تلگرام قالب‌بندی کن: <b>, <i>, <u>, <blockquote>. از تگ‌های دیگر (مثل <p>, <div>, <ul>, markdown مثل ** یا #) استفاده نکن.
۴. عنوان را داخل <b>...</b> در خط اول بیاور. بعد از عنوان یک خط خالی بگذار و سپس متن خلاصه را بنویس.
۵. هیچ لینکی داخل متن اضافه نکن؛ لینک اپیزود جداگانه توسط سیستم اضافه می‌شود.
۶. فقط خروجی نهایی را بده، بدون توضیح اضافه، بدون مقدمه مثل «البته» یا «در اینجا خلاصه است».
"""

USER_PROMPT_TEMPLATE = """\
عنوان اصلی اپیزود: {title}

رونوشت اپیزود:
\"\"\"
{transcript}
\"\"\"
"""


def _truncate(transcript: str) -> str:
    if len(transcript) <= MAX_TRANSCRIPT_CHARS:
        return transcript
    logger.warning(
        "Transcript too long (%d chars), truncating to %d chars before sending to the LLM",
        len(transcript), MAX_TRANSCRIPT_CHARS,
    )
    return transcript[:MAX_TRANSCRIPT_CHARS] + "\n...[transcript truncated]"


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=3, min=3, max=60),
    retry=retry_if_exception_type((APIStatusError,)),
)
def summarize_to_persian_html(transcript: str, episode_title: str, groq_api_key: str, model: str) -> str:
    """Return a Telegram-HTML-formatted Persian summary of the transcript."""
    client = Groq(api_key=groq_api_key)
    user_prompt = USER_PROMPT_TEMPLATE.format(title=episode_title, transcript=_truncate(transcript))

    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.6,
        max_tokens=2000,
    )
    html = completion.choices[0].message.content.strip()
    return _sanitize_html(html)


_ALLOWED_TAGS = {"b", "i", "u", "blockquote"}


def _sanitize_html(html: str) -> str:
    """Strip markdown code fences and any HTML tag Telegram doesn't allow."""
    html = html.strip()
    if html.startswith("```"):
        html = html.strip("`")
        if html.lower().startswith("html"):
            html = html[4:].strip()

    import re

    def _tag_filter(match: "re.Match") -> str:
        tag = match.group(1).lower().lstrip("/")
        return match.group(0) if tag in _ALLOWED_TAGS else ""

    return re.sub(r"</?([a-zA-Z0-9]+)[^>]*>", _tag_filter, html)
