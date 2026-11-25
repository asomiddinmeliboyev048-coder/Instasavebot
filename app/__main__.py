import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import get_settings
from app.diagnostics import TelegramLogBuffer
from app.handlers import build_router


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    # Keep recent operational logs in memory for the private /logs command.
    log_buffer = TelegramLogBuffer(max_records=200)
    logging.getLogger().addHandler(log_buffer)

    settings.download_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(build_router(settings, semaphore, log_buffer))
    logging.getLogger(__name__).info(
        "Insta Save starting | admins_configured=%s | max_file_size_mb=%s",
        bool(settings.admin_ids),
        settings.max_file_size_mb,
    )
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
