from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message


async def render_screen(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Reuse a bot-authored message when possible, otherwise send a new one."""
    author = getattr(message, "from_user", None)
    if getattr(message, "text", None) and author and getattr(author, "is_bot", False):
        try:
            await message.edit_text(text, reply_markup=reply_markup)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
        except Exception:
            logging.exception("render_screen edit failed; falling back to answer")
    await message.answer(text, reply_markup=reply_markup)
