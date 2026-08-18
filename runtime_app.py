from __future__ import annotations

import asyncio
import logging
import uuid

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand, ErrorEvent

import db
from config import settings
from production_runtime import (
    automatic_backup_worker,
    pre_migration_backup,
    reservation_cleanup_worker,
    telegram_retry,
    wait_for_telegram,
)
from sqlite_fsm import SQLiteStorage
from startup_tasks import apply_auto_emoji_mapping_once, apply_auto_text_emoji_mapping_once, sync_ui_button_registry
from ui_buttons import load_custom_labels as ui_load_custom_labels
from premium_emoji import load_rules as premium_load_rules, load_placements as premium_load_placements

log = logging.getLogger(__name__)


async def _report_error(event: ErrorEvent) -> bool:
    error_id = uuid.uuid4().hex[:8].upper()
    exc = event.exception
    log.error("Unhandled bot error id=%s: %s", error_id, exc, exc_info=(type(exc), exc, exc.__traceback__))
    update = getattr(event, "update", None)
    message = getattr(update, "message", None) if update else None
    callback = getattr(update, "callback_query", None) if update else None
    try:
        if callback:
            await callback.answer(f"Ошибка {error_id}. Попробуйте ещё раз.", show_alert=True)
        elif message:
            await message.answer(f"⚠️ Произошла ошибка <code>{error_id}</code>. Попробуйте действие ещё раз.")
    except Exception:
        log.debug("Could not notify user about error %s", error_id, exc_info=True)
    return True


async def run_bot(
    *,
    router,
    legal_gate,
    premium_request_middleware,
    abandoned_cart_worker,
    build: str,
) -> None:
    if not settings.bot_token:
        raise RuntimeError("Не найден BOT_TOKEN в переменных окружения/.env")

    # Protect the current user database before any automatic schema migration.
    await pre_migration_backup()
    await db.init_db()
    await sync_ui_button_registry()
    await apply_auto_emoji_mapping_once()
    await apply_auto_text_emoji_mapping_once()
    await db.clear_all_ui_button_custom_styles()
    ui_load_custom_labels(await db.ui_button_customizations())
    premium_load_rules(await db.premium_emoji_rules())
    premium_load_placements(await db.premium_emoji_placements())

    session = AiohttpSession(timeout=float(settings.telegram_request_timeout))
    bot = Bot(
        settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    bot.session.middleware(premium_request_middleware)

    storage = SQLiteStorage(settings.db_path)
    dp = Dispatcher(storage=storage, events_isolation=SimpleEventIsolation())
    router.message.middleware(legal_gate)
    router.callback_query.middleware(legal_gate)
    dp.include_router(router)
    dp.errors.register(_report_error)

    log.info("Starting bot build %s", build)
    # Unlike the old startup path, a temporary Windows/network timeout does not
    # terminate the process. The bot waits until Telegram is reachable again.
    await wait_for_telegram(bot)

    commands = [
        BotCommand(command="start", description="Открыть главное меню"),
        BotCommand(command="catalog", description="Выбрать товар в каталоге"),
        BotCommand(command="cart", description="Корзина и оформление заказа"),
        BotCommand(command="orders", description="Мои заказы и их статус"),
        BotCommand(command="support", description="Помощь с заказом"),
    ]
    await telegram_retry(lambda: bot.set_my_commands(commands), name="setMyCommands")
    await telegram_retry(lambda: bot.delete_webhook(drop_pending_updates=False), name="deleteWebhook", forever=True)

    workers = [
        asyncio.create_task(abandoned_cart_worker(bot), name="abandoned-cart"),
        asyncio.create_task(automatic_backup_worker(), name="automatic-backup"),
        asyncio.create_task(reservation_cleanup_worker(), name="reservation-cleanup"),
    ]
    try:
        # aiogram itself retries normal polling network errors. Wrapping the whole
        # polling session adds recovery for failures that escape startup/polling.
        while True:
            try:
                await dp.start_polling(
                    bot,
                    close_bot_session=False,
                    tasks_concurrency_limit=max(10, settings.polling_concurrency_limit),
                )
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Polling stopped unexpectedly; restarting in 5 seconds: %s", exc)
                await asyncio.sleep(5)
                await wait_for_telegram(bot)
    finally:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await storage.close()
        await bot.session.close()
