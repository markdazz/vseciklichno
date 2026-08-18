from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter

import db
from premium_emoji import parse_custom_emoji_pack_name

premium_pack_import_lock = asyncio.Lock()

_PACK_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/addemoji/[A-Za-z0-9_]+",
    re.IGNORECASE,
)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


async def fetch_custom_emoji_pack(bot: Bot, set_name: str):
    sticker_set = await bot.get_sticker_set(set_name)
    if _enum_value(getattr(sticker_set, "sticker_type", "")) != "custom_emoji":
        raise ValueError("По этой ссылке найден не набор Premium/custom emoji.")
    items: list[dict[str, str]] = []
    for sticker in getattr(sticker_set, "stickers", []) or []:
        custom_id = str(getattr(sticker, "custom_emoji_id", "") or "").strip()
        if not custom_id:
            continue
        items.append({
            "custom_emoji_id": custom_id,
            "fallback_text": str(getattr(sticker, "emoji", "") or "💎").strip() or "💎",
            "file_id": str(getattr(sticker, "file_id", "") or "").strip(),
        })
    if not items:
        raise ValueError("В наборе не найдено custom emoji.")
    return sticker_set, items


async def save_custom_emoji_pack(bot: Bot, raw_link_or_name: str):
    set_name = parse_custom_emoji_pack_name(raw_link_or_name)
    if not set_name:
        raise ValueError("Не удалось распознать ссылку. Нужен формат https://t.me/addemoji/NAME")
    sticker_set, items = await fetch_custom_emoji_pack(bot, set_name)
    return await db.upsert_premium_emoji_pack(
        set_name,
        str(getattr(sticker_set, "title", "") or set_name),
        f"https://t.me/addemoji/{set_name}",
        items,
    )


def premium_pack_names_from_text(value: str | None) -> list[str]:
    raw = (value or "").strip()
    if not raw:
        return []
    tokens = _PACK_LINK_RE.findall(raw)
    if not tokens:
        tokens = [
            token.strip(" ,;|\t\r\n")
            for token in re.split(r"[\s,;|]+", raw)
            if token.strip(" ,;|\t\r\n")
        ]
    result: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        name = parse_custom_emoji_pack_name(token)
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(name)
    return result


async def import_premium_pack_names(
    bot: Bot,
    names: list[str],
    *,
    skip_existing: bool = False,
    progress_cb=None,
) -> dict[str, Any]:
    imported = refreshed = skipped = emoji_count = 0
    errors: list[tuple[str, str]] = []
    total = len(names)
    for index, set_name in enumerate(names, start=1):
        existing = await db.premium_emoji_pack_by_name(set_name)
        if existing and skip_existing:
            skipped += 1
            if progress_cb:
                await progress_cb(index, total, imported, refreshed, skipped, len(errors), set_name)
            continue
        pack = None
        for attempt in range(3):
            try:
                pack = await save_custom_emoji_pack(bot, set_name)
                break
            except TelegramRetryAfter as exc:
                await asyncio.sleep(max(1.0, float(getattr(exc, "retry_after", 1))) + 0.25)
            except (TelegramBadRequest, ValueError) as exc:
                errors.append((set_name, str(exc)))
                break
            except TelegramNetworkError as exc:
                if attempt >= 2:
                    errors.append((set_name, str(exc)))
                else:
                    await asyncio.sleep(1.0 + attempt)
            except Exception as exc:
                logging.exception("bulk premium emoji pack import: %s", set_name)
                errors.append((set_name, str(exc)))
                break
        if pack is not None:
            if existing:
                refreshed += 1
            else:
                imported += 1
            emoji_count += int(pack["sticker_count"] or 0)
        if progress_cb:
            await progress_cb(index, total, imported, refreshed, skipped, len(errors), set_name)
        await asyncio.sleep(0.05)
    return {
        "total": total,
        "imported": imported,
        "refreshed": refreshed,
        "skipped": skipped,
        "emoji_count": emoji_count,
        "errors": errors,
    }
