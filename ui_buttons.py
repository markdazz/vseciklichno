from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram.types import InlineKeyboardButton as _AiogramInlineKeyboardButton
from aiogram.types import KeyboardButton as _AiogramKeyboardButton
from premium_emoji import global_button_icon_for_text, button_text_without_fallback_for_icon
from emoji_text import replace_first_emoji



@dataclass
class ButtonDefinition:
    button_key: str
    default_text: str
    kind: str
    group_name: str


_CUSTOM: dict[str, str] = {}
_CUSTOM_ICON: dict[str, str] = {}
_CUSTOM_STYLE: dict[str, str] = {}
_SEEN: dict[str, ButtonDefinition] = {}

GROUP_TITLES = {
    "customer": "👤 Покупатель",
    "catalog": "🛍 Каталог и товар",
    "checkout": "🧾 Корзина и оформление",
    "orders": "📦 Заказы и оплата",
    "admin_orders": "⚙️ Админ: заказы",
    "admin_catalog": "👕 Админ: товары",
    "admin_marketing": "📣 Админ: клиенты и маркетинг",
    "admin_system": "🛠 Админ: система",
    "common": "🔘 Общие кнопки",
}


def _signature(text: str) -> str:
    # Счётчики и ID меняются, но сама кнопка остаётся той же.
    value = re.sub(r"\d+", "{n}", text or "")
    return re.sub(r"\s+", " ", value).strip()


def _normalize_callback(callback_data: str) -> str:
    parts = (callback_data or "").split(":")
    return ":".join("*" if p.isdigit() else p for p in parts)


def _key(kind: str, text: str, callback_data: str = "", url: str = "") -> str:
    # Название настраивается по типу кнопки и её стандартной подписи.
    # callback/url намеренно не входят в ключ: одинаковая кнопка «Назад»
    # получает одно понятное название во всех местах, а действия не меняются.
    base = f"{kind}|{_signature(text)}"
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]
    return f"uib:{digest}"


def _group(kind: str, text: str, callback_data: str = "") -> str:
    cb = callback_data or ""
    if kind == "reply":
        if "Админ" in text:
            return "admin_system"
        if text in {
            "🛍 Каталог", "📦 Мои заказы", "👤 Мой профиль", "🛒 Корзина",
            "⭐ Отзывы", "📏 Размеры", "🎁 Пригласить", "☎️ Поддержка", "🗑 Мои данные",
        }:
            return "customer"
        if text in {"⬅️ Назад", "❌ Отмена", "📱 Отправить номер", "➡️ Нет корпуса", "➡️ Нет квартиры", "➡️ Без комментария"}:
            return "checkout"
        return "common"

    if cb.startswith(("admstatus:", "admnext:", "admorder:", "adm:payments", "adm:shipping", "adm:active", "adm:queue:", "receiptadmin:", "receiptview:", "admintrack:", "adminnote:")):
        return "admin_orders"
    if cb.startswith(("adm:products", "adm:add", "adminproduct:", "productedit:", "producttoggle:", "productmedia:", "variantedit:", "adminphoto:", "addmode:")):
        return "admin_catalog"
    if cb.startswith(("adm:customers", "adm:bonuses", "adm:reviews", "adm:stats", "adm:promos", "adm:welcome", "adm:sizechart", "adm:broadcast", "promo:", "client:", "bonus:", "review:", "content:")):
        return "admin_marketing"
    if cb.startswith(("adm:", "admin:", "cleanup:", "privacyadmin:", "requiredsub:", "uibadmin:", "premiumemoji:")):
        return "admin_system"
    if cb.startswith(("checkout", "delivery:", "cdektype:", "pvz:", "deliveryprofile:", "saveprofile:", "cart", "cartinc:", "cartdec:", "cartdel:")):
        return "checkout"
    if cb.startswith(("pay", "receipt:", "myorder:", "nav:myorders")):
        return "orders"
    if cb.startswith(("catalog", "cat:", "product:", "pcolor:", "variant:", "addv:", "watch:")):
        return "catalog"
    if cb.startswith(("profile:", "legal:", "privacy:", "nav:home", "broadcast:off")):
        return "customer"
    return "common"


