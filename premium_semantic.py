from __future__ import annotations

from emoji_text import canonical_emoji

# Words are intentionally Russian-first because this bot's admin UI is Russian.
SEMANTIC_EMOJI = {
    "магазин": ["🛍", "🛒", "🏪", "👕"],
    "каталог": ["🛍", "👕", "📚", "🔎"],
    "корзина": ["🛒", "🧺"],
    "деньги": ["💳", "💰", "💵", "🪙", "💸"],
    "оплата": ["💳", "💰", "🏦", "✅"],
    "доставка": ["🚚", "📦", "📍", "🏠", "✈️"],
    "заказ": ["📦", "🧾", "✅", "🛍"],
    "профиль": ["👤", "👥", "🪪", "⚙️"],
    "настройки": ["⚙️", "🛠", "🎛", "🔧"],
    "удаление": ["🗑", "❌", "➖", "🚫"],
    "удалить": ["🗑", "❌", "➖"],
    "успех": ["✅", "✔️", "🎉", "✨"],
    "ошибка": ["❌", "⚠️", "🚫", "⛔"],
    "предупреждение": ["⚠️", "❗", "🚨"],
    "информация": ["ℹ️", "💬", "📄"],
    "контакт": ["☎️", "📱", "💬", "✉️"],
    "поддержка": ["☎️", "💬", "🛟", "👨‍💻"],
    "отзыв": ["⭐", "💬", "❤️"],
    "размер": ["📏", "👕", "📐"],
    "бонус": ["🎁", "⭐", "🪙", "✨"],
    "поиск": ["🔎", "🔍"],
    "назад": ["⬅️", "↩️"],
    "вперёд": ["➡️", "▶️"],
    "дом": ["🏠", "🏡"],
}

STYLE_PRESETS = {
    "minimal": ("minimal", "outline", "vector", "adaptive", "unigram", "basicinterface"),
    "macos": ("mac", "ios", "apple", "tgmac"),
    "telegram": ("telegram", "tg", "web a", "unigram"),
}


def semantic_fallbacks(query: str) -> list[str]:
    q = (query or "").strip().casefold()
    for key, values in SEMANTIC_EMOJI.items():
        if q == key or key in q:
            return [canonical_emoji(v) for v in values]
    return []


def preset_keywords(name: str) -> tuple[str, ...]:
    return STYLE_PRESETS.get((name or "").casefold(), ())


async def apply_style_preset(name: str) -> dict[str, int]:
    """Apply a coherent Premium-emoji pack preference to existing UI/text fallbacks.

    It never sends Telegram messages. It only updates mappings in the database;
    users see changes naturally on the next normal bot response.
    """
    import db
    from emoji_text import first_emoji

    keywords = preset_keywords(name)
    if not keywords:
        raise ValueError("Неизвестный пресет")
    rows = await db.premium_emoji_all_items()
    preferred = [
        row for row in rows
        if any(k in (str(row["pack_title"] or "") + " " + str(row["pack_set_name"] or "")).casefold() for k in keywords)
    ]
    if not preferred:
        raise ValueError("В библиотеке нет подходящих паков для этого пресета")

    by_fallback: dict[str, object] = {}
    for row in preferred:
        fallback = canonical_emoji(str(row["fallback_text"] or ""))
        if fallback and fallback not in by_fallback:
            by_fallback[fallback] = row

    # Update text rules that have an equivalent icon in the selected visual family.
    text_count = 0
    for rule in await db.premium_emoji_rules():
        fallback = canonical_emoji(str(rule["fallback_text"] or ""))
        item = by_fallback.get(fallback)
        if item:
            await db.upsert_premium_emoji_rule(str(rule["fallback_text"]), str(item["custom_emoji_id"]))
            text_count += 1

    button_map: dict[str, str] = {}
    offset = 0
    while True:
        chunk = await db.ui_buttons('', 250, offset)
        if not chunk:
            break
        for button in chunk:
            emoji = first_emoji(str(button["default_text"] or ""))
            fallback = canonical_emoji(emoji) if emoji else ""
            item = by_fallback.get(fallback)
            if item:
                button_map[str(button["button_key"])] = str(item["custom_emoji_id"])
        offset += len(chunk)
    button_count = await db.apply_ui_button_custom_emoji_map(button_map)
    await db.set_setting("premium_emoji_style_preset", name)
    return {"buttons": int(button_count), "text_rules": int(text_count), "pack_candidates": len(preferred)}
