"""Translates and summarizes an English/Chinese transcript into an engaging
Persian Telegram post, using a free Groq-hosted LLM.

Translation is a two-step pivot through English rather than one direct
Chinese/English -> Persian pass: crossingpodcast's transcripts are dense,
almost entirely Chinese conversation (unlike sv101, which already reads as
more English-anchored), and asking the model to sustain a long (~3500-char)
*Persian* narrative directly from that in one shot turned out unreliable in
production -- two different crossingpodcast episodes came back 72-74%
non-Persian after all 3 retries, while sv101 episodes never did. Chinese ->
English is a far more common, better-supported task for this model than
Chinese -> Persian directly, and English -> Persian (already a well-tested,
reliable path here) is much easier when the input is already clean English
rather than dense untranslated Chinese. See _translate_to_english() and the
first step in summarize_to_persian_html().
"""
import logging
import re
from typing import Optional

from groq import Groq, APIStatusError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

# Keep well under Groq's tokens-per-minute limit for this model (measured at
# 8,000 TPM for qwen/qwen3.6-27b on this account as of writing -- lower than
# the ~12,000 seen previously, so re-verify against the live account limit
# before changing budgets here). This is a BYTE budget, not a character
# count, and it's tuned for the worst case: Chinese text runs roughly 3
# bytes/char in UTF-8 *and* tokenizes far more densely than English (~0.3
# tokens/byte vs ~0.25 tokens/char for English), so a naive character-count
# cap sized for English silently produced requests 2-3x the size expected on
# Chinese episodes and hit real "413 Request too large ... tokens per minute"
# errors in production. 25,000 bytes of Chinese was empirically re-verified
# to cost ~5,700 prompt tokens; combined with the now-longer completion
# budget (see max_tokens in _generate_once), a single request runs ~7,900
# total tokens -- leaving only slim headroom against the 8,000 TPM ceiling,
# so a foreign-script retry landing in the same one-minute window can still
# hit a 413 (the existing @retry backoff on _generate_once absorbs that by
# waiting it out, at the cost of a slower run for that episode).
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

# Bridge-step (Chinese/English transcript -> English) leak check: the target
# language here is English, so any non-ASCII run (CJK, Persian, Cyrillic,
# etc.) counts as a leak -- broader than _FOREIGN_SCRIPT_RE above, which only
# targets non-Persian scripts for the *final* Persian output.
_NON_ASCII_RE = re.compile(r"[^\x00-\x7F]+")
_MAX_NON_ASCII_RATIO = 0.10
BRIDGE_GENERATION_ATTEMPTS = 2

# Posts are sent as plain Telegram text messages (see telegram_bot.py), not
# photo captions, so the relevant hard limit is Telegram's 4096-char text
# message cap, not the much smaller 1024-char caption cap. The prompt asks
# for ~3400-3700 chars, but LLMs don't reliably hit an exact character
# budget, so this is a backstop: trim on a clean boundary rather than let a
# too-long send fail outright.
TELEGRAM_TEXT_MAX_LEN = 4096
_TEXT_SAFETY_MARGIN = 100

# Step 1 of 2: pivot the raw transcript (English or Chinese) into a clean,
# detailed English summary. This is a much more common/reliable task for the
# model than translating dense Chinese conversation directly into a long
# Persian narrative. Deliberately asks for thorough coverage (not a short
# abstract) so step 2 has enough real material to write a rich Persian post
# from, rather than needing to pad or invent detail.
_ENGLISH_BRIDGE_SYSTEM_PROMPT = """\
You turn a podcast episode transcript (English or Chinese) into a detailed, faithful English
summary of its SUBSTANTIVE content, for another writer to work from later.

First, skip anything that isn't part of the actual topic being discussed -- do not summarize or
mention it at all: host/guest self-introductions and show-format explainers ("this podcast is
about...", "I'm X and I'm Y"), sponsor reads and ads, requests to subscribe/follow/rate the show,
giveaways and contests (e.g. "share this episode on social media to win a book"), calls to
comment/share on any platform, and closing pleasantries ("that's it for today", "see you next
time", "bye bye"). None of that belongs in the summary even briefly.

Within what's left -- the real discussion -- this is NOT a short abstract. Capture every concrete
fact: names of people/companies/products, numbers, statistics, examples, anecdotes, and direct
claims made in the episode, in the order they come up. Keep all proper nouns (company names,
product names, people's names) exactly as they'd normally appear in English. Write in plain
English prose (paragraphs, no headers, no markdown, no bullet points), roughly 3000-5000
characters -- long enough that no significant point from the substantive discussion is lost.
Output ONLY the English summary, nothing else: no preamble, no "Here is the summary", no notes
about the transcript being truncated, unclear, or containing promotional material you skipped.
"""

