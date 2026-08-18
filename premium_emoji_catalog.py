from __future__ import annotations

from collections import defaultdict

import db
from emoji_text import canonical_emoji, first_emoji
from premium_semantic import semantic_fallbacks


def normalize_search_query(value: str | None) -> tuple[str, bool]:
    raw = (value or "").strip()
    emoji = first_emoji(raw)
    if emoji:
        return canonical_emoji(emoji), True
    return raw.casefold(), False


async def search_items(value: str | None) -> list:
    key, is_emoji = normalize_search_query(value)
    if not key:
        return []
    rows = await db.premium_emoji_all_items()
    if is_emoji:
        return [row for row in rows if canonical_emoji(str(row["fallback_text"] or "")) == key]
    semantic = set(semantic_fallbacks(value or ""))
    if semantic:
        semantic_rows = [
            row for row in rows
            if canonical_emoji(str(row["fallback_text"] or "")) in semantic
        ]
        if semantic_rows:
            return semantic_rows
    return [
        row for row in rows
        if key in str(row["pack_title"] or "").casefold()
        or key in str(row["pack_set_name"] or "").casefold()
        or key in str(row["fallback_text"] or "").casefold()
    ]


async def view_items(view: str, admin_id: int) -> list:
    if view == "favorites":
        return await db.premium_emoji_favorite_items(admin_id)
    if view == "recent":
        return await db.premium_emoji_recent_items(admin_id, 250)
    if view == "used":
        return await db.premium_emoji_used_items()
    return []


async def usage_counts() -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in await db.premium_emoji_rules():
        counts[str(row["custom_emoji_id"])] += 1
    for row in await db.premium_emoji_placements(10000, 0):
        counts[str(row["custom_emoji_id"])] += 1
    for row in await db.ui_button_customizations():
        custom_id = str(row["custom_emoji_id"] or "").strip()
        if custom_id:
            counts[custom_id] += 1
    return dict(counts)
