import asyncio
import logging
import time
from html import escape
from pathlib import Path

import yt_dlp
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import Settings
from app.diagnostics import TelegramLogBuffer
from app.services.downloader import classify_url, download_video, extract_url
from app.services.media_tools import identify_music, make_video_note
from app.services.zikra import resolve_zikra_video

log = logging.getLogger(__name__)
MEDIA_TTL_SECONDS = 20 * 60
# Source URLs are cached first; the actual media is downloaded only after a button is pressed.
MEDIA_CACHE: dict[tuple[int, int], dict] = {}
MEDIA_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


def _source_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Videoni yuklab olish", callback_data="media:video")],
        [InlineKeyboardButton(text="🎵 Musiqani aniqlash", callback_data="media:music")],
        [InlineKeyboardButton(text="⭕ Yumaloq video", callback_data="media:note")],
    ])


def _archive_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📥 {item['title'][:35]}",
            callback_data=f"archive:{item['identifier']}",
        )]
        for item in items[:5]
    ])


def _cache_source(key: tuple[int, int], url: str, source: str) -> None:
    previous = MEDIA_CACHE.get(key)
    if previous and previous.get("path"):
        previous["path"].unlink(missing_ok=True)
    MEDIA_CACHE[key] = {
        "url": url,
        "source": source,
        "path": None,
        "expires": time.monotonic() + MEDIA_TTL_SECONDS,
    }

    async def expire() -> None:
        await asyncio.sleep(MEDIA_TTL_SECONDS)
        current = MEDIA_CACHE.get(key)
        if current and current["expires"] <= time.monotonic():
            MEDIA_CACHE.pop(key, None)
            path = current.get("path")
            if path:
                path.unlink(missing_ok=True)

    asyncio.create_task(expire())


async def _get_cached_media(
    key: tuple[int, int], settings: Settings, semaphore: asyncio.Semaphore
) -> Path:
    item = MEDIA_CACHE.get(key)
    if not item or item["expires"] <= time.monotonic():
        MEDIA_CACHE.pop(key, None)
        raise LookupError("Havola muddati tugagan. Uni qayta yuboring.")
    path = item.get("path")
    if path and path.is_file():
        return path

    lock = MEDIA_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        item = MEDIA_CACHE.get(key)
        if not item or item["expires"] <= time.monotonic():
            raise LookupError("Havola muddati tugagan. Uni qayta yuboring.")
        path = item.get("path")
        if path and path.is_file():
            return path
        path = await download_video(item["url"], settings, semaphore)
        item["path"] = path
        return path


