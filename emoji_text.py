from __future__ import annotations

import re
import unicodedata

# Broad Telegram/UI emoji matcher. It intentionally includes arrows, symbols,
# dingbats, flags, skin-tone modifiers, VS-16 and ZWJ sequences commonly used
# in button labels and shop messages.
_EMOJI_BASE = (
    r"[\U0001F000-\U0001FAFF"
    r"\u2190-\u21FF"
    r"\u2300-\u23FF"
    r"\u2600-\u27BF"
    r"\u2B00-\u2BFF"
    r"\u3030\u303D\u3297\u3299"
    r"\u00A9\u00AE\u2122\u2139]"
)
_EMOJI_MODIFIERS = r"[\uFE0E\uFE0F\U0001F3FB-\U0001F3FF]*"
_EMOJI_CLUSTER_PATTERN = (
    rf"(?:[#*0-9]\uFE0F?\u20E3|"
    rf"{_EMOJI_BASE}{_EMOJI_MODIFIERS}(?:\u200D{_EMOJI_BASE}{_EMOJI_MODIFIERS})*)"
)
_EMOJI_CLUSTER_RE = re.compile(_EMOJI_CLUSTER_PATTERN)
_LEADING_EMOJI_RE = re.compile(rf"^\s*({_EMOJI_CLUSTER_PATTERN})(?:\s+|$)")




def canonical_emoji(value: str | None) -> str:
    """Normalize an emoji cluster for matching.

    Telegram and clients may represent the same visible emoji with or without
    variation selectors (for example ``⭐`` vs ``⭐️``).  Global replacement
    rules must treat those forms as the same emoji.
    """
    text = unicodedata.normalize("NFC", value or "")
    return text.replace("\ufe0e", "").replace("\ufe0f", "")


def find_equivalent_emoji(value: str | None, target: str | None):
    """Return the first regex match equivalent to *target*, ignoring VS15/VS16."""
    if not value or not target:
        return None
    target_key = canonical_emoji(target)
    if not target_key:
        return None
    for match in _EMOJI_CLUSTER_RE.finditer(value):
        if canonical_emoji(match.group(0)) == target_key:
            return match
    return None


def replace_equivalent_emoji(value: str | None, target: str | None, replacement: str) -> str | None:
    """Replace every visually equivalent emoji cluster in *value*.

    This intentionally ignores only Unicode variation selectors, so a rule
    created from ``⭐️`` also matches source text written as ``⭐`` and vice versa.
    """
    if value is None or not target:
        return value
    target_key = canonical_emoji(target)
    if not target_key:
        return value

    parts: list[str] = []
    last = 0
    changed = False
    for match in _EMOJI_CLUSTER_RE.finditer(value):
        if canonical_emoji(match.group(0)) != target_key:
            continue
        parts.append(value[last:match.start()])
        parts.append(replacement)
        last = match.end()
        changed = True
    if not changed:
        return value
    parts.append(value[last:])
    return "".join(parts)


def remove_equivalent_emoji(value: str | None, target: str | None) -> str | None:
    return replace_equivalent_emoji(value, target, "")

def contains_emoji(value: str | None) -> bool:
    return bool(value and _EMOJI_CLUSTER_RE.search(value))


def strip_leading_emoji(value: str | None) -> str:
    """Remove one leading ordinary emoji and its separator, preserving text.

    If the label consists only of an emoji, it is returned unchanged because
    Telegram buttons should not be left with an empty text value.
    """
    text = value or ""
    match = _LEADING_EMOJI_RE.match(text)
    if not match:
        return text
    rest = text[match.end():]
    return rest if rest.strip() else text


def replace_first_emoji(value: str | None, replacement: str) -> str | None:
    """Replace the first ordinary emoji cluster anywhere in *value*."""
    if value is None:
        return None
    match = _EMOJI_CLUSTER_RE.search(value)
    if not match:
        return value
    return value[:match.start()] + replacement + value[match.end():]


def first_emoji(value: str | None) -> str:
    """Return the first ordinary Unicode emoji cluster from *value*, or an empty string."""
    if not value:
        return ""
    match = _EMOJI_CLUSTER_RE.search(value)
    return match.group(0) if match else ""
