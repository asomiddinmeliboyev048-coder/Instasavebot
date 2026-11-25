import asyncio
import logging
from html import escape
from pathlib import Path

import yt_dlp
from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message

from app.config import Settings
from app.diagnostics import TelegramLogBuffer
from app.services.downloader import classify_url, download_video, extract_url
from app.services.zikra import resolve_zikra_video

log = logging.getLogger(__name__)


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
            "Menga Instagram, YouTube yoki Zikra video havolasini yuboring. "
            "Men videoni Telegram orqali qaytaraman.\n\n"
            "Faqat o‘zingiz foydalanishga haqli bo‘lgan videolarni yuboring."
        )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "Video havolasini yuboring. Qo‘llab-quvvatlanadi: Instagram, YouTube va Zikra Reels."
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
        if not settings.admin_id_set:
            await message.answer("⚙️ Render Environment'da ADMIN_IDS sozlanmagan.")
            return
        await message.answer(
            "🛠 <b>Insta Save boshqaruv paneli</b>\n\n"
            f"👤 Admin ID: <code>{message.from_user.id}</code>\n"
            f"📦 Maksimal fayl: <b>{settings.max_file_size_mb} MB</b>\n"
            f"⚙️ Parallel yuklash: <b>{settings.max_concurrent_downloads}</b>\n"
            f"🍪 YouTube cookies: <b>{'Sozlangan' if settings.ytdlp_cookies_file else 'Sozlanmagan'}</b>\n"
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
            f"🧩 Zikra jadvali: <code>{settings.zikra_video_table}</code>\n"
            f"🆔 ID ustuni: <code>{settings.zikra_video_id_column}</code>\n"
            f"🎞 Video ustuni: <code>{settings.zikra_video_path_column}</code>\n"
            f"🍪 YouTube cookies: {'✅' if settings.ytdlp_cookies_file and Path(settings.ytdlp_cookies_file).is_file() else '❌'}\n"
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
        # Telegram message length limit; send newest entries in compact chunks.
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

    @router.message(F.text)
    async def handle_link(message: Message) -> None:
        url = extract_url(message.text or "")
        if not url:
            await message.answer("Iltimos, video havolasini yuboring.")
            return

        user_id = message.from_user.id if message.from_user else 0
        log.info("request_received user_id=%s url_host=%s", user_id, url.split("/")[2] if "://" in url else "unknown")
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

            try:
                await message.answer_video(
                    FSInputFile(path),
                    caption="✅ Video tayyor!",
                    supports_streaming=True,
                )
            except Exception:
                log.info("Telegram video upload rejected; retrying as document.", exc_info=True)
                await message.answer_document(FSInputFile(path), caption="✅ Video tayyor!")

            await status.delete()
            log.info("delivery_success source=%s user_id=%s", source, user_id)
        except asyncio.TimeoutError:
            log.warning("download_timeout source=%s user_id=%s", source, user_id)
            await status.edit_text("⏱ Yuklash vaqti tugadi. Boshqa havolani yoki keyinroq urinib ko‘ring.")
        except ValueError as exc:
            log.warning("request_rejected source=%s user_id=%s reason=%s", source, user_id, exc)
            await status.edit_text(f"⚠️ {exc}")
        except yt_dlp.utils.DownloadError as exc:
            error_text = str(exc).lower()
            log.exception("ytdlp_download_error source=%s user_id=%s", source, user_id)
            if "sign in to confirm" in error_text or "not a bot" in error_text:
                await status.edit_text(
                    "⚠️ YouTube server so‘rovini vaqtincha chekladi. "
                    "Admin /logs buyrug‘i orqali batafsil sababni ko‘rishi mumkin."
                )
            else:
                await status.edit_text(
                    "❌ Video manbasi yuklashga ruxsat bermadi yoki formatni topib bo‘lmadi."
                )
        except Exception:
            log.exception("video_processing_failed source=%s user_id=%s", source, user_id)
            await status.edit_text(
                "❌ Videoni olishda xatolik yuz berdi. Admin /logs orqali sababini tekshirishi mumkin."
            )
        finally:
            if path:
                path.unlink(missing_ok=True)

    return router
