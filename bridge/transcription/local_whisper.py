"""
Local faster-whisper backend.

The import is lazy and the model is loaded once per process (cached on
the class). The model lives in RAM for the lifetime of the bridge — the
``small`` default needs about 500 MB. Use ``tiny`` or ``base`` on a
memory-constrained host; ``medium`` / ``large`` if the host has the
spare RAM and you want better accuracy.

``faster-whisper`` is NOT pinned in requirements.txt — it pulls in
ctranslate2 and torch-like deps that bloat the image. Install it on
demand: ``pip install faster-whisper``. The bridge logs a clear error
and falls back to "no transcript" if the package is missing.
"""

import asyncio
import os

from bridge.transcription.base import Transcriber
from bridge.utils.logger import get_logger

logger = get_logger("transcription.local")


class LocalTranscriber(Transcriber):
    _model = None
    _model_name = None
    _model_lock = asyncio.Lock()

    def __init__(self, model_name: str = "small"):
        self.model_name = model_name

    async def _ensure_model(self):
        async with LocalTranscriber._model_lock:
            if (
                LocalTranscriber._model is not None
                and LocalTranscriber._model_name == self.model_name
            ):
                return LocalTranscriber._model
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                logger.error(
                    "voice_transcription provider=local needs faster-whisper — "
                    "pip install faster-whisper"
                )
                return None
            # Model load is heavy and synchronous; offload to a thread so
            # the asyncio loop keeps spinning.
            try:
                model = await asyncio.to_thread(
                    WhisperModel, self.model_name, device="cpu", compute_type="int8",
                )
            except Exception as exc:
                logger.error("faster-whisper failed to load model %r: %s", self.model_name, exc)
                return None
            LocalTranscriber._model = model
            LocalTranscriber._model_name = self.model_name
            return model

    async def transcribe(self, path: str, language: str | None = None) -> str | None:
        if not path or not os.path.isfile(path):
            return None
        model = await self._ensure_model()
        if model is None:
            return None
        lang = language if (language and language != "auto") else None

        def _run():
            segments, _info = model.transcribe(path, language=lang, beam_size=1)
            return " ".join(s.text.strip() for s in segments if s.text).strip()

        try:
            text = await asyncio.to_thread(_run)
        except Exception as exc:
            logger.warning("local whisper failed on %s: %s", os.path.basename(path), exc)
            return None
        return text or None