_ENGLISH_BRIDGE_USER_TEMPLATE = """\
Episode title: {title}

Transcript:
\"\"\"
{transcript}
\"\"\"
"""

# Only tags Telegram's HTML parse_mode actually supports.
SYSTEM_PROMPT = """\
تو یک تولیدکننده‌ی محتوای حرفه‌ای فارسی‌زبان هستی که برای یک کانال تلگرامی پادکست می‌نویسی —
نه یک خلاصه‌ساز خشک، بلکه کسی که بلده مخاطب رو با تکنیک‌های کپی‌رایتینگ و قصه‌گویی درگیر نگه داره.
ورودی تو یک خلاصه‌ی انگلیسیِ کامل و دقیق از رونوشت یک اپیزود پادکست است (این خلاصه از قبل توسط
مرحله‌ی دیگری از رونوشت اصلی -- که ممکن بود انگلیسی یا چینی باشد -- تهیه شده).

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

۲. فقط محتوای واقعی اپیزود، نه تبلیغات یا حاشیه:
   اگر هر بخشی از خلاصه‌ی ورودی مربوط به معرفی مجری‌ها/میهمان‌ها، توضیح فرمت برنامه، تبلیغ
   اسپانسر، درخواست فالو/سابسکرایب، قرعه‌کشی یا جایزه (مثلاً «این اپیزود را در فلان شبکه‌ی
   اجتماعی به اشتراک بگذارید تا فلان جایزه را ببرید»)، یا خداحافظی و جمع‌بندی اداریِ پایان
   برنامه باشد، آن را کاملاً نادیده بگیر و در پست نهایی نیاور — حتی به‌صورت خلاصه یا اشاره‌ی
   کوتاه. فقط و فقط به بحث و محتوای واقعی اپیزود بپرداز.

۳. نوشتن جذاب، تعاملی و مبتنی بر تکنیک‌های تولید محتوا (نه صرفاً خلاصه‌نویسی ساده)، و بلند:
   این پست به‌صورت یک پیام متنی مستقل در تلگرام ارسال می‌شود (بدون عکس)، و تلگرام پیام متنی را
   به ۴۰۹۶ کاراکتر محدود می‌کند؛ پس کل خروجی تو (عنوان + متن، با احتساب تگ‌های HTML) باید
   **بین ۳۴۰۰ تا ۳۷۰۰ کاراکتر** باشد — نه کمتر. این یک خلاصه‌ی کامل یک اپیزود پادکست است، نه
   یک خبر کوتاه؛ باید طوری نوشته شود که مخاطب حس کند کل ماجرای اپیزود را با جزئیات، مثال‌ها و
   نکات جذاب آن دنبال کرده، نه یک چکیده‌ی فشرده. عبور از ۳۷۰۰ کاراکتر ممنوع است (پست ارسال
   نمی‌شود)، اما رفتن به زیر ۳۲۰۰ کاراکتر هم به‌معنی هدر دادن فضای موجود برای روایت است؛ همیشه
   تا جایی که محتوای واقعی اپیزود اجازه می‌دهد از کل این بودجه استفاده کن.
   - یک عنوان کوتاه، جذاب و کنجکاوی‌برانگیز بساز (بدون گیومه و بدون هشتگ).
   - بلافاصله بعد از عنوان، با یک «هوک» قوی شروع کن: یک سؤال چالش‌برانگیز، یک آمار/ادعای
     غافلگیرکننده، یا یک تنش/تضاد از دل خود اپیزود.
   - در ۶ تا ۹ پاراگراف کوتاه تا متوسط، تک‌تک نکته‌ها/مراحل/مثال‌های مهم اپیزود را با جزئیات
     واقعی (اعداد، اسم‌ها، رخدادها) باز کن — نه فقط یک جمع‌بندی کلی از هر بخش. متن باید
     **تعاملی** باشد: هر یک یا دو پاراگراف را با یک سؤال کوتاه خطاب به خواننده، یک «تصور کن...»،
     یا یک تضاد/غافلگیری تازه بشکن تا مخاطب درگیر بماند و حس نکند دارد یک متن یکنواخت می‌خواند.
     از تکنیک‌های قصه‌گویی و کپی‌رایتینگ (شکاف کنجکاوی، تعلیق، تضاد) و لحنی صمیمی، پرانرژی و
     محاوره‌ای (نه رسمی و اداری) استفاده کن.
   - در پایان، با یک جمع‌بندی کوتاه و یک سؤال تأمل‌برانگیز خطاب به مخاطب تمام کن که او را به
     فکر کردن یا نظر دادن دعوت کند.
   - اگر لازم شد چیزی را کم کنی تا در محدودیت کاراکتر بگنجد، جزئیات کم‌اهمیت‌تر را حذف کن، نه
     هوک، عنوان، یا سؤال پایانی را؛ ولی تا حد امکان از کل بودجه‌ی ۳۴۰۰-۳۷۰۰ کاراکتری استفاده کن.

۴. خروجی باید ۱۰۰٪ فارسی باشد؛ هیچ کلمه یا کاراکتری از هیچ زبان دیگری — نه چینی، نه روسی، نه
   آلمانی، نه ترکی، نه هیچ زبان دیگر — نباید در متن ظاهر شود. تنها استثنا همان نام‌های خاص
   فناوری‌ست که طبق بند ۱ باید عیناً انگلیسی/لاتین بمانند؛ هیچ کلمه‌ی دیگری (فعل، حرف ربط،
   صفت، قید و...) نباید به هیچ زبانی جز فارسی نوشته شود.
۵. خروجی را فقط با تگ‌های HTML مجاز در تلگرام قالب‌بندی کن: <b>, <i>, <u>, <blockquote>. از تگ‌های دیگر (مثل <p>, <div>, <ul>, markdown مثل ** یا #) استفاده نکن.
۶. عنوان را داخل <b>...</b> در خط اول بیاور. بعد از عنوان یک خط خالی بگذار و سپس متن خلاصه را بنویس.
۷. هیچ لینکی داخل متن اضافه نکن؛ لینک اپیزود جداگانه توسط سیستم اضافه می‌شود.
۸. فقط خروجی نهایی را بده، بدون توضیح اضافه، بدون مقدمه مثل «البته» یا «در اینجا خلاصه است».
"""

