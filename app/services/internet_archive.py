import asyncio
import re
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx

from app.config import Settings


API = "https://archive.org"


async def search_archive_audio(title: str, artist: str) -> list[dict]:
    """Search public Archive.org audio items and retain reusable public-domain/CC items."""
    terms = " ".join(x for x in (title, artist) if x).strip()
    if not terms:
        return []
    query = f'mediatype:audio AND (title:("{title}") OR "{terms}")'
    params = {
        "q": query,
        "fl[]": ["identifier", "title", "creator", "licenseurl", "mediatype"],
        "rows": 12,
        "page": 1,
        "output": "json",
    }
    timeout = httpx.Timeout(15.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 headers={"User-Agent": "InstasaveBot/1.0 (audio metadata lookup)"}) as client:
        response = await client.get(f"{API}/advancedsearch.php", params=params)
        response.raise_for_status()
        docs = response.json().get("response", {}).get("docs", [])
    results = []
    for doc in docs:
        license_url = str(doc.get("licenseurl") or "").lower()
        # Do not offer items with unknown or restrictive licensing for download.
        if not any(token in license_url for token in ("publicdomain", "creativecommons.org/licenses/by", "creativecommons.org/publicdomain")):
            continue
        identifier = str(doc.get("identifier") or "")
        if not identifier:
            continue
        results.append({
            "identifier": identifier,
            "title": str(doc.get("title") or identifier),
            "creator": str(doc.get("creator") or ""),
            "page_url": f"https://archive.org/details/{identifier}",
        })
    return results[:5]


async def download_archive_audio(identifier: str, settings: Settings) -> tuple[Path, str, str]:
    timeout = httpx.Timeout(45.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 headers={"User-Agent": "InstasaveBot/1.0"}) as client:
        response = await client.get(f"{API}/metadata/{quote(identifier, safe='')}")
        response.raise_for_status()
        data = response.json()
        metadata = data.get("metadata") or {}
        files = data.get("files") or []
        license_url = str(metadata.get("licenseurl") or "").lower()
        if not any(token in license_url for token in ("publicdomain", "creativecommons.org/licenses/by", "creativecommons.org/publicdomain")):
            raise LookupError("Bu audio uchun ochiq foydalanish litsenziyasi tasdiqlanmadi.")
        candidates = [
            f for f in files
            if str(f.get("name", "")).lower().endswith((".mp3", ".ogg", ".m4a", ".wav"))
            and not f.get("private") and not f.get("source") == "derivative"
            and int(f.get("size") or 0) <= settings.max_file_size_bytes
        ]
        if not candidates:
            raise LookupError("Ruxsat etilgan yoki Telegram hajmiga mos audio fayl topilmadi.")
        candidates.sort(key=lambda f: (not f["name"].lower().endswith(".mp3"), int(f.get("size") or 0)))
        file_info = candidates[0]
        name = str(file_info["name"])
        url = f"{API}/download/{quote(identifier, safe='')}/{quote(name, safe='/')}"
        path = settings.download_dir / f"archive-{uuid.uuid4().hex[:10]}{Path(name).suffix.lower()}"
        settings.download_dir.mkdir(parents=True, exist_ok=True)
        try:
            async with client.stream("GET", url) as stream:
                stream.raise_for_status()
                size = 0
                with path.open("wb") as out:
                    async for chunk in stream.aiter_bytes(64 * 1024):
                        size += len(chunk)
                        if size > settings.max_file_size_bytes:
                            raise LookupError("Audio Telegram hajm limitidan katta.")
                        out.write(chunk)
        except Exception:
            path.unlink(missing_ok=True)
            raise
    return path, str(metadata.get("title") or identifier), str(metadata.get("creator") or "")
