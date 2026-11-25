import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import get_settings
from app.diagnostics import TelegramLogBuffer
from app.handlers import build_router


async def _healthcheck(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await reader.read(4096)
        body = b"ok"
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    log_buffer = TelegramLogBuffer(max_records=200)
    logging.getLogger().addHandler(log_buffer)

    # Keep compatibility with an existing Render Web Service (PORT is injected).
    # Background Workers simply won't set PORT, so no HTTP listener is started.
    health_server = None
    port = os.getenv("PORT")
    if port:
        health_server = await asyncio.start_server(_healthcheck, "0.0.0.0", int(port))
        logging.getLogger(__name__).info("Render health endpoint listening on port %s", port)

    settings.download_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(build_router(settings, semaphore, log_buffer))
    logging.getLogger(__name__).info(
        "Insta Save starting | admins_configured=%s | max_file_size_mb=%s",
        bool(settings.admin_id_set),
        settings.max_file_size_mb,
    )
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if health_server:
            health_server.close()
            await health_server.wait_closed()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
