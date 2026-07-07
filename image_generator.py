"""Generates an episode-specific topic image: a short English image prompt is
derived from the Persian summary via the same free Groq LLM already used for
summarization, then rendered into an actual image by Pollinations.ai (a free,
no-signup, no-API-key text-to-image service).

If either step fails (LLM error, network error, service down), the caller
should fall back to a generic local banner (see telegram_bot.py) rather than
blocking the whole pipeline on an image.
"""
import logging
import re
import urllib.parse

import requests
from groq import Groq, APIStatusError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from summarizer import _is_reasoning_model  # reuse the same reasoning-model detection

logger = logging.getLogger(__name__)

IMAGE_API_BASE = "https://image.pollinations.ai/prompt"
IMAGE_WIDTH = 1200
IMAGE_HEIGHT = 630
HTTP_TIMEOUT = 40

_PROMPT_SYSTEM = """\
You write short prompts for an AI image generator, based on a Persian podcast
summary about an AI/technology topic. Output ONE vivid, concrete visual scene
description in English (15-25 words): specific people, objects, or settings
mentioned in the summary (e.g. a named company's product, a robot, a
courtroom, a stock chart) rather than abstract ideas. This will be rendered
directly by an image model, so do not ask for any text, logos, or words to
appear in the image. Output ONLY the description, nothing else -- no quotes,
no preamble.
"""


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


@retry(
    reraise=True,
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=2, min=2, max=15),
    retry=retry_if_exception_type((APIStatusError,)),
)
def _generate_image_prompt(summary_html: str, episode_title: str, groq_api_key: str, model: str) -> str:
    client = Groq(api_key=groq_api_key)
    summary_text = _strip_html(summary_html)[:2000]
    user_prompt = f"عنوان اپیزود: {episode_title}\n\nخلاصه:\n{summary_text}"

    extra_kwargs = {"reasoning_effort": "none"} if _is_reasoning_model(model) else {}
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _PROMPT_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.6,
        max_tokens=80,
        **extra_kwargs,
    )
    content = completion.choices[0].message.content.strip()
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    return content.strip('"').strip()


def fetch_topic_image(prompt: str) -> bytes:
    """Render `prompt` into an image via Pollinations.ai. Raises on failure."""
    encoded = urllib.parse.quote(prompt)
    url = f"{IMAGE_API_BASE}/{encoded}"
    resp = requests.get(
        url,
        params={"width": IMAGE_WIDTH, "height": IMAGE_HEIGHT, "nologo": "true"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.content


def get_episode_image(summary_html: str, episode_title: str, groq_api_key: str, model: str) -> bytes:
    """Return topic-relevant image bytes for this episode, or raise if either
    the prompt generation or the image render fails -- callers should catch
    and fall back to a generic banner rather than let this block posting."""
    prompt = _generate_image_prompt(summary_html, episode_title, groq_api_key, model)
    logger.info("Generated image prompt: %r", prompt)
    return fetch_topic_image(prompt)