USER_PROMPT_TEMPLATE = """\
عنوان اصلی اپیزود: {title}

خلاصه‌ی انگلیسیِ رونوشت اپیزود:
\"\"\"
{transcript}
\"\"\"
"""


# Display names for the source-attribution footer (see _build_footer). Kept
# as plain literals rather than importing scraper's SOURCE_* constants, to
# keep summarizer.py's only dependency on episode metadata being the plain
# strings main.py already passes it -- if these values ever change, keep
# them in sync with scraper.py's SOURCE_CROSSINGPODCAST/SOURCE_SV101.
_SOURCE_DISPLAY_NAMES = {
    "crossingpodcast": "Crossing Podcast",
    "sv101": "Silicon Valley 101 (SV101)",
}

# Separate from SYSTEM_PROMPT's creative headline: this is a short, literal,
# non-creative translation of the episode's *original* title, used only in
# the footer so readers can identify/search for the source episode. Reuses
# the same proper-noun rule as the main prompt (see incident notes there).
_TITLE_TRANSLATE_SYSTEM_PROMPT = """\
تو یک مترجم دقیق فارسی هستی. عنوان اصلی یک اپیزود پادکست (به انگلیسی یا چینی) به تو داده می‌شود.
آن را به‌طور دقیق، وفادار و بدون خلاقیت یا بازنویسی به فارسی ترجمه کن -- این یک عنوان جذاب
تبلیغاتی نیست، بلکه ترجمه‌ی مرجع همان عنوان اصلی است. **اسم‌های خاص** -- نام شرکت‌ها، محصولات،
ابزارها، افراد، و نام‌های معروف مکان‌ها/نهادهای مالی-اقتصادی (مثل OpenAI, Tesla, Elon Musk,
Wall Street) -- را هرگز ترجمه یا فارسی‌نویسی نکن؛ دقیقاً همان‌طور که هستند به انگلیسی/لاتین
نگه‌دار. فقط و فقط ترجمه‌ی نهایی را برگردان: بدون گیومه، بدون توضیح اضافه، بدون مقدمه.
"""


