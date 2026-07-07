"""Translates and summarizes an English/Chinese transcript into an engaging
Persian Telegram post, using a free Groq-hosted LLM.
"""
import logging
import re

from groq import Groq, APIStatusError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

# Keep well under Groq's free-tier tokens-per-minute limit (12,000 TPM as of
# writing). This is a BYTE budget, not a character count, and it's tuned for
# the worst case: Chinese text runs roughly 3 bytes/char in UTF-8 *and*
# tokenizes far more densely than English (~0.3 tokens/byte vs ~0.25
# tokens/char for English), so a naive character-count cap sized for English
# silently produced requests 2-3x the size expected on Chinese episodes and
# hit real "413 Request too large ... tokens per minute" errors in production.
# 25,000 bytes of Chinese was empirically verified to cost ~7,800 total tokens
# (prompt + completion) against this account's limit, leaving real headroom.
MAX_TRANSCRIPT_BYTES = 25_000

# The model occasionally leaks stray non-Persian script characters (CJK,
# Cyrillic, Hangul, Kana) into an otherwise Persian output. These ranges
# catch that so we can retry generation instead of shipping garbled text.
_FOREIGN_SCRIPT_RE = re.compile(
    r"[一-鿿぀-ヿ가-힯Ѐ-ӿ]+"
)
GENERATION_ATTEMPTS = 3

# If foreign-script characters are only a small stray fraction of the output,
# stripping them still leaves a readable Persian summary. But if the model
# fails outright and answers mostly/entirely in the source language (seen in
# production on a Chinese episode: stripping left only numbers, punctuation,
# and English company names -- effectively empty), stripping instead produces
# garbage. Above this ratio we raise instead of shipping a broken summary.
_MAX_FOREIGN_SCRIPT_RATIO = 0.15

# The whole post is sent as a single photo caption (see telegram_bot.py), and
# Telegram hard-caps captions at 1024 characters. The prompt asks for ~800
# chars, but LLMs don't reliably hit an exact character budget, so this is a
# backstop: trim on a clean boundary rather than let a caption send fail.
TELEGRAM_CAPTION_MAX_LEN = 1024
_CAPTION_SAFETY_MARGIN = 20

