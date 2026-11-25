import asyncio
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message

from app.config import Settings
from app.services.downloader import classify_url, download_video, extract_url
from app.services.zikra import resolve_zikra_video

log = logging.getLogger(__name__)


def build_router(settings: Settings, semaphore: asyncio.Semaphore) -> Router:
    # Build a fresh router per dispatcher; avoids duplicate-handler registration.
    router = Router()

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
            "Video havolasini yuboring. Qo‘llab-quvvatlanadi: Instagram, YouTube va sozlangan Zikra havolalari."
        )

    @router.message(F.text)
    async def handle_link(message: Message) -> None:
        url = extract_url(message.text or "")
        if not url:
            await message.answer("Iltimos, video havolasini yuboring.")
            return

        status = await message.answer("🔎 Havola tekshirilmoqda…")
        path: Path | None = None
        try:
            source = classify_url(url, settings)
            if source == "zikra":
                url = await resolve_zikra_video(url, settings)

            await status.edit_text("⬇️ Video yuklanmoqda…")
            path = await download_video(url, settings, semaphore)
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
        except asyncio.TimeoutError:
            await status.edit_text("⏱ Yuklash vaqti tugadi. Boshqa havolani yoki keyinroq urinib ko‘ring.")
        except ValueError as exc:
            await status.edit_text(f"⚠️ {exc}")
        except Exception:
            log.exception("Video processing failed")
            await status.edit_text(
                "❌ Videoni olishda xatolik yuz berdi. Havola ochiqligini tekshirib, qayta urinib ko‘ring."
            )
        finally:
            if path:
                path.unlink(missing_ok=True)

    return router
