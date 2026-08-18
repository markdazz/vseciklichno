from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

import db
from ui_buttons import InlineKeyboardButton

ROLE_NAMES = {
    "owner": "Владелец",
    "manager": "Менеджер",
    "content": "Контент-менеджер",
    "warehouse": "Склад / отправка",
}

SECTION_TITLES = {
    "orders": "📦 Заказы",
    "catalog": "👕 Каталог",
    "clients": "👥 Клиенты",
    "marketing": "📣 Маркетинг",
    "settings": "⚙️ Настройки",
}


def allowed_sections(role: str) -> list[str]:
    if role == "owner":
        return ["orders", "catalog", "clients", "marketing", "settings"]
    if role == "manager":
        return ["orders", "catalog", "clients"]
    if role == "content":
        return ["catalog", "marketing"]
    if role == "warehouse":
        return ["orders"]
    return []


async def admin_menu(role: str) -> InlineKeyboardMarkup:
    sections = allowed_sections(role)
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton(text='🔎 Поиск по магазину', callback_data='adm:globalsearch')])
    # Compact two-column dashboard; the fifth owner section occupies its own row.
    for i in range(0, len(sections), 2):
        rows.append([
            InlineKeyboardButton(text=SECTION_TITLES[key], callback_data=f"adm:section:{key}")
            for key in sections[i:i + 2]
        ])
    rows.append([InlineKeyboardButton(text="🏠 В магазин", callback_data="nav:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def admin_dashboard_text(role: str) -> str:
    base = f"⚙️ <b>Админ-панель</b>\nРоль: <b>{ROLE_NAMES.get(role, role)}</b>"
    if role == "content":
        return base + "\n\nВыберите раздел управления."

    shipping = await db.fetchone("SELECT COUNT(*) c FROM orders WHERE status IN ('Подтверждён','Собирается','Собран','Передан в доставку')")
    finish = await db.fetchone("SELECT COUNT(*) c FROM orders WHERE status IN ('Отправлен','Получен')")
    active = await db.fetchone("SELECT COUNT(*) c FROM orders WHERE status IN ('Подтверждён','Собирается','Собран','Передан в доставку','Отправлен','Получен')")
    total = await db.fetchone("SELECT COUNT(*) c FROM orders")
    lines = [base, ""]
    if role in {"owner", "manager"}:
        pending = await db.fetchone("SELECT COUNT(*) c FROM orders WHERE status='На проверке оплаты'")
        lines.append(f"💳 Оплаты: <b>{int(pending['c'] or 0)}</b>")
    lines += [
        f"🚚 К отправке: <b>{int(shipping['c'] or 0)}</b>",
        f"🏁 Завершение: <b>{int(finish['c'] or 0)}</b>",
        f"⚡ Активные: <b>{int(active['c'] or 0)}</b>",
        f"📋 Всего заказов: <b>{int(total['c'] or 0)}</b>",
    ]
    return "\n".join(lines)


def _back() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="⬅️ К разделам", callback_data="adm:home")]


async def admin_section_menu(role: str, section: str) -> tuple[str, InlineKeyboardMarkup] | None:
    if section not in allowed_sections(role):
        return None

    rows: list[list[InlineKeyboardButton]] = []
    if section == "orders":
        pending = await db.fetchone("SELECT COUNT(*) c FROM orders WHERE status='На проверке оплаты'")
        shipping = await db.fetchone("SELECT COUNT(*) c FROM orders WHERE status IN ('Подтверждён','Собирается','Собран','Передан в доставку')")
        finish = await db.fetchone("SELECT COUNT(*) c FROM orders WHERE status IN ('Отправлен','Получен')")
        active = await db.fetchone("SELECT COUNT(*) c FROM orders WHERE status IN ('Подтверждён','Собирается','Собран','Передан в доставку','Отправлен','Получен')")
        total = await db.fetchone("SELECT COUNT(*) c FROM orders")
        if role in {"owner", "manager"}:
            rows.append([InlineKeyboardButton(text=f"💳 Проверить оплаты · {int(pending['c'] or 0)}", callback_data="adm:payments")])
        rows.append([InlineKeyboardButton(text=f"🚚 К отправке · {int(shipping['c'] or 0)}", callback_data="adm:shipping")])
        rows.append([
            InlineKeyboardButton(text=f"🏁 Завершение · {int(finish['c'] or 0)}", callback_data="adm:queue:finish"),
            InlineKeyboardButton(text=f"⚡ Активные · {int(active['c'] or 0)}", callback_data="adm:active"),
        ])
        rows.append([InlineKeyboardButton(text=f"📋 Все заказы · {int(total['c'] or 0)}", callback_data="adm:queue:all")])
        title = (
            "📦 <b>Заказы</b>\n\n"
            "1. Подтвердите оплату.\n"
            "2. Заказ появится в «К отправке».\n"
            "3. После фактической отправки добавьте трек-номер."
        )

    elif section == "catalog":
        rows += [
            [InlineKeyboardButton(text="👕 Товары / остатки", callback_data="adm:products"),
             InlineKeyboardButton(text="🗂 Категории", callback_data="adm:categories")],
            [InlineKeyboardButton(text="➕ Добавить товар", callback_data="adm:add")],
        ]
        if role in {"owner", "manager", "content"}:
            rows.append([InlineKeyboardButton(text="📊 Статистика магазина", callback_data="adm:stats")])
        title = "👕 <b>Каталог</b>\n\nТовары, остатки, категории и контент каталога."

    elif section == "clients":
        rows += [
            [InlineKeyboardButton(text="👥 Клиенты", callback_data="adm:customers"),
             InlineKeyboardButton(text="🎁 Бонусы", callback_data="adm:bonuses")],
            [InlineKeyboardButton(text="⭐ Отзывы", callback_data="adm:reviews"),
             InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
        ]
        title = "👥 <b>Клиенты</b>\n\nПокупатели, бонусы, отзывы и показатели магазина."

    elif section == "marketing":
        rows += [
            [InlineKeyboardButton(text="🎟 Промокоды", callback_data="adm:promos"),
             InlineKeyboardButton(text="🖼 Приветствие", callback_data="adm:welcome")],
            [InlineKeyboardButton(text="📏 Размерная сетка", callback_data="adm:sizechart"),
             InlineKeyboardButton(text="📣 Рассылка", callback_data="adm:broadcast")],
            [InlineKeyboardButton(text="📈 Статистика рассылок", callback_data="adm:broadcast_stats")],
        ]
        title = "📣 <b>Маркетинг</b>\n\nПромокоды, контент и коммуникации с покупателями."

    else:  # settings, owner only
        rows += [
            [InlineKeyboardButton(text="🎛 Настройка кнопок", callback_data="adm:buttons"),
             InlineKeyboardButton(text="💎 Premium emoji", callback_data="adm:premiumemoji")],
            [InlineKeyboardButton(text="📢 Обязательная подписка", callback_data="adm:subscription")],
            [InlineKeyboardButton(text="📤 Excel", callback_data="adm:export"),
             InlineKeyboardButton(text="💾 Резервная копия", callback_data="adm:backup")],
            [InlineKeyboardButton(text="🧹 Очистка", callback_data="adm:cleanup"),
             InlineKeyboardButton(text="🛡 Данные", callback_data="adm:privacy")],
            [InlineKeyboardButton(text="👨‍💼 Администраторы", callback_data="adm:admins"),
             InlineKeyboardButton(text="🧾 Журнал", callback_data="adm:logs")],
            [InlineKeyboardButton(text="🛠 Диагностика", callback_data="adm:diagnostics")],
        ]
        title = "⚙️ <b>Настройки</b>\n\nИнтерфейс, Premium emoji, доступы, резервные копии и служебные инструменты."

    rows.append(_back())
    return title, InlineKeyboardMarkup(inline_keyboard=rows)
