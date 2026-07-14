"""Downloads episode audio and converts it to text using Groq's free Whisper API.

Groq's free tier caps uploaded audio size (and Whisper itself struggles with very
long single files), so long episodes are split into fixed-length chunks with
pydub/ffmpeg and transcribed piece by piece, then stitched back together.
"""
import logging
import os
import uuid
from typing import Optional

import requests
from groq import Groq, APIStatusError
from pydub import AudioSegment
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 60
DOWNLOAD_CHUNK_BYTES = 1024 * 1024

# Keep chunks comfortably under Groq's free-tier per-file limit.
CHUNK_LENGTH_MS = 10 * 60 * 1000  # 10 minutes
MAX_CHUNK_FILE_BYTES = 20 * 1024 * 1024  # 20 MB safety margin

# Bare "PodcastAgent/1.0" (no browser-style prefix, no Accept header) is a
# classic bot fingerprint -- real incident: Lenny's Podcast audio URLs
# (api.substack.com/feed/podcast/.../*.mp3, which 307-redirect to a signed
# substackcdn.com/Cloudflare URL) downloaded fine from a residential/dev
# network but came back 403 Forbidden specifically from GitHub Actions
# runners, matching Cloudflare's well-known pattern of blocking/challenging
# known cloud-provider IP ranges more aggressively for suspicious-looking
# requests. crossingpodcast/sv101 use different, less-protected hosts and
# never hit this. Matching scraper.py's browser-style UA plus a real Accept
# header is a cheap mitigation; it's not guaranteed to defeat pure
# IP-reputation blocking, hence also adding retry below for the cases where
# it's a transient challenge rather than a hard block.
DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PodcastAgent/1.0)",
    "Accept": "*/*",
}


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=3, min=3, max=30),
    retry=retry_if_exception_type(requests.exceptions.RequestException),
)
def download_audio(url: str, dest_dir: str) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    ext = os.path.splitext(url.split("?")[0])[1] or ".mp3"
    dest_path = os.path.join(dest_dir, f"{uuid.uuid4().hex}{ext}")

    with requests.get(url, stream=True, timeout=HTTP_TIMEOUT, headers=DOWNLOAD_HEADERS) as resp:
        resp.raise_for_status()
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
                if chunk:
                    fh.write(chunk)

    logger.info("Downloaded audio %s -> %s (%.1f MB)", url, dest_path, os.path.getsize(dest_path) / 1e6)
    return dest_path


def _split_into_chunks(audio_path: str, tmp_dir: str) -> list[str]:
    """Split audio into <=CHUNK_LENGTH_MS pieces. Returns list of chunk file paths."""
    if os.path.getsize(audio_path) <= MAX_CHUNK_FILE_BYTES:
        return [audio_path]

    logger.info("Audio exceeds size threshold, splitting into chunks: %s", audio_path)
    audio = AudioSegment.from_file(audio_path)
    chunk_paths = []
    for i, start_ms in enumerate(range(0, len(audio), CHUNK_LENGTH_MS)):
        chunk = audio[start_ms:start_ms + CHUNK_LENGTH_MS]
        chunk_path = os.path.join(tmp_dir, f"{uuid.uuid4().hex}_part{i}.mp3")
        chunk.export(chunk_path, format="mp3", bitrate="64k")
        chunk_paths.append(chunk_path)
    return chunk_paths


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=3, min=3, max=60),
    retry=retry_if_exception_type((APIStatusError, requests.exceptions.RequestException)),
)
def _transcribe_chunk(client: Groq, model: str, chunk_path: str, language: Optional[str]) -> str:
    with open(chunk_path, "rb") as fh:
        kwargs = {"file": (os.path.basename(chunk_path), fh.read()), "model": model, "response_format": "text"}
        if language:
            kwargs["language"] = language
        result = client.audio.transcriptions.create(**kwargs)
    # response_format="text" -> result is already a plain string in recent SDK versions,
    # but fall back to `.text` for safety across SDK versions.
    return result if isinstance(result, str) else getattr(result, "text", str(result))


def transcribe_episode(
    audio_url: str,
    tmp_dir: str,
    groq_api_key: str,
    model: str,
    language: Optional[str] = None,
) -> str:
    """Download the episode audio and return its full transcript."""
    client = Groq(api_key=groq_api_key)
    downloaded_path = download_audio(audio_url, tmp_dir)
    files_to_clean = [downloaded_path]

    try:
        chunk_paths = _split_into_chunks(downloaded_path, tmp_dir)
        files_to_clean.extend(p for p in chunk_paths if p != downloaded_path)

        transcripts = []
        for idx, chunk_path in enumerate(chunk_paths):
            logger.info("Transcribing chunk %d/%d", idx + 1, len(chunk_paths))
            transcripts.append(_transcribe_chunk(client, model, chunk_path, language))

        return "\n".join(t.strip() for t in transcripts if t and t.strip())
    finally:
        for path in files_to_clean:
            try:
                os.remove(path)
            except OSError:
                pass
