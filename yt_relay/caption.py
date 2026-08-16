"""Generates the Persian caption JSON for one video via a single Groq call.

Reuses summarizer._generate_once (the podcast pipeline's hardened Groq call
wrapper) rather than calling the Groq SDK directly: that function already
disables Qwen's verbose <think> reasoning preamble for reasoning-model
prefixes and retries on 429s with backoff -- both real, previously-debugged
issues on this exact Groq account (see CLAUDE.md). This is the only import
this package makes from the podcast pipeline besides telegram_bot.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from groq import Groq

from summarizer import _generate_once

logger = logging.getLogger(__name__)

MAX_HEADLINE_LEN = 70
MAX_TAKEAWAY_LEN = 90
MAX_TAGS = 3
MIN_TAKEAWAYS = 2

TEMPERATURE = 0.3
MAX_TOKENS = 700

SYSTEM_PROMPT = """\
تو یک ویراستار خبری فارسی هستی که برای یک کانال تلگرامی معرفی ویدیوهای یوتیوب می‌نویسی.
فقط بر اساس عنوان و توضیحات ویدیویی که کاربر می‌دهد بنویس -- هیچ عدد، ادعا، نتیجه‌گیری یا
اطلاعات بیرونی که مستقیماً از متن ورودی قابل استنتاج نیست اضافه نکن.

قواعد:
۱. خروجی فقط فارسی باشد. اسم‌های خاص -- نام شرکت‌ها، محصولات، افراد، ابزارها (مثل Claude،
   OpenAI، API، agent) -- باید دقیقاً به همان شکل لاتین بمانند و هرگز به فارسی ترنسلیتره
   نشوند. مثال غلط: «کلود»، «اوپن‌ای‌آی». مثال درست: «Claude»، «OpenAI».
۲. لحن خبری و خنثی باشد. از کلیشه‌های تبلیغاتی، «حتماً ببینید»، و علامت تعجب پرهیز کن.
۳. headline حداکثر ۷۰ کاراکتر، summary دقیقاً یک جمله، هر takeaway حداکثر ۹۰ کاراکتر،
   حداکثر ۳ تگ.
۴. اگر ورودی برای نوشتن حداقل دو takeaway واقعی کافی نبود، insufficient را true بگذار و
   بقیه‌ی فیلدها را رشته/آرایه‌ی خالی بگذار -- بهتر است چیزی تولید نشود تا چیزی بی‌محتوا یا
   ساختگی تولید شود.

فقط یک شیء JSON برگردان، بدون بک‌تیک و بدون هیچ متن اضافه قبل یا بعدش، دقیقاً با این شکل:
{"insufficient": false, "headline": "...", "summary": "...", "takeaways": ["...", "...", "..."], "tags": ["...", "..."]}
"""

USER_PROMPT_TEMPLATE = """\
عنوان ویدیو: {title}

توضیحات ویدیو:
{description}
"""


class CaptionValidationError(ValueError):
    pass


@dataclass
class Caption:
    insufficient: bool
    headline: str = ""
    summary: str = ""
    takeaways: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)


def _strip_code_fence(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    return raw


def validate_caption(raw: str) -> Caption:
    """Parse and validate the LLM's raw JSON output. Raises
    CaptionValidationError on any parse failure or invalid field -- callers
    must NOT publish on this path (spec section 6, "خطای پارس ... failed،
    نه انتشار")."""
    cleaned = _strip_code_fence(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise CaptionValidationError(f"Invalid JSON from caption LLM: {exc}") from exc

    if not isinstance(data, dict):
        raise CaptionValidationError("Caption JSON is not an object")

    insufficient = bool(data.get("insufficient", False))
    if insufficient:
        return Caption(insufficient=True)

    headline = str(data.get("headline", "")).strip()
    summary = str(data.get("summary", "")).strip()
    takeaways = [str(t).strip() for t in data.get("takeaways", []) if str(t).strip()]
    tags = [str(t).strip() for t in data.get("tags", []) if str(t).strip()][:MAX_TAGS]

    if not headline or len(headline) > MAX_HEADLINE_LEN:
        raise CaptionValidationError(f"Invalid headline (len={len(headline)}): {headline!r}")
    if not summary:
        raise CaptionValidationError("Missing summary")
    if len(takeaways) < MIN_TAKEAWAYS:
        raise CaptionValidationError(f"Only {len(takeaways)} takeaway(s), need >= {MIN_TAKEAWAYS}")
    for t in takeaways:
        if len(t) > MAX_TAKEAWAY_LEN:
            raise CaptionValidationError(f"Takeaway too long (len={len(t)}): {t!r}")

    return Caption(insufficient=False, headline=headline, summary=summary, takeaways=takeaways, tags=tags)


def generate_caption(client: Groq, model: str, title: str, cleaned_description: str) -> Caption:
    """One Groq call -> validated Caption. Raises CaptionValidationError if
    the model's output doesn't parse/validate; callers should mark the row
    'failed' on that exception rather than retrying inline (see module
    docstring and CLAUDE.md section 6)."""
    description = cleaned_description or "(بدون توضیحات؛ فقط بر اساس عنوان بنویس)"
    user_prompt = USER_PROMPT_TEMPLATE.format(title=title, description=description)
    raw = _generate_once(client, model, SYSTEM_PROMPT, user_prompt, temperature=TEMPERATURE, max_tokens=MAX_TOKENS)
    return validate_caption(raw)
