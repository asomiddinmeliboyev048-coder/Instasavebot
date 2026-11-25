import asyncio
import subprocess
import uuid
from pathlib import Path

from shazamio import Shazam

from app.config import Settings


def _run_ffmpeg(args: list[str], timeout: int = 90) -> None:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "FFmpeg error")[-1200:]
        raise RuntimeError(f"FFmpeg ishlamadi: {detail}")


async def make_video_note(source: Path, settings: Settings) -> Path:
    """Create a clear square Telegram video note with a fast software encode."""
    output = settings.download_dir / f"note-{uuid.uuid4().hex[:10]}.mp4"
    args = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-t", "30",
        "-vf", "fps=20,scale=480:480:force_original_aspect_ratio=increase:flags=bilinear,crop=480:480",
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-crf", "26", "-threads", "2", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-ac", "1",
        "-movflags", "+faststart", str(output),
    ]
    try:
        await asyncio.wait_for(asyncio.to_thread(_run_ffmpeg, args, 90), timeout=95)
        if not output.is_file() or output.stat().st_size > settings.max_file_size_bytes:
            raise ValueError("Yumaloq video Telegram hajm limitidan katta bo‘ldi.")
        return output
    except Exception:
        output.unlink(missing_ok=True)
        raise


async def identify_music(source: Path, settings: Settings) -> dict:
    """Recognize music with ShazamIO; audio extraction and recognition are bounded."""
    sample = settings.download_dir / f"music-{uuid.uuid4().hex[:10]}.mp3"
    args = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-vn", "-t", "25", "-ac", "1",
        "-ar", "32000", "-b:a", "80k", str(sample),
    ]
    try:
        await asyncio.wait_for(asyncio.to_thread(_run_ffmpeg, args, 35), timeout=40)
        if not sample.is_file() or sample.stat().st_size < 1500:
            raise LookupError("Videoda aniqlash uchun yetarli audio topilmadi.")
        async with Shazam() as shazam:
            result = await asyncio.wait_for(shazam.recognize(str(sample)), timeout=30)
        track = (result or {}).get("track") or {}
        if not track:
            raise LookupError("Bu videodagi musiqani topa olmadim. Boshqa videoni sinab ko‘ring.")
        return {
            "title": track.get("title"),
            "artist": track.get("subtitle"),
            "url": track.get("url"),
            "album": (track.get("sections") or [{}])[0].get("metadata", [{}])[0].get("text")
            if (track.get("sections") or [{}])[0].get("metadata") else None,
        }
    except asyncio.TimeoutError as exc:
        raise LookupError("Musiqa aniqlash vaqti tugadi. Yana urinib ko‘ring.") from exc
    finally:
        sample.unlink(missing_ok=True)
