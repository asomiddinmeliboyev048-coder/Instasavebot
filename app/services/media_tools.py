import asyncio
import subprocess
import uuid
from pathlib import Path

from shazamio import Shazam

from app.config import Settings


def _run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True, text=True, timeout=75)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "FFmpeg error")[-1200:]
        raise RuntimeError(f"FFmpeg ishlamadi: {detail}")


async def make_video_note(source: Path, settings: Settings) -> Path:
    """Create a square MP4 video note and preserve the original audio when present."""
    output = settings.download_dir / f"note-{uuid.uuid4().hex[:10]}.mp4"
    args = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-t", "30",
        # Smaller square + 24 fps + ultrafast preset greatly reduces CPU work
        # on Render while keeping the video note clear on a phone screen.
        "-vf", "fps=15,scale=320:320:force_original_aspect_ratio=increase:flags=fast_bilinear,crop=320:320",
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-crf", "32",
        "-threads", "2", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k", "-ac", "1",
        "-movflags", "+faststart", str(output),
    ]
    await asyncio.to_thread(_run_ffmpeg, args)
    if not output.is_file() or output.stat().st_size > settings.max_file_size_bytes:
        output.unlink(missing_ok=True)
        raise ValueError("Dumaloq video Telegram hajm limitidan katta bo‘ldi.")
    return output


async def identify_music(source: Path, settings: Settings) -> dict:
    """Recognize music with the free ShazamIO client; no API token required."""
    sample = settings.download_dir / f"music-{uuid.uuid4().hex[:10]}.mp3"
    args = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-vn", "-t", "45", "-ac", "1",
        "-ar", "44100", "-b:a", "96k", str(sample),
    ]
    try:
        await asyncio.to_thread(_run_ffmpeg, args)
        if not sample.is_file() or sample.stat().st_size < 2048:
            raise LookupError("Videoda aniqlash uchun yetarli audio topilmadi.")
        async with Shazam() as shazam:
            result = await asyncio.wait_for(shazam.recognize(str(sample)), timeout=35)
        track = (result or {}).get("track") or {}
        if not track:
            raise LookupError("Bu videodagi musiqani topa olmadim. Boshqa videoni sinab ko‘ring.")
        return {"title": track.get("title"), "artist": track.get("subtitle"), "url": track.get("url")}
    except asyncio.TimeoutError as exc:
        raise LookupError("Musiqa aniqlash vaqti tugadi. Yana urinib ko‘ring.") from exc
    finally:
        sample.unlink(missing_ok=True)
