from __future__ import annotations

from aiogram.types import ReplyKeyboardMarkup
from ui_buttons import KeyboardButton

# Kept exactly as in FIX17: UX cleanup intentionally does not change the
# customer's main menu or remove the "Мои данные" action.
MAIN_MENU_BUTTON_TEXTS = {
    "🛍 Каталог", "🛒 Корзина", "📦 Мои заказы", "👤 Мой профиль",
    "⭐ Отзывы", "📏 Размеры", "🎁 Пригласить", "☎️ Поддержка",
    "🗑 Мои данные", "⚙️ Админ-панель",
}


def main_menu(admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🛍 Каталог"), KeyboardButton(text="🛒 Корзина")],
        [KeyboardButton(text="📦 Мои заказы"), KeyboardButton(text="👤 Мой профиль")],
        [KeyboardButton(text="⭐ Отзывы"), KeyboardButton(text="📏 Размеры")],
        [KeyboardButton(text="🎁 Пригласить"), KeyboardButton(text="☎️ Поддержка")],
        [KeyboardButton(text="🗑 Мои данные")],
    ]
    if admin:
        rows.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)