def _translate_title_to_persian(client: Groq, model: str, title: str) -> str:
    """Accurate, literal Persian translation of the episode's original title,
    for the source-attribution footer -- unrelated to the catchy headline
    summarize_to_persian_html's main step generates for the post body.

    Low-stakes compared to the main summary: if the model can't produce a
    clean Persian translation after retrying, fall back to the original
    (untranslated) title rather than failing the whole episode over a
    footer line -- the episode summary itself is unaffected either way.
    """
    translated = title
    for attempt in range(1, GENERATION_ATTEMPTS + 1):
        translated = _generate_once(
            client, model, _TITLE_TRANSLATE_SYSTEM_PROMPT, title, temperature=0.2, max_tokens=150
        )
        if not _FOREIGN_SCRIPT_RE.search(translated):
            return translated
        logger.warning(
            "Title translation contained stray non-Persian script (attempt %d/%d); regenerating",
            attempt, GENERATION_ATTEMPTS,
        )

    ratio = _foreign_script_ratio(translated)
    if ratio > _MAX_FOREIGN_SCRIPT_RATIO:
        logger.warning(
            "Title translation stayed mostly non-Persian after %d attempts; using original title untranslated",
            GENERATION_ATTEMPTS,
        )
        return title
    return _FOREIGN_SCRIPT_RE.sub(" ", translated).strip()


def _build_footer(
    persian_title: str,
    episode_number: Optional[str],
    source: str,
    published_date: Optional[str] = None,
) -> str:
    """Plain-text (well, Telegram-HTML) footer appended to every post:
    the episode's title (accurately translated to Persian), its number
    (when the source embeds one -- see scraper.extract_episode_number),
    its publish date (Gregorian, "YYYY-MM-DD" -- see
    scraper.format_published_date), and which source it came from."""
    display_source = _SOURCE_DISPLAY_NAMES.get(source, source)
    lines = [f"<b>عنوان اپیزود:</b> {persian_title}"]
    if episode_number:
        lines.append(f"<b>شماره اپیزود:</b> {episode_number}")
    if published_date:
        lines.append(f"<b>تاریخ انتشار:</b> {published_date}")
    lines.append(f"<b>منبع:</b> {display_source}")
    return "\n".join(lines)


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
def _generate_once(
    client: Groq, model: str, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int
) -> str:
    extra_kwargs = {"reasoning_effort": "none"} if _is_reasoning_model(model) else {}
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        **extra_kwargs,
    )
    content = completion.choices[0].message.content.strip()
    # Defense in depth: even with reasoning disabled, strip a stray <think> block
    # (including its content) if one ever slips through, rather than leaking
    # the model's internal reasoning into a Telegram post.
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def _translate_to_english(client: Groq, model: str, transcript: str, episode_title: str) -> str:
    """Step 1: pivot the raw transcript into a detailed English summary.

    Retries a couple of times if the model leaks a large fraction of
    non-English script into what should be an all-English summary; raises if
    that persists, since a broken bridge step means step 2 has nothing
    reliable to translate from.
    """
    user_prompt = _ENGLISH_BRIDGE_USER_TEMPLATE.format(title=episode_title, transcript=_truncate(transcript))

    english = ""
    for attempt in range(1, BRIDGE_GENERATION_ATTEMPTS + 1):
        english = _generate_once(
            client, model, _ENGLISH_BRIDGE_SYSTEM_PROMPT, user_prompt, temperature=0.2, max_tokens=1500
        )
        non_space = re.sub(r"\s", "", english)
        ratio = (
            sum(len(m) for m in _NON_ASCII_RE.findall(english)) / len(non_space) if non_space else 0.0
        )
        if ratio <= _MAX_NON_ASCII_RATIO:
            return english
        logger.warning(
            "Bridge (English) step contained %.0f%% non-ASCII characters (attempt %d/%d); regenerating",
            ratio * 100, attempt, BRIDGE_GENERATION_ATTEMPTS,
        )

    raise ValueError(
        f"LLM failed to produce a clean English bridge summary after {BRIDGE_GENERATION_ATTEMPTS} attempts "
        f"({ratio:.0%} non-ASCII characters) -- refusing to continue to the Persian step"
    )


