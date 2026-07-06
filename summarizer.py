"""Translates and summarizes an English/Chinese transcript into an engaging
Persian Telegram post, using a free Groq-hosted LLM.
"""
import logging
import re

from groq import Groq, APIStatusError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

# Keep well under Groq's free-tier tokens-per-minute limits for a single call.
MAX_TRANSCRIPT_CHARS = 90_000

# The model occasionally leaks stray non-Persian script characters (CJK,
# Cyrillic, Hangul, Kana) into an otherwise Persian output. These ranges
# catch that so we can retry generation instead of shipping garbled text.
_FOREIGN_SCRIPT_RE = re.compile(
    r"[一-鿿぀-ヿ가-힯Ѐ-ӿ]+"
)
GENERATION_ATTEMPTS = 3

# Only tags Telegram's HTML parse_mode actually supports.
SYSTEM_PROMPT = """\
تو یک تولیدکننده‌ی محتوای حرفه‌ای فارسی‌زبان هستی که برای یک کانال تلگرامی پادکست می‌نویسی —
نه یک خلاصه‌ساز خشک، بلکه کسی که بلده مخاطب رو با تکنیک‌های کپی‌رایتینگ و قصه‌گویی درگیر نگه داره.
ورودی تو رونوشت (transcript) یک اپیزود پادکست است که ممکن است به زبان انگلیسی یا چینی باشد.

وظایف تو:

۱. ترجمه‌ی دقیق:
   مفاهیم و اصطلاحات کلیدی را دقیق و بدون تحریف به فارسی منتقل کن. اما نام‌های خاص فناوری —
   یعنی نام ابزارها، محصولات، مدل‌های هوش مصنوعی و شرکت‌های تکنولوژی (مثل OpenAI, Anthropic,
   Claude, GPT-4, Llama, Groq, GitHub Copilot, Cursor, Google, xAI و مشابه آن‌ها) — را هرگز
   ترجمه یا فارسی‌نویسی نکن؛ همیشه دقیقاً به همان شکل انگلیسی/لاتین اصلی‌شان در متن بیاور
   (نه ترانویسی فارسی مثل «جی‌پی‌تی» یا «کلاد»).

۲. نوشتن جذاب و مبتنی بر تکنیک‌های تولید محتوا (نه صرفاً خلاصه‌نویسی ساده):
   - یک عنوان کوتاه، جذاب و کنجکاوی‌برانگیز بساز (بدون گیومه و بدون هشتگ).
   - بلافاصله بعد از عنوان، با یک «هوک» قوی شروع کن: یک سؤال چالش‌برانگیز، یک آمار/ادعای
     غافلگیرکننده، یا یک تنش/تضاد از دل خود اپیزود — چیزی که در همان یکی-دو جمله‌ی اول
     خواننده را متوقف کند و کنجکاوش کند که ادامه را بخواند.
   - در ادامه، به‌جای فهرست خشک نکات، از تکنیک‌های قصه‌گویی و کپی‌رایتینگ استفاده کن: ایجاد
     «شکاف کنجکاوی» (curiosity gap)، ساختن تعلیق قبل از افشای نکته‌ی کلیدی، تضادها و
     غافلگیری‌های خود بحث را برجسته کن، و از زبان صمیمی و پرانرژی (نه رسمی و اداری) استفاده کن.
   - در ۴ تا ۸ پاراگراف کوتاه مهم‌ترین نکات، بحث‌ها و ایده‌های اپیزود را پوشش بده، طوری که
     خواننده تا انتها درگیر بماند.
   - در پایان، در صورت مناسب بودن، با یک جمله‌ی تأمل‌برانگیز یا سؤالی برای فکرکردن مخاطب
     تمام کن (نه یک جمع‌بندی خشک اداری).

۳. خروجی باید ۱۰۰٪ فارسی باشد؛ هیچ کلمه یا کاراکتری از هیچ زبان دیگری — نه چینی، نه روسی، نه
   آلمانی، نه ترکی، نه هیچ زبان دیگر — نباید در متن ظاهر شود. تنها استثنا همان نام‌های خاص
   فناوری‌ست که طبق بند ۱ باید عیناً انگلیسی/لاتین بمانند؛ هیچ کلمه‌ی دیگری (فعل، حرف ربط،
   صفت، قید و...) نباید به هیچ زبانی جز فارسی نوشته شود.
۴. خروجی را فقط با تگ‌های HTML مجاز در تلگرام قالب‌بندی کن: <b>, <i>, <u>, <blockquote>. از تگ‌های دیگر (مثل <p>, <div>, <ul>, markdown مثل ** یا #) استفاده نکن.
۵. عنوان را داخل <b>...</b> در خط اول بیاور. بعد از عنوان یک خط خالی بگذار و سپس متن خلاصه را بنویس.
۶. هیچ لینکی داخل متن اضافه نکن؛ لینک اپیزود جداگانه توسط سیستم اضافه می‌شود.
۷. فقط خروجی نهایی را بده، بدون توضیح اضافه، بدون مقدمه مثل «البته» یا «در اینجا خلاصه است».
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
def _generate_once(client: Groq, model: str, user_prompt: str, temperature: float) -> str:
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
        max_tokens=2000,
    )
    return completion.choices[0].message.content.strip()


def summarize_to_persian_html(transcript: str, episode_title: str, groq_api_key: str, model: str) -> str:
    """Return a Telegram-HTML-formatted Persian summary of the transcript.

    The model occasionally leaks stray characters from an unrelated script
    (e.g. Chinese) into the Persian output; if that happens we regenerate a
    couple of times before falling back to stripping the offending runs.
    """
    client = Groq(api_key=groq_api_key)
    user_prompt = USER_PROMPT_TEMPLATE.format(title=episode_title, transcript=_truncate(transcript))

    html = ""
    for attempt in range(1, GENERATION_ATTEMPTS + 1):
        html = _generate_once(client, model, user_prompt, temperature=0.4)
        if not _FOREIGN_SCRIPT_RE.search(html):
            return _sanitize_html(html)
        logger.warning(
            "LLM output contained stray non-Persian script characters (attempt %d/%d); regenerating",
            attempt, GENERATION_ATTEMPTS,
        )

    logger.warning("Stray non-Persian characters persisted after %d attempts; stripping them", GENERATION_ATTEMPTS)
    html = _FOREIGN_SCRIPT_RE.sub(" ", html)
    return _sanitize_html(_collapse_whitespace(html))


def _collapse_whitespace(html: str) -> str:
    """Tidy up after stripping foreign-script runs: collapse repeated spaces
    left behind and drop tags that ended up empty (or whitespace/punctuation-only)."""
    html = re.sub(r"[ \t]{2,}", " ", html)
    html = re.sub(r"<(b|i|u|blockquote)>[\s,،]*</\1>", "", html)
    html = re.sub(r"[ \t]+\n", "\n", html)
    return html.strip()


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
