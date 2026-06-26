"""
OpenAI Whisper backend — POST audio to /v1/audio/transcriptions.

Uses aiohttp (already a project dep). No need for the ``openai`` SDK.
Streams the file from disk rather than reading it into memory.
"""

import os

import aiohttp

from bridge.transcription.base import Transcriber
from bridge.utils.logger import get_logger

logger = get_logger("transcription.openai")

_API_URL = "https://api.openai.com/v1/audio/transcriptions"
_OPENAI_MAX_BYTES = 25 * 1024 * 1024  # OpenAI's documented upload cap
_TIMEOUT = 180.0


class OpenAITranscriber(Transcriber):
    def __init__(self, api_key: str, model: str = "whisper-1"):
        self.api_key = api_key
        self.model = model

    async def transcribe(self, path: str, language: str | None = None) -> str | None:
        if not path or not os.path.isfile(path):
            return None
        try:
            size = os.path.getsize(path)
        except OSError:
            return None
        if size > _OPENAI_MAX_BYTES:
            logger.warning(
                "voice file %s is %.1f MB > OpenAI cap %.0f MB — skipping",
                os.path.basename(path), size / (1024 * 1024), _OPENAI_MAX_BYTES / (1024 * 1024),
            )
            return None

        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            with open(path, "rb") as fh:
                data = aiohttp.FormData()
                data.add_field(
                    "file", fh,
                    filename=os.path.basename(path),
                    content_type="application/octet-stream",
                )
                data.add_field("model", self.model)
                if language and language != "auto":
                    data.add_field("language", language)
                # response_format=text — server returns the plain transcript
                # as the body, no JSON parse needed.
                data.add_field("response_format", "text")
                timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(_API_URL, data=data, headers=headers) as resp:
                        body = await resp.text()
                        if resp.status != 200:
                            logger.warning(
                                "OpenAI Whisper failed (%d): %s", resp.status, body[:300],
                            )
                            return None
                        text = body.strip()
                        return text or None
        except (aiohttp.ClientError, OSError) as exc:
            logger.warning("OpenAI Whisper transport error: %s", exc)
            return None
