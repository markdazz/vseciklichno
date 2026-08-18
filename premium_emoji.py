from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import urlparse

from emoji_text import (
    canonical_emoji,
    contains_emoji,
    find_equivalent_emoji,
    replace_equivalent_emoji,
    replace_first_emoji,
)


_RULES: dict[str, str] = {}
_PLACEMENTS: list[dict[str, str | int]] = []
_TG_EMOJI_RE = re.compile(r"<tg-emoji\b[^>]*>.*?</tg-emoji>", re.IGNORECASE | re.DOTALL)
_CODE_BLOCK_RE = re.compile(r"<(code|pre)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_PACK_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _row_value(row: Any, key: str, index: int, default: str = "") -> str:
    try:
        value = row[key]
    except Exception:
        try:
            value = row[index]
        except Exception:
            value = default
    return str(value or "")


def _drop_equivalent_rule(fallback_text: str) -> None:
    target = canonical_emoji((fallback_text or "").strip())
    if not target:
        return
    for current in list(_RULES):
        if canonical_emoji(current) == target:
            _RULES.pop(current, None)


def load_rules(rows: Iterable[Any]) -> None:
    _RULES.clear()
    for row in rows:
        fallback = _row_value(row, "fallback_text", 1).strip()
        emoji_id = _row_value(row, "custom_emoji_id", 2).strip()
        if fallback and emoji_id:
            # Telegram may give the same visible emoji as ⭐ or ⭐️ depending on
            # the client/sticker pack. Keep only one in-memory rule per visible
            # emoji so either Unicode representation works everywhere.
            _drop_equivalent_rule(fallback)
            _RULES[fallback] = emoji_id


def set_rule(fallback_text: str, custom_emoji_id: str) -> None:
    fallback = (fallback_text or "").strip()
    emoji_id = (custom_emoji_id or "").strip()
    if fallback and emoji_id:
        _drop_equivalent_rule(fallback)
        _RULES[fallback] = emoji_id


def remove_rule(fallback_text: str) -> None:
    _drop_equivalent_rule(fallback_text)


def clear_rules() -> None:
    _RULES.clear()


def rules_snapshot() -> dict[str, str]:
    return dict(_RULES)


def global_button_icon_for_text(value: str | None) -> str:
    """Return the global Premium icon configured for a button label.

    Global fallback->custom rules are also applied to buttons. Telegram buttons
    cannot contain <tg-emoji> entities inside their text, so the matching custom
    emoji is sent through icon_custom_emoji_id instead. Explicit per-button icons
    still take priority in ui_buttons.py.
    """
    text = value or ""
    candidates: list[tuple[int, int, str]] = []
    for fallback, emoji_id in _RULES.items():
        if not fallback or not emoji_id or not contains_emoji(fallback):
            continue
        match = find_equivalent_emoji(text, fallback)
        if match is not None:
            candidates.append((match.start(), -len(match.group(0)), emoji_id))
    if not candidates:
        return ""
    candidates.sort()
    return candidates[0][2]


def button_text_without_fallback_for_icon(value: str | None, custom_emoji_id: str | None) -> str:
    """Remove the Unicode fallback that corresponds to a Premium button icon.

    The original label is kept in the aiogram model until request time so the
    session middleware can restore it if Telegram rejects the custom icon.
    """
    text = value or ""
    emoji_id = (custom_emoji_id or "").strip()
    if not text or not emoji_id:
        return text
    matches: list[tuple[int, int, re.Match[str]]] = []
    for fallback, mapped_id in _RULES.items():
        if mapped_id != emoji_id or not fallback or not contains_emoji(fallback):
            continue
        match = find_equivalent_emoji(text, fallback)
        if match is not None:
            matches.append((match.start(), -len(match.group(0)), match))
    if not matches:
        return text
    matches.sort(key=lambda item: (item[0], item[1]))
    match = matches[0][2]
    rendered = text[:match.start()] + text[match.end():]
    # Removing an ordinary fallback from a button may legitimately leave no
    # visible text (for example an icon-only button whose label is just "🗑").
    # Telegram still requires a non-empty ``text`` field, so the caller replaces
    # the empty result with an invisible placeholder for inline buttons.
    rendered = re.sub(r"[ \t]{2,}", " ", rendered).strip()
    return rendered


def load_placements(rows: Iterable[Any]) -> None:
    """Load arbitrary text placement rules from the database.

    A placement can insert a custom emoji before/after a visible text fragment,
    or replace that fragment with a custom emoji. This lets the admin put an
    imported emoji next to essentially any bot-generated text/caption without
    editing Python source code.
    """
    _PLACEMENTS.clear()
    for row in rows:
        try:
            rule_id = int(row["id"])
            custom_emoji_id = str(row["custom_emoji_id"] or "").strip()
            fallback_text = str(row["fallback_text"] or "💎").strip() or "💎"
            match_text = str(row["match_text"] or "")
            position = str(row["position"] or "before").strip().lower()
        except Exception:
            continue
        if custom_emoji_id and match_text and position in {"before", "after", "replace", "replace_emoji"}:
            _PLACEMENTS.append({
                "id": rule_id,
                "custom_emoji_id": custom_emoji_id,
                "fallback_text": fallback_text,
                "match_text": match_text,
                "position": position,
            })
    _PLACEMENTS.sort(key=lambda rule: (-len(str(rule["match_text"])), int(rule["id"])))


def placements_snapshot() -> list[dict[str, str | int]]:
    return [dict(rule) for rule in _PLACEMENTS]


def parse_custom_emoji_pack_name(value: str | None) -> str | None:
    """Extract a custom-emoji sticker-set name from t.me/addemoji links.

    Also accepts the raw short name for convenience. Only the public set name is
    returned; no web scraping is necessary because Bot API getStickerSet accepts
    the set name directly.
    """
    raw = (value or "").strip()
    if not raw:
        return None

    if _PACK_NAME_RE.fullmatch(raw):
        return raw

    candidate = raw
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        parsed = urlparse(candidate)
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0].lower() != "addemoji":
        return None
    name = parts[1]
    return name if _PACK_NAME_RE.fullmatch(name) else None


