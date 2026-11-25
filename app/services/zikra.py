import asyncio
import logging
import re
from urllib.parse import parse_qs, unquote, urlparse

from supabase import create_client

from app.config import Settings

log = logging.getLogger(__name__)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


def _safe_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value or ""):
        raise ValueError("Zikra jadval/ustun nomi noto‘g‘ri sozlangan.")
    return value


def _storage_object_from_url(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    parts = [unquote(p) for p in parsed.path.split("/") if p]
    # /storage/v1/object/(public|sign|authenticated)/bucket/path...
    try:
        idx = parts.index("object")
        if len(parts) < idx + 3:
            return None
        mode = parts[idx + 1]
        if mode not in {"public", "sign", "authenticated"}:
            return None
        return parts[idx + 2], "/".join(parts[idx + 3:])
    except ValueError:
        return None


def _video_id_from_page_url(url: str) -> str | None:
    """Extract the UUID used by Zikra's /reels?start=<reel UUID> share links."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    candidates = query.get("start", [])
    if candidates and _UUID_RE.fullmatch(candidates[0]):
        return candidates[0]

    match = re.search(r"/(?:video|videos|watch)/([0-9a-fA-F-]{36})/?$", parsed.path)
    if match and _UUID_RE.fullmatch(match.group(1)):
        return match.group(1)
    return None


async def resolve_zikra_video(url: str, settings: Settings) -> str:
    """Resolve a Zikra share URL to its actual downloadable media URL.

    Zikra Reels are stored in public.reels(video_url), with the video itself
    hosted on the platform's S3-compatible public storage. Other configured
    Supabase Storage paths remain supported for legacy/private assets.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed = settings.allowed_zikra_hosts.copy()
    if settings.supabase_url:
        allowed.add((urlparse(settings.supabase_url).hostname or "").lower())
    if host not in allowed:
        raise ValueError("Zikra havolasining domeni ruxsat etilmagan.")

    storage = _storage_object_from_url(url)
    if storage:
        bucket, object_path = storage
        if not object_path:
            raise ValueError("Supabase Storage fayl manzili topilmadi.")
        parts = [p for p in parsed.path.split("/") if p]
        mode = parts[parts.index("object") + 1] if "object" in parts else ""
        if mode in {"public", "sign"}:
            return url
        if not settings.supabase_url or not settings.supabase_service_role_key:
            raise ValueError("Bu Zikra videosi private. Supabase server kalitlari sozlanmagan.")
        return await asyncio.to_thread(_create_signed_url, settings, bucket, object_path)

    # The live Zikra share format is /reels?start=<UUID>.
    # Also retain support for /video/<UUID>, /videos/<UUID> and /watch/<UUID>.
    video_id = _video_id_from_page_url(url)
    if video_id:
        if not settings.supabase_url or not settings.supabase_service_role_key:
            raise ValueError(
                "Zikra reels havolasini ochish uchun Render'da SUPABASE_URL "
                "va SUPABASE_SERVICE_ROLE_KEY sozlanishi kerak."
            )
        if not settings.zikra_video_table:
            raise ValueError(
                "Zikra reels jadvali sozlanmagan. ZIKRA_VIDEO_TABLE=reels "
                "va ZIKRA_VIDEO_PATH_COLUMN=video_url qiymatlarini kiriting."
            )
        return await asyncio.to_thread(_lookup_video_url, settings, video_id)

    # A Zikra URL that is itself a direct video file can be downloaded by yt-dlp.
    if parsed.path.lower().endswith((".mp4", ".mov", ".m4v", ".webm")):
        return url

    raise ValueError(
        "Zikra havolasi tanildi, lekin video ID aniqlanmadi. "
        "Reels ulashish havolasi /reels?start=<video-id> formatida bo‘lishi kerak."
    )


def _create_signed_url(settings: Settings, bucket: str, path: str) -> str:
    client = create_client(settings.supabase_url, settings.supabase_service_role_key)
    result = client.storage.from_(bucket).create_signed_url(path, 900)
    signed = result.get("signedURL") or result.get("signedUrl")
    if not signed:
        raise RuntimeError("Supabase signed URL yaratolmadi.")
    if signed.startswith("/"):
        return settings.supabase_url.rstrip("/") + signed
    return signed


def _lookup_video_url(settings: Settings, video_id: str) -> str:
    table = _safe_identifier(settings.zikra_video_table)
    id_col = _safe_identifier(settings.zikra_video_id_column)
    media_col = _safe_identifier(settings.zikra_video_path_column)
    client = create_client(settings.supabase_url, settings.supabase_service_role_key)
    query = client.table(table).select("*").eq(id_col, video_id).limit(1).execute()
    rows = query.data or []
    if not rows:
        raise ValueError(
            "Zikra reeli bazadan topilmadi. Havola eskirgan yoki reel o‘chirilgan bo‘lishi mumkin."
        )

    media_value = rows[0].get(media_col)
    if not isinstance(media_value, str) or not media_value.strip():
        raise ValueError("Zikra bazasida video_url qiymati bo‘sh.")

    # Zikra reels.video_url stores an absolute public S3 URL, not a Supabase
    # Storage object path. Return it directly so yt-dlp downloads the real file.
    if media_value.startswith(("https://", "http://")):
        return media_value

    bucket_col = settings.zikra_video_bucket_column
    bucket = rows[0].get(_safe_identifier(bucket_col)) if bucket_col else settings.zikra_storage_bucket
    if not bucket:
        raise ValueError("Supabase Storage bucket nomi aniqlanmadi.")
    return _create_signed_url(settings, bucket, media_value)
