import asyncio
import logging
import subprocess
import uuid
from pathlib import Path

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


def _run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "FFmpeg error")[-1200:]
        raise RuntimeError(f"FFmpeg ishlamadi: {detail}")


async def make_video_note(source: Path, settings: Settings) -> Path:
    """Create Telegram-compatible square MP4 video note (up to 60 seconds)."""
    output = settings.download_dir / f"note-{uuid.uuid4().hex[:10]}.mp4"
    args = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-t", "60",
        "-vf", "scale=640:640:force_original_aspect_ratio=increase,crop=640:640,fps=30",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
    ]
    await asyncio.to_thread(_run_ffmpeg, args)
    if not output.is_file() or output.stat().st_size > settings.max_file_size_bytes:
        output.unlink(missing_ok=True)
        raise ValueError("Dumaloq video Telegram hajm limitidan katta bo‘ldi.")
    return output


async def identify_music(source: Path, settings: Settings) -> dict:
    """Send a short audio sample to AudD. Requires AUDD_API_TOKEN."""
    if not settings.audd_api_token:
        raise ValueError("Musiqani aniqlash xizmati ulanmagan. Admin Render'ga AUDD_API_TOKEN qo‘shishi kerak.")

    sample = settings.download_dir / f"sample-{uuid.uuid4().hex[:10]}.mp3"
    args = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-t", "18", "-vn", "-ac", "1",
        "-ar", "44100", "-b:a", "128k", str(sample),
    ]
    try:
        await asyncio.to_thread(_run_ffmpeg, args)
        timeout = httpx.Timeout(25.0, connect=8.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            with sample.open("rb") as audio:
                response = await client.post(
                    "https://api.audd.io/",
                    data={"api_token": settings.audd_api_token, "return": "spotify,apple_music"},
                    files={"file": ("sample.mp3", audio, "audio/mpeg")},
                )
            response.raise_for_status()
            payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(payload.get("error", {}).get("error_message", "Musiqa aniqlanmadi."))
        result = payload.get("result")
        if not result:
            raise LookupError("Bu videoda aniq musiqa topilmadi.")
        return result
    finally:
        sample.unlink(missing_ok=True)
