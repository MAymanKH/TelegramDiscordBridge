"""
Transcriber interface — one method, async, returns plain text or None.
"""


class Transcriber:
    async def transcribe(self, path: str, language: str | None = None) -> str | None:
        """Return the transcript of the audio at *path*, or ``None`` on failure.

        Implementations must not raise; a failure (missing file, network
        error, model error) returns ``None`` so the caller can fall back
        to the original audio without the transcript companion.
        """
        raise NotImplementedError