def _register(kind: str, text: str, callback_data: str = "", url: str = "") -> str:
    key = _key(kind, text, callback_data, url)
    # Строки внутри списка редактора — это превью других кнопок, а не отдельные
    # элементы интерфейса. Не добавляем их в реестр, иначе список рос бы сам от себя.
    if callback_data.startswith((
        "uibadmin:item:",
        "uibadmin:ep:",
        "uibadmin:euse:",
        "uibadmin:ev:",
        "uibadmin:epacks:",
        "premiumemoji:pack:",
        "premiumemoji:pi:",
        "premiumemoji:pr:",
        "premiumemoji:bg:",
        "premiumemoji:bu:",
        "premiumemoji:catalogitem:",
        "premiumemoji:view:",
        "premiumemoji:searchpage:",
        "uibadmin:style:",
    )):
        return key
    _SEEN[key] = ButtonDefinition(key, text, kind, _group(kind, text, callback_data))
    return key


def _apply(key: str, default_text: str) -> str:
    custom = (_CUSTOM.get(key) or "").strip()
    result = custom or default_text

    # Удобные placeholders для динамических кнопок.
    if custom:
        count_match = re.search(r"\s·\s(\d+)\s*$", default_text)
        id_match = re.search(r"№\s*(\d+)", default_text)
        if count_match:
            if "{count}" in result:
                result = result.replace("{count}", count_match.group(1))
            elif not re.search(r"\s·\s\d+\s*$", result):
                result = f"{result} · {count_match.group(1)}"
        if id_match:
            if "{id}" in result:
                result = result.replace("{id}", id_match.group(1))
            elif "№" not in result and not re.search(r"\d", result):
                result = f"{result} №{id_match.group(1)}"

    # Обычный emoji здесь НЕ удаляем. Его убирает session middleware прямо
    # перед отправкой запроса, только когда Telegram действительно получает
    # icon_custom_emoji_id. Благодаря этому при отказе Telegram middleware
    # может повторить запрос без Premium-иконки и сохранить обычный emoji.
    return result[:64]


def _apply_icon(key: str, rendered_text: str, kwargs: dict[str, Any]) -> None:
    # Per-button assignment wins. If there is none, a global Unicode->Premium
    # replacement is automatically applied to every button containing that emoji.
    icon = (_CUSTOM_ICON.get(key) or "").strip()
    if not icon:
        icon = global_button_icon_for_text(rendered_text)
    if icon and not kwargs.get("icon_custom_emoji_id"):
        kwargs["icon_custom_emoji_id"] = icon


_ALLOWED_STYLES = {"primary", "success", "danger"}
_INVISIBLE_ICON_TEXT = ""  # Icon-only inline buttons: no placeholder width; middleware has a U+2060 fallback


