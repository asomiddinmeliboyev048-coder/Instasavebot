import asyncio
import logging
import re
from urllib.parse import unquote, urlparse

import httpx
from supabase import create_client

from app.config import Settings

log = logging.getLogger(__name__)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


async def resolve_zikra_video(url: str, settings: Settings) -> str:
    """Return a downloadable URL for a Zikra video link.

    Direct public/signed URLs are passed through. Private Storage object paths can
    be signed when SUPABASE_SERVICE_ROLE_KEY and a bucket are configured.
    Optional table lookup is deliberately schema-configurable; no DB schema is assumed.
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
        # Public and already signed links are directly usable.
        mode = [p for p in parsed.path.split("/") if p]
        mode = mode[mode.index("object") + 1] if "object" in mode else ""
        if mode in {"public", "sign"}:
            return url
        if not settings.supabase_url or not settings.supabase_service_role_key:
            raise ValueError("Bu Zikra videosi private. Supabase server kalitlari sozlanmagan.")
        return await asyncio.to_thread(_create_signed_url, settings, bucket, object_path)

    # Optional DB lookup for platform routes such as /video/<id>.
    if settings.zikra_video_table:
        match = re.search(r"/(?:video|videos|watch)/([A-Za-z0-9_-]+)", parsed.path)
        if match and settings.supabase_url and settings.supabase_service_role_key:
            return await asyncio.to_thread(_lookup_video_url, settings, match.group(1))

    # A Zikra URL that is itself a direct video file can be downloaded by yt-dlp.
    if parsed.path.lower().endswith((".mp4", ".mov", ".m4v", ".webm")):
        return url
    raise ValueError(
        "Bu Zikra havolasidan video manzilini aniqlab bo‘lmadi. "
        "Zikra video sahifasining URL formati yoki Supabase jadval sxemasi kerak bo‘ladi."
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
    path_col = _safe_identifier(settings.zikra_video_path_column)
    client = create_client(settings.supabase_url, settings.supabase_service_role_key)
    query = client.table(table).select("*").eq(id_col, video_id).limit(1).execute()
    rows = query.data or []
    if not rows:
        raise ValueError("Zikra videosi bazadan topilmadi yoki unga kirish ruxsati yo‘q.")
    row = rows[0]
    path = row.get(path_col)
    bucket_col = settings.zikra_video_bucket_column
    bucket = row.get(_safe_identifier(bucket_col)) if bucket_col else settings.zikra_storage_bucket
    if not path or not bucket:
        raise ValueError("Zikra jadvalidan Storage bucket yoki fayl yo‘li topilmadi.")
    return _create_signed_url(settings, bucket, path)
