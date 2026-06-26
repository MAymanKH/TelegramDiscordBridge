"""
Voice-message transcription.

Two backends, each loaded lazily so the dependency only gets imported
when actually used:

* ``openai`` — calls the OpenAI Whisper API (no extra Python dep; uses
  aiohttp, which the bridge already pulls in). Needs an API key.
* ``local`` — runs faster-whisper on the host CPU. No key, no network.
  Adds ~50 MB of pip deps + a ~150 MB-1.5 GB model download on first
  use, so it's optional.

The chosen backend is configured per-platform under
``platforms.<name>.voice_transcription`` and resolved by
:func:`get_transcriber`. Returns ``None`` when the config is missing
required fields — callers then skip transcription silently.
"""

from bridge.transcription.base import Transcriber
from bridge.utils.logger import get_logger

logger = get_logger("transcription")


def get_transcriber(cfg: dict) -> "Transcriber | None":
    """Resolve a :class:`Transcriber` from the parsed voice_transcription cfg.

    Returns ``None`` if the provider is unknown or its required keys
    are missing — never raises. Lazy-imports the backend module so
    OpenAI-only deployments don't pay for faster-whisper and vice versa.
    """
    provider = (cfg.get("provider") or "openai").lower()
    if provider == "openai":
        key = cfg.get("openai_api_key")
        if not key:
            logger.warning("voice_transcription provider=openai requires openai_api_key — disabled")
            return None
        from bridge.transcription.openai_whisper import OpenAITranscriber
        return OpenAITranscriber(
            api_key=key,
            model=cfg.get("openai_model") or "whisper-1",
        )
    if provider in ("local", "faster-whisper", "faster_whisper", "whisper"):
        from bridge.transcription.local_whisper import LocalTranscriber
        return LocalTranscriber(model_name=cfg.get("local_model") or "small")
    logger.warning("voice_transcription: unknown provider %r — disabled", provider)
    return None


__all__ = ["Transcriber", "get_transcriber"]