def _auto_style(text: str, callback_data: str = "", kind: str = "inline") -> str:
    """Return the single automatic color for a Telegram button.

    The palette is deliberately restrained:
      * danger  — destructive / rejecting actions only;
      * success — purchase, confirmation, save/create/add actions;
      * primary — main navigation, viewing, editing and useful forward actions;
      * ""      — secondary/back/auxiliary controls stay neutral.

    There are no per-button overrides: the same semantic rules are used for
    every button in the bot, including admin and customer keyboards.
    """
    raw = str(text or "")
    value = raw.casefold().strip()
    cb = str(callback_data or "").casefold().strip()
    stripped = raw.strip()

    # Icon-only / delete-family controls are destructive even when their text
    # also contains a secondary word such as «Ещё».
    if stripped in {"➖", "−", "-", "🗑"} or cb.startswith("cartdec:"):
        return "danger"
    if stripped.startswith("❌"):
        return "danger"
    if stripped.startswith("🗑") and "мои данные" not in value:
        return "danger"

    # Secondary navigation should not compete visually with the main action.
    neutral_words = (
        "назад", "к списку", "к товару", "к заказу", "к категории",
        "к клиентам", "к наборам", "к набору", "к кнопке", "к документам",
        "к бонусам", "к промокодам", "к очереди", "к товарам", "к разделам",
        "в меню", "в админ-панель", "админ-панель", "панель", "свернуть",
        "ещё", "предыдущ", "вернуть главное меню", "без комментария",
        "без медиа", "нет корпуса", "нет квартиры",
    )
    neutral_callbacks = (
        "back", "nav:home", "adm:home", "uibadmin:list:", "uibadmin:item:",
        "premiumemoji:pack:", "premiumemoji:pi:", "premiumemoji:pr:",
    )
    if any(word in value for word in neutral_words) or any(token in cb for token in neutral_callbacks):
        return ""

    # Red is reserved for clearly destructive, rejecting or disabling actions.
    danger_words = (
        "удал", "очист", "сброс", "отклон", "заблок", "отключ", "убрать",
        "аннулир", "анонимиз", "не принимаю", "не сохранять", "нет, отмена", "отмена",
        "не получать рассылку", "запросить удаление", "списать бонус", "списать",
    )
    danger_callbacks = (
        "cartdel:", "delete", "del:", "remove", "reset", "cleanup:", "block",
        "reject", "disable", "cancel", "premiumemoji:prd:", "broadcast:off",
    )
    if any(word in value for word in danger_words) or any(token in cb for token in danger_callbacks):
        return "danger"

    # Green = positive commitment / commerce / creation / confirmation.
    success_words = (
        "оформить заказ", "создать заказ", "оплатить", "перевод по реквизитам",
        "добавить в корзину", "открыть корзину", "корзина", "каталог", "в магазин",
        "подтвердить", "сохранить", "создать", "добавить", "начислить", "готово",
        "принимаю условия", "применить", "загруз", "отправить чек", "ввести трек и отправить",
        "товар в эту категорию", "сделать хитом", "сделать новинкой", "в наличии",
        "оставить отзыв", "завершение",
    )
    success_callbacks = (
        "cartinc:", "addv:", "add", "create", "save", "confirm", "approve",
        "pay", "checkout", "upload", "submit", "producttoggle:",
    )
    if any(word in value for word in success_words) or any(token in cb for token in success_callbacks):
        return "success"

    if stripped in {"➕", "+"} or stripped.startswith("✅"):
        return "success"

    # Blue = main navigation, information, viewing, editing and forward progress.
    primary_words = (
        "мой профиль", "профиль", "мои заказы", "заказы", "отзывы", "размер",
        "поддержка", "пригласить", "мои данные", "статистика", "доставка",
        "пользовател", "клиент", "администратор", "товары", "категори", "бонус",
        "промокод", "рассыл", "оплаты", "документ", "политика", "оферта",
        "журнал", "логи", "premium emoji", "настройка кнопок", "резервная копия",
        "предпросмотр", "открыть", "посмотреть", "отследить", "проверить",
        "изменить", "редакт", "переимен", "название", "описание", "цена",
        "трек", "мультимедиа", "цвет", "ввести", "указать", "отправить номер",
        "следующ", "продолж", "сдэк", "почта россии", "пункт выдачи", "курьер",
        "написать продавцу", "чек", "excel", "обязательная подписка", "подписаться",
        "обработка персональных данных", "данные", "активные", "сборка",
        "заменить", "настроить", "обновить", "приветствие", "комментарий", "вес",
        "отправить emoji", "отправить всем", "отправка", "прямой тест", "предзаказ",
        "контент-менеджер", "менеджер", "склад / отправка",
    )
    primary_callbacks = (
        "catalog", "cat:", "product:", "profile:", "myorder:", "nav:myorders",
        "review", "content:", "adm:", "admin:", "client:", "promo:", "bonus:",
        "delivery:", "cdektype:", "pvz:", "receipt", "watch:", "legal:", "privacy:",
    )
    if any(word in value for word in primary_words) or any(token in cb for token in primary_callbacks):
        return "primary"

    # Unknown/secondary choices stay neutral instead of turning the whole UI blue.
    return ""


def _apply_style(
    key: str,
    kwargs: dict[str, Any],
    *,
    text: str,
    callback_data: str = "",
    kind: str = "inline",
) -> None:
    """Apply semantic color globally, with no manual/custom priority."""
    # Ignore any style passed by old code or stored in the database.  This makes
    # every button follow one consistent visual system.
    style = _auto_style(text, callback_data, kind)
    if style:
        kwargs["style"] = style
    else:
        kwargs.pop("style", None)


def InlineKeyboardButton(*args: Any, **kwargs: Any) -> _AiogramInlineKeyboardButton:
    text = str(kwargs.get("text", args[0] if args else ""))
    callback_data = str(kwargs.get("callback_data") or "")
    url = str(kwargs.get("url") or "")
    key = _register("inline", text, callback_data, url)
    kwargs["text"] = _apply(key, text)
    _apply_icon(key, kwargs["text"], kwargs)
    _apply_style(key, kwargs, text=kwargs["text"], callback_data=callback_data, kind="inline")
    if args:
        args = args[1:]
    return _AiogramInlineKeyboardButton(*args, **kwargs)


def KeyboardButton(*args: Any, **kwargs: Any) -> _AiogramKeyboardButton:
    text = str(kwargs.get("text", args[0] if args else ""))
    key = _register("reply", text)
    kwargs["text"] = _apply(key, text)
    _apply_icon(key, kwargs["text"], kwargs)
    _apply_style(key, kwargs, text=kwargs["text"], kind="reply")
    if args:
        args = args[1:]
    return _AiogramKeyboardButton(*args, **kwargs)


def reply_key(default_text: str) -> str:
    return _key("reply", default_text)