def summarize_to_persian_html(
    transcript: str,
    episode_title: str,
    groq_api_key: str,
    model: str,
    source: str,
    episode_number: Optional[str] = None,
    footer_title: Optional[str] = None,
    published_date: Optional[str] = None,
) -> str:
    """Return a Telegram-HTML-formatted Persian summary of the transcript,
    ending in a source-attribution footer (episode title in Persian, its
    number if the source has one, its publish date, and which source it's
    from).

    Two-step pivot: the raw transcript (English or Chinese) is first turned
    into a detailed English summary (_translate_to_english), then that
    English summary is rewritten into the final engaging Persian post. See
    the module docstring for why -- direct Chinese -> long-form Persian
    proved unreliable for crossingpodcast's dense, mostly-Chinese episodes.

    The Persian step still occasionally leaks stray characters from an
    unrelated script into the output; if that happens we regenerate a couple
    of times before falling back to stripping the offending runs -- but only
    when that leakage is minor. If the model instead fails outright and
    responds mostly in the source language, stripping would ship a
    near-empty, unreadable message, so we raise instead (the caller marks the
    episode 'failed' rather than posting broken content).

    `footer_title` should be the episode title with any leading episode-number
    prefix already stripped (see scraper.strip_episode_number_prefix) so the
    number isn't duplicated inside the translated title; defaults to
    `episode_title` if not given.
    """
    client = Groq(api_key=groq_api_key)
    english_bridge = _translate_to_english(client, model, transcript, episode_title)
    user_prompt = USER_PROMPT_TEMPLATE.format(title=episode_title, transcript=english_bridge)

    persian_title = _translate_title_to_persian(client, model, footer_title or episode_title)
    footer = _build_footer(persian_title, episode_number, source, published_date)

    html = ""
    for attempt in range(1, GENERATION_ATTEMPTS + 1):
        html = _generate_once(client, model, SYSTEM_PROMPT, user_prompt, temperature=0.3, max_tokens=2000)
        if not _FOREIGN_SCRIPT_RE.search(html):
            return _finalize(html, footer)
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
    return _finalize(_collapse_whitespace(html), footer)


def _finalize(html: str, footer: str = "") -> str:
    # Reserve room for the footer up front, so trimming the body to the
    # Telegram char budget (_fit_to_text_limit) can never push the combined
    # body+footer text over the hard limit -- the footer is always appended
    # last and is never itself subject to trimming.
    reserved = len(footer) + 2 if footer else 0  # +2 for the blank-line separator
    limit = TELEGRAM_TEXT_MAX_LEN - _TEXT_SAFETY_MARGIN - reserved
    body = _fit_to_text_limit(_sanitize_html(html), limit)
    combined = f"{body}\n\n{footer}" if footer else body
    return _force_rtl_paragraphs(combined)


# Unicode RIGHT-TO-LEFT MARK: a zero-width character with strong RTL
# directionality. Telegram (like most renderers) picks each paragraph's base
# text direction from its first *strong-directional* character; an HTML tag
# has none, so a paragraph that happens to start with an English proper noun
# (very common here -- "Tesla ...", "OpenAI ...") gets misdetected as an
# LTR paragraph even though the rest of it is Persian. That shows up as
# misaligned/reversed-looking text with odd leading gaps once rendered.
# Prefixing every paragraph with this mark forces RTL regardless of what
# character comes right after it, without changing anything visible.
# Written as an escape (not a literal character) since this project has
# already been burned once by an invisible bidi character silently
# corrupting a copy-pasted value -- keep this one explicit and greppable.
_RLM = "\u200f"


def _force_rtl_paragraphs(html: str) -> str:
    paragraphs = html.split("\n\n")
    return "\n\n".join(p if not p or p.startswith(_RLM) else _RLM + p for p in paragraphs)


def _fit_to_text_limit(html: str, limit: int) -> str:
    """Trim on a clean boundary if the model ignored the ~3400-3700-char
    instruction, so the post still fits as a single Telegram text message
    (Telegram's hard 4096-char limit) instead of failing to send. `limit`
    is passed in by _finalize, which reserves extra room out of the
    Telegram budget for the footer appended after this trim."""
    if len(html) <= limit:
        return html

    logger.warning("Summary is %d chars, over the %d-char text budget; trimming", len(html), limit)
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