# Only tags Telegram's HTML parse_mode actually supports.
SYSTEM_PROMPT = """\
تو یک تولیدکننده‌ی محتوای حرفه‌ای فارسی‌زبان هستی که برای یک کانال تلگرامی پادکست می‌نویسی —
نه یک خلاصه‌ساز خشک، بلکه کسی که بلده مخاطب رو با تکنیک‌های کپی‌رایتینگ و قصه‌گویی درگیر نگه داره.
ورودی تو رونوشت (transcript) یک اپیزود پادکست است که ممکن است به زبان انگلیسی یا چینی باشد.

وظایف تو:

۱. ترجمه‌ی دقیق:
   مفاهیم و اصطلاحات کلیدی را دقیق و بدون تحریف به فارسی منتقل کن. اما **هر اسم خاص** — نام
   شرکت‌ها (مثل OpenAI, Anthropic, Google, Tesla, SpaceX, Netflix, Toyota)، محصولات و برندها
   (مثل GPT-4, Model 3, Cybertruck, iPhone)، ابزارها و مدل‌های هوش مصنوعی (مثل Claude, Llama,
   Groq, GitHub Copilot, Cursor, xAI)، اسامی افراد (مثل Elon Musk)، و **نام مکان‌ها/نهادهای
   مالی و اقتصادی معروف** (مثل Wall Street, Silicon Valley, Nasdaq) — را هرگز ترجمه،
   فارسی‌نویسی یا ترانویسی نکن؛ همیشه دقیقاً به همان شکل انگلیسی/لاتین اصلی‌شان بنویس.
   مثال‌های اشتباهِ رایج که باید از آن‌ها پرهیز کنی: «جی‌پی‌تی» (درست: GPT)، «تسلا» یا «تلسا»
   (درست: Tesla)، «ایلان ماسک» (درست: Elon Musk)، «وال‌استریت» یا هر شکل مشابه/مخلوط دیگر
   (درست: Wall Street). اگر مطمئن نیستی یک اسم خاص است یا نه، آن را انگلیسی نگه‌دار؛ ریسک
   فارسی‌نویسی‌کردن یک اسم خاص از ریسک انگلیسی گذاشتن یک کلمه‌ی عادی بیشتر است.

۲. نوشتن جذاب و مبتنی بر تکنیک‌های تولید محتوا (نه صرفاً خلاصه‌نویسی ساده)، اما جمع‌وجور:
   این پست زیر یک عکس در تلگرام به‌عنوان کپشن قرار می‌گیرد و تلگرام کپشن را به ۱۰۲۴ کاراکتر
   محدود می‌کند؛ پس کل خروجی تو (عنوان + متن، با احتساب تگ‌های HTML) باید **بین ۹۰۰ تا ۹۵۰
   کاراکتر** باشد — نه کمتر. این یک پادکست است، نه یک خبر کوتاه تکنولوژی؛ از کل این فضا برای
   روایتی غنی و جذاب استفاده کن، نه یک خلاصه‌ی حداقلی. عبور از ۹۵۰ کاراکتر ممنوع است (پست
   ارسال نمی‌شود)، اما رفتن به زیر ۸۵۰ کاراکتر هم به‌معنی هدر دادن فضای موجود برای روایت است.
   - یک عنوان کوتاه، جذاب و کنجکاوی‌برانگیز بساز (بدون گیومه و بدون هشتگ).
   - بلافاصله بعد از عنوان، با یک «هوک» قوی شروع کن: یک سؤال چالش‌برانگیز، یک آمار/ادعای
     غافلگیرکننده، یا یک تنش/تضاد از دل خود اپیزود.
   - در ۳ تا ۴ پاراگراف کوتاه (نه فقط یک نکته، بلکه چند نکته یا مرحله‌ی مهم داستان اپیزود)،
     با تکنیک‌های قصه‌گویی و کپی‌رایتینگ (شکاف کنجکاوی، تعلیق، تضاد) و لحنی صمیمی و پرانرژی
     (نه رسمی و اداری) روایت کن.
   - در پایان، با یک جمله‌ی کوتاه تأمل‌برانگیز یا سؤالی تمام کن.
   - اگر لازم شد چیزی را کم کنی تا در محدودیت کاراکتر بگنجد، جزئیات کم‌اهمیت‌تر را حذف کن، نه
     هوک یا عنوان را؛ ولی تا حد امکان از کل بودجه‌ی ۹۰۰-۹۵۰ کاراکتری استفاده کن.

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
    encoded = transcript.encode("utf-8")
    if len(encoded) <= MAX_TRANSCRIPT_BYTES:
        return transcript
    logger.warning(
        "Transcript too long (%d bytes), truncating to %d bytes before sending to the LLM",
        len(encoded), MAX_TRANSCRIPT_BYTES,
    )
    # Slicing raw UTF-8 bytes can land mid-character; decode with errors="ignore"
    # to drop any incomplete trailing byte sequence instead of raising/corrupting.
    truncated = encoded[:MAX_TRANSCRIPT_BYTES].decode("utf-8", errors="ignore")
    return truncated + "\n...[transcript truncated]"


# Qwen3 models ("hybrid" reasoning models) default to an extremely verbose
# "thinking" mode that can burn through max_tokens before ever producing the
# actual answer, wrapped in a <think>...</think> block we don't want to show
# users. Passing reasoning_effort="none" disables that (confirmed empirically
# against Groq's Qwen3 models); passing it to a non-reasoning model like the
# Llama models is a hard 400 error, so it's only included for models that need it.
_REASONING_MODEL_PREFIXES = ("qwen/",)


def _is_reasoning_model(model: str) -> bool:
    return model.startswith(_REASONING_MODEL_PREFIXES)


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=3, min=3, max=60),
    retry=retry_if_exception_type((APIStatusError,)),
)
def _generate_once(client: Groq, model: str, user_prompt: str, temperature: float) -> str:
    extra_kwargs = {"reasoning_effort": "none"} if _is_reasoning_model(model) else {}
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
        max_tokens=650,
        **extra_kwargs,
    )
    content = completion.choices[0].message.content.strip()
    # Defense in depth: even with reasoning disabled, strip a stray <think> block
    # (including its content) if one ever slips through, rather than leaking
    # the model's internal reasoning into a Telegram post.
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def summarize_to_persian_html(transcript: str, episode_title: str, groq_api_key: str, model: str) -> str:
    """Return a Telegram-HTML-formatted Persian summary of the transcript.

    The model occasionally leaks stray characters from an unrelated script
    (e.g. Chinese) into the Persian output; if that happens we regenerate a
    couple of times before falling back to stripping the offending runs --
    but only when that leakage is minor. If the model instead fails outright
    and responds mostly in the source language, stripping would ship a
    near-empty, unreadable message, so we raise instead (the caller marks the
    episode 'failed' rather than posting broken content).
    """
    client = Groq(api_key=groq_api_key)
    user_prompt = USER_PROMPT_TEMPLATE.format(title=episode_title, transcript=_truncate(transcript))

    html = ""
    for attempt in range(1, GENERATION_ATTEMPTS + 1):
        html = _generate_once(client, model, user_prompt, temperature=0.3)
        if not _FOREIGN_SCRIPT_RE.search(html):
            return _finalize(html)
        logger.warning(
            "LLM output contained stray non-Persian script characters (attempt %d/%d); regenerating",
            attempt, GENERATION_ATTEMPTS,
        )

    ratio = _foreign_script_ratio(html)
    if ratio > _MAX_FOREIGN_SCRIPT_RATIO:
        raise ValueError(
            f"LLM failed to produce a majority-Persian summary after {GENERATION_ATTEMPTS} attempts "
            f"({ratio:.0%} non-Persian-script characters) -- refusing to post a garbled summary"
        )

    logger.warning("Stray non-Persian characters persisted after %d attempts; stripping them", GENERATION_ATTEMPTS)
    html = _FOREIGN_SCRIPT_RE.sub(" ", html)
    return _finalize(_collapse_whitespace(html))


def _finalize(html: str) -> str:
    return _fit_to_caption_limit(_sanitize_html(html))


def _fit_to_caption_limit(html: str) -> str:
    """Trim on a clean boundary if the model ignored the ~800-char instruction,
    so the post still fits as a single photo caption (Telegram's hard 1024-char
    caption limit) instead of failing to send."""
    limit = TELEGRAM_CAPTION_MAX_LEN - _CAPTION_SAFETY_MARGIN
    if len(html) <= limit:
        return html

    logger.warning("Summary is %d chars, over the %d-char caption budget; trimming", len(html), limit)
    window = html[:limit]
    cut_at = window.rfind("\n\n")
    if cut_at == -1 or cut_at < limit * 0.4:
        cut_at = -1
        for punct in (".", "؟", "!", "،", "؛"):
            idx = window.rfind(punct)
            if idx > cut_at:
                cut_at = idx + 1
    if cut_at == -1 or cut_at < limit * 0.4:
        cut_at = limit

    return _close_open_tags(html[:cut_at].rstrip())


def _close_open_tags(html: str) -> str:
    """Append closing tags for any allowed tag left open after truncation."""
    open_tags = []
    for is_closing, tag in re.findall(r"<(/?)([a-zA-Z]+)>", html):
        tag = tag.lower()
        if tag not in _ALLOWED_TAGS:
            continue
        if is_closing:
            if open_tags and open_tags[-1] == tag:
                open_tags.pop()
        else:
            open_tags.append(tag)
    for tag in reversed(open_tags):
        html += f"</{tag}>"
    return html


def _foreign_script_ratio(text: str) -> float:
    """Fraction of non-whitespace characters that belong to a foreign script."""
    non_space = re.sub(r"\s", "", text)
    if not non_space:
        return 0.0
    foreign_chars = sum(len(match) for match in _FOREIGN_SCRIPT_RE.findall(text))
    return foreign_chars / len(non_space)


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