def _configured_icon(key: str, rendered: str) -> str:
    return (_CUSTOM_ICON.get(key) or "").strip() or global_button_icon_for_text(rendered)


def _telegram_button_text(key: str, rendered: str) -> str:
    icon = _configured_icon(key, rendered)
    if not icon:
        return rendered
    # Global rules know the exact fallback to remove. For a manually assigned
    # icon, replace the first ordinary emoji anywhere in the label. Icon-only
    # inline actions such as "🗑" intentionally become an empty label so the
    # Premium icon can sit in the visual center with no placeholder width.
    global_rendered = button_text_without_fallback_for_icon(rendered, icon)
    if global_rendered != rendered:
        return global_rendered or _INVISIBLE_ICON_TEXT
    manual_rendered = replace_first_emoji(rendered, "")
    if manual_rendered != rendered:
        manual_rendered = re.sub(r"[ \t]{2,}", " ", manual_rendered).strip()
        return manual_rendered or _INVISIBLE_ICON_TEXT
    return rendered


def text_matches(actual: str | None, default_text: str) -> bool:
    if not actual:
        return False
    if actual == default_text:
        return True
    key = reply_key(default_text)
    rendered = _apply(key, default_text)
    return actual in {rendered, _telegram_button_text(key, rendered)}


def rendered_text(button_key: str, default_text: str) -> str:
    """Return the actual visible text Telegram receives for a configured button."""
    rendered = _apply(button_key, default_text)
    return _telegram_button_text(button_key, rendered)


def load_custom_labels(rows: list[Any]) -> None:
    _CUSTOM.clear()
    _CUSTOM_ICON.clear()
    _CUSTOM_STYLE.clear()
    for row in rows:
        try:
            key = row["button_key"]
            value = row["custom_text"]
            try:
                icon = row["custom_emoji_id"]
            except Exception:
                icon = ""
            try:
                style = row["custom_style"]
            except Exception:
                style = ""
        except Exception:
            key = row[0]
            value = row[1] if len(row) > 1 else ""
            icon = row[2] if len(row) > 2 else ""
            style = row[3] if len(row) > 3 else ""
        if value:
            _CUSTOM[str(key)] = str(value)
        if icon:
            _CUSTOM_ICON[str(key)] = str(icon)
        style_value = str(style or "").strip().lower()
        if style_value in (_ALLOWED_STYLES | {"default"}):
            _CUSTOM_STYLE[str(key)] = style_value


def set_custom_label(button_key: str, value: str) -> None:
    if value.strip():
        _CUSTOM[button_key] = value.strip()
    else:
        _CUSTOM.pop(button_key, None)


def set_custom_icon(button_key: str, custom_emoji_id: str) -> None:
    value = (custom_emoji_id or "").strip()
    if value:
        _CUSTOM_ICON[button_key] = value
    else:
        _CUSTOM_ICON.pop(button_key, None)


def set_custom_style(button_key: str, style: str) -> None:
    value = (style or "").strip().lower()
    if value in (_ALLOWED_STYLES | {"default"}):
        _CUSTOM_STYLE[button_key] = value
    else:
        _CUSTOM_STYLE.pop(button_key, None)


def clear_custom_labels() -> None:
    _CUSTOM.clear()


def clear_custom_icons() -> None:
    _CUSTOM_ICON.clear()


def clear_custom_styles() -> None:
    _CUSTOM_STYLE.clear()


def snapshot_definitions() -> list[ButtonDefinition]:
    return list(_SEEN.values())


def discover_static_buttons(source_path: str | Path) -> list[ButtonDefinition]:
    """Находит статические кнопки в bot.py, чтобы они были видны в редакторе ещё до первого нажатия."""
    path = Path(source_path)
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except Exception:
        return []

    def text_options(node: ast.AST | None) -> list[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.IfExp):
            return text_options(node.body) + text_options(node.orelse)
        return []

    found: dict[str, ButtonDefinition] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in {"InlineKeyboardButton", "KeyboardButton"}:
            continue
        keyword_nodes = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        texts = text_options(keyword_nodes.get("text"))
        if not texts:
            continue
        kind = "reply" if node.func.id == "KeyboardButton" else "inline"
        cb_node = keyword_nodes.get("callback_data")
        cb = cb_node.value if isinstance(cb_node, ast.Constant) and isinstance(cb_node.value, str) else ""
        url_node = keyword_nodes.get("url")
        url = url_node.value if isinstance(url_node, ast.Constant) and isinstance(url_node.value, str) else ("*" if url_node is not None else "")
        for text in texts:
            key = _register(kind, text, cb, url)
            found[key] = _SEEN[key]
    return list(found.values())
