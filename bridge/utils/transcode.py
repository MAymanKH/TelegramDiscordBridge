"""
ffmpeg-based media transcoding to shrink oversized files for Discord.

The public entry point is :func:`compress_to_fit` — it returns a path to a
freshly-encoded copy that fits within ``target_bytes``, or ``None`` if the
format is unsupported or the encode failed. The caller is responsible for
deleting the returned file when done.
"""

import asyncio
import os
import shutil
import tempfile
from bridge.utils.logger import get_logger

logger = get_logger("transcode")

VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".gif"}
AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".oga", ".wav", ".opus", ".aac", ".flac"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}

_HEADROOM_RATIO = 0.92  # encoders overshoot bitrate; aim a bit below target
_FFMPEG_TIMEOUT = 300.0  # seconds; bound runtime so a malicious file can't hang the bridge
_FFPROBE_TIMEOUT = 30.0

# Where transcoded files are written. Defaults to ``messages/transcoded/``
# so a single docker-compose volume mapping for ``/app/messages`` covers the
# DB, source-side downloads, AND transcoder output. Override via env var
# ``BRIDGER_TRANSCODE_DIR`` if you want them somewhere else (e.g. tmpfs).
TRANSCODE_DIR = os.environ.get("BRIDGER_TRANSCODE_DIR") or os.path.join("messages", "transcoded")

async def compress_to_fit(file_path: str, target_bytes: int) -> str | None:
    """Produce a transcoded copy of *file_path* under *target_bytes*.

    Returns the new file's path on success, or ``None`` if the file isn't a
    supported format, ffmpeg isn't available, or the resulting file still
    exceeds the target.
    """
    if shutil.which("ffmpeg") is None:
        logger.warning("ffmpeg not found on PATH — cannot transcode %s", file_path)
        return None
    ext = os.path.splitext(file_path)[1].lower()
    target_bytes = max(int(target_bytes * _HEADROOM_RATIO), 65_536)
    try:
        if ext in VIDEO_EXTS: return await _compress_video(file_path, target_bytes)
        if ext in AUDIO_EXTS: return await _compress_audio(file_path, target_bytes)
        if ext in IMAGE_EXTS: return await _compress_image(file_path, target_bytes)
    except Exception as exc:
        logger.warning("Transcode failed for %s: %s", file_path, exc, exc_info=True)
    return None

# ---------------------------------------------------------------------- helpers

async def _run(*args: str, timeout: float = _FFMPEG_TIMEOUT) -> bool:
    """Run a subprocess with a hard timeout. Returns True on success.

    A malicious or malformed media file can cause ffmpeg to hang or burn
    CPU indefinitely; the timeout guarantees the bridge stays responsive.
    On timeout the child process is killed.
    """
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("%s timed out after %.0fs — killing", args[0], timeout)
        try: proc.kill()
        except ProcessLookupError: pass
        try: await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, ProcessLookupError): pass
        return False
    if proc.returncode != 0:
        logger.warning("%s failed (rc=%d): %s", args[0], proc.returncode, stderr[:500].decode("utf-8", errors="replace"))
        return False
    return True

async def _probe_duration(file_path: str) -> float | None:
    if shutil.which("ffprobe") is None: return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", file_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_FFPROBE_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("ffprobe timed out on %s — killing", file_path)
            try: proc.kill()
            except ProcessLookupError: pass
            try: await asyncio.wait_for(proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError): pass
            return None
        if proc.returncode != 0: return None
        return float(stdout.decode().strip())
    except (ValueError, OSError):
        return None

def _temp(suffix: str) -> str:
    """Create a unique transcoded-output file in :data:`TRANSCODE_DIR`.

    Uses :func:`tempfile.mkstemp` so the file is created O_EXCL (no race
    against a same-named file)."""
    os.makedirs(TRANSCODE_DIR, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="bridge_xcode_", dir=TRANSCODE_DIR)
    os.close(fd)
    return path

def _drop(path: str) -> None:
    try: os.remove(path)
    except OSError: pass

# -------------------------------------------------------------------- encoders

async def _compress_video(file_path: str, target_bytes: int) -> str | None:
    duration = await _probe_duration(file_path)
    if not duration or duration <= 0: return None

    target_bits = target_bytes * 8
    audio_kbps = 96
    audio_bps = audio_kbps * 1000
    video_bps = max(int(target_bits / duration) - audio_bps, 100_000)

    out = _temp(".mp4")
    ok = await _run(
        "ffmpeg", "-y", "-loglevel", "error", "-i", file_path,
        "-c:v", "libx264", "-b:v", str(video_bps),
        "-maxrate", str(int(video_bps * 1.2)), "-bufsize", str(int(video_bps * 2)),
        "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart",
        out,
    )
    if ok and os.path.getsize(out) <= target_bytes:
        return out
    _drop(out)
    return None

async def _compress_audio(file_path: str, target_bytes: int) -> str | None:
    duration = await _probe_duration(file_path)
    if not duration or duration <= 0: return None

    target_bps = max(int(target_bytes * 8 / duration), 32_000)
    target_bps = min(target_bps, 192_000)

    out = _temp(".m4a")
    ok = await _run(
        "ffmpeg", "-y", "-loglevel", "error", "-i", file_path,
        "-vn", "-c:a", "aac", "-b:a", str(target_bps),
        out,
    )
    if ok and os.path.getsize(out) <= target_bytes:
        return out
    _drop(out)
    return None

async def _compress_image(file_path: str, target_bytes: int) -> str | None:
    """Iteratively drop quality / resolution until the image fits."""
    out = _temp(".jpg")
    # ffmpeg's mjpeg quality scale is 2 (best) → 31 (worst); pair with shrinking
    for q, max_dim in [(4, 4096), (6, 3000), (10, 2200), (15, 1600), (20, 1200), (28, 800)]:
        ok = await _run(
            "ffmpeg", "-y", "-loglevel", "error", "-i", file_path,
            "-vf", f"scale='min({max_dim},iw)':'min({max_dim},ih)':force_original_aspect_ratio=decrease",
            "-q:v", str(q), out,
        )
        if ok and os.path.getsize(out) <= target_bytes:
            return out
    _drop(out)
    return None
