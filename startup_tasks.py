from __future__ import annotations

import logging
from pathlib import Path

import db
from auto_emoji_mapping import AUTO_EMOJI_ASSIGNMENTS, AUTO_EMOJI_MAPPING_VERSION
from auto_text_emoji_mapping import AUTO_TEXT_EMOJI_RULES, AUTO_TEXT_EMOJI_MAPPING_VERSION
from emoji_text import canonical_emoji
from ui_buttons import (
    KeyboardButton,
    discover_static_buttons as ui_discover_static_buttons,
    snapshot_definitions as ui_snapshot_definitions,
)


async def apply_auto_emoji_mapping_once() -> None:
    if await db.get_setting(AUTO_EMOJI_MAPPING_VERSION, "") == "1":
        return
    await db.apply_ui_button_custom_emoji_map(AUTO_EMOJI_ASSIGNMENTS)
    await db.set_setting(AUTO_EMOJI_MAPPING_VERSION, "1")
    logging.info("Applied automatic Premium emoji mapping to %s button keys", len(AUTO_EMOJI_ASSIGNMENTS))


async def apply_auto_text_emoji_mapping_once() -> None:
    if await db.get_setting(AUTO_TEXT_EMOJI_MAPPING_VERSION, "") == "1":
        return
    existing_rows = await db.premium_emoji_rules()
    existing_visible = {
        canonical_emoji(str(row["fallback_text"] or "").strip())
        for row in existing_rows
        if str(row["fallback_text"] or "").strip()
    }
    applied = skipped_manual = 0
    for fallback, custom_emoji_id in AUTO_TEXT_EMOJI_RULES.items():
        visible = canonical_emoji(fallback)
        if not visible:
            continue
        if visible in existing_visible:
            skipped_manual += 1
            continue
        await db.upsert_premium_emoji_rule(fallback, custom_emoji_id)
        existing_visible.add(visible)
        applied += 1
    await db.set_setting(AUTO_TEXT_EMOJI_MAPPING_VERSION, "1")
    logging.info(
        "Applied automatic Premium emoji mapping to %s text emoji; preserved %s existing admin rules",
        applied,
        skipped_manual,
    )


async def sync_ui_button_registry() -> None:
    # Dynamic reply-button labels that static AST discovery cannot see.
    for label in (
        "✅ Готово",
        "✅ Медиа этого цвета загружены",
        "➡️ Без медиа этого цвета",
        "✅ Фото этого цвета загружены",
        "➡️ Без фото этого цвета",
        "➡️ Нет корпуса",
        "➡️ Нет квартиры",
        "➡️ Без комментария",
    ):
        KeyboardButton(text=label)

    base_dir = Path(__file__).resolve().parent
    discovered = []
    for source in (base_dir / "bot.py", base_dir / "customer_ui.py", base_dir / "admin_sections.py"):
        if source.exists():
            discovered.extend(ui_discover_static_buttons(source))
    merged = {d.button_key: d for d in discovered}
    for definition in ui_snapshot_definitions():
        merged[definition.button_key] = definition
    await db.sync_ui_button_definitions(list(merged.values()))