def build_router(
    settings: Settings,
    semaphore: asyncio.Semaphore,
    log_buffer: TelegramLogBuffer | None = None,
) -> Router:
    router = Router()

    def is_admin(message: Message) -> bool:
        return bool(
            message.chat.type == "private"
            and message.from_user
            and message.from_user.id in settings.admin_id_set
        )

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(
            "Salom! 👋\n\n"
            "Instagram yoki Zikra video havolasini yuboring. Video avtomatik yuklanmaydi — "
            "kerakli tugmani tanlang: video, musiqa yoki yumaloq video.\n\n"
            "Musiqa nomi va ijrochisini yozib ham qidirishingiz mumkin."
        )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "Instagram yoki Zikra havolasini yuboring va kerakli amalni tanlang. "
            "Musiqa nomini yozib qidirsangiz ham bo‘ladi."
        )

    @router.message(Command("myid"))
    async def my_id_command(message: Message) -> None:
        if message.from_user:
            await message.answer(
                f"🆔 Telegram ID: <code>{message.from_user.id}</code>\n"
                "Admin panelini yoqish uchun Render Environment'da "
                "ADMIN_IDS qiymatiga shu raqamni kiriting."
            )

    @router.message(Command("admin"))
    async def admin_command(message: Message) -> None:
        if not is_admin(message):
            await message.answer("⛔ Bu buyruq faqat administrator uchun.")
            return
        await message.answer(
            "🛠 <b>Insta Save boshqaruv paneli</b>\n\n"
            f"👤 Admin ID: <code>{message.from_user.id}</code>\n"
            f"📦 Maksimal fayl: <b>{settings.max_file_size_mb} MB</b>\n"
            f"⚙️ Parallel yuklash: <b>{settings.max_concurrent_downloads}</b>\n"
            "🎵 Musiqa aniqlash: <b>Shazam (API tokensiz)</b>\n"
            f"🗄 Supabase: <b>{'Ulangan' if settings.supabase_url and settings.supabase_service_role_key else 'Sozlanmagan'}</b>\n\n"
            "Buyruqlar: /status, /logs, /logs 50, /clearlogs"
        )

    @router.message(Command("status"))
    async def status_command(message: Message) -> None:
        if not is_admin(message):
            await message.answer("⛔ Bu buyruq faqat administrator uchun.")
            return
        me = await message.bot.get_me()
        await message.answer(
            "🟢 <b>Bot holati</b>\n"
            f"🤖 @{me.username}\n"
            "📥 Manbalar: Instagram, Zikra\n"
            f"🧩 Zikra jadvali: <code>{escape(settings.zikra_video_table)}</code>\n"
            f"🆔 ID ustuni: <code>{escape(settings.zikra_video_id_column)}</code>\n"
            f"🎞 Video ustuni: <code>{escape(settings.zikra_video_path_column)}</code>\n"
            "🎵 Musiqa: Shazam (API tokensiz)\n"
            f"🔐 Adminlar: {len(settings.admin_id_set)}"
        )

    @router.message(Command("logs"))
    async def logs_command(message: Message) -> None:
        if not is_admin(message):
            await message.answer("⛔ Bu buyruq faqat administrator uchun.")
            return
        if log_buffer is None:
            await message.answer("Log xotirasi ulanmagan.")
            return
        parts = (message.text or "").split(maxsplit=1)
        try:
            count = int(parts[1]) if len(parts) > 1 else 20
        except ValueError:
            count = 20
        entries = log_buffer.tail(count)
        if not entries:
            await message.answer("Hozircha log yozuvlari yo‘q.")
            return
        content = "📋 <b>Oxirgi loglar</b>\n<pre>" + escape("\n".join(entries)) + "</pre>"
        if len(content) > 3900:
            content = "📋 <b>Oxirgi loglar</b>\n<pre>" + escape("\n".join(entries[-12:])) + "</pre>"
        await message.answer(content)

    @router.message(Command("clearlogs"))
    async def clear_logs_command(message: Message) -> None:
        if not is_admin(message):
            await message.answer("⛔ Bu buyruq faqat administrator uchun.")
            return
        if log_buffer:
            log_buffer.clear()
        await message.answer("🧹 Log xotirasi tozalandi.")

    @router.callback_query(F.data.startswith("archive:"))
    async def archive_download(callback: CallbackQuery) -> None:
        if not callback.message or not callback.from_user:
            await callback.answer("Xabar topilmadi.", show_alert=True)
            return
        identifier = (callback.data or "").split(":", 1)[1]
        if not identifier or len(identifier) > 120 or not identifier.replace("-", "").replace("_", "").isalnum():
            await callback.answer("Noto‘g‘ri audio identifikatori.", show_alert=True)
            return
        await callback.answer("Yuklanmoqda…")
        status = await callback.message.answer("📥 Audio tayyorlanmoqda…")
        try:
            from app.services.internet_archive import download_archive_audio
            path, title, artist = await download_archive_audio(identifier, settings)
            try:
                await callback.message.answer_audio(
                    FSInputFile(path), title=title[:64] if title else "Audio",
                    performer=artist[:64] if artist else None,
                    caption="📚 Manba: Internet Archive",
                )
            finally:
                path.unlink(missing_ok=True)
            await status.delete()
        except LookupError as exc:
            await status.edit_text(f"🔎 {escape(str(exc))}")
        except Exception:
            log.exception("archive_audio_download_failed user_id=%s identifier=%s", callback.from_user.id, identifier)
            await status.edit_text("❌ Audio yuklab bo‘lmadi. Boshqa natijani tanlang.")

    @router.callback_query(F.data.in_({"media:video", "media:music", "media:note"}))
    async def media_action(callback: CallbackQuery) -> None:
        if not callback.message or not callback.from_user:
            await callback.answer("Xabar topilmadi.", show_alert=True)
            return
        key = (callback.message.chat.id, callback.from_user.id)
        action = (callback.data or "").split(":", 1)[1]
        await callback.answer("⏳ Tayyorlanmoqda…")
        status = await callback.message.answer("📥 Kerakli media tayyorlanmoqda…")
        source: Path | None = None
        output: Path | None = None
        started = time.monotonic()
        try:
            source = await _get_cached_media(key, settings, semaphore)
            if action == "video":
                await callback.message.answer_video(
                    FSInputFile(source),
                    caption="✅ Video tayyor!",
                    supports_streaming=True,
                )
                await status.delete()
                log.info("video_sent user_id=%s seconds=%.2f", callback.from_user.id, time.monotonic() - started)
                return

            if action == "music":
                await status.edit_text("🎵 Videodagi musiqa aniqlanmoqda…")
                result = await identify_music(source, settings)
                title = escape(str(result.get("title") or "Noma’lum"))
                artist = escape(str(result.get("artist") or "Noma’lum ijrochi"))
                lines = [f"🎵 <b>{title}</b>", f"👤 {artist}"]
                if result.get("url"):
                    lines.append(f'🎧 <a href="{escape(str(result["url"]), quote=True)}">Shazam’da ochish</a>')
                from app.services.internet_archive import search_archive_audio
                archive_results = await search_archive_audio(result.get("title") or "", result.get("artist") or "")
                if archive_results:
                    lines.extend(["", "📚 <b>Ochiq litsenziyali audio nusxalar:</b>"])
                    await status.edit_text("\n".join(lines), disable_web_page_preview=True)
                    await callback.message.answer(
                        "Yuklab olish uchun audio nusxasini tanlang:",
                        reply_markup=_archive_keyboard(archive_results),
                    )
                else:
                    lines.extend(["", "📭 Internet Archive'da mos ochiq litsenziyali audio topilmadi."])
                    await status.edit_text("\n".join(lines), disable_web_page_preview=True)
                log.info("music_identified user_id=%s title=%s seconds=%.2f",
                         callback.from_user.id, result.get("title"), time.monotonic() - started)
                return

            await status.edit_text("⭕ Yumaloq video tayyorlanmoqda…")
            output = await make_video_note(source, settings)
            await callback.message.answer_video_note(FSInputFile(output))
            await status.delete()
            log.info("video_note_sent user_id=%s seconds=%.2f bytes=%s",
                     callback.from_user.id, time.monotonic() - started, output.stat().st_size)
        except LookupError as exc:
            await status.edit_text(f"🔎 {escape(str(exc))}")
        except asyncio.TimeoutError:
            log.exception("media_action_timeout user_id=%s action=%s", callback.from_user.id, action)
            await status.edit_text("⏱ Jarayon vaqti tugadi. Qisqaroq video bilan urinib ko‘ring.")
        except ValueError as exc:
            await status.edit_text(f"⚠️ {escape(str(exc))}")
        except yt_dlp.utils.DownloadError:
            log.exception("instagram_download_error user_id=%s", callback.from_user.id)
            await status.edit_text("❌ Instagram videoni bermadi yoki havola yopiq.")
        except Exception:
            log.exception("media_action_failed user_id=%s action=%s", callback.from_user.id, action)
            await status.edit_text("❌ Amalni bajarishda xatolik. Admin /logs orqali sababini ko‘rishi mumkin.")
        finally:
            if output:
                output.unlink(missing_ok=True)

    @router.message(F.text)
    async def handle_text(message: Message) -> None:
        text = (message.text or "").strip()
        url = extract_url(text)
        if url:
            user_id = message.from_user.id if message.from_user else 0
            key = (message.chat.id, user_id)
            status = await message.answer("🔎 Havola tekshirilmoqda…")
            try:
                source = classify_url(url, settings)
                if source == "zikra":
                    url = await resolve_zikra_video(url, settings)
                _cache_source(key, url, source)
                await status.edit_text(
                    "✅ Havola tayyor! Endi faqat kerakli amalni tanlang:",
                    reply_markup=_source_keyboard(),
                )
                log.info("link_ready source=%s user_id=%s", source, user_id)
            except ValueError as exc:
                await status.edit_text(f"⚠️ {escape(str(exc))}")
            except Exception:
                log.exception("link_validation_failed user_id=%s", user_id)
                await status.edit_text("❌ Havolani tekshirishda xatolik yuz berdi.")
            return

        # Plain text is treated as a music title/artist search.
        query = text
        if len(query) < 2:
            await message.answer("Instagram/Zikra havolasini yoki musiqa nomi va ijrochisini yuboring.")
            return
        status = await message.answer(f"🔎 “{escape(query[:100])}” bo‘yicha musiqa qidirilmoqda…")
        try:
            from app.services.internet_archive import search_archive_audio
            results = await search_archive_audio(query, "")
            if not results:
                await status.edit_text(
                    "📭 Bu nom bo‘yicha ochiq litsenziyali audio topilmadi. "
                    "Musiqa nomi bilan birga ijrochi nomini ham yozib ko‘ring."
                )
                return
            await status.edit_text(
                f"🎵 “{escape(query[:100])}” bo‘yicha topilgan ochiq litsenziyali audiolar:",
                reply_markup=_archive_keyboard(results),
            )
        except Exception:
            log.exception("music_search_failed query=%s", query[:80])
            await status.edit_text("❌ Musiqa qidirishda xatolik. Keyinroq urinib ko‘ring.")

    return router
