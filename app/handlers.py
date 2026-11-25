import asyncio
import logging
import time
from html import escape
from pathlib import Path

import yt_dlp
from aiogram import F, Router
from aiogram.enums import ChatAction
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
# Cache the last successfully downloaded media per user/chat for quick follow-up actions.
MEDIA_CACHE: dict[tuple[int, int], tuple[Path, float]] = {}


def _media_keyboard() -> InlineKeyboardMarkup:
    # Keep actions on separate rows so labels stay readable on narrow phones.
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵  Musiqani topish", callback_data="media:music")],
        [InlineKeyboardButton(text="⭕  Yumaloq video qilish", callback_data="media:note")],
    ])


def _cache_media(key: tuple[int, int], path: Path) -> None:
    previous = MEDIA_CACHE.get(key)
    if previous and previous[0] != path:
        previous[0].unlink(missing_ok=True)
    MEDIA_CACHE[key] = (path, time.monotonic() + MEDIA_TTL_SECONDS)

    async def expire() -> None:
        await asyncio.sleep(MEDIA_TTL_SECONDS)
        current = MEDIA_CACHE.get(key)
        if current and current[0] == path and current[1] <= time.monotonic():
            MEDIA_CACHE.pop(key, None)
            path.unlink(missing_ok=True)

    asyncio.create_task(expire())


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
            "Instagram yoki Zikra video havolasini yuboring. "
            "Video tayyor bo‘lgach, musiqasini aniqlash yoki dumaloq video qilish mumkin.\n\n"
            "Faqat foydalanishga haqli bo‘lgan videolaringizni yuboring."
        )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "Instagram yoki Zikra havolasini yuboring. "
            "Video kelgach, 🎵 musiqani aniqlash yoki ⭕ dumaloq video tugmasini bosing."
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
        text = "📋 <b>Oxirgi loglar</b>\n<pre>" + escape("\n".join(entries)) + "</pre>"
        if len(text) > 3900:
            text = "📋 <b>Oxirgi loglar</b>\n<pre>" + escape("\n".join(entries[-12:])) + "</pre>"
        await message.answer(text)

    @router.message(Command("clearlogs"))
    async def clear_logs_command(message: Message) -> None:
        if not is_admin(message):
            await message.answer("⛔ Bu buyruq faqat administrator uchun.")
            return
        if log_buffer:
            log_buffer.clear()
        await message.answer("🧹 Log xotirasi tozalandi.")

    @router.callback_query(F.data.in_({"media:music", "media:note"}))
    async def media_action(callback: CallbackQuery) -> None:
        if not callback.message or not callback.from_user:
            await callback.answer("Xabar topilmadi.", show_alert=True)
            return
        key = (callback.message.chat.id, callback.from_user.id)
        cached = MEDIA_CACHE.get(key)
        if not cached or not cached[0].is_file() or cached[1] <= time.monotonic():
            MEDIA_CACHE.pop(key, None)
            await callback.answer("Video muddati tugagan. Havolani qayta yuboring.", show_alert=True)
            return

        source = cached[0]
        await callback.answer("⏳ Bajarilmoqda…")
        if callback.data == "media:music":
            status = await callback.message.answer("🎵 Videodagi musiqa aniqlanmoqda…")
            try:
                result = await identify_music(source, settings)
                title = escape(str(result.get("title") or "Noma’lum"))
                artist = escape(str(result.get("artist") or "Noma’lum ijrochi"))
                lines = [f"🎵 <b>{title}</b>", f"👤 {artist}"]
                album = result.get("album")
                if album:
                    lines.append(f"💿 {escape(str(album))}")
                track_url = result.get("url")
                if track_url:
                    lines.append(f'🎧 <a href="{escape(str(track_url), quote=True)}">Shazam’da ochish</a>')
                await status.edit_text("\n".join(lines), disable_web_page_preview=True)
                log.info("music_identified user_id=%s title=%s", callback.from_user.id, result.get("title"))
            except LookupError as exc:
                await status.edit_text(f"🔎 {escape(str(exc))}")
            except ValueError as exc:
                await status.edit_text(f"⚠️ {escape(str(exc))}")
            except Exception:
                log.exception("music_identification_failed user_id=%s", callback.from_user.id)
                await status.edit_text("❌ Musiqani aniqlashda xatolik. Keyinroq urinib ko‘ring.")
            return

        status = await callback.message.answer(
            "⭕ <b>Yumaloq video tayyorlanmoqda…</b>\n"
            "🎞 Video 30 soniyagacha qisqartirilib, tezkor formatga o‘tkaziladi."
        )
        note_path: Path | None = None
        started_at = time.monotonic()
        try:
            note_path = await make_video_note(source, settings)
            log.info("video_note_encoded user_id=%s encoding_seconds=%.2f output_bytes=%s",
                     callback.from_user.id, time.monotonic() - started_at, note_path.stat().st_size)
            await status.edit_text("📤 Yumaloq video Telegram'ga yuborilmoqda…")
            await callback.message.bot.send_chat_action(callback.message.chat.id, ChatAction.UPLOAD_VIDEO)
            await asyncio.wait_for(callback.message.answer_video_note(FSInputFile(note_path)), timeout=90)
            await status.delete()
            log.info(
                "video_note_sent user_id=%s processing_seconds=%.2f output_bytes=%s",
                callback.from_user.id,
                time.monotonic() - started_at,
                note_path.stat().st_size,
            )
        except asyncio.TimeoutError:
            log.exception("video_note_timeout user_id=%s", callback.from_user.id)
            await status.edit_text("⏱ Yumaloq video tayyorlash yoki yuborish 90 soniyadan oshdi. Qisqaroq video bilan urinib ko‘ring.")
        except Exception:
            log.exception("video_note_failed user_id=%s", callback.from_user.id)
            await status.edit_text("❌ Dumaloq video tayyorlashda xatolik yuz berdi. Admin /logs orqali sababini ko‘rishi mumkin.")
        finally:
            if note_path:
                note_path.unlink(missing_ok=True)

    @router.message(F.text)
    async def handle_link(message: Message) -> None:
        url = extract_url(message.text or "")
        if not url:
            await message.answer("Iltimos, Instagram yoki Zikra video havolasini yuboring.")
            return

        user_id = message.from_user.id if message.from_user else 0
        host = url.split("/")[2] if "://" in url else "unknown"
        log.info("request_received user_id=%s url_host=%s", user_id, host)
        status = await message.answer("🔎 Havola tekshirilmoqda…")
        path: Path | None = None
        source = "unknown"
        try:
            source = classify_url(url, settings)
            log.info("source_detected source=%s user_id=%s", source, user_id)
            if source == "zikra":
                url = await resolve_zikra_video(url, settings)
                log.info("zikra_url_resolved user_id=%s", user_id)

            await status.edit_text("⬇️ Video yuklanmoqda…")
            path = await download_video(url, settings, semaphore)
            log.info("download_success source=%s user_id=%s size_bytes=%s", source, user_id, path.stat().st_size)
            await status.edit_text("📤 Video Telegram'ga yuborilmoqda…")
            await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_VIDEO)

            markup = _media_keyboard()
            try:
                await message.answer_video(
                    FSInputFile(path),
                    caption="✅ Video tayyor! Quyidagi tugmalardan foydalaning.",
                    supports_streaming=True,
                    reply_markup=markup,
                )
            except Exception:
                log.info("Telegram video upload rejected; retrying as document.", exc_info=True)
                await message.answer_document(
                    FSInputFile(path),
                    caption="✅ Video tayyor! Quyidagi tugmalardan foydalaning.",
                    reply_markup=markup,
                )

            _cache_media((message.chat.id, user_id), path)
            path = None  # The cache owns this file until its TTL expires.
            await status.delete()
            log.info("delivery_success source=%s user_id=%s", source, user_id)
        except asyncio.TimeoutError:
            log.warning("download_timeout source=%s user_id=%s", source, user_id)
            await status.edit_text("⏱ Yuklash vaqti tugadi. Boshqa havola yoki keyinroq urinib ko‘ring.")
        except ValueError as exc:
            log.warning("request_rejected source=%s user_id=%s reason=%s", source, user_id, exc)
            await status.edit_text(f"⚠️ {escape(str(exc))}")
        except yt_dlp.utils.DownloadError:
            log.exception("instagram_download_error source=%s user_id=%s", source, user_id)
            await status.edit_text("❌ Instagram videoni bermadi yoki havola yopiq. Boshqa havolani sinab ko‘ring.")
        except Exception:
            log.exception("video_processing_failed source=%s user_id=%s", source, user_id)
            await status.edit_text("❌ Videoni olishda xatolik. Admin /logs orqali sababini tekshirishi mumkin.")
        finally:
            if path:
                path.unlink(missing_ok=True)

    return router
