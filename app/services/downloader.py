import asyncio
import logging
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from app.config import Settings

log = logging.getLogger(__name__)
_URL_RE = re.compile(r"https?://[^\\s<>]+", re.IGNORECASE)


def extract_url(text: str) -> str | None:
    match = _URL_RE.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(".,!?)»]}'\\"")


def classify_url(url: str, settings: Settings) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}:
        return "youtube"
    if host in {"instagram.com", "www.instagram.com", "m.instagram.com"}:
        return "instagram"
    supabase_host = (urlparse(settings.supabase_url).hostname or "").lower() if settings.supabase_url else ""
    if host in settings.allowed_zikra_hosts or (supabase_host and host == supabase_host):
        return "zikra"
    raise ValueError("Bu havola qo‘llab-quvvatlanmaydi. Instagram, YouTube yoki Zikra havolasini yuboring.")


def _download_sync(url: str, settings: Settings) -> Path:
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    # UUID avoids collisions when multiple users submit links at the same time.
    stamp = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    output = settings.download_dir / f"{stamp}.%(ext)s"
    opts = {
        "outtmpl": str(output),
        # Prefer a broadly compatible MP4 stream and cap quality to reduce processing/upload time.
        "format": "best[height<=1080][ext=mp4]/best[height<=1080]/best",
        "format_sort": ["res:1080", "fps", "codec:h264"],
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 20,
        "max_filesize": settings.max_file_size_bytes,
        "restrictfilenames": True,
        "cachedir": False,
    }
    if settings.ytdlp_cookies_file:
        cookie_path = Path(settings.ytdlp_cookies_file)
        if cookie_path.is_file():
            opts["cookiefile"] = str(cookie_path)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        requested = info.get("requested_downloads") or []
        filepath = requested[0].get("filepath") if requested else ydl.prepare_filename(info)
        path = Path(filepath)
        if not path.exists():
            candidate = path.with_suffix(".mp4")
            if candidate.exists():
                path = candidate
        if not path.is_file():
            matches = sorted(settings.download_dir.glob(f"{stamp}.*"))
            if matches:
                path = matches[0]
        if not path.is_file():
            raise RuntimeError("Video yuklandi, ammo fayl topilmadi.")
        if path.stat().st_size > settings.max_file_size_bytes:
            path.unlink(missing_ok=True)
            raise ValueError(f"Video {settings.max_file_size_mb} MB limitdan katta.")
        return path


async def download_video(url: str, settings: Settings, semaphore: asyncio.Semaphore) -> Path:
    async with semaphore:
        return await asyncio.wait_for(
            asyncio.to_thread(_download_sync, url, settings),
            timeout=settings.download_timeout_seconds,
        )
