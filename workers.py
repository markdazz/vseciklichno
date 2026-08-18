from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup

import db
from config import settings
from ui_buttons import InlineKeyboardButton


async def abandoned_cart_worker(bot: Bot) -> None:
    """Optional marketing reminder; respects the customer's broadcast opt-in."""
    while True:
        try:
            threshold=(datetime.now()-timedelta(hours=settings.abandoned_cart_hours)).strftime("%Y-%m-%d %H:%M:%S")
            users=await db.fetchall(
                """SELECT * FROM users WHERE broadcasts_enabled=1 AND is_blocked=0
                   AND cart_updated_at IS NOT NULL AND cart_updated_at<=?
                   AND cart_reminder_sent_at IS NULL LIMIT 200""",
                (threshold,),
            )
            for user in users:
                if not await db.cart(user["user_id"]):
                    continue
                try:
                    await bot.send_message(
                        chat_id=user["user_id"],
                        text="🛒 В вашей корзине остались товары. Если хотите завершить покупку — корзина всё ещё сохранена.",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="🛒 Открыть корзину",callback_data="cart")],
                            [InlineKeyboardButton(text="🔕 Не получать рассылку",callback_data="broadcast:off")],
                        ]),
                    )
                    await db.execute("UPDATE users SET cart_reminder_sent_at=? WHERE user_id=?",(db.NOW(),user["user_id"]))
                except TelegramForbiddenError:
                    await db.mark_blocked(user["user_id"])
                except Exception:
                    logging.exception("abandoned cart reminder")
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("abandoned_cart_worker")
        await asyncio.sleep(600)