def _entity_type(entity: Any) -> str:
    value = getattr(entity, "type", "")
    return str(getattr(value, "value", value) or "")


def _utf16_slice(text: str, offset: int, length: int) -> str:
    raw = (text or "").encode("utf-16-le")
    start = max(0, int(offset)) * 2
    end = max(start, int(offset + length)) * 2
    try:
        return raw[start:end].decode("utf-16-le")
    except UnicodeDecodeError:
        return ""


def extract_custom_emoji_pairs(text: str | None, entities: Iterable[Any] | None) -> list[tuple[str, str]]:
    """Return (fallback text, custom_emoji_id) pairs from an incoming Telegram message."""
    if not text or not entities:
        return []
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entity in entities:
        if _entity_type(entity) != "custom_emoji":
            continue
        emoji_id = str(getattr(entity, "custom_emoji_id", "") or "").strip()
        if not emoji_id:
            continue
        fallback = _utf16_slice(text, getattr(entity, "offset", 0), getattr(entity, "length", 0)).strip()
        if not fallback:
            continue
        pair = (fallback, emoji_id)
        if pair not in seen:
            seen.add(pair)
            result.append(pair)
    return result


def apply_to_html(value: str | None) -> str | None:
    """Apply Premium emoji rules to outgoing HTML text/captions.

    Existing Telegram custom-emoji tags, HTML tags and code/pre blocks are
    protected. Imported-pack placement rules support three modes:
      * before  - insert custom emoji before a visible text fragment;
      * after   - insert custom emoji after a visible text fragment;
      * replace       - replace the whole fragment with the custom emoji;
      * replace_emoji - replace only the first ordinary emoji inside the fragment.

    Legacy fallback->custom-emoji replacements are still supported afterwards.
    """
    if not value or (not _RULES and not _PLACEMENTS):
        return value

    protected: list[str] = []

    def _protect_value(original: str) -> str:
        protected.append(original)
        return f"\ue000TGPROTECT{len(protected)-1}\ue001"

    def _protect_match(match: re.Match[str]) -> str:
        return _protect_value(match.group(0))

    def _emoji_placeholder(custom_emoji_id: str, fallback_text: str) -> str:
        fallback = (fallback_text or "💎").strip() or "💎"
        return _protect_value(
            f'<tg-emoji emoji-id="{custom_emoji_id}">{fallback}</tg-emoji>'
        )

    # Telegram formatting entities cannot overlap code/pre entities. Protect
    # those blocks, already-custom emoji, and raw HTML tags so text matching only
    # touches visible content.
    result = _CODE_BLOCK_RE.sub(_protect_match, value)
    result = _TG_EMOJI_RE.sub(_protect_match, result)
    result = _HTML_TAG_RE.sub(_protect_match, result)

    for rule in _PLACEMENTS:
        match_text = str(rule.get("match_text") or "")
        emoji_id = str(rule.get("custom_emoji_id") or "")
        fallback = str(rule.get("fallback_text") or "💎")
        position = str(rule.get("position") or "before")
        if not match_text or not emoji_id or match_text not in result:
            continue
        marker = _emoji_placeholder(emoji_id, fallback)
        if position == "replace":
            result = result.replace(match_text, marker)
        elif position == "replace_emoji":
            replacement = replace_first_emoji(match_text, marker)
            if replacement != match_text:
                result = result.replace(match_text, replacement)
        elif position == "after":
            result = result.replace(match_text, match_text + marker)
        else:
            result = result.replace(match_text, marker + match_text)

    # Keep the old global replacement feature. Inserted pack emojis are already
    # placeholders, so they cannot be nested or replaced a second time.
    for fallback, emoji_id in sorted(_RULES.items(), key=lambda item: len(item[0]), reverse=True):
        if not fallback:
            continue
        marker = _emoji_placeholder(emoji_id, fallback)
        replaced = replace_equivalent_emoji(result, fallback, marker)
        if replaced is not None:
            result = replaced

    for index, original in enumerate(protected):
        result = result.replace(f"\ue000TGPROTECT{index}\ue001", original)
    return result
