from __future__ import annotations

import asyncio
import html
import logging
import os
import tempfile
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    TelegramObject,
)

import db
from app_services import (
    SingleInstanceLock,
    cdek_points,
    calculate_pricing,
    delivery_address,
    delivery_summary,
    export_orders_xlsx,
    make_backup,
    money,
    order_delivery_summary,
    parse_variant_text,
    price_text,
    pricing_text,
    tracking_url,
)
from config import settings
from emoji_text import contains_emoji, first_emoji, replace_first_emoji

from ui_buttons import (
    InlineKeyboardButton,
    KeyboardButton,
    GROUP_TITLES as UI_BUTTON_GROUP_TITLES,
    clear_custom_labels as ui_clear_custom_labels,
    load_custom_labels as ui_load_custom_labels,
    set_custom_label as ui_set_custom_label,
    set_custom_icon as ui_set_custom_icon,
    rendered_text as ui_rendered_text,
    text_matches as ui_text_matches,
)
from states import (
    AdminAddAdmin,
    AdminAddPhoto,
    AdminAddProduct,
    AdminBroadcast,
    AdminBonus,
    AdminContentEdit,
    AdminEditValue,
    AdminNote,
    AdminPromo,
    AdminReviewLink,
    AdminRequiredChannel,
    AdminTracking,
    AdminVariantEdit,
    Checkout,
    ReceiptUpload,
    AdminButtonEdit,
    AdminPremiumEmoji,
    AdminCategoryEdit,
    AdminGlobalSearch,
)


from customer_ui import MAIN_MENU_BUTTON_TEXTS, main_menu
from admin_sections import ROLE_NAMES, admin_menu, admin_dashboard_text, admin_section_menu
from premium_pack_service import (
    premium_pack_import_lock,
    premium_pack_names_from_text,
    import_premium_pack_names,
    save_custom_emoji_pack,
)
from premium_emoji_catalog import search_items as premium_catalog_search_items, view_items as premium_catalog_view_items
from diagnostics import raw_bot_api, full_system_diagnostics
from admin_search import search_everything
from production_runtime import backup_once
from premium_semantic import apply_style_preset
from startup_tasks import sync_ui_button_registry
from runtime_app import run_bot
from ui_render import render_screen
from admin_auth import admin_role, is_admin
from workers import abandoned_cart_worker
from logging_setup import configure_logging

from premium_emoji import (
    apply_to_html as premium_apply_to_html,
    clear_rules as premium_clear_rules,
    extract_custom_emoji_pairs,
    load_placements as premium_load_placements,
    load_rules as premium_load_rules,
    remove_rule as premium_remove_rule,
    set_rule as premium_set_rule,
    button_text_without_fallback_for_icon as premium_button_text_without_fallback,
)

router = Router()
broadcast_lock = asyncio.Lock()
admin_product_photo_locks: dict[int, asyncio.Lock] = {}
BOT_BUILD = "ADMIN-ORDER-UX-23"


def order_ref(order_row) -> str:
    return db.public_order_ref(order_row)


PRE_SHIPPING_STATUSES = ("Подтверждён", "Собирается", "Собран", "Передан в доставку")

# -----------------------------
# BASIC UI / PERMISSIONS
# -----------------------------
class UIButtonText(Filter):
    """Фильтр для ReplyKeyboard: принимает и стандартное, и переименованное админом название."""
    def __init__(self, default_text: str):
        self.default_text = default_text

    async def __call__(self, message: Message) -> bool:
        return ui_text_matches(message.text, self.default_text)


STATUS_NEXT = {
    # New paid orders no longer require manual assembly-stage clicks.
    "Отправлен": ("Получен", "📬 Отметить полученным"),
    "Получен": ("Завершён", "🏁 Завершить заказ"),
}

STATUS_USER_TEXT = {
    "Собирается": "📦 Ваш заказ передан на сборку.",
    "Собран": "✅ Ваш заказ собран и готовится к отправке.",
    "Передан в доставку": "🚚 Ваш заказ передан в службу доставки. Скоро появится трек-номер.",
    "Отправлен": "📦 Ваш заказ отправлен.",
    "Получен": "📬 Заказ отмечен как полученный. Спасибо за покупку!",
    "Завершён": "🏁 Заказ завершён. Будем рады вашему отзыву!",
}

MEDIA_TYPE_LABELS = {
    "photo": "Фото",
    "video": "Видео",
    "animation": "GIF/анимация",
    "document": "Файл",
    "audio": "Аудио",
}


_PREMIUM_TEXT_METHODS = {"SendMessage", "EditMessageText", "SendMessageDraft"}
_PREMIUM_CAPTION_METHODS = {
    "SendPhoto", "SendVideo", "SendAnimation", "SendAudio", "SendVoice", "SendDocument",
    "SendPaidMedia", "EditMessageCaption",
}
_PREMIUM_MEDIA_METHODS = {"SendMediaGroup", "EditMessageMedia"}

# During an admin preview we must see Telegram's real error instead of silently
# falling back to an ordinary emoji. This makes button setup self-diagnosing.
_STRICT_PREMIUM_BUTTON_TEST: ContextVar[bool] = ContextVar(
    "strict_premium_button_test", default=False
)


def _set_attr_with_restore(originals: list[tuple[object, str, Any]], obj: object, attr: str, value: Any) -> None:
    """Set a mutable aiogram method/model field and remember its previous value."""
    try:
        previous = getattr(obj, attr)
    except Exception:
        return
    if previous == value:
        return
    try:
        setattr(obj, attr, value)
    except Exception:
        return
    originals.append((obj, attr, previous))


def _force_html_parse_mode(obj: object, originals: list[tuple[object, str, Any]]) -> None:
    """Premium tg-emoji tags are HTML, so don't rely on DefaultBotProperties here."""
    if not hasattr(obj, "parse_mode"):
        return
    _set_attr_with_restore(originals, obj, "parse_mode", ParseMode.HTML)


def _premiumize_outgoing_method(method) -> list[tuple[object, str, Any]]:
    """Apply Premium emoji replacements and explicitly enable HTML parsing.

    The previous implementation relied only on Bot(default=parse_mode=HTML).
    Session middleware can run while aiogram still carries a Default(...) marker,
    so custom <tg-emoji> markup could reach Telegram without a guaranteed HTML
    parse mode. We now set it on every changed text/caption itself.
    """
    originals: list[tuple[object, str, Any]] = []
    method_name = type(method).__name__

    def replace_attr(obj, attr: str) -> None:
        value = getattr(obj, attr, None)
        if not isinstance(value, str) or not value:
            return
        rendered = premium_apply_to_html(value)
        if rendered == value or rendered is None:
            return
        _set_attr_with_restore(originals, obj, attr, rendered)
        _force_html_parse_mode(obj, originals)

    if method_name in _PREMIUM_TEXT_METHODS:
        replace_attr(method, "text")
    if method_name in _PREMIUM_CAPTION_METHODS:
        replace_attr(method, "caption")
    if method_name in _PREMIUM_MEDIA_METHODS:
        media = getattr(method, "media", None)
        if isinstance(media, (list, tuple)):
            for item in media:
                replace_attr(item, "caption")
        elif media is not None:
            replace_attr(media, "caption")
    return originals


def _prepare_custom_emoji_button_icons(method) -> list[tuple[object, str, Any]]:
    """Hide the leading ordinary emoji only while a Premium button icon is sent.

    Example: stored/default label is "🛍 Каталог". The actual Telegram request is
    icon_custom_emoji_id=<id> + text="Каталог". If Telegram rejects the custom
    icon, the retry restores text="🛍 Каталог" before removing the icon, so the
    button never becomes visually empty.
    """
    originals: list[tuple[object, str, Any]] = []
    markup = getattr(method, "reply_markup", None)
    if markup is None:
        return originals
    rows = getattr(markup, "inline_keyboard", None) or getattr(markup, "keyboard", None)
    if not rows:
        return originals
    for row in rows:
        for button in row:
            icon_id = str(getattr(button, "icon_custom_emoji_id", "") or "").strip()
            text = getattr(button, "text", None)
            if not icon_id or not isinstance(text, str) or not text:
                continue
            # Global replacements know exactly which ordinary Unicode emoji
            # corresponds to this custom icon. Remove that exact fallback first.
            # For a manually assigned per-button icon, keep the old behavior and
            # remove the first leading ordinary emoji.
            rendered = premium_button_text_without_fallback(text, icon_id)
            if rendered == text:
                # A manually assigned Premium icon should replace the ordinary
                # emoji, not sit next to it.  Remove the first emoji anywhere in
                # the label (not only at the beginning).
                rendered = replace_first_emoji(text, "")
                if rendered != text:
                    rendered = " ".join(rendered.split())

            if rendered != text:
                if not rendered.strip():
                    # For icon-only INLINE buttons try a truly empty label first. This avoids
                    # reserving width for a fake/invisible character and keeps the Premium
                    # emoji visually centered. ReplyKeyboard buttons keep their original text
                    # because pressing them sends that text back to the bot.
                    is_inline = hasattr(markup, "inline_keyboard") and getattr(markup, "inline_keyboard", None) is not None
                    rendered = "" if is_inline else text
                if rendered != text:
                    _set_attr_with_restore(originals, button, "text", rendered)
    return originals


def _apply_zero_width_icon_text_fallback(method) -> list[tuple[object, str, Any]]:
    """Fallback for clients/API versions that reject an empty icon-only label.

    U+2060 WORD JOINER has zero advance width, unlike the old U+2800 Braille
    blank. It is used only after Telegram rejects the cleaner empty-label request.
    """
    changed: list[tuple[object, str, Any]] = []
    markup = getattr(method, "reply_markup", None)
    rows = getattr(markup, "inline_keyboard", None) if markup is not None else None
    if not rows:
        return changed
    for row in rows:
        for button in row:
            if getattr(button, "icon_custom_emoji_id", None) and getattr(button, "text", None) == "":
                _set_attr_with_restore(changed, button, "text", "\u2060")
    return changed


def _restore_outgoing_values(originals: list[tuple[object, str, Any]]) -> None:
    for obj, attr, value in reversed(originals):
        try:
            setattr(obj, attr, value)
        except Exception:
            logging.debug("Failed to restore temporary outgoing value", exc_info=True)


def _strip_custom_emoji_button_icons(method) -> bool:
    """Remove Premium button icons for a fallback retry."""
    markup = getattr(method, "reply_markup", None)
    if markup is None:
        return False
    rows = getattr(markup, "inline_keyboard", None) or getattr(markup, "keyboard", None)
    if not rows:
        return False
    changed = False
    for row in rows:
        for button in row:
            if getattr(button, "icon_custom_emoji_id", None):
                try:
                    button.icon_custom_emoji_id = None
                    changed = True
                except Exception:
                    logging.debug("Failed to strip custom emoji button icon for fallback", exc_info=True)
    return changed


async def premium_emoji_request_middleware(make_request, bot, method):
    """Render Premium emoji; retry safely with ordinary emoji if Telegram rejects them."""
    text_originals = _premiumize_outgoing_method(method)
    button_text_originals = _prepare_custom_emoji_button_icons(method)
    try:
        return await make_request(bot, method)
    except TelegramBadRequest as exc:
        # Never turn a harmless edit-noop into a second edit that removes emoji.
        if "message is not modified" in str(exc).lower():
            raise

        # Icon-only inline buttons are first sent with text="" so Telegram can
        # center the custom emoji without any placeholder width. If this specific
        # request is rejected, retry once with a true zero-width WORD JOINER while
        # keeping the Premium icon. Only after that do we fall back to Unicode.
        zero_width_originals = _apply_zero_width_icon_text_fallback(method)
        if zero_width_originals:
            try:
                result = await make_request(bot, method)
                _restore_outgoing_values(zero_width_originals)
                return result
            except TelegramBadRequest as zero_exc:
                _restore_outgoing_values(zero_width_originals)
                exc = zero_exc

        # Admin preview is intentionally strict: if Telegram rejects the custom
        # emoji icon, let the handler show the exact reason instead of silently
        # retrying with the ordinary Unicode emoji.
        if _STRICT_PREMIUM_BUTTON_TEST.get():
            _restore_outgoing_values(button_text_originals)
            _restore_outgoing_values(text_originals)
            raise

        # Log the real reason instead of silently hiding it. This is especially
        # useful when the bot owner's Telegram account has no active Premium.
        if text_originals or button_text_originals or getattr(getattr(method, "reply_markup", None), "inline_keyboard", None) or getattr(getattr(method, "reply_markup", None), "keyboard", None):
            logging.warning("Premium/custom emoji request rejected by Telegram: %s", exc)

        # Restore the original text/caption AND the ordinary emoji in button
        # labels before retrying without Premium button icons.
        _restore_outgoing_values(button_text_originals)
        icons_stripped = _strip_custom_emoji_button_icons(method)
        _restore_outgoing_values(text_originals)
        if text_originals or button_text_originals or icons_stripped:
            return await make_request(bot, method)
        raise


def extract_message_media(message: Message) -> tuple[str, str] | None:
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.animation:
        return "animation", message.animation.file_id
    if message.document:
        return "document", message.document.file_id
    if message.audio:
        return "audio", message.audio.file_id
    return None


def render_custom_text(
    raw: str,
    message: Message,
    first_name: str | None = None,
    rich_html: str | None = None,
) -> str:
    """Render stored custom text, preserving Telegram formatting when rich HTML is available."""
    if first_name is None:
        first_name = message.from_user.first_name if message.from_user else "друг"
    safe_first_name = html.escape(first_name or "друг")
    if rich_html is not None:
        return (rich_html or "").replace("{first_name}", safe_first_name)
    return html.escape((raw or "").replace("{first_name}", first_name or "друг"))


async def send_stored_media(
    message: Message,
    media_type: str,
    file_id: str,
    caption: str | None = None,
    reply_markup=None,
):
    if media_type == "photo":
        return await message.answer_photo(photo=file_id, caption=caption, reply_markup=reply_markup)
    elif media_type == "video":
        return await message.answer_video(video=file_id, caption=caption, reply_markup=reply_markup)
    elif media_type == "animation":
        return await message.answer_animation(animation=file_id, caption=caption, reply_markup=reply_markup)
    elif media_type == "audio":
        return await message.answer_audio(audio=file_id, caption=caption, reply_markup=reply_markup)
    else:
        return await message.answer_document(document=file_id, caption=caption, reply_markup=reply_markup)


async def send_custom_content(
    message: Message,
    raw_text: str,
    media_type: str = "",
    media_file_id: str = "",
    reply_markup=None,
    prefix_html: str = "",
    first_name: str | None = None,
    rich_html: str | None = None,
) -> None:
    body = render_custom_text(raw_text, message, first_name=first_name, rich_html=rich_html).strip()
    text = prefix_html + (("\n\n" + body) if prefix_html and body else body)
    if media_type and media_file_id:
        try:
            if text and len(text) <= 1024:
                await send_stored_media(message, media_type, media_file_id, caption=text, reply_markup=reply_markup)
            else:
                await send_stored_media(message, media_type, media_file_id, reply_markup=(reply_markup if not text else None))
                if text:
                    await message.answer(text, reply_markup=reply_markup)
            return
        except Exception:
            logging.exception("failed to send stored custom media")
    if text:
        await message.answer(text, reply_markup=reply_markup)
    else:
        await message.answer("Контент пока не настроен.", reply_markup=reply_markup)


async def force_refresh_main_menu(message: Message, actor_user_id: int, notice: str | None = None) -> None:
    """Hard-refresh Telegram's persistent reply keyboard.

    Some clients keep an already displayed ReplyKeyboard when only its metadata
    (for example icon_custom_emoji_id) changes while button texts stay the same.
    Remove the old keyboard first, then send a fresh one generated from the
    current in-memory Premium emoji settings.
    """
    remove_message = None
    try:
        remove_message = await message.answer("🔄 Обновляю меню…", reply_markup=ReplyKeyboardRemove())
    except Exception:
        logging.exception("failed to remove old reply keyboard before Premium emoji refresh")

    admin = await is_admin(actor_user_id)
    await message.answer(
        notice or "✅ Главное меню обновлено 👇",
        reply_markup=main_menu(admin),
    )

    if remove_message is not None:
        try:
            await remove_message.delete()
        except Exception:
            logging.debug("Failed to delete temporary reply-keyboard refresh message", exc_info=True)


async def verify_and_preview_button_icon(
    message: Message,
    row,
    custom_emoji_id: str,
    actor_user_id: int,
) -> tuple[bool, str]:
    """Verify the icon against Telegram and refresh the visible keyboard.

    Reply keyboards are persistent on the client. Changing the DB alone cannot
    update one that Telegram Desktop/mobile already shows. We first perform a
    strict one-button API test with the exact custom_emoji_id. On success, a new
    ReplyKeyboardMarkup is sent immediately for reply buttons.
    """
    if not row or not custom_emoji_id:
        return False, "Кнопка или Premium emoji не найдены."

    kind = str(row["kind"] or "")
    default_text = str(row["default_text"] or "Кнопка")
    test_message = None
    token = _STRICT_PREMIUM_BUTTON_TEST.set(True)
    try:
        # Use the field explicitly here, independent of the in-memory button
        # registry, so this test proves that Telegram accepts this exact ID.
        test_message = await message.answer(
            "🔎 Проверяю Premium emoji…",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=default_text,
                    callback_data="premiumemoji:previewnoop",
                    icon_custom_emoji_id=custom_emoji_id,
                )
            ]]),
        )
    except TelegramBadRequest as exc:
        return False, str(exc)
    finally:
        _STRICT_PREMIUM_BUTTON_TEST.reset(token)

    if kind == "reply":
        # The strict test is only diagnostic. Remove it and send the real fresh
        # reply keyboard so the old cached keyboard on the client is replaced.
        if test_message is not None:
            try:
                await test_message.delete()
            except Exception:
                logging.debug("Failed to delete strict Premium emoji test message", exc_info=True)
        if default_text in MAIN_MENU_BUTTON_TEXTS:
            await force_refresh_main_menu(
                message,
                actor_user_id,
                "✅ Premium emoji применён. Главное меню полностью обновлено 👇",
            )
        else:
            await message.answer(
                "✅ Premium emoji применён. Обновлённая кнопка 👇",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text=default_text, icon_custom_emoji_id=custom_emoji_id)]],
                    resize_keyboard=True,
                    one_time_keyboard=True,
                ),
            )
    else:
        # For inline buttons the strict test itself is the useful preview.
        try:
            await test_message.edit_text(
                "✅ Premium emoji принят Telegram. Предпросмотр кнопки:",
                reply_markup=test_message.reply_markup,
            )
        except Exception:
            logging.debug("Failed to edit Premium emoji preview message", exc_info=True)
    return True, ""


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⬅️ Назад")], [KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Отправить номер", request_contact=True)],
            [KeyboardButton(text="⬅️ Назад")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def optional_keyboard(text: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text)], [KeyboardButton(text="⬅️ Назад")], [KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def back_home_markup(text: str = "⬅️ Назад") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data="nav:home")]])


def append_inline_back(rows: list[list[InlineKeyboardButton]], callback_data: str = "nav:home", text: str = "⬅️ Назад") -> list[list[InlineKeyboardButton]]:
    rows.append([InlineKeyboardButton(text=text, callback_data=callback_data)])
    return rows


def legal_entry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Документы и условия", callback_data="legal:menu")],
    ])


def legal_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Публичная оферта", callback_data="legal:offer")],
        [InlineKeyboardButton(text="🔒 Политика конфиденциальности", callback_data="legal:privacy")],
        [InlineKeyboardButton(text="🧾 Обработка персональных данных", callback_data="legal:consent")],
        [InlineKeyboardButton(text="🚚 Доставка, обмен и возврат", callback_data="legal:delivery")],
        [InlineKeyboardButton(text="✅ Принимаю условия", callback_data="legal:accept")],
        [InlineKeyboardButton(text="❌ Не принимаю", callback_data="legal:decline")],
        [InlineKeyboardButton(text="⬅️ Свернуть", callback_data="legal:collapse")],
    ])


REVIEW_POST_SETTING = "review_post_url"
REQUIRED_SUB_ENABLED_SETTING = "required_subscription_enabled"
REQUIRED_SUB_CHAT_SETTING = "required_subscription_chat"
REQUIRED_SUB_URL_SETTING = "required_subscription_url"
PAID_ORDER_STATUSES = ("Подтверждён", "Собирается", "Собран", "Передан в доставку", "Отправлен", "Получен", "Завершён")


def _chat_target(value: str):
    value = (value or "").strip()
    if value.startswith("-") and value[1:].isdigit():
        return int(value)
    return value


def parse_required_channel_input(raw: str) -> tuple[str, str] | None:
    value = (raw or "").strip()
    if not value:
        return None

    # Private channel: -1001234567890 | https://t.me/+invite
    if "|" in value:
        left, right = (part.strip() for part in value.split("|", 1))
        if not (left.startswith("-") and left[1:].isdigit()):
            return None
        if right.startswith("t.me/") or right.startswith("telegram.me/"):
            right = "https://" + right
        if not (right.startswith("https://t.me/") or right.startswith("https://telegram.me/")):
            return None
        return left, right

    if value.startswith("t.me/") or value.startswith("telegram.me/"):
        value = "https://" + value
    if value.startswith("https://t.me/") or value.startswith("https://telegram.me/"):
        tail = value.split("/", 3)[-1].split("?", 1)[0].strip("/")
        # Invite links do not contain a chat ID, so they must be paired with -100... above.
        if not tail or tail.startswith("+") or tail.startswith("joinchat/") or tail.startswith("c/"):
            return None
        username = tail.split("/", 1)[0]
        if not username:
            return None
        return "@" + username.lstrip("@"), f"https://t.me/{username.lstrip('@')}"

    if value.startswith("-") and value[1:].isdigit():
        return None
    username = value.lstrip("@").strip()
    if not username or any(ch.isspace() for ch in username):
        return None
    return "@" + username, f"https://t.me/{username}"


async def required_subscription_config() -> tuple[bool, str, str]:
    enabled = (await db.get_setting(REQUIRED_SUB_ENABLED_SETTING, "0")) == "1"
    chat_ref = (await db.get_setting(REQUIRED_SUB_CHAT_SETTING, "")).strip()
    url = (await db.get_setting(REQUIRED_SUB_URL_SETTING, "")).strip()
    return enabled and bool(chat_ref and url), chat_ref, url


async def validate_required_channel(bot: Bot, chat_ref: str) -> tuple[bool, str]:
    if not chat_ref:
        return False, "Канал не указан."
    try:
        chat = await bot.get_chat(_chat_target(chat_ref))
        if chat.type != ChatType.CHANNEL:
            return False, "Указан не Telegram-канал."
        me = await bot.get_me()
        member = await bot.get_chat_member(_chat_target(chat_ref), me.id)
        if member.status not in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}:
            return False, "Сначала добавьте бота администратором канала."
        return True, chat.title or chat_ref
    except Exception as exc:
        logging.warning("required subscription channel validation failed: %s", exc)
        return False, "Бот не может получить доступ к каналу. Проверьте канал и права администратора."


async def is_required_subscribed(bot: Bot, user_id: int) -> bool:
    enabled, chat_ref, _ = await required_subscription_config()
    if not enabled:
        return True
    try:
        member = await bot.get_chat_member(_chat_target(chat_ref), user_id)
        if member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER}:
            return True
        if member.status == ChatMemberStatus.RESTRICTED:
            return bool(getattr(member, "is_member", False))
        return False
    except Exception as exc:
        logging.warning("required subscription check failed for user %s: %s", user_id, exc)
        return False


def required_subscription_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=url)],
        [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="sub:check")],
    ])


async def send_subscription_gate(message: Message):
    enabled, _, url = await required_subscription_config()
    if not enabled:
        return
    await message.answer(
        "📢 <b>Для использования бота необходимо подписаться на наш Telegram-канал.</b>\n\n"
        "1. Нажмите «Подписаться на канал».\n"
        "2. Подпишитесь.\n"
        "3. Вернитесь сюда и нажмите «Проверить подписку».\n\n"
        "Пока подписка не подтверждена, остальные функции бота недоступны.",
        reply_markup=required_subscription_keyboard(url),
    )


def normalize_review_post_url(raw: str) -> str | None:
    value = (raw or "").strip()
    if value.startswith("t.me/") or value.startswith("telegram.me/"):
        value = "https://" + value
    if not (value.startswith("https://t.me/") or value.startswith("https://telegram.me/")):
        return None
    clean = value.split("?", 1)[0].rstrip("/")
    parts = clean.split("/")
    # Public post: https://t.me/channel/123
    # Private channel post: https://t.me/c/123456789/123
    if len(parts) < 5 or not parts[-1].isdigit():
        return None
    return value


async def configured_review_url() -> str:
    return (await db.get_setting(REVIEW_POST_SETTING, "")).strip()


def review_url_keyboard(url: str, text: str = "⭐ Оставить отзыв") -> InlineKeyboardMarkup | None:
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]])


def customer_order_keyboard(order_id: int, *, review_url: str = "", tracking_url_value: str = "", allow_receipt: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if tracking_url_value:
        rows.append([InlineKeyboardButton(text="🔎 Отследить посылку", url=tracking_url_value)])
    if allow_receipt:
        rows.append([InlineKeyboardButton(text="📎 Отправить чек повторно", callback_data=f"receipt:{order_id}")])
    rows.append([
        InlineKeyboardButton(text="📦 Открыть заказ", callback_data=f"myorder:{order_id}"),
        InlineKeyboardButton(text="📋 Все заказы", callback_data="nav:myorders"),
    ])
    # Отзыв намеренно последним: он появляется только у завершённого заказа.
    if review_url:
        rows.append([InlineKeyboardButton(text="⭐ Оставить отзыв", url=review_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_order_quick_keyboard(order_id: int | None = None, *, include_payments: bool = True, next_queue: str = "") -> InlineKeyboardMarkup:
    """Быстрая админ-навигация без скрытых подменю."""
    rows: list[list[InlineKeyboardButton]] = []
    if order_id is not None:
        rows.append([InlineKeyboardButton(text="📦 Открыть заказ", callback_data=f"admorder:{order_id}")])
    if next_queue and order_id is not None:
        rows.append([InlineKeyboardButton(text="➡️ Следующий заказ", callback_data=f"admnext:{next_queue}:{order_id}")])
    if include_payments:
        rows += [
            [InlineKeyboardButton(text="💳 Оплаты", callback_data="adm:payments"),
             InlineKeyboardButton(text="🚚 К отправке", callback_data="adm:shipping")],
            [InlineKeyboardButton(text="🏁 Завершение", callback_data="adm:queue:finish"),
             InlineKeyboardButton(text="⚡ Активные", callback_data="adm:active")],
            [InlineKeyboardButton(text="📋 Все заказы", callback_data="adm:queue:all")],
        ]
    else:
        rows += [
            [InlineKeyboardButton(text="🚚 К отправке", callback_data="adm:shipping"),
             InlineKeyboardButton(text="🏁 Завершение", callback_data="adm:queue:finish")],
            [InlineKeyboardButton(text="⚡ Активные", callback_data="adm:active"),
             InlineKeyboardButton(text="📋 Все заказы", callback_data="adm:queue:all")],
        ]
    rows.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def open_admin(message: Message):
    role = await admin_role(message.from_user.id)
    if not role:
        await message.answer("⛔ Нет доступа.")
        return
    await message.answer(await admin_dashboard_text(role), reply_markup=await admin_menu(role))


# -----------------------------
# LEGAL GATE
# -----------------------------
def seller_details() -> str:
    contact = settings.support_contact or (f"@{settings.shop_username}" if settings.shop_username else settings.seller_email)
    return (
        f"Продавец: <b>{html.escape(settings.seller_name)}</b>\n"
        f"Статус: {html.escape(settings.seller_status)}\n"
        f"ИНН: {html.escape(settings.seller_inn)}\n"
        f"Контакт: {html.escape(contact)}\n"
        f"Адрес: {html.escape(settings.seller_address)}"
    )


def offer_text() -> str:
    return (
        "📄 <b>ПУБЛИЧНАЯ ОФЕРТА</b>\n\n" + seller_details() + "\n\n"
        "1. Магазин осуществляет дистанционную продажу одежды через Telegram-бот. "
        "В каталоге указываются цена, описание, цвета, размеры и наличие.\n\n"
        "2. До оплаты покупатель проверяет состав заказа, ФИО, телефон, способ и адрес доставки.\n\n"
        "3. Оплата может выполняться переводом по реквизитам с проверкой чека или через подключённого "
        "платёжного провайдера Telegram.\n\n"
        "4. Доставка выполняется Почтой России или СДЭК. Если подключены API перевозчиков, бот может "
        "рассчитать стоимость и предложить ПВЗ автоматически; иначе используются заданные/согласованные условия.\n\n"
        "5. Магазин может использовать промокоды, скидки, бонусы и реферальную программу.\n\n"
        "6. Обмен и возврат выполняются с учётом применимого законодательства и опубликованных условий магазина.\n\n"
        "7. Нажатие «Принимаю условия» подтверждает ознакомление с настоящими документами.\n\n"
        f"Версия: <code>{html.escape(settings.legal_version)}</code>"
    )


def privacy_text() -> str:
    return (
        "🔒 <b>ПОЛИТИКА КОНФИДЕНЦИАЛЬНОСТИ</b>\n\n" + seller_details() + "\n\n"
        "Бот может обрабатывать Telegram ID, username, имя, ФИО получателя, телефон, индекс и адрес, "
        "выбранный ПВЗ, историю заказов, корзину, отзывы, бонусный баланс, реферальную информацию, "
        "трек-номера и загруженные чеки.\n\n"
        "Данные используются для оформления и доставки заказов, оплаты, поддержки, лояльности, отзывов, "
        "уведомлений о наличии/статусах и рекламно-информационных рассылок. Массовую рассылку можно отключить "
        "командой <code>/unsubscribe</code>.\n\n"
        "Для исполнения заказа необходимые данные могут передаваться службе доставки и платёжному провайдеру. "
        "Бот не должен получать PIN, CVV/CVC и пароли от банка.\n\n"
        "Запрос на удаление/анонимизацию данных доступен в разделе «Мои данные». Некоторые сведения о совершённых "
        "операциях могут сохраняться, если это необходимо продавцу по закону.\n\n"
        f"Версия: <code>{html.escape(settings.legal_version)}</code>"
    )


def consent_text() -> str:
    return (
        "🧾 <b>СОГЛАСИЕ НА ОБРАБОТКУ ПЕРСОНАЛЬНЫХ ДАННЫХ</b>\n\n" + seller_details() + "\n\n"
        "Пользователь соглашается на обработку добровольно переданных данных для оформления, оплаты и доставки "
        "заказов, поддержки, программы лояльности, реферальной системы, отзывов и уведомлений. Обработка может "
        "включать получение, запись, хранение, уточнение, использование, передачу исполнителям в необходимом объёме, "
        "обезличивание и удаление.\n\n"
        f"Версия: <code>{html.escape(settings.legal_version)}</code>"
    )


def delivery_legal_text() -> str:
    return (
        "🚚 <b>ДОСТАВКА, ОБМЕН И ВОЗВРАТ</b>\n\n" + seller_details() + "\n\n"
        "• Почта России: ФИО, телефон, шестизначный индекс и полный адрес.\n"
        "• СДЭК: ФИО, телефон, город и ПВЗ либо полный адрес курьерской доставки.\n"
        "• После отправки покупателю передаётся трек-номер.\n"
        "• Для обмена/возврата свяжитесь с продавцом до отправки товара обратно.\n\n"
        f"Адрес возврата: {html.escape(settings.return_address or settings.seller_address)}\n"
        f"Версия: <code>{html.escape(settings.legal_version)}</code>"
    )


async def send_legal_gate(message: Message):
    await message.answer(
        "👋 <b>Добро пожаловать в магазин</b>\n\n"
        "Перед использованием ознакомьтесь с документами. Каталог и оформление заказа откроются после принятия условий.\n\n"
        f"Версия: <code>{html.escape(settings.legal_version)}</code>",
        reply_markup=legal_entry_keyboard(),
    )


class LegalGateMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]):
        user = getattr(event, "from_user", None)
        if not user or await is_admin(user.id):
            return await handler(event, data)

        bot: Bot | None = data.get("bot")
        if isinstance(event, Message):
            text = (event.text or "").strip()
            # /start performs the subscription check itself so the referral/start flow is preserved.
            # Unsubscribe remains available even when access to the shop is gated.
            if text.startswith("/start") or text.startswith("/unsubscribe"):
                return await handler(event, data)
        if isinstance(event, CallbackQuery):
            callback_data = event.data or ""
            if callback_data.startswith("sub:") or callback_data == "broadcast:off":
                return await handler(event, data)

        # Mandatory Telegram-channel subscription is always checked before the legal gate.
        if bot and not await is_required_subscribed(bot, user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("Сначала подпишитесь на канал", show_alert=True)
                await send_subscription_gate(event.message)
            elif isinstance(event, Message):
                await send_subscription_gate(event)
            return None

        if isinstance(event, Message):
            text = (event.text or "").strip()
            if text.startswith(("/legal", "/id", "/unsubscribe", "/subscribe")):
                return await handler(event, data)
        if isinstance(event, CallbackQuery):
            callback_data = event.data or ""
            if callback_data.startswith("legal:") or callback_data == "broadcast:off":
                return await handler(event, data)
        if await db.legal_accepted(user.id, settings.legal_version):
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer("Сначала примите условия магазина", show_alert=True)
            await send_legal_gate(event.message)
        elif isinstance(event, Message):
            await send_legal_gate(event)
        return None


# -----------------------------
# START / LEGAL / PROFILE
# -----------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await db.register_user(message.from_user)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            inviter = int(parts[1][4:])
            if inviter != message.from_user.id:
                await db.add_referral(message.from_user.id, inviter)
        except ValueError:
            pass
    if not await is_admin(message.from_user.id):
        if not await is_required_subscribed(bot, message.from_user.id):
            await send_subscription_gate(message)
            return
        if not await db.legal_accepted(message.from_user.id, settings.legal_version):
            await send_legal_gate(message)
            return
    await send_home(message)


# Быстрые команды из меню Telegram (кнопка «/» рядом с полем ввода).
# Они дублируют основные действия магазина, чтобы покупателю не приходилось
# каждый раз возвращаться в главное reply-меню.
@router.message(Command("catalog"))
async def cmd_catalog(message: Message, state: FSMContext):
    await state.clear()
    await send_catalog(message)


@router.message(Command("cart"))
async def cmd_cart(message: Message, state: FSMContext):
    await state.clear()
    await show_cart(message, message.from_user.id)


@router.message(Command("orders"))
async def cmd_orders(message: Message, state: FSMContext):
    await state.clear()
    await my_orders(message)


@router.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext):
    await state.clear()
    await support(message)


async def send_home(message: Message, user=None):
    actor=user or message.from_user
    admin = await is_admin(actor.id)
    row = await db.user_row(actor.id)
    if row and not row["welcome_sent_at"]:
        default_text = "Привет, {first_name}! 👋\n\nВыбирайте товары и оформляйте доставку прямо в боте."
        text = await db.get_setting("welcome_text", default_text)
        text_html = await db.get_setting("welcome_text_html", "")
        media_type = await db.get_setting("welcome_media_type", "photo" if settings.main_banner else "")
        media_file_id = await db.get_setting("welcome_media_file_id", settings.main_banner or "")
        await send_custom_content(
            message, text, media_type, media_file_id, reply_markup=main_menu(admin),
            first_name=actor.first_name, rich_html=(text_html if text_html else None),
        )
        await db.mark_welcome_sent(actor.id)
        return
    await message.answer("Главное меню", reply_markup=main_menu(admin))


@router.message(Command("legal"))
async def legal_cmd(message: Message): await send_legal_gate(message)


@router.callback_query(F.data == "sub:check")
async def subscription_check(c: CallbackQuery, bot: Bot):
    enabled, _, _ = await required_subscription_config()
    if not enabled:
        await c.answer("Проверка подписки отключена ✅", show_alert=True)
    elif not await is_required_subscribed(bot, c.from_user.id):
        await c.answer("❌ Подписка пока не найдена. Подпишитесь на канал и попробуйте ещё раз.", show_alert=True)
        return
    else:
        await c.answer("✅ Подписка подтверждена!", show_alert=True)
        try:
            await c.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass

    if not await db.legal_accepted(c.from_user.id, settings.legal_version):
        await send_legal_gate(c.message)
        return
    await c.message.answer("✅ <b>Подписка подтверждена.</b>")
    await send_home(c.message,c.from_user)


@router.callback_query(F.data == "legal:menu")
async def legal_menu(c: CallbackQuery):
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=legal_keyboard())


@router.callback_query(F.data == "legal:collapse")
async def legal_collapse(c: CallbackQuery):
    await c.answer()
    await c.message.edit_reply_markup(reply_markup=legal_entry_keyboard())


@router.callback_query(F.data == "legal:offer")
async def legal_offer(c: CallbackQuery): await c.answer(); await c.message.answer(offer_text(), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К документам", callback_data="legal:menu")]]))
@router.callback_query(F.data == "legal:privacy")
async def legal_privacy(c: CallbackQuery): await c.answer(); await c.message.answer(privacy_text(), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К документам", callback_data="legal:menu")]]))
@router.callback_query(F.data == "legal:consent")
async def legal_consent(c: CallbackQuery): await c.answer(); await c.message.answer(consent_text(), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К документам", callback_data="legal:menu")]]))
@router.callback_query(F.data == "legal:delivery")
async def legal_delivery(c: CallbackQuery): await c.answer(); await c.message.answer(delivery_legal_text(), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К документам", callback_data="legal:menu")]]))
@router.callback_query(F.data == "legal:decline")
async def legal_decline(c: CallbackQuery):
    await c.answer()
    await c.message.answer("Вы не приняли условия магазина.\n\nКаталог и оформление заказов останутся недоступны. Открыть документы снова: /legal")
@router.callback_query(F.data == "legal:accept")
async def legal_accept(c: CallbackQuery):
    await db.accept_legal(c.from_user.id, settings.legal_version)
    await c.answer("Условия приняты ✅")
    await c.message.answer("✅ <b>Условия приняты</b>")
    await send_home(c.message,c.from_user)


@router.message(Command("id"))
async def cmd_id(message: Message): await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Command("unsubscribe"))
async def unsubscribe(message: Message):
    await db.register_user(message.from_user); await db.set_broadcasts(message.from_user.id, False)
    await message.answer("🔕 Массовая рассылка отключена. Сервисные уведомления по заказам останутся включены. /subscribe — включить снова.")


@router.message(Command("subscribe"))
async def subscribe(message: Message):
    await db.register_user(message.from_user); await db.set_broadcasts(message.from_user.id, True)
    await message.answer("🔔 Рассылка снова включена.")


@router.callback_query(F.data == "broadcast:off")
async def broadcast_off(c: CallbackQuery):
    await db.set_broadcasts(c.from_user.id, False); await c.answer("Рассылка отключена ✅", show_alert=True)


async def send_profile(message: Message, user_id: int):
    spend = await db.lifetime_spend(user_id)
    balance = await db.bonus_balance(user_id)
    discount = settings.loyalty_discount_percent if spend >= settings.loyalty_threshold else 0
    left = max(0, settings.loyalty_threshold - spend)
    text = (
        "👤 <b>Мой профиль</b>\n\n"
        f"Покупок: <b>{money(spend)}</b>\n"
        f"Бонусы: <b>{balance}</b>\n"
        f"Постоянная скидка: <b>{discount}%</b>\n"
    )
    if not discount:
        text += f"До скидки {settings.loyalty_discount_percent}%: {money(left)}.\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="☰ Ещё", callback_data="profile:more")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="nav:home")],
    ])
    await render_screen(message, text, kb)


@router.message(UIButtonText("👤 Мой профиль"))
async def profile(message: Message):
    await send_profile(message, message.from_user.id)


@router.callback_query(F.data == "profile:home")
async def profile_home_cb(c: CallbackQuery):
    await c.answer()
    await send_profile(c.message, c.from_user.id)


@router.callback_query(F.data == "profile:more")
async def profile_more_cb(c: CallbackQuery):
    await c.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Отзывы", callback_data="profile:reviews"),
         InlineKeyboardButton(text="📏 Размеры", callback_data="profile:size")],
        [InlineKeyboardButton(text="🎁 Пригласить", callback_data="profile:referral"),
         InlineKeyboardButton(text="☎️ Поддержка", callback_data="profile:support")],
        [InlineKeyboardButton(text="🗑 Мои данные", callback_data="profile:privacy")],
        [InlineKeyboardButton(text="⬅️ Профиль", callback_data="profile:home")],
    ])
    await render_screen(c.message, "☰ <b>Ещё</b>\n\nЗдесь собраны функции, которые нужны реже.", kb)


@router.message(UIButtonText("🎁 Пригласить"))
@router.message(UIButtonText("🎁 Пригласить друга"))
async def referral(message: Message, bot: Bot):
    username = settings.bot_username
    if not username:
        try:
            username = (await bot.get_me()).username or ""
        except Exception:
            username = ""
    if not username:
        await message.answer("Username бота не удалось определить.")
        return
    link = f"https://t.me/{username}?start=ref_{message.from_user.id}"
    await message.answer(
        "🎁 <b>Реферальная программа</b>\n\n"
        f"Пригласите друга по ссылке. После его первой оплаченной покупки вы получите <b>{settings.referral_bonus_points} бонусов</b>.\n\n"
        f"<code>{link}</code>",
        reply_markup=back_home_markup(),
    )


@router.message(UIButtonText("☎️ Поддержка"))
async def support(message: Message):
    if settings.shop_username:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Написать продавцу", url=f"https://t.me/{settings.shop_username}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:home")],
        ])
        await message.answer("Напишите продавцу по вопросам товара, оплаты, доставки или возврата:", reply_markup=kb)
    else:
        await message.answer(f"Контакт продавца: {html.escape(settings.support_contact or settings.seller_email)}", reply_markup=back_home_markup())


async def send_size_chart_content(message: Message, text: str, reply_markup=None, rich_html: str | None = None) -> None:
    body = render_custom_text(text, message, rich_html=rich_html).strip()
    caption = "📏 <b>Таблица размеров</b>" + (("\n\n" + body) if body else "")
    media = await db.size_chart_media()
    if media and len(caption) <= 1024:
        if await send_photo_album(message, media, caption, reply_markup=reply_markup):
            return
    if media:
        # Telegram caption limit is 1024 chars. For unusually long size-chart text, keep the media and send the text below it.
        await send_photo_album(message, media)
        await message.answer(caption, reply_markup=reply_markup)
        return

    # Backward-compatible fallback for the old single-media settings.
    media_type = await db.get_setting("size_chart_media_type", "")
    media_file_id = await db.get_setting("size_chart_media_file_id", "")
    await send_custom_content(
        message, text, media_type, media_file_id, prefix_html="📏 <b>Таблица размеров</b>",
        reply_markup=reply_markup, rich_html=rich_html
    )


@router.message(UIButtonText("📏 Размеры"))
@router.message(UIButtonText("📏 Таблица размеров"))
async def size_chart(message: Message):
    text = await db.get_setting("size_chart", settings.size_chart_text)
    text_html = await db.get_setting("size_chart_html", "")
    await send_size_chart_content(message, text, reply_markup=back_home_markup(), rich_html=(text_html if text_html else None))


@router.message(UIButtonText("🗑 Мои данные"))
async def privacy_menu(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Запросить удаление / анонимизацию", callback_data="privacy:request")],
        [InlineKeyboardButton(text="📄 Политика конфиденциальности", callback_data="legal:privacy")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:home")],
    ])
    await message.answer("🛡 Здесь можно создать запрос на удаление/анонимизацию персональных данных.", reply_markup=kb)


@router.callback_query(F.data == "privacy:request")
async def privacy_request(c: CallbackQuery):
    await db.request_privacy(c.from_user.id); await c.answer("Запрос создан ✅", show_alert=True)
    await c.message.answer("Запрос передан владельцу магазина. Если есть незавершённые заказы, удаление может быть отложено до их исполнения.")


@router.callback_query(F.data == "profile:referral")
async def referral_cb(c: CallbackQuery, bot: Bot):
    await c.answer()
    username = settings.bot_username
    if not username:
        try:
            username = (await bot.get_me()).username or ""
        except Exception:
            username = ""
    if not username:
        await render_screen(c.message, "Username бота не удалось определить.", back_home_markup("⬅️ Профиль")); return
    link = f"https://t.me/{username}?start=ref_{c.from_user.id}"
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Ещё", callback_data="profile:more")]])
    await render_screen(c.message, "🎁 <b>Реферальная программа</b>\n\n"
        f"После первой оплаченной покупки друга вы получите <b>{settings.referral_bonus_points} бонусов</b>.\n\n<code>{link}</code>", kb)


@router.callback_query(F.data == "profile:support")
async def support_cb(c: CallbackQuery):
    await c.answer()
    rows=[]
    if settings.shop_username:
        rows.append([InlineKeyboardButton(text="💬 Написать продавцу", url=f"https://t.me/{settings.shop_username}")])
    rows.append([InlineKeyboardButton(text="⬅️ Ещё", callback_data="profile:more")])
    text = "☎️ <b>Поддержка</b>\n\nНапишите продавцу по вопросам товара, оплаты, доставки или возврата."
    if not settings.shop_username:
        text += f"\n\nКонтакт: {html.escape(settings.support_contact or settings.seller_email)}"
    await render_screen(c.message, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "profile:size")
async def size_chart_cb(c: CallbackQuery):
    await c.answer()
    text = await db.get_setting("size_chart", settings.size_chart_text)
    text_html = await db.get_setting("size_chart_html", "")
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Ещё", callback_data="profile:more")]])
    await send_size_chart_content(c.message, text, reply_markup=kb, rich_html=(text_html if text_html else None))


@router.callback_query(F.data == "profile:privacy")
async def privacy_menu_cb(c: CallbackQuery):
    await c.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Запросить удаление / анонимизацию", callback_data="privacy:request")],
        [InlineKeyboardButton(text="📄 Политика конфиденциальности", callback_data="legal:privacy")],
        [InlineKeyboardButton(text="⬅️ Ещё", callback_data="profile:more")],
    ])
    await render_screen(c.message, "🛡 Здесь можно управлять персональными данными.", kb)


@router.callback_query(F.data == "profile:reviews")
async def reviews_public_cb(c: CallbackQuery):
    await c.answer()
    url=await configured_review_url()
    if not url:
        await render_screen(c.message, "⭐ Раздел отзывов пока не настроен.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Ещё",callback_data="profile:more")]])); return
    await render_screen(c.message, "⭐ <b>Отзывы покупателей</b>", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Открыть отзывы", url=url)],
        [InlineKeyboardButton(text="⬅️ Ещё", callback_data="profile:more")],
    ]))


@router.message(Command("cancel"))
@router.message(UIButtonText("❌ Отмена"))
async def cancel_early(m:Message,state:FSMContext):
    await state.clear()
    await m.answer("Действие отменено.",reply_markup=main_menu(await is_admin(m.from_user.id)))


@router.message(UIButtonText("⬅️ Назад"))
async def universal_back_message(m: Message, state: FSMContext):
    await handle_universal_back(m, state)


async def _render_add_product_back(message: Message, state: FSMContext, current: str, data: dict[str, Any]) -> bool:
    if current == "AdminAddProduct:category":
        await state.clear(); await open_admin(message); return True
    if current == "AdminAddProduct:name":
        # If category was preselected from a category card, going further back returns to admin products.
        if data.get("category_locked"):
            await state.clear(); await open_admin(message); return True
        await state.set_state(AdminAddProduct.category)
        await message.answer(f"1/8 Категория товара:\nТекущее значение: <b>{html.escape(str(data.get('category','') or '—'))}</b>", reply_markup=cancel_keyboard()); return True
    if current == "AdminAddProduct:price":
        await state.set_state(AdminAddProduct.name); current_name=data.get('name_html') or f"<b>{html.escape(str(data.get('name','') or '—'))}</b>"; await message.answer(f"2/8 Название (форматирование сохранится):\nТекущее значение: {current_name}", reply_markup=cancel_keyboard()); return True
    if current == "AdminAddProduct:description":
        await state.set_state(AdminAddProduct.price); await message.answer(f"3/8 Цена, ₽:\nТекущее значение: <b>{html.escape(str(data.get('price','') or '—'))}</b>", reply_markup=cancel_keyboard()); return True
    if current == "AdminAddProduct:weight":
        await state.set_state(AdminAddProduct.description); current_desc=data.get('description_html') or html.escape(str(data.get('description','') or '—')); await message.answer(f"4/8 Описание:\nТекущее значение:\n{current_desc}", reply_markup=cancel_keyboard()); return True
    if current == "AdminAddProduct:color_mode":
        await state.set_state(AdminAddProduct.weight); await message.answer(f"5/8 Вес одной вещи в граммах (например {settings.default_product_weight}):\nТекущее значение: <b>{html.escape(str(data.get('weight','') or '—'))}</b>", reply_markup=cancel_keyboard()); return True
    if current == "AdminAddProduct:single_color":
        await state.set_state(AdminAddProduct.color_mode); await message.answer("6/8 🎨 Выберите режим цветов:", reply_markup=color_mode_keyboard("addmode")); return True
    if current == "AdminAddProduct:variants":
        if data.get("color_mode") == "single":
            await state.set_state(AdminAddProduct.single_color); await message.answer(f"7/8 Введите цвет товара.\nТекущее значение: <b>{html.escape(str(data.get('single_color','') or '—'))}</b>", reply_markup=cancel_keyboard())
        else:
            await state.set_state(AdminAddProduct.color_mode); await message.answer("6/8 🎨 Выберите режим цветов:", reply_markup=color_mode_keyboard("addmode"))
        return True
    if current == "AdminAddProduct:photo":
        await state.set_state(AdminAddProduct.variants)
        await message.answer("⬅️ Вернулись к размерам и остаткам. Введите исправленный список — после этого шаг мультимедиа начнётся заново.", reply_markup=cancel_keyboard()); return True
    return False


async def handle_universal_back(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    data = await state.get_data()

    if current and current.startswith("Checkout:"):
        if current == "Checkout:recipient_full_name" and not data.get("checkout_has_saved_profiles"):
            await state.clear(); await show_cart(message, message.from_user.id); return
        target = await checkout_back_target(state)
        if target:
            await checkout_render_step(message, state, target)
            return
        await state.clear(); await show_cart(message, message.from_user.id); return


    if current and current.startswith("AdminAddProduct:"):
        if await _render_add_product_back(message, state, current, data): return

    if current == "AdminPromo:code":
        await state.clear(); await open_admin(message); return
    if current == "AdminPromo:percent":
        await state.set_state(AdminPromo.code); await message.answer(f"Код промокода:\nТекущее значение: <code>{html.escape(str(data.get('code','') or '—'))}</code>", reply_markup=cancel_keyboard()); return
    if current == "AdminPromo:min_order":
        await state.set_state(AdminPromo.percent); await message.answer(f"Скидка в процентах (1–90):\nТекущее значение: <b>{html.escape(str(data.get('percent','') or '—'))}</b>", reply_markup=cancel_keyboard()); return
    if current == "AdminPromo:max_uses":
        await state.set_state(AdminPromo.min_order); await message.answer(f"Минимальная сумма заказа, ₽ (0 — без ограничения):\nТекущее значение: <b>{html.escape(str(data.get('min_order','') or '0'))}</b>", reply_markup=cancel_keyboard()); return

    if current == "AdminAddAdmin:role":
        await state.set_state(AdminAddAdmin.user_id); await message.answer(f"Введите Telegram ID сотрудника:\nТекущее значение: <code>{html.escape(str(data.get('user_id','') or '—'))}</code>", reply_markup=cancel_keyboard()); return

    if current == "AdminBroadcast:confirm":
        await state.set_state(AdminBroadcast.waiting_message)
        await message.answer("⬅️ Отправьте исправленное сообщение для рассылки. Старый предпросмотр не будет отправлен.", reply_markup=cancel_keyboard()); return

    if current == "ReceiptUpload:waiting":
        oid = int(data.get("order_id") or 0)
        await state.clear()
        order = await db.order(oid) if oid else None
        if order and order["user_id"] == message.from_user.id:
            await send_payment_choice(message, oid, int(order["total"])); return
        await send_home(message); return

    if current in {"AdminTracking:waiting_track", "AdminNote:text"}:
        oid = int(data.get("order_id") or 0); queue = str(data.get("queue") or "active"); await state.clear()
        if oid: await admin_order_card(message, oid, queue)
        else: await open_admin(message)
        return

    if current in {"AdminEditValue:value", "AdminVariantEdit:text", "AdminAddPhoto:color", "AdminAddPhoto:photo"}:
        pid = int(data.get("product_id") or 0); await state.clear()
        if pid: await admin_product_card(message, pid)
        else: await open_admin(message)
        return

    if current in {"AdminContentEdit:text", "AdminContentEdit:media"}:
        kind = str(data.get("content_kind") or ""); await state.clear()
        if kind: await show_admin_content(message, kind)
        else: await open_admin(message)
        return

    if current in {"AdminButtonEdit:text", "AdminButtonEdit:emoji"}:
        button_id = int(data.get("ui_button_id") or 0)
        group = str(data.get("ui_button_group") or "all")
        page = int(data.get("ui_button_page") or 0)
        await state.clear()
        if button_id: await render_ui_button_item(message, button_id, group, page)
        else: await render_ui_button_groups(message)
        return

    if current in {"AdminPremiumEmoji:waiting", "AdminPremiumEmoji:pack_link", "AdminPremiumEmoji:search_query"}:
        await state.clear(); await render_premium_emoji_panel(message); return

    if current in {"AdminPremiumEmoji:placement_target", "AdminPremiumEmoji:global_replace_target"}:
        item_id = int(data.get("premium_pack_item_id") or 0)
        page = int(data.get("premium_pack_page") or 0)
        await state.clear()
        if item_id:
            await render_premium_emoji_pack_item(message, item_id, page, admin_id=message.from_user.id)
        else:
            await render_premium_emoji_panel(message)
        return

    if current == "AdminRequiredChannel:channel":
        await state.clear(); await send_admin_subscription_panel(message); return

    if current in {"AdminReviewLink:url", "AdminBonus:amount", "AdminAddAdmin:user_id", "AdminBroadcast:waiting_message"}:
        await state.clear(); await open_admin(message); return

    if current:
        await state.clear()
        await message.answer("⬅️ Возврат к предыдущему безопасному экрану.")
        if await is_admin(message.from_user.id): await open_admin(message)
        else: await send_home(message)
        return

    await send_home(message)


@router.callback_query(F.data == "nav:fsm_back")
async def universal_back_callback(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await handle_universal_back(c.message, state)


@router.callback_query(F.data == "nav:home")
async def nav_home(c: CallbackQuery, state: FSMContext):
    await state.clear(); await c.answer(); await send_home(c.message, c.from_user)


@router.callback_query(F.data == "nav:myorders")
async def nav_myorders(c: CallbackQuery):
    await c.answer()
    rows = await db.user_orders(c.from_user.id, 15)
    if not rows:
        await c.message.answer("У вас пока нет заказов.", reply_markup=back_home_markup()); return
    buttons = [[InlineKeyboardButton(text=f"{order_ref(o)} · {money(o['total'])} · {o['status']}"[:60], callback_data=f"myorder:{o['id']}")] for o in rows]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:home")])
    await render_screen(c.message, "📦 <b>Мои заказы</b>", InlineKeyboardMarkup(inline_keyboard=buttons))


# -----------------------------
# PRODUCT CATALOG
# -----------------------------
async def send_catalog(message: Message):
    cats = await db.category_records()
    rows = [[InlineKeyboardButton(text=f"👕 {cat['name']}", callback_data=f"catid:{cat['id']}")] for cat in cats]
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="nav:home")])
    text = "🛍 <b>Каталог</b>\nВыберите категорию:" if cats else "🛍 <b>Каталог</b>\nТоваров пока нет."
    await render_screen(message, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(UIButtonText("🛍 Каталог"))
async def catalog_msg(message: Message): await send_catalog(message)


@router.callback_query(F.data == "catalog")
async def catalog_cb(c: CallbackQuery): await c.answer(); await send_catalog(c.message)


async def products_keyboard(rows, back_data: str = "catalog", back_text: str = "⬅️ Назад") -> InlineKeyboardMarkup:
    buttons = []
    for p in rows:
        stock = await db.total_stock(p["id"])
        badges = ("✨" if p["is_new"] else "") + ("🔥" if p["is_hit"] else "")
        status_icon = "🟠" if product_status_value(p) == "preorder" else "🟢"
        label = f"{status_icon} {badges} {p['name']} — {money(p['price'])}".strip()
        if stock <= 0: label += " · нет в наличии"
        buttons.append([InlineKeyboardButton(text=label[:60], callback_data=f"product:{p['id']}")])
    buttons.append([InlineKeyboardButton(text=back_text, callback_data=back_data)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def send_category_screen(message: Message, category_id: int):
    cat = await db.category_record(category_id)
    if not cat:
        await message.answer("Категория не найдена.")
        return
    products = await db.products("category=?", (cat["name"],), 50)
    markup = await products_keyboard(products, "catalog", "⬅️ К каталогу")
    media = await db.category_media(category_id)
    title = f"👕 <b>{html.escape(cat['name'])}</b>"
    if not media:
        await render_screen(message, title, markup)
        return

    # Одно фото/видео можно показать вместе с кнопками товаров.
    if len(media) == 1:
        item = media[0]
        try:
            await send_stored_media(message, item["media_type"], item["file_id"], caption=title, reply_markup=markup)
            return
        except Exception:
            logging.exception("failed to send category media")
            await render_screen(message, title, markup)
            return

    # Telegram media group поддерживает максимум 10 фото/видео за раз.
    for start in range(0, len(media), 10):
        chunk = media[start:start + 10]
        caption = title if start == 0 else ""
        if len(chunk) == 1:
            item = chunk[0]
            try:
                await send_stored_media(message, item["media_type"], item["file_id"], caption=caption or None)
            except Exception:
                logging.exception("failed to send category media tail")
        else:
            await send_photo_album(message, chunk, caption)
    await message.answer(f"{title}\nВыберите товар:", reply_markup=markup)


@router.callback_query(F.data.startswith("catid:"))
async def catid_cb(c: CallbackQuery):
    await c.answer()
    await send_category_screen(c.message, int(c.data.split(":", 1)[1]))


@router.callback_query(F.data.startswith("cat:"))
async def cat_cb(c: CallbackQuery):
    # Совместимость со старыми сообщениями/кнопками, где в callback хранилось название категории.
    await c.answer(); cat_name = c.data.split(":",1)[1]
    cat = await db.category_by_name(cat_name)
    if not cat:
        await c.message.answer("Категория не найдена.")
        return
    await send_category_screen(c.message, int(cat["id"]))


@router.message(UIButtonText("✨ Новинки"))
async def new_products(message: Message):
    rows=await db.products("is_new=1",(),30); await message.answer("✨ <b>Новинки</b>",reply_markup=await products_keyboard(rows,"nav:home"))
@router.message(UIButtonText("🔥 Хиты"))
async def hit_products(message: Message):
    rows=await db.products("is_hit=1",(),30); await message.answer("🔥 <b>Хиты продаж</b>",reply_markup=await products_keyboard(rows,"nav:home"))
@router.message(UIButtonText("🏷 Скидки"))
async def discount_products(message: Message):
    rows=await db.products("old_price>price AND old_price>0",(),30); await message.answer("🏷 <b>Товары со скидкой</b>",reply_markup=await products_keyboard(rows,"nav:home"))


async def send_photo_album(message: Message, photos, caption: str = "", reply_markup=None) -> bool:
    """Send stored product/category media without ever losing the media because of caption markup.

    Telegram can reject the whole media group when a caption is too long or contains
    formatting that is not accepted for a media caption. In that case we retry the
    same photos/videos *without* a caption and put the text + buttons in a normal
    message. This is intentionally a fallback only: when Telegram accepts the
    caption, the existing compact product-card behaviour is preserved.
    """
    items=[]; seen=set()
    for item in photos:
        if isinstance(item,dict) or hasattr(item,"keys"):
            file_id=item["file_id"]
            media_type=(item["media_type"] if "media_type" in item.keys() else "photo") or "photo"
        else:
            file_id=str(item); media_type="photo"
        media_type=str(media_type).lower()
        if media_type not in {"photo","video"}:
            continue
        key=(media_type,file_id)
        if not file_id or key in seen:
            continue
        seen.add(key); items.append((media_type,file_id))
    if not items:
        return False

    async def _send_media_only() -> list[Message]:
        """Retry helper: send every photo/video with no caption and never delete it."""
        result=[]
        for start in range(0,len(items),10):
            chunk=items[start:start+10]
            if len(chunk)==1:
                media_type,file_id=chunk[0]
                result.append(await send_stored_media(message,media_type,file_id))
                continue
            group=[]
            for media_type,file_id in chunk:
                group.append(InputMediaVideo(media=file_id) if media_type=="video" else InputMediaPhoto(media=file_id))
            result.extend(await message.answer_media_group(media=group))
        return result

    # A raw HTML string can be longer than Telegram's caption limit even when the
    # visible text is shorter. Do not suppress the photos in this case: send the
    # gallery first and the formatted card as a normal message.
    if caption and len(caption) > 1024:
        try:
            await _send_media_only()
            await message.answer(caption,reply_markup=reply_markup)
            return True
        except Exception:
            logging.exception("failed to send product media with long-caption fallback")
            return False

    sent_messages=[]
    card_message=None
    try:
        if len(items) == 1:
            media_type,file_id=items[0]
            try:
                await send_stored_media(message,media_type,file_id,caption=caption or None,reply_markup=reply_markup)
                return True
            except Exception:
                # Most often this is a caption/entity problem. Retry the image/video
                # itself so the customer still sees the product media.
                logging.exception("failed to send single product media with caption; retrying without caption")
                await send_stored_media(message,media_type,file_id)
                if caption:
                    await message.answer(caption,reply_markup=reply_markup)
                elif reply_markup:
                    await message.answer("Выберите параметры:",reply_markup=reply_markup)
                return True

        first=True
        # Telegram media groups support up to 10 items. A tail of one item is sent normally.
        for start in range(0,len(items),10):
            chunk=items[start:start+10]
            if len(chunk)==1:
                media_type,file_id=chunk[0]
                sent=await send_stored_media(message,media_type,file_id,caption=(caption if first and caption else None))
                sent_messages.append(sent)
                if first:
                    card_message=sent
                first=False
                continue
            media=[]
            for media_type,file_id in chunk:
                media.append(InputMediaVideo(media=file_id) if media_type=="video" else InputMediaPhoto(media=file_id))
            if first and caption:
                media[0].caption=caption
                media[0].parse_mode="HTML"
            sent=await message.answer_media_group(media=media)
            sent_messages.extend(sent)
            if first and sent:
                card_message=sent[0]
            first=False

        if reply_markup and card_message:
            try:
                await card_message.edit_reply_markup(reply_markup=reply_markup)
            except Exception:
                logging.exception("failed to attach keyboard to product card message")
                fallback_text="Выберите параметры:"
                if caption:
                    nonempty_lines=[line.strip() for line in caption.splitlines() if line.strip()]
                    if nonempty_lines and nonempty_lines[-1] in {"Выберите параметры:","Выберите размер:"}:
                        fallback_text=nonempty_lines[-1]
                await message.answer(fallback_text,reply_markup=reply_markup)
        return True

    except Exception:
        logging.exception("failed to send product album with caption")

        # If Telegram rejected the group before sending any media (for example due
        # to caption HTML/entities), retry the exact same gallery without a caption.
        # This fixes the case where the customer previously received only text.
        if not sent_messages:
            try:
                await _send_media_only()
                if caption:
                    await message.answer(caption,reply_markup=reply_markup)
                elif reply_markup:
                    await message.answer("Выберите параметры:",reply_markup=reply_markup)
                return True
            except Exception:
                logging.exception("failed to retry product album without caption")
                return False

        # Part of the album is already visible. Never delete it; only restore controls.
        if reply_markup:
            fallback_text="Выберите параметры:"
            if caption:
                nonempty_lines=[line.strip() for line in caption.splitlines() if line.strip()]
                if nonempty_lines and nonempty_lines[-1] in {"Выберите параметры:","Выберите размер:"}:
                    fallback_text=nonempty_lines[-1]
            try:
                await message.answer(fallback_text,reply_markup=reply_markup)
            except Exception:
                logging.exception("failed to send fallback controls after partial album")
        return True



def message_rich_html(message: Message) -> str:
    """Return Telegram text as safe HTML while preserving message entities/formatting."""
    plain = (message.text or "").strip()
    rich = getattr(message, "html_text", None)
    if callable(rich):
        rich = rich()
    if not rich:
        return html.escape(plain)
    return str(rich).strip()


def stored_product_description_html(product) -> str:
    """Render new rich descriptions as HTML and legacy descriptions as escaped plain text."""
    try:
        if "description_html" in product.keys() and product["description_html"]:
            return str(product["description_html"])
    except Exception:
        logging.debug("Could not read rich product description", exc_info=True)
    try:
        return html.escape(str(product["description"] or ""))
    except Exception:
        return ""

def stored_product_name_html(product) -> str:
    """Keep product-name formatting for cards while retaining a plain name for buttons/orders."""
    try:
        plain = str(product["name"] or "")
    except Exception:
        plain = ""
    escaped = html.escape(plain)
    try:
        rich = str(product["name_html"] or "") if "name_html" in product.keys() else ""
    except Exception:
        rich = ""
    if not rich or rich == escaped:
        return f"<b>{escaped}</b>"
    return rich


def product_status_value(product) -> str:
    try:
        value = str(product["availability_status"] or "in_stock")
    except Exception:
        value = "in_stock"
    return value if value in {"in_stock", "preorder"} else "in_stock"


def product_status_badge_html(product) -> str:
    return "🟠 <b>ПРЕДЗАКАЗ</b>" if product_status_value(product) == "preorder" else "🟢 <b>В НАЛИЧИИ</b>"


def product_status_short(product) -> str:
    return "🟠 ПРЕДЗАКАЗ" if product_status_value(product) == "preorder" else "🟢 В НАЛИЧИИ"

def unique_variant_colors(vars_):
    colors=[]
    for v in vars_:
        if v["color"] not in colors:colors.append(v["color"])
    return colors


async def photos_for_color(product_id:int,color:str):
    exact=await db.photos(product_id,color)
    if exact:return exact
    return await db.photos(product_id,"")


async def send_product(message: Message, product_id: int):
    p = await db.product(product_id)
    if not p:
        await message.answer("Товар не найден."); return
    vars_ = await db.customer_variants(product_id); photos = await db.product_media(product_id)
    colors = unique_variant_colors(vars_)
    badges=[]
    if p["is_new"]: badges.append("✨ Новинка")
    if p["is_hit"]: badges.append("🔥 Хит")
    stock=sum(int(v["available_stock"] or 0) for v in vars_)
    stock_label = "Доступно к предзаказу" if product_status_value(p) == "preorder" else "Всего в наличии"
    text=(
        product_status_badge_html(p)+"\n"+
        (" · ".join(badges)+"\n" if badges else "")+
        f"{stored_product_name_html(p)}\n\n{stored_product_description_html(p)}\n\n"
        f"💰 {price_text(p)}\n📦 {stock_label}: <b>{stock}</b>\n\n"
        "Выберите параметры:"
    )
    rows=[]
    if len(colors)>1:
        for i in range(0,len(colors),4):
            rows.append([InlineKeyboardButton(text=f"🎨 {colors[j]}",callback_data=f"pcolor:{product_id}:{j}") for j in range(i,min(i+4,len(colors)))])
    else:
        for v in vars_:
            available=int(v['available_stock'] or 0)
            label=f"{v['size']} · {available} шт."
            cb=f"variant:{v['id']}" if available>0 else f"watch:{v['id']}"
            rows.append([InlineKeyboardButton(text=("📏 " if available>0 else "🔔 ")+label,callback_data=cb)])
    rows.append([InlineKeyboardButton(text="📏 Размерная сетка", callback_data=f"productsize:p:{product_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ К категории", callback_data=f"cat:{p['category']}")])
    kb=InlineKeyboardMarkup(inline_keyboard=rows)
    sent=False
    if photos:
        sent=await send_photo_album(message,photos,text,reply_markup=kb)
    elif p["photo_url"] and len(text)<=1024:
        try:
            await message.answer_photo(photo=p["photo_url"],caption=text,reply_markup=kb); sent=True
        except Exception:
            logging.exception("failed to send product photo")
    if not sent:
        await message.answer(text,reply_markup=kb)


@router.callback_query(F.data.startswith("productsize:"))
async def product_size_chart_cb(c: CallbackQuery):
    """Show the size chart from a product flow and return the buyer to the same step."""
    parts = c.data.split(":")
    if len(parts) < 3:
        await c.answer("Не удалось открыть размерную сетку", show_alert=True)
        return

    source = parts[1]
    back_data = "catalog"
    back_text = "⬅️ К товару"

    try:
        if source == "p":
            product_id = int(parts[2])
            back_data = f"product:{product_id}"
        elif source == "c" and len(parts) >= 4:
            product_id = int(parts[2])
            color_index = int(parts[3])
            back_data = f"pcolor:{product_id}:{color_index}"
            back_text = "⬅️ К выбору размера"
        elif source == "v":
            variant_id = int(parts[2])
            back_data = f"variant:{variant_id}"
            back_text = "⬅️ К выбранному размеру"
        else:
            raise ValueError("unknown product size-chart source")
    except (TypeError, ValueError):
        await c.answer("Не удалось открыть размерную сетку", show_alert=True)
        return

    await c.answer()
    text = await db.get_setting("size_chart", settings.size_chart_text)
    text_html = await db.get_setting("size_chart_html", "")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=back_text, callback_data=back_data)]
    ])
    await send_size_chart_content(
        c.message,
        text,
        reply_markup=kb,
        rich_html=(text_html if text_html else None),
    )


@router.callback_query(F.data.startswith("product:"))
async def product_cb(c: CallbackQuery): await c.answer(); await send_product(c.message,int(c.data.split(":")[1]))


@router.callback_query(F.data.startswith("pcolor:"))
async def color_cb(c: CallbackQuery):
    _,pid,idx=c.data.split(":"); pid_i=int(pid); vars_=await db.customer_variants(pid_i); colors=unique_variant_colors(vars_)
    try: color=colors[int(idx)]
    except Exception: await c.answer("Цвет не найден",show_alert=True);return
    rows=[]
    for v in vars_:
        if v["color"]!=color:continue
        available=int(v['available_stock'] or 0)
        cb=f"variant:{v['id']}" if available>0 else f"watch:{v['id']}"
        rows.append([InlineKeyboardButton(text=("📏 " if available>0 else "🔔 ")+f"{v['size']} · {available} шт.",callback_data=cb)])
    rows.append([InlineKeyboardButton(text="📏 Размерная сетка", callback_data=f"productsize:c:{pid}:{idx}")])
    rows.append([InlineKeyboardButton(text="⬅️ К товару",callback_data=f"product:{pid}")])
    p=await db.product(pid_i); color_photos=await photos_for_color(pid_i,color)
    await c.answer()
    kb=InlineKeyboardMarkup(inline_keyboard=rows)
    text=(
        f"{product_status_badge_html(p)}\n"
        f"{stored_product_name_html(p)} · {html.escape(color)}\n"
        f"🎨 Цвет: <b>{html.escape(color)}</b>\n"
        "Выберите размер:"
    )
    sent=False
    if color_photos:
        sent=await send_photo_album(c.message,color_photos,text,reply_markup=kb)
    if not sent:
        await c.message.answer(text,reply_markup=kb)


@router.callback_query(F.data.startswith("variant:"))
async def variant_cb(c: CallbackQuery):
    v=await db.customer_variant(int(c.data.split(":")[1]))
    if not v or int(v["available_stock"] or 0)<=0: await c.answer("Вариант закончился",show_alert=True); return
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Добавить в корзину",callback_data=f"addv:{v['id']}")],
        [InlineKeyboardButton(text="📏 Размерная сетка", callback_data=f"productsize:v:{v['id']}")],
        [InlineKeyboardButton(text="⬅️ К товару", callback_data=f"product:{v['product_id']}")],
    ])
    status_line = product_status_badge_html(v)
    availability_line = "Доступно к предзаказу" if product_status_value(v) == "preorder" else "В наличии"
    await c.answer(); await c.message.answer(
        f"{status_line}\n{stored_product_name_html(v)}\n🎨 {html.escape(v['color'])} · 📏 {html.escape(v['size'])}\n"
        f"💰 {money(v['price'])}\n{availability_line}: {int(v['available_stock'] or 0)} шт.", reply_markup=kb
    )


@router.callback_query(F.data.startswith("addv:"))
async def addv_cb(c: CallbackQuery):
    ok,text=await db.add_cart(c.from_user.id,int(c.data.split(":")[1])); await c.answer(text,show_alert=not ok)
    if ok:
        v=await db.variant(int(c.data.split(":")[1]))
        rows=[[InlineKeyboardButton(text="🛒 Открыть корзину",callback_data="cart")]]
        if v: rows.append([InlineKeyboardButton(text="⬅️ К товару", callback_data=f"product:{v['product_id']}")])
        await c.message.answer("✅ Добавлено в корзину.",reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("watch:"))
async def watch_cb(c: CallbackQuery):
    vid=int(c.data.split(":")[1]); await db.watch_restock(c.from_user.id,vid); await c.answer("Сообщим, когда появится ✅",show_alert=True)



# -----------------------------
# CART / CHECKOUT
# -----------------------------
async def show_cart(message: Message, user_id: int, edit=False):
    items=await db.cart(user_id)
    if not items:
        text="🛒 <b>Корзина пуста</b>"; kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛍 В каталог",callback_data="catalog")],[InlineKeyboardButton(text="⬅️ Назад",callback_data="nav:home")]])
    else:
        total=0; lines=["🛒 <b>Корзина</b>\n"]; rows=[]
        for i in items:
            subtotal=i["price"]*i["qty"];total+=subtotal
            lines.append(f"• <b>{html.escape(i['name'])}</b> · {html.escape(i['color'])} / {html.escape(i['size'])}\n  {i['qty']} шт. · {money(subtotal)}")
            rows.append([InlineKeyboardButton(text="➖",callback_data=f"cartdec:{i['cart_id']}"),InlineKeyboardButton(text=str(i["qty"]),callback_data="noop"),InlineKeyboardButton(text="➕",callback_data=f"cartinc:{i['cart_id']}"),InlineKeyboardButton(text="🗑",callback_data=f"cartdel:{i['cart_id']}")])
        lines.append(f"\nТовары: <b>{money(total)}</b>");text="\n".join(lines)
        rows.append([InlineKeyboardButton(text="✅ Оформить заказ",callback_data="checkout")]); rows.append([InlineKeyboardButton(text="⬅️ Назад",callback_data="nav:home")]); kb=InlineKeyboardMarkup(inline_keyboard=rows)
    if edit:
        try: await message.edit_text(text,reply_markup=kb);return
        except Exception: logging.exception("Suppressed exception")
    await message.answer(text,reply_markup=kb)


@router.message(UIButtonText("🛒 Корзина"))
async def cart_msg(message:Message):await show_cart(message,message.from_user.id)
@router.callback_query(F.data=="cart")
async def cart_cb(c:CallbackQuery):await c.answer();await show_cart(c.message,c.from_user.id,True)
@router.callback_query(F.data=="noop")
async def noop(c:CallbackQuery):await c.answer()
@router.callback_query(F.data.startswith("cartinc:"))
async def cart_inc(c:CallbackQuery):
    ok,t=await db.cart_qty(c.from_user.id,int(c.data.split(":")[1]),1);await c.answer(t,show_alert=not ok)
    if ok:await show_cart(c.message,c.from_user.id,True)
@router.callback_query(F.data.startswith("cartdec:"))
async def cart_dec(c:CallbackQuery):
    ok,t=await db.cart_qty(c.from_user.id,int(c.data.split(":")[1]),-1);await c.answer(t)
    if ok:await show_cart(c.message,c.from_user.id,True)
@router.callback_query(F.data.startswith("cartdel:"))
async def cart_del(c:CallbackQuery):await db.cart_delete(c.from_user.id,int(c.data.split(":")[1]));await c.answer("Удалено");await show_cart(c.message,c.from_user.id,True)


def delivery_profile_label(profile) -> str:
    data={field:(profile[field] if field in profile.keys() else '') for field in db.DELIVERY_PROFILE_FIELDS}
    method='📮 Почта' if data.get('delivery_method')=='Почта России' else '📦 СДЭК'
    recipient=(data.get('recipient_full_name') or 'Получатель').strip()
    address=delivery_address(data)
    return f"{method} · {recipient} · {address}"[:64]


def checkout_back_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⬅️ Назад")], [KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


def checkout_phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Отправить номер", request_contact=True)],
            [KeyboardButton(text="⬅️ Назад")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def checkout_optional_keyboard(text: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text)], [KeyboardButton(text="⬅️ Назад")], [KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def checkout_back_target(state:FSMContext) -> str|None:
    current=await state.get_state()
    if not current or not current.startswith("Checkout:"):
        return None
    name=current.split(":",1)[1]
    data=await state.get_data()
    targets={
        "recipient_full_name":"saved_profile",
        "phone":"recipient_full_name",
        "postal_code":"delivery_method",
        "region":"postal_code",
        "city":"region",
        "cdek_type":"city",
        "cdek_point":"cdek_type",
        "street":"cdek_type" if data.get("delivery_method")=="СДЭК" else "city",
        "house":"street",
        "building":"house",
        "apartment":"building",
        "delivery_comment":"cdek_point" if data.get("delivery_method")=="СДЭК" and data.get("cdek_type")=="ПВЗ" else "apartment",
        "save_profile":"delivery_comment",
        "confirm":"saved_profile" if data.get("saved_profile_id") else "save_profile",
        "promo":"confirm",
    }
    return targets.get(name)


async def checkout_render_step(message:Message,state:FSMContext,step:str):
    if step=="saved_profile":
        await ask_saved_delivery_profile(message,state,message.from_user.id)
        return
    if step=="recipient_full_name":
        await state.set_state(Checkout.recipient_full_name)
        await message.answer('👤 Введите <b>ФИО получателя полностью</b>:\n\nНапример: <code>Иванов Иван Сергеевич</code>',reply_markup=checkout_back_keyboard())
        return
    if step=="phone":
        await state.set_state(Checkout.phone)
        await message.answer('📱 Телефон получателя:\n\nНапример: <code>+7 999 123-45-67</code>',reply_markup=checkout_phone_keyboard())
        return
    if step=="delivery_method":
        await ask_delivery(message,state)
        return
    if step=="postal_code":
        await state.set_state(Checkout.postal_code)
        await message.answer('🏷 Введите шестизначный почтовый индекс:\n\nНапример: <code>123456</code>',reply_markup=checkout_back_keyboard())
        return
    if step=="region":
        await state.set_state(Checkout.region)
        await message.answer('🗺 Регион / область / край:\n\nНапример: <code>Московская область</code>',reply_markup=checkout_back_keyboard())
        return
    if step=="city":
        await state.set_state(Checkout.city)
        await message.answer('🏙 Город / населённый пункт:\n\nНапример: <code>Москва</code>',reply_markup=checkout_back_keyboard())
        return
    if step=="cdek_type":
        await state.set_state(Checkout.cdek_type)
        kb=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏢 Пункт выдачи",callback_data="cdektype:pickup")],
            [InlineKeyboardButton(text="🚚 Курьер до двери",callback_data="cdektype:courier")],
            [InlineKeyboardButton(text="⬅️ Назад",callback_data="checkout:back")],
        ])
        await message.answer("Как получить СДЭК?",reply_markup=kb)
        return
    if step=="cdek_point":
        await state.set_state(Checkout.cdek_point)
        data=await state.get_data();pts=data.get("cdek_points") or []
        if pts:
            rows=[[InlineKeyboardButton(text=f"{p['code']} · {p['address']}"[:60],callback_data=f"pvz:{i}")] for i,p in enumerate(pts)]
            rows.append([InlineKeyboardButton(text="✍️ Ввести ПВЗ вручную",callback_data="pvz:manual")])
            rows.append([InlineKeyboardButton(text="⬅️ Назад",callback_data="checkout:back")])
            await message.answer("🏢 Выберите ПВЗ СДЭК:",reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        else:
            await message.answer('🏢 Укажите точный адрес ПВЗ СДЭК и, если знаете, его код:\n\nНапример: <code>ул. Примерная, д. 10, ПВЗ MSK123</code>',reply_markup=checkout_back_keyboard())
        return
    if step=="street":
        await state.set_state(Checkout.street)
        await message.answer('🛣 Улица:\n\nНапример: <code>ул. Примерная</code>',reply_markup=checkout_back_keyboard())
        return
    if step=="house":
        await state.set_state(Checkout.house)
        await message.answer('🏠 Номер дома:\n\nНапример: <code>10</code>',reply_markup=checkout_back_keyboard())
        return
    if step=="building":
        await state.set_state(Checkout.building)
        await message.answer('🏢 Корпус/строение или «Нет корпуса»:\n\nНапример: <code>2</code>',reply_markup=checkout_optional_keyboard("➡️ Нет корпуса"))
        return
    if step=="apartment":
        await state.set_state(Checkout.apartment)
        await message.answer('🚪 Квартира/офис или «Нет квартиры»:\n\nНапример: <code>45</code>',reply_markup=checkout_optional_keyboard("➡️ Нет квартиры"))
        return
    if step=="delivery_comment":
        await state.set_state(Checkout.delivery_comment)
        await message.answer('📝 Комментарий к доставке или нажмите «Без комментария»:\n\nНапример: <code>Позвоните за 30 минут до доставки</code>',reply_markup=checkout_optional_keyboard("➡️ Без комментария"))
        return
    if step=="save_profile":
        data=await state.get_data()
        kb=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, сохранить",callback_data="saveprofile:yes")],
            [InlineKeyboardButton(text="➡️ Нет, не сохранять",callback_data="saveprofile:no")],
            [InlineKeyboardButton(text="⬅️ Назад",callback_data="checkout:back")],
            [InlineKeyboardButton(text="❌ Отмена",callback_data="checkout:cancel_saveprofile")],
        ])
        await state.set_state(Checkout.save_profile)
        await message.answer("💾 <b>Сохранить эти данные для следующих заказов?</b>\n\n"+delivery_summary(data)+"\n\nЕсли сохранить, при следующем оформлении этот вариант появится отдельной кнопкой.",reply_markup=kb)
        return
    if step=="confirm":
        await show_checkout_confirm(message,state,message.from_user.id)
        return


@router.callback_query(F.data=="checkout:back")
async def co_back_callback(c:CallbackQuery,state:FSMContext):
    target=await checkout_back_target(state)
    if target is None:
        await c.answer("Назад недоступно")
        return
    await c.answer()
    await checkout_render_step(c.message,state,target)


async def start_new_delivery(message:Message,state:FSMContext):
    await state.update_data(
        saved_profile_id=0,
        save_delivery_profile=False,
        recipient_full_name='',phone='',delivery_method='',postal_code='',region='',city='',
        street='',house='',building='',apartment='',cdek_type='',cdek_point='',delivery_comment='',
        address='',pricing=None,
    )
    await state.set_state(Checkout.recipient_full_name)
    await message.answer('👤 Введите <b>ФИО получателя полностью</b>:\n\nНапример: <code>Иванов Иван Сергеевич</code>',reply_markup=checkout_back_keyboard())


async def ask_saved_delivery_profile(message:Message,state:FSMContext,user_id:int):
    profiles=await db.delivery_profiles(user_id)
    await state.update_data(checkout_has_saved_profiles=bool(profiles))
    if not profiles:
        await start_new_delivery(message,state)
        return
    rows=[]
    for profile in profiles:
        rows.append([InlineKeyboardButton(
            text=delivery_profile_label(profile),
            callback_data=f"deliveryprofile:{profile['id']}",
        )])
    rows.append([InlineKeyboardButton(text="➕ Ввести новые данные доставки",callback_data="deliveryprofile:new")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад в корзину",callback_data="cart")])
    rows.append([InlineKeyboardButton(text="❌ Отмена",callback_data="checkout:cancel_saved")])
    await state.set_state(Checkout.saved_profile)
    await message.answer(
        "🚚 <b>Выберите сохранённые данные доставки</b>\n\n"
        "Можно выбрать готовый вариант кнопкой или добавить новые данные.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data=="checkout")
async def checkout_start(c:CallbackQuery,state:FSMContext):
    ok,err=await db.validate_cart(c.from_user.id)
    if not ok:await c.answer(err,show_alert=True);return
    if not await db.cart(c.from_user.id):await c.answer("Корзина пуста",show_alert=True);return
    await c.answer();await state.clear();await ask_saved_delivery_profile(c.message,state,c.from_user.id)


@router.callback_query(Checkout.saved_profile,F.data=="deliveryprofile:new")
async def co_delivery_profile_new(c:CallbackQuery,state:FSMContext):
    await c.answer();await start_new_delivery(c.message,state)


@router.callback_query(Checkout.saved_profile,F.data.startswith("deliveryprofile:"))
async def co_delivery_profile_select(c:CallbackQuery,state:FSMContext):
    try:profile_id=int(c.data.split(":",1)[1])
    except ValueError:
        await c.answer("Профиль не найден",show_alert=True);return
    profile=await db.delivery_profile(c.from_user.id,profile_id)
    if not profile:
        await c.answer("Сохранённые данные не найдены",show_alert=True);return
    payload={field:(profile[field] or '') for field in db.DELIVERY_PROFILE_FIELDS}
    payload["saved_profile_id"]=profile_id
    payload["save_delivery_profile"]=False
    payload["address"]=delivery_address(payload)
    payload["pricing"]=None
    await state.update_data(**payload)
    await db.touch_delivery_profile(c.from_user.id,profile_id)
    await c.answer("Данные выбраны")
    await show_checkout_confirm(c.message,state,c.from_user.id)


@router.callback_query(Checkout.saved_profile,F.data=="checkout:cancel_saved")
async def co_cancel_saved(c:CallbackQuery,state:FSMContext):
    await state.clear();await c.answer()
    await c.message.answer(
        "Оформление отменено. Товары остались в корзине.",
        reply_markup=main_menu(await is_admin(c.from_user.id)),
    )


@router.message(Checkout.recipient_full_name,F.text)
async def co_name(m:Message,state:FSMContext):
    if len(m.text.strip().split())<2:await m.answer('Укажите хотя бы фамилию и имя.\n\nНапример: <code>Иванов Иван Сергеевич</code>');return
    await state.update_data(recipient_full_name=m.text.strip());await state.set_state(Checkout.phone);await m.answer('📱 Телефон получателя:\n\nНапример: <code>+7 999 123-45-67</code>',reply_markup=checkout_phone_keyboard())


async def ask_delivery(m:Message,state:FSMContext):
    await state.set_state(Checkout.delivery_method);kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📮 Почта России",callback_data="delivery:post")],[InlineKeyboardButton(text="📦 СДЭК",callback_data="delivery:cdek")],[InlineKeyboardButton(text="⬅️ Назад",callback_data="checkout:back")]])
    await m.answer("🚚 Выберите способ получения:",reply_markup=kb)


@router.message(Checkout.phone,F.contact)
async def co_phone_contact(m:Message,state:FSMContext):await state.update_data(phone=m.contact.phone_number);await ask_delivery(m,state)
@router.message(Checkout.phone,F.text)
async def co_phone(m:Message,state:FSMContext):
    if len("".join(x for x in m.text if x.isdigit()))<10:await m.answer('Введите полный номер телефона.\n\nНапример: <code>+7 999 123-45-67</code>');return
    await state.update_data(phone=m.text.strip());await ask_delivery(m,state)


@router.callback_query(Checkout.delivery_method,F.data=="delivery:post")
async def co_post(c:CallbackQuery,state:FSMContext):
    await state.update_data(delivery_method="Почта России",cdek_type="",cdek_point="");await state.set_state(Checkout.postal_code);await c.answer();await c.message.answer('🏷 Введите шестизначный почтовый индекс:\n\nНапример: <code>123456</code>',reply_markup=checkout_back_keyboard())
@router.callback_query(Checkout.delivery_method,F.data=="delivery:cdek")
async def co_cdek(c:CallbackQuery,state:FSMContext):
    await state.update_data(delivery_method="СДЭК",postal_code="");await state.set_state(Checkout.region);await c.answer();await c.message.answer('🗺 Регион / область / край:\n\nНапример: <code>Московская область</code>',reply_markup=cancel_keyboard())


@router.message(Checkout.postal_code,F.text)
async def co_postal(m:Message,state:FSMContext):
    code="".join(x for x in m.text if x.isdigit())
    if len(code)!=6:await m.answer('Индекс должен состоять из 6 цифр.\n\nНапример: <code>123456</code>');return
    await state.update_data(postal_code=code);await state.set_state(Checkout.region);await m.answer('🗺 Регион / область / край:\n\nНапример: <code>Московская область</code>',reply_markup=checkout_back_keyboard())
@router.message(Checkout.region,F.text)
async def co_region(m:Message,state:FSMContext):await state.update_data(region=m.text.strip());await state.set_state(Checkout.city);await m.answer('🏙 Город / населённый пункт:\n\nНапример: <code>Москва</code>',reply_markup=checkout_back_keyboard())


@router.message(Checkout.city,F.text)
async def co_city(m:Message,state:FSMContext):
    await state.update_data(city=m.text.strip());data=await state.get_data()
    if data.get("delivery_method")=="СДЭК":
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏢 Пункт выдачи",callback_data="cdektype:pickup")],[InlineKeyboardButton(text="🚚 Курьер до двери",callback_data="cdektype:courier")],[InlineKeyboardButton(text="⬅️ Назад",callback_data="checkout:back")]])
        await state.set_state(Checkout.cdek_type);await m.answer("Как получить СДЭК?",reply_markup=kb);return
    await state.set_state(Checkout.street);await m.answer('🛣 Улица:\n\nНапример: <code>ул. Примерная</code>',reply_markup=checkout_back_keyboard())


@router.callback_query(Checkout.cdek_type,F.data=="cdektype:pickup")
async def co_pickup(c:CallbackQuery,state:FSMContext):
    await c.answer();data=await state.get_data();await state.update_data(cdek_type="ПВЗ",street="",house="",building="",apartment="")
    pts=[]
    try:pts=await cdek_points(data.get("city",""),data.get("region",""),10)
    except Exception:logging.exception("CDEK PVZ lookup failed")
    if pts:
        await state.update_data(cdek_points=pts);rows=[]
        for i,p in enumerate(pts):rows.append([InlineKeyboardButton(text=f"{p['code']} · {p['address']}"[:60],callback_data=f"pvz:{i}")])
        rows.append([InlineKeyboardButton(text="✍️ Ввести ПВЗ вручную",callback_data="pvz:manual")]);rows.append([InlineKeyboardButton(text="⬅️ Назад",callback_data="checkout:back")]);await state.set_state(Checkout.cdek_point);await c.message.answer("🏢 Выберите ПВЗ СДЭК:",reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    else:
        await state.set_state(Checkout.cdek_point);await c.message.answer('🏢 Укажите точный адрес ПВЗ СДЭК и, если знаете, его код:\n\nНапример: <code>ул. Примерная, д. 10, ПВЗ MSK123</code>',reply_markup=checkout_back_keyboard())


@router.callback_query(Checkout.cdek_point,F.data.startswith("pvz:"))
async def co_pvz_btn(c:CallbackQuery,state:FSMContext):
    val=c.data.split(":",1)[1]
    if val=="manual":await c.answer();await c.message.answer('Введите адрес/код ПВЗ:\n\nНапример: <code>ул. Примерная, д. 10, ПВЗ MSK123</code>',reply_markup=checkout_back_keyboard());return
    data=await state.get_data();pts=data.get("cdek_points",[])
    try:p=pts[int(val)]
    except Exception:await c.answer("ПВЗ не найден",show_alert=True);return
    await state.update_data(cdek_point=f"{p['code']} — {p['address']}");await c.answer();await state.set_state(Checkout.delivery_comment);await c.message.answer('📝 Комментарий к доставке или нажмите «Без комментария»:\n\nНапример: <code>Позвоните за 30 минут до доставки</code>',reply_markup=optional_keyboard("➡️ Без комментария"))


@router.message(Checkout.cdek_point,F.text)
async def co_pvz_text(m:Message,state:FSMContext):
    await state.update_data(cdek_point=m.text.strip());await state.set_state(Checkout.delivery_comment);await m.answer('📝 Комментарий или без комментария:\n\nНапример: <code>Позвоните за 30 минут до доставки</code>',reply_markup=checkout_optional_keyboard("➡️ Без комментария"))


@router.callback_query(Checkout.cdek_type,F.data=="cdektype:courier")
async def co_courier(c:CallbackQuery,state:FSMContext):await c.answer();await state.update_data(cdek_type="Курьер до двери",cdek_point="");await state.set_state(Checkout.street);await c.message.answer('🛣 Улица:\n\nНапример: <code>ул. Примерная</code>',reply_markup=checkout_back_keyboard())
@router.message(Checkout.street,F.text)
async def co_street(m:Message,state:FSMContext):await state.update_data(street=m.text.strip());await state.set_state(Checkout.house);await m.answer('🏠 Номер дома:\n\nНапример: <code>10</code>',reply_markup=checkout_back_keyboard())
@router.message(Checkout.house,F.text)
async def co_house(m:Message,state:FSMContext):await state.update_data(house=m.text.strip());await state.set_state(Checkout.building);await m.answer('🏢 Корпус/строение или «Нет корпуса»:\n\nНапример: <code>2</code>',reply_markup=checkout_optional_keyboard("➡️ Нет корпуса"))
@router.message(Checkout.building,F.text)
async def co_building(m:Message,state:FSMContext):await state.update_data(building="" if ui_text_matches(m.text, "➡️ Нет корпуса") else m.text.strip());await state.set_state(Checkout.apartment);await m.answer('🚪 Квартира/офис или «Нет квартиры»:\n\nНапример: <code>45</code>',reply_markup=checkout_optional_keyboard("➡️ Нет квартиры"))
@router.message(Checkout.apartment,F.text)
async def co_apartment(m:Message,state:FSMContext):await state.update_data(apartment="" if ui_text_matches(m.text, "➡️ Нет квартиры") else m.text.strip());await state.set_state(Checkout.delivery_comment);await m.answer('📝 Комментарий или без комментария:\n\nНапример: <code>Позвоните за 30 минут до доставки</code>',reply_markup=checkout_optional_keyboard("➡️ Без комментария"))


@router.message(Checkout.delivery_comment,F.text)
async def co_comment(m:Message,state:FSMContext):
    await state.update_data(delivery_comment="" if ui_text_matches(m.text, "➡️ Без комментария") else m.text.strip())
    data=await state.get_data();data["address"]=delivery_address(data);await state.update_data(address=data["address"])
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, сохранить",callback_data="saveprofile:yes")],
        [InlineKeyboardButton(text="➡️ Нет, не сохранять",callback_data="saveprofile:no")],
        [InlineKeyboardButton(text="⬅️ Назад",callback_data="checkout:back")],
        [InlineKeyboardButton(text="❌ Отмена",callback_data="checkout:cancel_saveprofile")],
    ])
    await state.set_state(Checkout.save_profile)
    await m.answer(
        "💾 <b>Сохранить эти данные для следующих заказов?</b>\n\n"
        +delivery_summary(data)
        +"\n\nЕсли сохранить, при следующем оформлении этот вариант появится отдельной кнопкой.",
        reply_markup=kb,
    )


@router.callback_query(Checkout.save_profile,F.data=="saveprofile:yes")
async def co_save_profile_yes(c:CallbackQuery,state:FSMContext):
    await state.update_data(save_delivery_profile=True)
    await c.answer("Сохраним после создания заказа")
    await show_checkout_confirm(c.message,state,c.from_user.id)


@router.callback_query(Checkout.save_profile,F.data=="saveprofile:no")
async def co_save_profile_no(c:CallbackQuery,state:FSMContext):
    await state.update_data(save_delivery_profile=False)
    await c.answer("Данные не будут сохранены")
    await show_checkout_confirm(c.message,state,c.from_user.id)


@router.callback_query(Checkout.save_profile,F.data=="checkout:cancel_saveprofile")
async def co_cancel_save_profile(c:CallbackQuery,state:FSMContext):
    await state.clear();await c.answer()
    await c.message.answer(
        "Оформление отменено. Товары остались в корзине.",
        reply_markup=main_menu(await is_admin(c.from_user.id)),
    )


async def show_checkout_confirm(m:Message,state:FSMContext,user_id:int|None=None):
    uid=user_id or m.from_user.id
    data=await state.get_data();data["address"]=delivery_address(data);await state.update_data(address=data["address"])
    try:pricing=await calculate_pricing(uid,data,data.get("promo_code",""),bool(data.get("use_points")))
    except ValueError as e:
        await state.update_data(promo_code="");pricing=await calculate_pricing(uid,data,"",bool(data.get("use_points")));await m.answer(f"⚠️ {html.escape(str(e))}")
    await state.update_data(pricing=pricing)
    balance=await db.bonus_balance(uid)
    rows=[]
    if pricing['promo_code']:
        rows.append([InlineKeyboardButton(text=f"🎟 Промокод: {pricing['promo_code']} ✅",callback_data="checkout:promo")])
        rows.append([InlineKeyboardButton(text="❌ Убрать промокод",callback_data="checkout:promo_clear")])
    elif data.get("use_points"):
        rows.append([InlineKeyboardButton(text=f"🎁 Бонусы выбраны: −{pricing['points_used']}",callback_data="checkout:points")])
        rows.append([InlineKeyboardButton(text="ℹ️ Промокод недоступен при списании бонусов",callback_data="checkout:exclusive_info")])
    else:
        rows.append([InlineKeyboardButton(text="🎟 Ввести промокод",callback_data="checkout:promo")])
        if balance>0:rows.append([InlineKeyboardButton(text=f"🎁 Использовать бонусы ({balance})",callback_data="checkout:points")])
    rows += [[InlineKeyboardButton(text="✅ Создать заказ",callback_data="checkout:create")],[InlineKeyboardButton(text="⬅️ Назад",callback_data="checkout:back")],[InlineKeyboardButton(text="❌ Отмена",callback_data="checkout:cancel")]]
    await state.set_state(Checkout.confirm)
    await m.answer(
        "🧾 <b>Проверьте заказ и доставку</b>\n\n"+delivery_summary(data)+"\n\n"+pricing_text(pricing)+f"\n\n<i>{html.escape(pricing['shipping_note'])}</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(Checkout.confirm,F.data=="checkout:promo")
async def co_promo_btn(c:CallbackQuery,state:FSMContext):
    data=await state.get_data()
    if data.get("use_points"):
        await c.answer("Сначала отключите списание бонусов",show_alert=True);return
    await c.answer();await state.set_state(Checkout.promo);await c.message.answer("🎟 Введите промокод:",reply_markup=checkout_back_keyboard())
@router.message(Checkout.promo,F.text)
async def co_promo_input(m:Message,state:FSMContext):
    await state.update_data(promo_code=m.text.strip(),use_points=False);await show_checkout_confirm(m,state)
@router.callback_query(Checkout.confirm,F.data=="checkout:promo_clear")
async def co_promo_clear(c:CallbackQuery,state:FSMContext):
    await state.update_data(promo_code="",pricing=None);await c.answer("Промокод убран");await show_checkout_confirm(c.message,state,c.from_user.id)
@router.callback_query(Checkout.confirm,F.data=="checkout:exclusive_info")
async def co_exclusive_info(c:CallbackQuery):
    await c.answer("Можно использовать либо промокод, либо бонусы — одновременно нельзя.",show_alert=True)
@router.callback_query(Checkout.confirm,F.data=="checkout:points")
async def co_points(c:CallbackQuery,state:FSMContext):
    data=await state.get_data();turning_on=not bool(data.get("use_points"))
    if turning_on and data.get("promo_code"):
        await c.answer("Сначала уберите промокод",show_alert=True);return
    await state.update_data(use_points=turning_on,promo_code="" if turning_on else data.get("promo_code",""),pricing=None)
    await c.answer();await show_checkout_confirm(c.message,state,c.from_user.id)
@router.callback_query(Checkout.confirm,F.data=="checkout:cancel")
async def co_cancel(c:CallbackQuery,state:FSMContext):await state.clear();await c.answer();await c.message.answer("Оформление отменено. Товары остались в корзине.",reply_markup=main_menu(await is_admin(c.from_user.id)))


@router.callback_query(Checkout.confirm,F.data=="checkout:create")
async def co_create(c:CallbackQuery,state:FSMContext):
    data=await state.get_data();pricing=data.get("pricing") or await calculate_pricing(c.from_user.id,data,data.get("promo_code",""),bool(data.get("use_points")))
    try:oid=await db.create_order(c.from_user.id,c.from_user.username or "",c.from_user.full_name,data,pricing)
    except ValueError as e:await c.answer(str(e),show_alert=True);return
    if not oid:await c.answer("Корзина пуста",show_alert=True);return
    saved_profile_id=0
    if data.get("save_delivery_profile"):
        try:saved_profile_id=await db.save_delivery_profile(c.from_user.id,data)
        except Exception:logging.exception("Could not save reusable delivery profile")
    await state.clear();await c.answer()
    saved_text="\n💾 Данные доставки сохранены для следующих заказов." if saved_profile_id else ""
    created_order = await db.order(oid)
    reserve_text = ""
    if created_order and created_order["reservation_expires_at"]:
        reserve_text = f"\n⏳ Товар зарезервирован до <b>{html.escape(created_order['reservation_expires_at'])}</b>."
    await c.message.answer(
        f"✅ Заказ <b>{order_ref(created_order)}</b> создан.\nИтого к оплате: <b>{money(pricing['total'])}</b>{reserve_text}{saved_text}",
        reply_markup=main_menu(await is_admin(c.from_user.id)),
    )
    await send_payment_choice(c.message,oid,pricing["total"])


async def send_payment_choice(message:Message,order_id:int,total:int):
    rows=[]
    if settings.payment_provider_token:rows.append([InlineKeyboardButton(text="💳 Оплатить онлайн",callback_data=f"payonline:{order_id}")])
    if settings.payment_card:rows.append([InlineKeyboardButton(text="🏦 Перевод по реквизитам",callback_data=f"paymanual:{order_id}")])
    if not rows:
        await message.answer("⚠️ Способ оплаты ещё не настроен владельцем магазина.");return
    rows.append([
        InlineKeyboardButton(text="📦 К заказу", callback_data=f"myorder:{order_id}"),
        InlineKeyboardButton(text="📋 Мои заказы", callback_data="nav:myorders"),
    ])
    await message.answer("💳 Выберите способ оплаты:",reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("paymanual:"))
async def pay_manual(c:CallbackQuery,state:FSMContext):
    oid=int(c.data.split(":")[1]);o=await db.order(oid)
    if not o or o["user_id"]!=c.from_user.id:await c.answer("Заказ не найден",show_alert=True);return
    try:
        await db.ensure_order_reservation(oid, minutes=settings.reservation_minutes)
    except ValueError as exc:
        await c.answer(f"Товар больше недоступен: {exc}", show_alert=True)
        return
    await db.execute("UPDATE orders SET payment_method='manual' WHERE id=?",(oid,))
    # Сразу ждём чек: дополнительная кнопка «Отправить чек» больше не нужна.
    await state.set_state(ReceiptUpload.waiting);await state.update_data(order_id=oid);await c.answer()
    recipient=f"\nПолучатель: <b>{html.escape(settings.payment_recipient)}</b>" if settings.payment_recipient else ""
    await c.message.answer(
        f"🏦 <b>Оплата заказа {order_ref(o)}</b>\n\nПереведите <b>{money(o['total'])}</b> по реквизитам:\n"
        f"<code>{html.escape(settings.payment_card)}</code>{recipient}\n\n"
        "📎 После перевода <b>сразу отправьте сюда</b> фото/скриншот чека или файл — нажимать дополнительную кнопку не нужно.",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(F.data.startswith("payonline:"))
async def pay_online(c:CallbackQuery,bot:Bot):
    oid=int(c.data.split(":")[1]);o=await db.order(oid)
    if not o or o["user_id"]!=c.from_user.id:await c.answer("Заказ не найден",show_alert=True);return
    if not settings.payment_provider_token:await c.answer("Онлайн-оплата не настроена",show_alert=True);return
    try:
        await db.ensure_order_reservation(oid, minutes=settings.reservation_minutes)
    except ValueError as exc:
        await c.answer(f"Товар больше недоступен: {exc}", show_alert=True)
        return
    await c.answer();await bot.send_invoice(chat_id=c.from_user.id,title=f"Заказ {order_ref(o)}",description=f"Одежда · заказ {order_ref(o)}",payload=f"order:{oid}",provider_token=settings.payment_provider_token,currency="RUB",prices=[LabeledPrice(label=f"Заказ {order_ref(o)}",amount=int(o["total"])*100)])


@router.pre_checkout_query()
async def pre_checkout(q:PreCheckoutQuery):
    error_message = "Заказ не найден или уже обработан"
    try:
        oid=int(q.invoice_payload.split(":")[1]);o=await db.order(oid);ok=bool(o and o["user_id"]==q.from_user.id and o["status"] in ("Ожидает оплаты","Чек отклонён"))
        if ok:
            try:
                await db.ensure_order_reservation(oid, minutes=settings.reservation_minutes)
            except ValueError as exc:
                ok=False
                error_message=f"Товар больше недоступен: {exc}"[:200]
    except Exception:
        logging.exception("pre_checkout validation failed")
        ok=False
    await q.answer(ok=ok,error_message=None if ok else error_message)


@router.message(F.successful_payment)
async def successful_payment(m:Message,bot:Bot):
    try:oid=int(m.successful_payment.invoice_payload.split(":")[1])
    except Exception:return
    await db.save_online_payment(oid,m.successful_payment.telegram_payment_charge_id,m.successful_payment.provider_payment_charge_id)
    ok,error=await confirm_paid_order(bot,oid,"telegram",m.from_user.id)
    if ok:
        paid_order=await db.order(oid)
        await m.answer(f"✅ Онлайн-оплата заказа {order_ref(paid_order)} подтверждена. Заказ принят в работу.")
    else:await m.answer(f"⚠️ Платёж получен, но заказ требует ручной проверки: {html.escape(error)}")


@router.callback_query(F.data.startswith("receipt:"))
async def receipt_start(c:CallbackQuery,state:FSMContext):
    oid=int(c.data.split(":")[1]);o=await db.order(oid)
    if not o or o["user_id"]!=c.from_user.id:await c.answer("Заказ не найден",show_alert=True);return
    await state.set_state(ReceiptUpload.waiting);await state.update_data(order_id=oid);await c.answer();await c.message.answer("📎 Отправьте фото/скриншот чека или файл:",reply_markup=cancel_keyboard())


@router.message(ReceiptUpload.waiting,F.photo)
async def receipt_photo(m:Message,state:FSMContext,bot:Bot):await process_receipt(m,state,bot,m.photo[-1].file_id,"photo")
@router.message(ReceiptUpload.waiting,F.document)
async def receipt_doc(m:Message,state:FSMContext,bot:Bot):await process_receipt(m,state,bot,m.document.file_id,"document")


async def process_receipt(m:Message,state:FSMContext,bot:Bot,file_id:str,kind:str):
    data=await state.get_data();oid=data.get("order_id");o=await db.order(oid)
    if not o or o["user_id"]!=m.from_user.id:await state.clear();await m.answer("Заказ не найден.");return
    try:
        await db.save_receipt(oid,file_id,kind)
    except ValueError as exc:
        await state.clear()
        await m.answer(
            f"⚠️ Чек не принят: резерв товара уже истёк, и товар больше недоступен.\n\n{html.escape(str(exc))}",
            reply_markup=customer_order_keyboard(oid),
        )
        return
    await state.clear()
    await m.answer(
        "✅ Чек отправлен. Ожидайте подтверждения администратора.",
        reply_markup=customer_order_keyboard(oid),
    )
    await notify_admin_receipt(bot,oid)


# -----------------------------
# ORDERS / REVIEWS
# -----------------------------
@router.message(UIButtonText("📦 Мои заказы"))
async def my_orders(m:Message):
    rows=await db.user_orders(m.from_user.id,15)
    if not rows:await m.answer("У вас пока нет заказов.");return
    buttons=[]
    for o in rows:
        label=f"{order_ref(o)} · {money(o['total'])} · {o['status']}"
        buttons.append([InlineKeyboardButton(text=label[:60],callback_data=f"myorder:{o['id']}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:home")])
    await m.answer("📦 <b>Мои заказы</b>",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("myorder:"))
async def my_order_card(c:CallbackQuery):
    oid=int(c.data.split(":")[1]);o=await db.order(oid)
    if not o or o["user_id"]!=c.from_user.id:await c.answer("Заказ не найден",show_alert=True);return
    items=await db.order_items(oid);lines=[f"📦 <b>Заказ {order_ref(o)}</b>",f"Статус: <b>{o['status']}</b>"]
    if o["reservation_expires_at"] and o["status"] in ("Ожидает оплаты","Чек отклонён","На проверке оплаты"):
        lines.append(f"⏳ Резерв товара до: <b>{html.escape(o['reservation_expires_at'])}</b>")
    lines += ["",order_delivery_summary(o),""]
    for i in items:lines.append(f"• {html.escape(i['product_name'])} · {html.escape(i['color'] or '')}/{html.escape(i['size'])} × {i['qty']} — {money(i['price']*i['qty'])}")
    lines.append(f"\nИтого: <b>{money(o['total'])}</b>")
    rows=[]
    if o["tracking_number"]:
        lines.append(f"🚚 Трек: <code>{html.escape(o['tracking_number'])}</code>");url=tracking_url(o["delivery_method"]);
        if url:rows.append([InlineKeyboardButton(text="🔎 Отследить",url=url)])

    # Неоплаченный заказ можно оплатить прямо из карточки — без возврата назад.
    if o["status"] in ("Ожидает оплаты", "Чек отклонён"):
        if settings.payment_provider_token:
            rows.append([InlineKeyboardButton(text="💳 Оплатить онлайн",callback_data=f"payonline:{oid}")])
        if settings.payment_card:
            rows.append([InlineKeyboardButton(text="🏦 Оплатить по реквизитам",callback_data=f"paymanual:{oid}")])
        if o["status"] == "Чек отклонён":
            rows.append([InlineKeyboardButton(text="📎 Отправить чек повторно",callback_data=f"receipt:{oid}")])
    elif o["status"] == "На проверке оплаты":
        lines.append("\n⌛ Чек уже отправлен и ожидает проверки администратора.")

    rows.append([InlineKeyboardButton(text="⬅️ Мои заказы", callback_data="nav:myorders")])
    if o["status"] == "Завершён":
        review_url=await configured_review_url()
        if review_url:rows.append([InlineKeyboardButton(text="⭐ Оставить отзыв",url=review_url)])
    await c.answer();await render_screen(c.message,"\n".join(lines),InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(UIButtonText("⭐ Отзывы"))
async def reviews_public(m:Message):
    url=await configured_review_url()
    if not url:
        await m.answer("⭐ Раздел отзывов пока не настроен администратором.")
        return
    await m.answer(
        "⭐ <b>Отзывы покупателей</b>\n\nОтзывы находятся в отдельном Telegram-канале. "
        "Откройте пост по кнопке ниже и перейдите в комментарии.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Открыть отзывы", url=url)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:home")],
        ]),
    )


@router.callback_query(F.data == "premiumemoji:previewnoop")
async def premiumemoji_preview_noop(c: CallbackQuery):
    await c.answer("Это предпросмотр кнопки с Premium emoji ✅")


# -----------------------------
# ADMIN COMMON / ORDER PAYMENT
# -----------------------------
@router.message(Command("admin"))
@router.message(UIButtonText("⚙️ Админ-панель"))
async def admin_entry(m:Message):await open_admin(m)


@router.callback_query(F.data=="adm:home")
async def adm_home(c:CallbackQuery):
    role=await admin_role(c.from_user.id)
    if not role:await c.answer("Нет доступа",show_alert=True);return
    await c.answer();await render_screen(c.message, await admin_dashboard_text(role), await admin_menu(role))


@router.callback_query(F.data=="adm:more")
async def adm_more(c:CallbackQuery):
    # Совместимость со старыми сообщениями: отдельного скрытого меню больше нет.
    role=await admin_role(c.from_user.id)
    if not role:await c.answer("Нет доступа",show_alert=True);return
    await c.answer();await render_screen(c.message, await admin_dashboard_text(role), await admin_menu(role))


@router.callback_query(F.data.startswith("adm:section:"))
async def adm_section(c: CallbackQuery):
    role = await admin_role(c.from_user.id)
    if not role:
        await c.answer("Нет доступа", show_alert=True); return
    section = c.data.rsplit(":", 1)[1]
    screen = await admin_section_menu(role, section)
    if not screen:
        await c.answer("Этот раздел недоступен для вашей роли", show_alert=True); return
    text, markup = screen
    await c.answer()
    await render_screen(c.message, text, markup)


@router.callback_query(F.data == "adm:globalsearch")
async def adm_global_search_start(c: CallbackQuery, state: FSMContext):
    if not await admin_role(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    await state.set_state(AdminGlobalSearch.query)
    await c.answer()
    await render_screen(c.message, "🔎 <b>Поиск по магазину</b>\n\nВведите код заказа (например #DX12XX), Telegram ID, @username, телефон, ФИО или название товара.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К админке", callback_data="adm:home")]]))


@router.message(AdminGlobalSearch.query, F.text)
async def adm_global_search_result(m: Message, state: FSMContext):
    role = await admin_role(m.from_user.id)
    if not role:
        await state.clear(); return
    query = (m.text or "").strip()
    if ui_text_matches(query, "❌ Отмена") or ui_text_matches(query, "⬅️ Назад"):
        await state.clear(); await open_admin(m); return
    result = await search_everything(query)
    rows: list[list[InlineKeyboardButton]] = []
    lines = [f"🔎 <b>Результаты: {html.escape(query)}</b>", ""]
    if result["orders"]:
        lines.append(f"📦 Заказы: <b>{len(result['orders'])}</b>")
        for o in result["orders"]:
            rows.append([InlineKeyboardButton(text=f"📦 {order_ref(o)} · {o['status']} · {money(o['total'])}"[:64], callback_data=f"admorder:{o['id']}")])
    if result["users"]:
        lines.append(f"👥 Клиенты: <b>{len(result['users'])}</b>")
        for u in result["users"]:
            label=u['full_name'] or (('@'+u['username']) if u['username'] else str(u['user_id']))
            rows.append([InlineKeyboardButton(text=f"👤 {label}"[:64], callback_data=f"customer:{u['user_id']}")])
    if result["products"]:
        lines.append(f"👕 Товары: <b>{len(result['products'])}</b>")
        for prod in result["products"]:
            rows.append([InlineKeyboardButton(text=f"👕 #{prod['id']} {prod['name']}"[:64], callback_data=f"admprod:{prod['id']}")])
    if not any(result.values()):
        lines.append("Ничего не найдено.")
    rows.append([InlineKeyboardButton(text="🔎 Новый поиск", callback_data="adm:globalsearch")])
    rows.append([InlineKeyboardButton(text="⬅️ К админке", callback_data="adm:home")])
    await state.clear()
    await m.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def render_admin_diagnostics(message: Message, admin_id: int) -> None:
    packs = await db.premium_emoji_packs()
    all_items = await db.premium_emoji_all_items()
    used = await db.premium_emoji_used_items()
    recent = await db.premium_emoji_recent_items(admin_id, 8)
    candidates = recent or used[:8] or all_items[:8]
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🩺 Полная проверка системы", callback_data="adm:diagnostics:full")],
        [InlineKeyboardButton(text="💾 Создать проверенный backup сейчас", callback_data="adm:diagnostics:backup")],
    ]
    for item in candidates:
        rows.append([InlineKeyboardButton(
            text=f"RAW-тест · {item['pack_title']} · №{int(item['position'])+1}"[:64],
            callback_data=f"premiumemoji:diag:{item['id']}:0",
            icon_custom_emoji_id=str(item['custom_emoji_id']),
        )])
    rows.append([InlineKeyboardButton(text="⬅️ К настройкам", callback_data="adm:section:settings")])
    await render_screen(
        message,
        "🛠 <b>Диагностика</b>\n\n"
        f"Сборка: <code>{BOT_BUILD}</code>\n"
        f"Premium emoji паков: <b>{len(packs)}</b>\n"
        f"Premium emoji в библиотеке: <b>{len(all_items)}</b>\n"
        f"Используется разных emoji: <b>{len(used)}</b>\n\n"
        "RAW-тесты вынесены сюда, чтобы не загромождать обычную карточку каждого emoji. "
        "Ни один тест не запускается автоматически.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "adm:diagnostics")
async def adm_diagnostics(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    await c.answer()
    await render_admin_diagnostics(c.message, c.from_user.id)


@router.callback_query(F.data == "adm:diagnostics:full")
async def adm_diagnostics_full(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    await c.answer("Проверяю…")
    checks = await full_system_diagnostics()
    passed = sum(1 for _name, ok, _detail in checks if ok)
    lines = [f"🩺 <b>Диагностика системы · {passed}/{len(checks)}</b>", ""]
    for name, ok, detail in checks:
        lines.append(f"{'✅' if ok else '❌'} <b>{html.escape(name)}</b> — {html.escape(str(detail))}")
    await render_screen(c.message, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Проверить снова", callback_data="adm:diagnostics:full")],[InlineKeyboardButton(text="⬅️ Диагностика", callback_data="adm:diagnostics")]]))


@router.callback_query(F.data == "adm:diagnostics:backup")
async def adm_diagnostics_backup(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    try:
        path = await backup_once()
        await c.answer("Backup создан ✅", show_alert=True)
        await db.audit(c.from_user.id, "verified_backup", path.name)
    except Exception as exc:
        logging.exception("manual verified backup failed")
        await c.answer(f"Ошибка backup: {type(exc).__name__}", show_alert=True)


UI_BUTTON_PAGE_SIZE = 10


def _ui_button_value(row) -> str:
    # Показываем именно тот текст, который Telegram получит после настроек.
    # При Premium-иконке обычный ведущий emoji автоматически скрывается.
    return ui_rendered_text(str(row["button_key"]), str(row["default_text"]))


async def render_ui_button_groups(message: Message) -> None:
    await sync_ui_button_registry()
    groups = {r["group_name"]: int(r["c"] or 0) for r in await db.ui_button_groups()}
    rows: list[list[InlineKeyboardButton]] = []
    ordered = list(UI_BUTTON_GROUP_TITLES.keys())
    for group in ordered:
        count = groups.get(group, 0)
        if not count:
            continue
        rows.append([InlineKeyboardButton(
            text=f"{UI_BUTTON_GROUP_TITLES.get(group, group)} · {count}",
            callback_data=f"uibadmin:list:{group}:0",
        )])
    total = await db.ui_button_count()
    rows += [
        [InlineKeyboardButton(text=f"📋 Все кнопки · {total}", callback_data="uibadmin:list:all:0")],
        [InlineKeyboardButton(text="♻️ Сбросить все названия", callback_data="uibadmin:resetall:ask")],
        [InlineKeyboardButton(text="⬅️ К настройкам", callback_data="adm:section:settings")],
    ]
    await render_screen(
        message,
        "🎛 <b>Настройка кнопок</b>\n\n"
        "У каждой кнопки теперь можно отдельно менять <b>название</b> и ставить <b>Premium/custom emoji</b> как настоящую иконку Telegram.\n\n"
        "Для кнопок со счётчиком можно использовать <code>{count}</code>, для кнопок заказа — <code>{id}</code>.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def render_ui_button_list(message: Message, group: str, page: int) -> None:
    await sync_ui_button_registry()
    db_group = "" if group == "all" else group
    total = await db.ui_button_count(db_group)
    max_page = max(0, (total - 1) // UI_BUTTON_PAGE_SIZE)
    page = max(0, min(page, max_page))
    offset = page * UI_BUTTON_PAGE_SIZE
    items = await db.ui_buttons(db_group, UI_BUTTON_PAGE_SIZE, offset)
    rows: list[list[InlineKeyboardButton]] = []
    for row in items:
        current = _ui_button_value(row).replace("\n", " ")
        renamed = bool((row["custom_text"] or "").strip())
        has_icon = bool((row["custom_emoji_id"] or "").strip())
        style = str(row["custom_style"] or "").strip().lower()
        style_marker = {"primary": "🔵", "success": "🟢", "danger": "🔴"}.get(style, "")
        marker = ("✏️" if renamed else "▫️") + ("💎" if has_icon else "") + style_marker
        rows.append([InlineKeyboardButton(
            text=f"{marker} {current}"[:64],
            callback_data=f"uibadmin:item:{row['id']}:{group}:{page}",
        )])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Предыдущие", callback_data=f"uibadmin:list:{group}:{page-1}"))
    if page < max_page:
        nav.append(InlineKeyboardButton(text="Следующие ➡️", callback_data=f"uibadmin:list:{group}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ Разделы кнопок", callback_data="adm:buttons")])
    title = "Все кнопки" if group == "all" else UI_BUTTON_GROUP_TITLES.get(group, group)
    await render_screen(
        message,
        f"🎛 <b>{html.escape(title)}</b>\n"
        f"Страница <b>{page+1}/{max_page+1}</b> · кнопок: <b>{total}</b>\n\n"
        "▫️ — стандартное название\n✏️ — название изменено\n💎 — установлена Premium-иконка",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def render_ui_button_item(message: Message, button_id: int, group: str, page: int) -> None:
    row = await db.ui_button(button_id)
    if not row:
        await render_ui_button_list(message, group, page)
        return
    default = html.escape(row["default_text"])
    current = html.escape(_ui_button_value(row))
    changed = bool((row["custom_text"] or "").strip())
    icon_id = (row["custom_emoji_id"] or "").strip()
    rows = [
        [InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"uibadmin:edit:{button_id}:{group}:{page}")],
        [InlineKeyboardButton(
            text="💎 Заменить Premium emoji" if icon_id else "💎 Добавить Premium emoji",
            callback_data=f"uibadmin:emoji:set:{button_id}:{group}:{page}",
        )],
    ]
    if changed:
        rows.append([InlineKeyboardButton(text="↩️ Вернуть стандартное название", callback_data=f"uibadmin:reset:{button_id}:{group}:{page}")])
    if icon_id:
        rows.append([InlineKeyboardButton(text="❌ Убрать Premium emoji", callback_data=f"uibadmin:emoji:reset:{button_id}:{group}:{page}")])
    rows += [
        [InlineKeyboardButton(text="⬅️ К списку", callback_data=f"uibadmin:list:{group}:{page}")],
        [InlineKeyboardButton(text="⬅️ К настройкам", callback_data="adm:section:settings")],
    ]
    icon_status = f"<code>{html.escape(icon_id)}</code>" if icon_id else "не установлен"
    style_status = "✨ автоматически по смыслу"
    await render_screen(
        message,
        "🎛 <b>Кнопка</b>\n\n"
        f"Стандартно: <b>{default}</b>\n"
        f"Сейчас: <b>{current}</b>\n"
        f"Тип: <code>{html.escape(row['kind'])}</code>\n"
        f"Premium emoji: {icon_status}\n"
        f"Цвет: <b>{style_status}</b>\n\n"
        "Для Premium-иконки достаточно отправить боту нужный custom emoji — ID бот получит сам.\n"
        "Если подпись начинается с обычного emoji (например 🛍 Каталог), при установке Premium-иконки обычный emoji автоматически убирается. При удалении Premium-иконки он возвращается. После выбора бот сразу пришлёт свежую клавиатуру и проверит, принял ли Telegram эту иконку.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def render_ui_button_style_picker(message: Message, button_id: int, group: str, page: int) -> None:
    row = await db.ui_button(button_id)
    if not row:
        await render_ui_button_list(message, group, page)
        return
    rows = [[InlineKeyboardButton(text="⬅️ К кнопке", callback_data=f"uibadmin:item:{button_id}:{group}:{page}")]]
    await render_screen(
        message,
        "🎨 <b>Автоматические цвета</b>\n\n"
        "Цвета теперь назначаются автоматически без ручных приоритетов:\n"
        "🟢 покупка, добавление, подтверждение и сохранение\n"
        "🔵 основные переходы, просмотр, редактирование и информация\n"
        "🔴 удаление, отмена, отклонение и отключение\n"
        "▫️ второстепенная навигация вроде «Назад» и «Ещё».\n\n"
        "Одна и та же логика применяется ко всем кнопкам бота.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.regexp(r"^uibadmin:style:\d+:"))
async def ui_button_style_start(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, button_id, group, page_s = c.data.split(":", 4)
    await c.answer()
    await render_ui_button_style_picker(c.message, int(button_id), group, int(page_s))


@router.callback_query(F.data.startswith("uibadmin:style:set:"))
async def ui_button_style_set(c: CallbackQuery, bot: Bot):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    await c.answer("Цвета теперь назначаются автоматически по смыслу", show_alert=True)


@router.callback_query(F.data == "adm:buttons")
async def adm_buttons(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    await c.answer()
    await render_ui_button_groups(c.message)


@router.callback_query(F.data.startswith("uibadmin:list:"))
async def ui_button_list_cb(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, group, page_s = c.data.split(":", 3)
    await c.answer()
    await render_ui_button_list(c.message, group, int(page_s))


@router.callback_query(F.data.startswith("uibadmin:item:"))
async def ui_button_item_cb(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, button_id, group, page_s = c.data.split(":", 4)
    await c.answer()
    await render_ui_button_item(c.message, int(button_id), group, int(page_s))


@router.callback_query(F.data.startswith("uibadmin:edit:"))
async def ui_button_edit_start(c: CallbackQuery, state: FSMContext):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, button_id, group, page_s = c.data.split(":", 4)
    row = await db.ui_button(int(button_id))
    if not row:
        await c.answer("Кнопка не найдена", show_alert=True); return
    await state.set_state(AdminButtonEdit.text)
    await state.update_data(ui_button_id=int(button_id), ui_button_group=group, ui_button_page=int(page_s))
    await c.answer()
    await c.message.answer(
        "✏️ <b>Новое название кнопки</b>\n\n"
        f"Сейчас: <b>{html.escape(_ui_button_value(row))}</b>\n\n"
        "Отправьте новое название одним сообщением. Можно использовать эмодзи.\n"
        "Для динамического счётчика доступен <code>{count}</code>, для номера заказа — <code>{id}</code>.",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminButtonEdit.text, F.text)
async def ui_button_edit_save(m: Message, state: FSMContext):
    if await admin_role(m.from_user.id) != "owner":
        await state.clear(); await m.answer("⛔ Нет доступа."); return
    value = (m.text or "").strip()
    if ui_text_matches(value, "❌ Отмена"):
        await state.clear(); await m.answer("Изменение отменено.", reply_markup=main_menu(True)); return
    if not value:
        await m.answer("Название не может быть пустым."); return
    if len(value) > 64:
        await m.answer("Слишком длинно. Максимум 64 символа."); return
    data = await state.get_data()
    button_id = int(data.get("ui_button_id") or 0)
    group = str(data.get("ui_button_group") or "all")
    page = int(data.get("ui_button_page") or 0)
    row = await db.set_ui_button_custom_text(button_id, value)
    if not row:
        await state.clear(); await m.answer("Кнопка не найдена.", reply_markup=main_menu(True)); return
    ui_set_custom_label(row["button_key"], row["custom_text"])
    await db.audit(m.from_user.id, "button_label_edit", f"button={button_id}")
    await state.clear()
    await m.answer("✅ Название кнопки изменено.", reply_markup=main_menu(True))
    await render_ui_button_item(m, button_id, group, page)


async def render_ui_button_emoji_sources(message: Message, button_id: int, group: str, page: int) -> None:
    row = await db.ui_button(button_id)
    if not row:
        await render_ui_button_list(message, group, page)
        return
    target_emoji = first_emoji(_ui_button_value(row)) or first_emoji(str(row["default_text"] or ""))
    rows: list[list[InlineKeyboardButton]] = []
    if target_emoji:
        rows.append([InlineKeyboardButton(
            text=f"🔎 Все Premium-варианты для {target_emoji}",
            callback_data=f"uibadmin:ev:{button_id}:{group}:{page}:match:0",
        )])
    rows += [
        [InlineKeyboardButton(text="⭐ Избранные", callback_data=f"uibadmin:ev:{button_id}:{group}:{page}:favorites:0"),
         InlineKeyboardButton(text="🕘 Недавние", callback_data=f"uibadmin:ev:{button_id}:{group}:{page}:recent:0")],
        [InlineKeyboardButton(text="📦 Выбрать по паку", callback_data=f"uibadmin:epacks:{button_id}:{group}:{page}:0")],
        [InlineKeyboardButton(text="✉️ Отправить emoji вручную", callback_data=f"uibadmin:eman:{button_id}:{group}:{page}")],
        [InlineKeyboardButton(text="⬅️ К кнопке", callback_data=f"uibadmin:item:{button_id}:{group}:{page}")],
    ]
    hint = (
        f"\n\nБот уже распознал обычный emoji <b>{html.escape(target_emoji)}</b> и может сразу показать все его Premium-варианты."
        if target_emoji else
        "\n\nВ подписи этой кнопки нет обычного emoji, поэтому используйте избранное, недавние, паки или ручную отправку."
    )
    await render_screen(
        message,
        "💎 <b>Premium emoji для кнопки</b>\n\n"
        f"Кнопка: <b>{html.escape(_ui_button_value(row))}</b>" + hint,
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def render_ui_button_catalog_items(
    message: Message,
    button_id: int,
    group: str,
    page: int,
    view: str,
    catalog_page: int,
    admin_id: int,
) -> None:
    button = await db.ui_button(button_id)
    if not button:
        await render_ui_button_list(message, group, page); return
    if view == "match":
        target_emoji = first_emoji(_ui_button_value(button)) or first_emoji(str(button["default_text"] or ""))
        items = await premium_catalog_search_items(target_emoji)
        title = f"🔎 <b>Варианты для {html.escape(target_emoji or 'emoji')}</b>"
    elif view in {"favorites", "recent"}:
        items = await premium_catalog_view_items(view, admin_id)
        title = "⭐ <b>Избранные</b>" if view == "favorites" else "🕘 <b>Недавние</b>"
    else:
        items = []
        title = "💎 <b>Premium emoji</b>"
    total = len(items)
    max_page = max(0, (total - 1) // PREMIUM_CATALOG_PAGE_SIZE)
    catalog_page = max(0, min(catalog_page, max_page))
    start = catalog_page * PREMIUM_CATALOG_PAGE_SIZE
    rows: list[list[InlineKeyboardButton]] = []
    for item in items[start:start + PREMIUM_CATALOG_PAGE_SIZE]:
        rows.append([InlineKeyboardButton(
            text=f"{item['pack_title']} · №{int(item['position'])+1}"[:64],
            callback_data=f"uibadmin:euse:{button_id}:{group}:{page}:{item['id']}",
            icon_custom_emoji_id=str(item["custom_emoji_id"]),
        )])
    nav: list[InlineKeyboardButton] = []
    if catalog_page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"uibadmin:ev:{button_id}:{group}:{page}:{view}:{catalog_page-1}"))
    if total > PREMIUM_CATALOG_PAGE_SIZE:
        nav.append(InlineKeyboardButton(text=f"{catalog_page+1}/{max_page+1}", callback_data="premiumemoji:nop"))
    if catalog_page < max_page:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"uibadmin:ev:{button_id}:{group}:{page}:{view}:{catalog_page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ К выбору emoji", callback_data=f"uibadmin:emoji:set:{button_id}:{group}:{page}")])
    await render_screen(
        message,
        f"{title}\n\nКнопка: <b>{html.escape(_ui_button_value(button))}</b>\nВариантов: <b>{total}</b>.\n\nНажмите на нужный Premium emoji — он сразу будет назначен кнопке.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("uibadmin:ev:"))
async def ui_button_emoji_catalog_view(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, button_id_s, group, page_s, view, catalog_page_s = c.data.split(":", 6)
    await c.answer()
    await render_ui_button_catalog_items(
        c.message, int(button_id_s), group, int(page_s), view, int(catalog_page_s), c.from_user.id,
    )


async def render_ui_button_pack_sources(message: Message, button_id: int, group: str, page: int, pack_page: int = 0) -> None:
    row = await db.ui_button(button_id)
    if not row:
        await render_ui_button_list(message, group, page); return
    packs = await db.premium_emoji_packs()
    per_page = 12
    max_page = max(0, (len(packs) - 1) // per_page)
    pack_page = max(0, min(pack_page, max_page))
    start = pack_page * per_page
    rows: list[list[InlineKeyboardButton]] = []
    for pack in packs[start:start + per_page]:
        rows.append([InlineKeyboardButton(
            text=f"📦 {pack['title']} · {pack['sticker_count']}"[:64],
            callback_data=f"uibadmin:ep:{button_id}:{group}:{page}:{pack['id']}:0",
        )])
    nav: list[InlineKeyboardButton] = []
    if pack_page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"uibadmin:epacks:{button_id}:{group}:{page}:{pack_page-1}"))
    if len(packs) > per_page:
        nav.append(InlineKeyboardButton(text=f"{pack_page+1}/{max_page+1}", callback_data="premiumemoji:nop"))
    if pack_page < max_page:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"uibadmin:epacks:{button_id}:{group}:{page}:{pack_page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ К выбору emoji", callback_data=f"uibadmin:emoji:set:{button_id}:{group}:{page}")])
    await render_screen(
        message,
        f"📦 <b>Выбор по паку</b>\n\nСтраница <b>{pack_page+1}/{max_page+1}</b>. Это запасной режим; быстрее использовать поиск по обычному emoji.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("uibadmin:epacks:"))
async def ui_button_emoji_pack_sources(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    parts = c.data.split(":")
    button_id_s, group, page_s = parts[2], parts[3], parts[4]
    pack_page_s = parts[5] if len(parts) > 5 else "0"
    await c.answer()
    await render_ui_button_pack_sources(c.message, int(button_id_s), group, int(page_s), int(pack_page_s))


@router.callback_query(F.data.startswith("uibadmin:emoji:set:"))
async def ui_button_emoji_start(c: CallbackQuery, state: FSMContext):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, _, button_id, group, page_s = c.data.split(":", 5)
    await state.clear()
    await c.answer()
    await render_ui_button_emoji_sources(c.message, int(button_id), group, int(page_s))


@router.callback_query(F.data.startswith("uibadmin:eman:"))
async def ui_button_emoji_manual_start(c: CallbackQuery, state: FSMContext):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, button_id, group, page_s = c.data.split(":", 4)
    row = await db.ui_button(int(button_id))
    if not row:
        await c.answer("Кнопка не найдена", show_alert=True); return
    await state.set_state(AdminButtonEdit.emoji)
    await state.update_data(ui_button_id=int(button_id), ui_button_group=group, ui_button_page=int(page_s))
    await c.answer()
    await c.message.answer(
        "💎 <b>Premium emoji для кнопки</b>\n\n"
        f"Кнопка: <b>{html.escape(_ui_button_value(row))}</b>\n\n"
        "Отправьте <b>один Premium/custom emoji</b>. Бот сам извлечёт его <code>custom_emoji_id</code>.",
        reply_markup=cancel_keyboard(),
    )


UI_EMOJI_PACK_PAGE_SIZE = 10


async def render_ui_button_pack_items(message: Message, button_id: int, group: str, page: int, pack_id: int, pack_page: int) -> None:
    button = await db.ui_button(button_id)
    pack = await db.premium_emoji_pack(pack_id)
    if not button or not pack:
        await render_ui_button_emoji_sources(message, button_id, group, page)
        return
    total = await db.premium_emoji_pack_item_count(pack_id)
    max_page = max(0, (total - 1) // UI_EMOJI_PACK_PAGE_SIZE)
    pack_page = max(0, min(pack_page, max_page))
    items = await db.premium_emoji_pack_items(pack_id, UI_EMOJI_PACK_PAGE_SIZE, pack_page * UI_EMOJI_PACK_PAGE_SIZE)
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        rows.append([InlineKeyboardButton(
            text=f"№{int(item['position'])+1} · {item['fallback_text']}"[:64],
            callback_data=f"uibadmin:euse:{button_id}:{group}:{page}:{item['id']}",
            icon_custom_emoji_id=str(item['custom_emoji_id']),
        )])
    nav: list[InlineKeyboardButton] = []
    if pack_page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"uibadmin:ep:{button_id}:{group}:{page}:{pack_id}:{pack_page-1}"))
    if pack_page < max_page:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"uibadmin:ep:{button_id}:{group}:{page}:{pack_id}:{pack_page+1}"))
    if nav:
        rows.append(nav)
    rows += [
        [InlineKeyboardButton(text="⬅️ К наборам", callback_data=f"uibadmin:emoji:set:{button_id}:{group}:{page}")],
        [InlineKeyboardButton(text="⬅️ К кнопке", callback_data=f"uibadmin:item:{button_id}:{group}:{page}")],
    ]
    await render_screen(
        message,
        "📦 <b>Выберите emoji из набора</b>\n\n"
        f"Набор: <b>{html.escape(pack['title'])}</b>\n"
        f"Кнопка: <b>{html.escape(_ui_button_value(button))}</b>\n"
        f"Страница: <b>{pack_page+1}/{max_page+1}</b>",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("uibadmin:ep:"))
async def ui_button_emoji_pack_page(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, button_id, group, page_s, pack_id, pack_page = c.data.split(":", 6)
    await c.answer()
    await render_ui_button_pack_items(c.message, int(button_id), group, int(page_s), int(pack_id), int(pack_page))


@router.callback_query(F.data.startswith("uibadmin:euse:"))
async def ui_button_emoji_use_imported(c: CallbackQuery, bot: Bot):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, button_id, group, page_s, item_id = c.data.split(":", 5)
    item = await db.premium_emoji_pack_item(int(item_id))
    row = await db.ui_button(int(button_id))
    if not item or not row:
        await c.answer("Emoji или кнопка не найдены", show_alert=True); return
    emoji_id = str(item['custom_emoji_id'])
    row = await db.set_ui_button_custom_emoji(int(button_id), emoji_id)
    ui_set_custom_icon(row["button_key"], emoji_id)

    ok, error = await verify_and_preview_button_icon(c.message, row, emoji_id, c.from_user.id)
    if not ok:
        row = await db.reset_ui_button_emoji(int(button_id))
        if row:
            ui_set_custom_icon(row["button_key"], "")
        await db.audit(c.from_user.id, "button_premium_emoji_rejected", f"button={button_id},item={item_id},emoji={emoji_id},error={error}")
        await c.answer("Telegram отклонил Premium emoji для кнопки", show_alert=True)
        await c.message.answer(
            "❌ <b>Telegram не принял Premium emoji для кнопки.</b>\n\n"
            f"Причина API: <code>{html.escape(error)}</code>\n\n"
            "Назначение отменено, поэтому обычный emoji останется на месте. "
            "Проверьте, что владелец этого бота в @BotFather имеет активный Telegram Premium.",
            reply_markup=main_menu(True),
        )
        await render_ui_button_item(c.message, int(button_id), group, int(page_s))
        return

    await db.audit(c.from_user.id, "button_premium_emoji_set_from_pack", f"button={button_id},item={item_id},emoji={emoji_id}")
    await db.mark_premium_emoji_recent(c.from_user.id, int(item_id))
    await c.answer("Premium emoji установлен и проверен ✅", show_alert=True)
    await render_ui_button_item(c.message, int(button_id), group, int(page_s))


@router.message(AdminButtonEdit.emoji)
async def ui_button_emoji_save(m: Message, state: FSMContext, bot: Bot):
    if await admin_role(m.from_user.id) != "owner":
        await state.clear(); await m.answer("⛔ Нет доступа."); return
    if ui_text_matches(m.text, "❌ Отмена"):
        await state.clear(); await m.answer("Изменение отменено.", reply_markup=main_menu(True)); return
    pairs = extract_custom_emoji_pairs(m.text, m.entities)
    if not pairs:
        await m.answer(
            "Не вижу Premium/custom emoji в сообщении. Отправьте именно кастомный эмодзи из Telegram Premium, а не обычный Unicode-эмодзи."
        )
        return
    data = await state.get_data()
    button_id = int(data.get("ui_button_id") or 0)
    group = str(data.get("ui_button_group") or "all")
    page = int(data.get("ui_button_page") or 0)
    fallback, emoji_id = pairs[0]
    row = await db.set_ui_button_custom_emoji(button_id, emoji_id)
    if not row:
        await state.clear(); await m.answer("Кнопка не найдена.", reply_markup=main_menu(True)); return
    ui_set_custom_icon(row["button_key"], emoji_id)
    await state.clear()

    ok, error = await verify_and_preview_button_icon(m, row, emoji_id, m.from_user.id)
    if not ok:
        row = await db.reset_ui_button_emoji(button_id)
        if row:
            ui_set_custom_icon(row["button_key"], "")
        await db.audit(m.from_user.id, "button_premium_emoji_rejected", f"button={button_id},emoji={emoji_id},error={error}")
        await m.answer(
            "❌ <b>Telegram не принял Premium emoji для кнопки.</b>\n\n"
            f"Причина API: <code>{html.escape(error)}</code>\n\n"
            "Назначение отменено. Проверьте, что именно аккаунт-владелец бота "
            "в @BotFather имеет активный Telegram Premium.",
            reply_markup=main_menu(True),
        )
        await render_ui_button_item(m, button_id, group, page)
        return

    await db.audit(m.from_user.id, "button_premium_emoji_set", f"button={button_id},emoji={emoji_id}")
    imported_item = await db.premium_emoji_item_by_custom_id(emoji_id)
    if imported_item:
        await db.mark_premium_emoji_recent(m.from_user.id, int(imported_item["id"]))
    extra = " В сообщении было несколько custom emoji — для кнопки использован первый." if len(pairs) > 1 else ""
    await m.answer(f"✅ Premium emoji установлен и проверен.{extra}", reply_markup=main_menu(True))
    await render_ui_button_item(m, button_id, group, page)


@router.callback_query(F.data.startswith("uibadmin:emoji:reset:"))
async def ui_button_emoji_reset(c: CallbackQuery, bot: Bot):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, _, button_id, group, page_s = c.data.split(":", 5)
    row = await db.reset_ui_button_emoji(int(button_id))
    if row:
        ui_set_custom_icon(row["button_key"], "")
        await db.audit(c.from_user.id, "button_premium_emoji_reset", f"button={button_id}")
    await c.answer("Premium emoji убран ✅ Обычный emoji кнопки возвращён.")
    if row and str(row["kind"] or "") == "reply" and str(row["default_text"] or "") in MAIN_MENU_BUTTON_TEXTS:
        await force_refresh_main_menu(c.message, c.from_user.id, "✅ Premium emoji удалён. Обычная иконка возвращена 👇")
    await render_ui_button_item(c.message, int(button_id), group, int(page_s))


@router.callback_query(F.data.startswith("uibadmin:reset:"))
async def ui_button_reset_cb(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, button_id, group, page_s = c.data.split(":", 4)
    row = await db.reset_ui_button(int(button_id))
    if row:
        ui_set_custom_label(row["button_key"], "")
        await db.audit(c.from_user.id, "button_label_reset", f"button={button_id}")
    await c.answer("Стандартное название возвращено ✅")
    await render_ui_button_item(c.message, int(button_id), group, int(page_s))


@router.callback_query(F.data == "uibadmin:resetall:ask")
async def ui_button_reset_all_ask(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    await c.answer()
    await render_screen(c.message, "♻️ <b>Сбросить названия всех кнопок?</b>\n\nВсе кнопки вернут стандартные подписи.", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, сбросить все", callback_data="uibadmin:resetall:yes")],
        [InlineKeyboardButton(text="❌ Нет", callback_data="adm:buttons")],
    ]))


@router.callback_query(F.data == "uibadmin:resetall:yes")
async def ui_button_reset_all_yes(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    await db.reset_all_ui_buttons()
    ui_clear_custom_labels()
    await db.audit(c.from_user.id, "button_labels_reset_all")
    await c.answer("Все названия сброшены ✅", show_alert=True)
    await c.message.answer("✅ Все кнопки возвращены к стандартным названиям.", reply_markup=main_menu(True))
    await render_ui_button_groups(c.message)


PREMIUM_PACK_PAGE_SIZE = 10
PREMIUM_PACK_LIBRARY_PAGE_SIZE = 12
PREMIUM_PLACEMENT_PAGE_SIZE = 10


PREMIUM_CATALOG_PAGE_SIZE = 12


async def render_premium_emoji_panel(message: Message) -> None:
    packs = await db.premium_emoji_packs()
    rules = await db.premium_emoji_rules()
    placement_count = await db.premium_emoji_placement_count()
    used = await db.premium_emoji_used_items()
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🔎 Найти по обычному emoji", callback_data="premiumemoji:search")],
        [InlineKeyboardButton(text="⭐ Избранные", callback_data="premiumemoji:view:favorites:0"),
         InlineKeyboardButton(text="🕘 Недавние", callback_data="premiumemoji:view:recent:0")],
        [InlineKeyboardButton(text=f"🎯 Используемые · {len(used)}", callback_data="premiumemoji:view:used:0"),
         InlineKeyboardButton(text=f"📦 Все паки · {len(packs)}", callback_data="premiumemoji:packs:0")],
        [InlineKeyboardButton(text="🔗 Добавить паки списком", callback_data="premiumemoji:packadd")],
        [InlineKeyboardButton(text="🎨 Пресеты стиля", callback_data="premiumemoji:presets")],
        [InlineKeyboardButton(text=f"🎯 Расстановки в тексте · {placement_count}", callback_data="premiumemoji:placements:0")],
        [InlineKeyboardButton(text=f"🌍 Глобальные замены · {len(rules)}", callback_data="premiumemoji:singles")],
        [InlineKeyboardButton(text="➕ Добавить отдельные emoji", callback_data="premiumemoji:add")],
        [InlineKeyboardButton(text="⬅️ К настройкам", callback_data="adm:section:settings")],
    ]
    await render_screen(
        message,
        "💎 <b>Premium emoji</b>\n\n"
        "Самый быстрый способ — <b>поиск по обычному emoji</b>. Отправьте, например, "
        "<code>🗑</code>, <code>📦</code>, <code>⭐</code> или <code>💳</code>, и бот сразу покажет "
        "<b>все Premium-варианты из всех импортированных паков</b>.\n\n"
        "⭐ <b>Избранные</b> — ваши сохранённые варианты.\n"
        "🕘 <b>Недавние</b> — emoji, которые вы реально назначали последними.\n"
        "🎯 <b>Используемые</b> — всё, что уже применяется в текстах или кнопках.\n"
        "📦 <b>Все паки</b> — старый просмотр по наборам, если он понадобится.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "premiumemoji:presets")
async def premium_emoji_presets(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    current = await db.get_setting("premium_emoji_style_preset", "")
    rows = [
        [InlineKeyboardButton(text=("✅ " if current=="minimal" else "")+"Minimal / Outline", callback_data="premiumemoji:preset:minimal")],
        [InlineKeyboardButton(text=("✅ " if current=="macos" else "")+"macOS / iOS", callback_data="premiumemoji:preset:macos")],
        [InlineKeyboardButton(text=("✅ " if current=="telegram" else "")+"Telegram UI", callback_data="premiumemoji:preset:telegram")],
        [InlineKeyboardButton(text="⬅️ К Premium emoji", callback_data="adm:premiumemoji")],
    ]
    await c.answer()
    await render_screen(c.message, "🎨 <b>Пресеты Premium emoji</b>\n\nПресет приводит кнопки и глобальные текстовые emoji к одной визуальной семье, используя уже импортированные паки. Никаких сообщений покупателям при применении не отправляется.", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("premiumemoji:preset:"))
async def premium_emoji_apply_preset(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    name = c.data.rsplit(":",1)[1]
    await c.answer("Применяю…")
    try:
        result = await apply_style_preset(name)
    except ValueError as exc:
        await c.answer(str(exc), show_alert=True); return
    ui_load_custom_labels(await db.ui_button_customizations())
    premium_load_rules(await db.premium_emoji_rules())
    await db.audit(c.from_user.id, "premium_emoji_style_preset", f"{name}:buttons={result['buttons']},text={result['text_rules']}")
    await render_screen(c.message, f"✅ <b>Пресет применён</b>\n\nКнопок: <b>{result['buttons']}</b>\nТекстовых правил: <b>{result['text_rules']}</b>\nПодходящих emoji в выбранной семье: <b>{result['pack_candidates']}</b>\n\nПокупателям технические сообщения не отправлялись.", InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К пресетам", callback_data="premiumemoji:presets")]]))


async def render_premium_emoji_pack_library(message: Message, page: int = 0) -> None:
    packs = await db.premium_emoji_packs()
    max_page = max(0, (len(packs) - 1) // PREMIUM_PACK_LIBRARY_PAGE_SIZE)
    page = max(0, min(int(page), max_page))
    start = page * PREMIUM_PACK_LIBRARY_PAGE_SIZE
    rows: list[list[InlineKeyboardButton]] = []
    for pack in packs[start:start + PREMIUM_PACK_LIBRARY_PAGE_SIZE]:
        rows.append([InlineKeyboardButton(
            text=f"📦 {pack['title']} · {pack['sticker_count']} emoji"[:64],
            callback_data=f"premiumemoji:pack:{pack['id']}:0",
        )])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"premiumemoji:packs:{page-1}"))
    if len(packs) > PREMIUM_PACK_LIBRARY_PAGE_SIZE:
        nav.append(InlineKeyboardButton(text=f"{page+1}/{max_page+1}", callback_data="premiumemoji:nop"))
    if page < max_page:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"premiumemoji:packs:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ К Premium emoji", callback_data="adm:premiumemoji")])
    await render_screen(
        message,
        f"📦 <b>Все Premium emoji паки</b>\n\nНаборов: <b>{len(packs)}</b> · страница <b>{page+1}/{max_page+1}</b>.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _premium_catalog_title(view: str) -> str:
    return {"favorites": "⭐ Избранные", "recent": "🕘 Недавние", "used": "🎯 Используемые"}.get(view, "💎 Premium emoji")


async def render_premium_catalog_items(
    message: Message,
    items: list,
    *,
    title: str,
    page: int = 0,
    page_callback_prefix: str,
    view: str,
    empty_text: str = "Ничего не найдено.",
) -> None:
    total = len(items)
    max_page = max(0, (total - 1) // PREMIUM_CATALOG_PAGE_SIZE)
    page = max(0, min(int(page), max_page))
    start = page * PREMIUM_CATALOG_PAGE_SIZE
    rows: list[list[InlineKeyboardButton]] = []
    for item in items[start:start + PREMIUM_CATALOG_PAGE_SIZE]:
        pack_title = str(item["pack_title"] or item["pack_set_name"] or "Пак")
        position = int(item["position"] or 0) + 1
        suffix = ""
        if "usage_count" in item.keys():
            suffix = f" · мест: {int(item['usage_count'] or 0)}"
        elif "use_count" in item.keys():
            suffix = f" · использовано: {int(item['use_count'] or 0)}"
        rows.append([InlineKeyboardButton(
            text=f"{pack_title} · №{position}{suffix}"[:64],
            callback_data=f"premiumemoji:catalogitem:{item['id']}:{view}:{page}",
            icon_custom_emoji_id=str(item["custom_emoji_id"]),
        )])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"{page_callback_prefix}{page-1}"))
    if total > PREMIUM_CATALOG_PAGE_SIZE:
        nav.append(InlineKeyboardButton(text=f"{page+1}/{max_page+1}", callback_data="premiumemoji:nop"))
    if page < max_page:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"{page_callback_prefix}{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ К Premium emoji", callback_data="adm:premiumemoji")])
    body = f"{title}\n\nВсего вариантов: <b>{total}</b>."
    if not total:
        body += f"\n\n{empty_text}"
    await render_screen(message, body, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "adm:premiumemoji")
async def adm_premium_emoji(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    await c.answer()
    await render_premium_emoji_panel(c.message)


@router.callback_query(F.data == "premiumemoji:search")
async def premium_emoji_search_start(c: CallbackQuery, state: FSMContext):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    await state.clear()
    await state.set_state(AdminPremiumEmoji.search_query)
    await c.answer()
    await c.message.answer(
        "🔎 <b>Поиск Premium emoji</b>\n\n"
        "Отправьте обычный emoji <b>или смысл словами</b>. Например:\n"
        "<code>🗑</code>  <code>📦</code>  <code>⭐</code>  <code>💳</code>  <code>⚙️</code>\n"
        "или <code>доставка</code>, <code>оплата</code>, <code>магазин</code>, <code>удаление</code>.\n\n"
        "Я покажу подходящие Premium-варианты сразу из всех импортированных паков.",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminPremiumEmoji.search_query, F.text)
async def premium_emoji_search_save(m: Message, state: FSMContext):
    if await admin_role(m.from_user.id) != "owner":
        await state.clear(); await m.answer("⛔ Нет доступа."); return
    if ui_text_matches(m.text, "❌ Отмена"):
        await state.clear(); await m.answer("Поиск отменён.", reply_markup=main_menu(True)); return
    query = (m.text or "").strip()
    items = await premium_catalog_search_items(query)
    await db.set_setting(f"premium_emoji_last_search_{m.from_user.id}", query)
    await state.clear()
    await render_premium_catalog_items(
        m,
        items,
        title=f"🔎 <b>Результаты для {html.escape(query)}</b>",
        page=0,
        page_callback_prefix="premiumemoji:searchpage:",
        view="search",
        empty_text="В импортированных паках нет Premium emoji для этого символа или смысловой группы.",
    )


@router.callback_query(F.data.startswith("premiumemoji:searchpage:"))
async def premium_emoji_search_page(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    page = int(c.data.rsplit(":", 1)[1])
    query = await db.get_setting(f"premium_emoji_last_search_{c.from_user.id}", "")
    items = await premium_catalog_search_items(query)
    await c.answer()
    await render_premium_catalog_items(
        c.message, items, title=f"🔎 <b>Результаты для {html.escape(query)}</b>", page=page,
        page_callback_prefix="premiumemoji:searchpage:", view="search",
    )


@router.callback_query(F.data.startswith("premiumemoji:view:"))
async def premium_emoji_catalog_view(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, view, page_s = c.data.split(":", 3)
    if view not in {"favorites", "recent", "used"}:
        await c.answer("Неизвестный раздел", show_alert=True); return
    items = await premium_catalog_view_items(view, c.from_user.id)
    await c.answer()
    await render_premium_catalog_items(
        c.message, items, title=f"<b>{_premium_catalog_title(view)}</b>",
        page=int(page_s), page_callback_prefix=f"premiumemoji:view:{view}:", view=view,
        empty_text={
            "favorites": "Добавляйте удачные варианты в избранное прямо в карточке emoji.",
            "recent": "Здесь появятся emoji после назначения на кнопку или в текст.",
            "used": "Сейчас ни один импортированный Premium emoji не используется.",
        }[view],
    )


@router.callback_query(F.data.startswith("premiumemoji:packs:"))
async def premium_emoji_packs_page(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    await c.answer()
    await render_premium_emoji_pack_library(c.message, int(c.data.rsplit(":", 1)[1]))


# Backward compatibility for buttons sent by FIX16/FIX17.
@router.callback_query(F.data.startswith("premiumemoji:panel:"))
async def premium_emoji_panel_page(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    await c.answer()
    await render_premium_emoji_pack_library(c.message, int(c.data.rsplit(":", 1)[1]))


@router.callback_query(F.data == "premiumemoji:nop")
async def premium_emoji_nop(c: CallbackQuery):
    await c.answer()


@router.callback_query(F.data == "premiumemoji:packadd")
async def premium_emoji_pack_add_start(c: CallbackQuery, state: FSMContext):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    await state.clear()
    await state.set_state(AdminPremiumEmoji.pack_link)
    await c.answer()
    await c.message.answer(
        "🔗 <b>Массовый импорт Premium emoji</b>\n\n"
        "Отправьте <b>одну или много ссылок одним сообщением</b>. Можно по одной ссылке на строку или через пробел:\n"
        "<code>https://t.me/addemoji/tgmacicons\nhttps://t.me/addemoji/UnigramIcons\nhttps://t.me/addemoji/OutlineEmoji</code>\n\n"
        "Бот сам найдёт все ссылки, уберёт дубликаты и импортирует каждый набор целиком.",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminPremiumEmoji.pack_link, F.text)
async def premium_emoji_pack_add_save(m: Message, state: FSMContext, bot: Bot):
    if await admin_role(m.from_user.id) != "owner":
        await state.clear(); await m.answer("⛔ Нет доступа."); return
    if ui_text_matches(m.text, "❌ Отмена"):
        await state.clear(); await m.answer("Импорт отменён.", reply_markup=main_menu(True)); return

    names = premium_pack_names_from_text(m.text)
    if not names:
        await m.answer(
            "❌ Не нашёл ссылок на Premium emoji наборы.\n\n"
            "Отправьте ссылки вида <code>https://t.me/addemoji/NAME</code>. Можно сразу много ссылок одним сообщением."
        )
        return

    status = await m.answer(f"⏳ Импортирую наборы: <b>0/{len(names)}</b>…")
    last_edit = 0

    async def progress(index, total, imported, refreshed, skipped, error_count, set_name):
        nonlocal last_edit
        # Avoid editing the progress message on every pack in very large batches.
        if index != total and index - last_edit < 5:
            return
        last_edit = index
        try:
            await status.edit_text(
                "⏳ <b>Импорт Premium emoji</b>\n\n"
                f"Обработано: <b>{index}/{total}</b>\n"
                f"Новых: <b>{imported}</b> · обновлено: <b>{refreshed}</b> · ошибок: <b>{error_count}</b>\n"
                f"Сейчас: <code>{html.escape(set_name)}</code>"
            )
        except TelegramBadRequest:
            pass

    async with premium_pack_import_lock:
        result = await import_premium_pack_names(bot, names, progress_cb=progress)
    await db.audit(
        m.from_user.id,
        "premium_emoji_pack_bulk_import",
        f"total={result['total']},imported={result['imported']},refreshed={result['refreshed']},errors={len(result['errors'])}",
    )
    await state.clear()

    error_text = ""
    if result["errors"]:
        shown = result["errors"][:12]
        error_text = "\n\n<b>Не удалось импортировать:</b>\n" + "\n".join(
            f"• <code>{html.escape(name)}</code> — {html.escape(reason[:120])}" for name, reason in shown
        )
        if len(result["errors"]) > len(shown):
            error_text += f"\n…и ещё {len(result['errors']) - len(shown)}"

    summary = (
        "✅ <b>Массовый импорт завершён</b>\n\n"
        f"Ссылок распознано: <b>{result['total']}</b>\n"
        f"Новых наборов: <b>{result['imported']}</b>\n"
        f"Обновлено существующих: <b>{result['refreshed']}</b>\n"
        f"Emoji обработано: <b>{result['emoji_count']}</b>\n"
        f"Ошибок: <b>{len(result['errors'])}</b>"
        + error_text
    )
    try:
        await status.edit_text(summary)
    except TelegramBadRequest:
        await m.answer(summary)
    await render_premium_emoji_panel(m)


async def render_premium_emoji_pack(message: Message, pack_id: int, page: int = 0) -> None:
    pack = await db.premium_emoji_pack(pack_id)
    if not pack:
        await render_premium_emoji_panel(message)
        return
    total = await db.premium_emoji_pack_item_count(pack_id)
    max_page = max(0, (total - 1) // PREMIUM_PACK_PAGE_SIZE)
    page = max(0, min(page, max_page))
    items = await db.premium_emoji_pack_items(pack_id, PREMIUM_PACK_PAGE_SIZE, page * PREMIUM_PACK_PAGE_SIZE)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🔄 Обновить набор по ссылке", callback_data=f"premiumemoji:packrefresh:{pack_id}:{page}")],
    ]
    for item in items:
        text_uses = len(await db.premium_emoji_pack_item_placements(int(item["id"])))
        button_uses = len(await db.ui_buttons_by_custom_emoji(str(item["custom_emoji_id"]), 10000))
        uses = text_uses + button_uses
        rows.append([InlineKeyboardButton(
            text=f"№{int(item['position'])+1} · {item['fallback_text']} · мест: {uses}"[:64],
            callback_data=f"premiumemoji:pi:{item['id']}:{page}",
            icon_custom_emoji_id=str(item["custom_emoji_id"]),
        )])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Предыдущие", callback_data=f"premiumemoji:pack:{pack_id}:{page-1}"))
    if page < max_page:
        nav.append(InlineKeyboardButton(text="Следующие ➡️", callback_data=f"premiumemoji:pack:{pack_id}:{page+1}"))
    if nav:
        rows.append(nav)
    rows += [
        [InlineKeyboardButton(text="🗑 Удалить набор из библиотеки", callback_data=f"premiumemoji:packdelask:{pack_id}")],
        [InlineKeyboardButton(text="⬅️ К Premium emoji", callback_data="adm:premiumemoji")],
    ]
    await render_screen(
        message,
        "📦 <b>Premium emoji набор</b>\n\n"
        f"Название: <b>{html.escape(pack['title'])}</b>\n"
        f"Имя: <code>{html.escape(pack['set_name'])}</code>\n"
        f"Emoji: <b>{total}</b>\n"
        f"Страница: <b>{page+1}/{max_page+1}</b>\n\n"
        "Нажмите на emoji, чтобы поставить его в нужный текст или на кнопку.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("premiumemoji:pack:"))
async def premium_emoji_pack_open(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, pack_id, page = c.data.split(":", 3)
    await c.answer()
    await render_premium_emoji_pack(c.message, int(pack_id), int(page))


@router.callback_query(F.data.startswith("premiumemoji:packrefresh:"))
async def premium_emoji_pack_refresh(c: CallbackQuery, bot: Bot):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, pack_id, page = c.data.split(":", 3)
    pack = await db.premium_emoji_pack(int(pack_id))
    if not pack:
        await c.answer("Набор не найден", show_alert=True); return
    try:
        updated = await save_custom_emoji_pack(bot, str(pack["set_name"]))
    except Exception as exc:
        await c.answer(f"Ошибка: {str(exc)[:120]}", show_alert=True); return
    await db.audit(c.from_user.id, "premium_emoji_pack_refresh", f"pack={pack['set_name']},count={updated['sticker_count']}")
    await c.answer(f"Обновлено: {updated['sticker_count']} emoji ✅", show_alert=True)
    await render_premium_emoji_pack(c.message, int(pack_id), int(page))


@router.callback_query(F.data.startswith("premiumemoji:packdelask:"))
async def premium_emoji_pack_delete_ask(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    pack_id = int(c.data.rsplit(":", 1)[1])
    pack = await db.premium_emoji_pack(pack_id)
    if not pack:
        await c.answer("Набор не найден", show_alert=True); return
    await c.answer()
    await render_screen(
        c.message,
        f"🗑 Удалить набор <b>{html.escape(pack['title'])}</b> из библиотеки?\n\n"
        "Уже назначенные emoji в текстах и на кнопках <b>останутся работать</b>; удалится только сам набор из списка выбора.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"premiumemoji:packdelyes:{pack_id}")],
            [InlineKeyboardButton(text="❌ Нет", callback_data=f"premiumemoji:pack:{pack_id}:0")],
        ]),
    )


@router.callback_query(F.data.startswith("premiumemoji:packdelyes:"))
async def premium_emoji_pack_delete_yes(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    pack_id = int(c.data.rsplit(":", 1)[1])
    row = await db.delete_premium_emoji_pack(pack_id)
    if row:
        await db.audit(c.from_user.id, "premium_emoji_pack_delete", f"pack={row['set_name']}")
    await c.answer("Набор удалён из библиотеки ✅", show_alert=True)
    await render_premium_emoji_panel(c.message)


async def render_premium_emoji_pack_item(
    message: Message,
    item_id: int,
    pack_page: int = 0,
    *,
    admin_id: int | None = None,
    back_callback: str | None = None,
    back_text: str = "⬅️ К набору",
    context_view: str = "pack",
    context_page: int | None = None,
) -> None:
    item = await db.premium_emoji_pack_item(item_id)
    if not item:
        await render_premium_emoji_panel(message)
        return
    owner_id = int(admin_id or settings.admin_id or 0)
    is_favorite = await db.premium_emoji_is_favorite(owner_id, item_id) if owner_id else False
    placements = await db.premium_emoji_pack_item_placements(item_id)
    button_uses = await db.ui_buttons_by_custom_emoji(str(item["custom_emoji_id"]), 10000)
    global_rules = await db.premium_emoji_rules_by_custom_emoji(str(item["custom_emoji_id"]))
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text="★ Убрать из избранного" if is_favorite else "⭐ В избранное",
            callback_data=f"premiumemoji:fav:{item_id}:{context_view}:{pack_page if context_page is None else context_page}",
        )],
        [InlineKeyboardButton(text="🌍 Заменить обычный emoji ВЕЗДЕ", callback_data=f"premiumemoji:global:{item_id}:{pack_page}")],
        [InlineKeyboardButton(text="🎯 Настроить только конкретный текст", callback_data=f"premiumemoji:place:{item_id}:{pack_page}")],
        [InlineKeyboardButton(text="🔘 Настроить только конкретную кнопку", callback_data=f"premiumemoji:buttons:{item_id}")],
    ]
    for rule in global_rules[:6]:
        fallback = str(rule["fallback_text"] or "").replace("\n", " ")
        rows.append([InlineKeyboardButton(
            text=f"🗑 Везде · {fallback}"[:64],
            callback_data=f"premiumemoji:gdel:{rule['id']}:{item_id}:{pack_page}",
        )])
    for rule in placements[:6]:
        mode = {
            "before":"перед", "after":"после", "replace":"вместо фрагмента",
            "replace_emoji":"замена emoji",
        }.get(str(rule["position"]), str(rule["position"]))
        anchor = str(rule["match_text"]).replace("\n", " ↵ ")
        rows.append([InlineKeyboardButton(
            text=f"🗑 Текст · {mode}: {anchor}"[:64],
            callback_data=f"premiumemoji:pdel:{rule['id']}:{item_id}:{pack_page}",
        )])
    for button in button_uses[:6]:
        rows.append([InlineKeyboardButton(
            text=f"🗑 Кнопка · {_ui_button_value(button)}"[:64],
            callback_data=f"premiumemoji:bdel:{button['id']}:{item_id}:{pack_page}",
        )])
    if placements or button_uses or global_rules:
        rows.append([InlineKeyboardButton(
            text="🧹 Удалить этот Premium emoji отовсюду",
            callback_data=f"premiumemoji:clearask:{item_id}:{pack_page}",
        )])
    if back_callback:
        rows.append([InlineKeyboardButton(text=back_text, callback_data=back_callback)])
    else:
        rows.append([InlineKeyboardButton(text="⬅️ К набору", callback_data=f"premiumemoji:pack:{item['pack_id']}:{pack_page}")])
    test_tag = f'<tg-emoji emoji-id="{html.escape(str(item["custom_emoji_id"]))}">{html.escape(str(item["fallback_text"]))}</tg-emoji>'
    await render_screen(
        message,
        "💎 <b>Premium emoji</b>\n\n"
        f"Набор: <b>{html.escape(str(item['pack_title'] or ''))}</b>\n"
        f"Emoji: {test_tag}\n"
        f"Обычный символ: <b>{html.escape(str(item['fallback_text']))}</b>\n"
        f"Глобальных замен: <b>{len(global_rules)}</b>\n"
        f"Точечных расстановок: <b>{len(placements)}</b>\n"
        f"Отдельно настроенных кнопок: <b>{len(button_uses)}</b>\n\n"
        "Выберите, где использовать этот вариант. RAW/API-тесты перенесены в «⚙️ Настройки → 🛠 Диагностика».",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _premium_catalog_back(view: str, page: int) -> tuple[str, str]:
    if view == "search":
        return f"premiumemoji:searchpage:{page}", "⬅️ К результатам поиска"
    if view in {"favorites", "recent", "used"}:
        return f"premiumemoji:view:{view}:{page}", f"⬅️ {_premium_catalog_title(view)}"
    return "adm:premiumemoji", "⬅️ К Premium emoji"


@router.callback_query(F.data.startswith("premiumemoji:catalogitem:"))
async def premium_emoji_catalog_item_open(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, item_id_s, view, page_s = c.data.split(":", 4)
    back_cb, back_text = _premium_catalog_back(view, int(page_s))
    await c.answer()
    await render_premium_emoji_pack_item(
        c.message, int(item_id_s), 0, admin_id=c.from_user.id,
        back_callback=back_cb, back_text=back_text, context_view=view, context_page=int(page_s),
    )


@router.callback_query(F.data.startswith("premiumemoji:fav:"))
async def premium_emoji_favorite_toggle(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, item_id_s, view, page_s = c.data.split(":", 4)
    item_id = int(item_id_s)
    enabled = await db.toggle_premium_emoji_favorite(c.from_user.id, item_id)
    await c.answer("Добавлено в избранное ⭐" if enabled else "Убрано из избранного")
    if view == "pack":
        await render_premium_emoji_pack_item(c.message, item_id, int(page_s), admin_id=c.from_user.id)
    else:
        back_cb, back_text = _premium_catalog_back(view, int(page_s))
        await render_premium_emoji_pack_item(
            c.message, item_id, 0, admin_id=c.from_user.id,
            back_callback=back_cb, back_text=back_text, context_view=view, context_page=int(page_s),
        )


@router.callback_query(F.data.startswith("premiumemoji:pi:"))
async def premium_emoji_pack_item_open(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, item_id, page = c.data.split(":", 3)
    await c.answer()
    await render_premium_emoji_pack_item(c.message, int(item_id), int(page), admin_id=c.from_user.id)


def _main_menu_premium_debug(custom_id: str) -> tuple[int, list[str]]:
    """Inspect the keyboard our real customer menu generator currently builds."""
    markup = main_menu(True)
    matches: list[str] = []
    total_icons = 0
    for row in markup.keyboard:
        for button in row:
            icon = str(getattr(button, "icon_custom_emoji_id", "") or "").strip()
            if icon:
                total_icons += 1
            if icon == custom_id:
                matches.append(str(getattr(button, "text", "") or ""))
    return total_icons, matches


@router.callback_query(F.data.startswith("premiumemoji:diag:"))
async def premium_emoji_direct_diagnostic(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, item_id_s, page_s = c.data.split(":", 3)
    item_id = int(item_id_s)
    page = int(page_s)
    item = await db.premium_emoji_pack_item(item_id)
    if not item:
        await c.answer("Emoji не найден", show_alert=True); return

    custom_id = str(item["custom_emoji_id"] or "").strip()
    stored_fallback = str(item["fallback_text"] or "💎").strip() or "💎"
    await c.answer("Запускаю прямую проверку Telegram…")

    checks: list[str] = []
    api_fallback = stored_fallback
    sticker_set_name = str(item["pack_set_name"] or "")

    # 1) Telegram recognizes the ID. Raw API call: no aiogram, no middleware.
    ok, detail, result = await raw_bot_api(
        "getCustomEmojiStickers",
        {"custom_emoji_ids": [custom_id]},
    )
    if ok and isinstance(result, list) and result:
        st = result[0] if isinstance(result[0], dict) else {}
        returned_id = str(st.get("custom_emoji_id") or "")
        api_fallback = str(st.get("emoji") or stored_fallback).strip() or stored_fallback
        sticker_set_name = str(st.get("set_name") or sticker_set_name)
        if returned_id == custom_id:
            checks.append("✅ RAW getCustomEmojiStickers: Telegram распознал ID")
        else:
            checks.append(f"⚠️ RAW getCustomEmojiStickers: другой ID: {returned_id or 'пусто'}")
    else:
        checks.append(f"❌ RAW getCustomEmojiStickers: {detail}")

    # 2) Direct HTML custom emoji in a normal bot message.
    tag = f'<tg-emoji emoji-id="{custom_id}">{api_fallback}</tg-emoji>'
    ok, detail, _ = await raw_bot_api(
        "sendMessage",
        {
            "chat_id": c.from_user.id,
            "text": f"DIRECT MESSAGE TEST: {tag}  ← слева должен быть Premium emoji",
            "parse_mode": "HTML",
        },
    )
    checks.append(
        "✅ RAW sendMessage + <tg-emoji>: Telegram принял запрос"
        if ok else f"❌ RAW sendMessage + <tg-emoji>: {detail}"
    )

    # 3) Direct inline button with custom emoji. This is the exact Bot API JSON.
    ok, detail, _ = await raw_bot_api(
        "sendMessage",
        {
            "chat_id": c.from_user.id,
            "text": "DIRECT INLINE BUTTON TEST — Premium emoji должен быть перед текстом кнопки:",
            "reply_markup": {
                "inline_keyboard": [[{
                    "text": "DIRECT INLINE TEST",
                    "callback_data": "premiumemoji:previewnoop",
                    "icon_custom_emoji_id": custom_id,
                }]],
            },
        },
    )
    checks.append(
        "✅ RAW InlineKeyboardButton.icon_custom_emoji_id: Telegram принял запрос"
        if ok else f"❌ RAW InlineKeyboardButton.icon_custom_emoji_id: {detail}"
    )

    # 4) Direct reply keyboard. This temporarily replaces the bottom keyboard.
    ok, detail, _ = await raw_bot_api(
        "sendMessage",
        {
            "chat_id": c.from_user.id,
            "text": "DIRECT REPLY BUTTON TEST — проверьте нижнюю клавиатуру. Затем нажмите «Вернуть меню» в отчёте.",
            "reply_markup": {
                "keyboard": [[{
                    "text": "DIRECT REPLY TEST",
                    "icon_custom_emoji_id": custom_id,
                }]],
                "resize_keyboard": True,
            },
        },
    )
    checks.append(
        "✅ RAW KeyboardButton.icon_custom_emoji_id: Telegram принял запрос"
        if ok else f"❌ RAW KeyboardButton.icon_custom_emoji_id: {detail}"
    )

    # 5) Inspect our actual global replacement state and real main-menu builder.
    global_rules = await db.premium_emoji_rules_by_custom_emoji(custom_id)
    total_menu_icons, menu_matches = _main_menu_premium_debug(custom_id)
    if global_rules:
        fallbacks = ", ".join(str(r["fallback_text"] or "") for r in global_rules[:8])
        checks.append(f"✅ База: глобальных правил для ID = {len(global_rules)} ({fallbacks})")
    else:
        checks.append("⚠️ База: для этого ID нет глобального правила замены")
    if menu_matches:
        checks.append(
            "✅ Генератор меню: выбранный ID реально попадает в кнопки: "
            + ", ".join(menu_matches[:8])
        )
    else:
        checks.append(
            f"⚠️ Генератор меню: выбранного ID нет в main_menu (Premium-иконок всего: {total_menu_icons})"
        )

    await db.audit(
        c.from_user.id,
        "premium_emoji_direct_diagnostic",
        f"item={item_id};emoji={custom_id};checks={' | '.join(checks)}",
    )

    status_text = "\n".join(html.escape(line) for line in checks)
    report_text = (
        "🧪 <b>Прямая диагностика Premium emoji</b>\n\n"
        f"Сборка: <code>{BOT_BUILD}</code>\n"
        f"custom_emoji_id: <code>{html.escape(custom_id)}</code>\n"
        f"fallback Telegram: <code>{html.escape(api_fallback)}</code>\n"
        f"set_name: <code>{html.escape(sticker_set_name or '—')}</code>\n\n"
        f"{status_text}\n\n"
        "<b>Как читать:</b>\n"
        "• Если RAW-кнопки дают ✅ и Premium emoji виден в DIRECT TEST — Telegram и аккаунт работают, значит проблема только в нашей глобальной логике.\n"
        "• Если RAW даёт ✅, но DIRECT TEST визуально без Premium emoji — обновите Telegram Desktop/mobile до актуальной версии; запрос уже принят Telegram.\n"
        "• Если RAW даёт ❌ — пришлите мне эту строку целиком: это точный ответ Bot API.\n"
        "• Проверять с того же Telegram-аккаунта можно."
    )
    report_markup = {
        "inline_keyboard": [
            [{"text": "↩️ Вернуть главное меню", "callback_data": f"premiumemoji:diagrestore:{item_id}:{page}"}],
            [{"text": "⬅️ К выбранному emoji", "callback_data": f"premiumemoji:pi:{item_id}:{page}"}],
        ]
    }
    report_ok, report_detail, _ = await raw_bot_api(
        "sendMessage",
        {
            "chat_id": c.from_user.id,
            "text": report_text,
            "parse_mode": "HTML",
            "reply_markup": report_markup,
        },
    )
    if not report_ok:
        await c.message.answer(
            report_text + f"\n\n⚠️ RAW-отчёт не отправился: <code>{html.escape(report_detail)}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Вернуть главное меню", callback_data=f"premiumemoji:diagrestore:{item_id}:{page}")],
                [InlineKeyboardButton(text="⬅️ К выбранному emoji", callback_data=f"premiumemoji:pi:{item_id}:{page}")],
            ]),
        )


@router.callback_query(F.data.startswith("premiumemoji:diagrestore:"))
async def premium_emoji_direct_diagnostic_restore(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, item_id_s, page_s = c.data.split(":", 3)
    await c.answer("Главное меню возвращено ✅")
    await force_refresh_main_menu(c.message, c.from_user.id, "✅ Главное меню восстановлено после теста 👇")
    await render_premium_emoji_pack_item(c.message, int(item_id_s), int(page_s))


@router.callback_query(F.data.startswith("premiumemoji:global:"))
async def premium_emoji_global_replace_start(c: CallbackQuery, state: FSMContext):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, item_id, page = c.data.split(":", 3)
    item = await db.premium_emoji_pack_item(int(item_id))
    if not item:
        await c.answer("Emoji не найден", show_alert=True); return
    await state.clear()
    await state.set_state(AdminPremiumEmoji.global_replace_target)
    await state.update_data(premium_pack_item_id=int(item_id), premium_pack_page=int(page))
    await c.answer()
    test_tag = f'<tg-emoji emoji-id="{html.escape(str(item["custom_emoji_id"]))}">{html.escape(str(item["fallback_text"]))}</tg-emoji>'
    await c.message.answer(
        "🌍 <b>Глобальная замена Premium emoji</b>\n\n"
        f"Выбранный Premium emoji: {test_tag}\n\n"
        "Отправьте <b>обычный emoji, который нужно заменить абсолютно везде</b>.\n"
        "Например, отправьте <code>⭐</code>. После этого во всех новых сообщениях, подписях и кнопках бота вместо ⭐ будет использоваться выбранный Premium emoji.\n\n"
        "Можно отправить и фразу вроде <code>⭐ Отзывы</code> — бот сам возьмёт из неё первый обычный emoji.",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminPremiumEmoji.global_replace_target, F.text)
async def premium_emoji_global_replace_save(m: Message, state: FSMContext, bot: Bot):
    if await admin_role(m.from_user.id) != "owner":
        await state.clear(); await m.answer("⛔ Нет доступа."); return
    if ui_text_matches(m.text, "❌ Отмена"):
        await state.clear(); await m.answer("Глобальная замена отменена.", reply_markup=main_menu(True)); return
    ordinary = first_emoji(m.text or "")
    if not ordinary:
        await m.answer("Не нашёл обычный emoji. Отправьте, например: <code>⭐</code> или <code>⭐ Отзывы</code>.")
        return
    data = await state.get_data()
    item_id = int(data.get("premium_pack_item_id") or 0)
    pack_page = int(data.get("premium_pack_page") or 0)
    item = await db.premium_emoji_pack_item(item_id)
    if not item:
        await state.clear(); await m.answer("Emoji больше не найден в библиотеке.", reply_markup=main_menu(True)); return
    emoji_id = str(item["custom_emoji_id"] or "")
    await db.upsert_premium_emoji_rule(ordinary, emoji_id)
    premium_set_rule(ordinary, emoji_id)
    await db.audit(m.from_user.id, "premium_emoji_global_replace", f"fallback={ordinary};emoji={emoji_id};item={item_id}")
    await db.mark_premium_emoji_recent(m.from_user.id, item_id)
    await state.clear()
    # Verify the real customer menu generator immediately. This catches Unicode
    # spelling differences such as ⭐ vs ⭐️ before we tell the owner it worked.
    total_menu_icons, menu_matches = _main_menu_premium_debug(emoji_id)
    menu_note = (
        f"\nКнопок главного меню с этой Premium-иконкой: {len(menu_matches)}."
        if menu_matches else
        f"\n⚠️ Главное меню пока не содержит эту иконку (всего Premium-иконок: {total_menu_icons})."
    )
    # Refresh the owner's persistent menu immediately. All customer-facing
    # keyboards/messages generated from now on use the same global rule.
    await force_refresh_main_menu(
        m, m.from_user.id,
        notice=f"✅ Глобальная замена включена: {html.escape(ordinary)} → Premium emoji.\nВсе новые сообщения и кнопки покупателей теперь используют её.{menu_note} 👇",
    )
    await render_premium_emoji_pack_item(m, item_id, pack_page)


@router.callback_query(F.data.startswith("premiumemoji:gdel:"))
async def premium_emoji_global_replace_delete(c: CallbackQuery, bot: Bot):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, rule_id, item_id, page = c.data.split(":", 4)
    row = await db.delete_premium_emoji_rule(int(rule_id))
    if row:
        premium_remove_rule(str(row["fallback_text"] or ""))
        await db.audit(c.from_user.id, "premium_emoji_global_replace_delete", f"rule={rule_id};item={item_id}")
    await c.answer("Глобальная замена удалена ✅ Обычный emoji возвращён.", show_alert=True)
    await force_refresh_main_menu(c.message, c.from_user.id, notice="✅ Обычный emoji возвращён в главное меню 👇")
    await render_premium_emoji_pack_item(c.message, int(item_id), int(page))


@router.callback_query(F.data.startswith("premiumemoji:place:"))
async def premium_emoji_place_choose_mode(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, item_id, page = c.data.split(":", 3)
    item = await db.premium_emoji_pack_item(int(item_id))
    if not item:
        await c.answer("Emoji не найден", show_alert=True); return
    await c.answer()
    await render_screen(
        c.message,
        "🎯 <b>Куда поставить emoji?</b>\n\n"
        "Самый удобный режим — <b>«Заменить обычный emoji»</b>. Отправьте целую фразу, например <code>🛍 Каталог</code>: обычный 🛍 исчезнет, а текст «Каталог» останется с выбранным Premium emoji.\n\n"
        "Для одного конкретного места отправляйте более длинный уникальный фрагмент.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="♻️ Заменить обычный emoji", callback_data=f"premiumemoji:pm:replace_emoji:{item_id}:{page}")],
            [InlineKeyboardButton(text="⬅️ Добавить перед фразой", callback_data=f"premiumemoji:pm:before:{item_id}:{page}")],
            [InlineKeyboardButton(text="➡️ Добавить после фразы", callback_data=f"premiumemoji:pm:after:{item_id}:{page}")],
            [InlineKeyboardButton(text="🔁 Заменить весь фрагмент", callback_data=f"premiumemoji:pm:replace:{item_id}:{page}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"premiumemoji:pi:{item_id}:{page}")],
        ]),
    )


@router.callback_query(F.data.startswith("premiumemoji:pm:"))
async def premium_emoji_place_wait_target(c: CallbackQuery, state: FSMContext):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, mode, item_id, page = c.data.split(":", 4)
    if mode not in {"before", "after", "replace", "replace_emoji"}:
        await c.answer("Неверный режим", show_alert=True); return
    item = await db.premium_emoji_pack_item(int(item_id))
    if not item:
        await c.answer("Emoji не найден", show_alert=True); return
    await state.clear()
    await state.set_state(AdminPremiumEmoji.placement_target)
    await state.update_data(premium_pack_item_id=int(item_id), premium_place_mode=mode, premium_pack_page=int(page))
    await c.answer()
    mode_text = {"before":"перед", "after":"после", "replace":"вместо всего фрагмента", "replace_emoji":"вместо первого обычного emoji внутри фрагмента"}[mode]
    await c.message.answer(
        "🎯 <b>Укажите место в тексте</b>\n\n"
        f"Emoji будет поставлен <b>{mode_text}</b> указанного фрагмента.\n\n"
        "Отправьте точный текст, слово, фразу или обычный emoji. Регистр и пробелы учитываются.\n"
        "Для режима замены emoji можно отправить, например: <code>🛍 Каталог</code>.",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminPremiumEmoji.placement_target, F.text)
async def premium_emoji_place_save(m: Message, state: FSMContext):
    if await admin_role(m.from_user.id) != "owner":
        await state.clear(); await m.answer("⛔ Нет доступа."); return
    if ui_text_matches(m.text, "❌ Отмена"):
        await state.clear(); await m.answer("Расстановка отменена.", reply_markup=main_menu(True)); return
    target = (m.text or "").strip()
    if not target:
        await m.answer("Фрагмент не может быть пустым."); return
    if len(target) > 250:
        await m.answer("Слишком длинный фрагмент. Максимум 250 символов."); return
    data = await state.get_data()
    item_id = int(data.get("premium_pack_item_id") or 0)
    mode = str(data.get("premium_place_mode") or "before")
    pack_page = int(data.get("premium_pack_page") or 0)
    item = await db.premium_emoji_pack_item(item_id)
    if not item:
        await state.clear(); await m.answer("Emoji больше не найден в библиотеке.", reply_markup=main_menu(True)); return
    if mode == "replace_emoji" and not contains_emoji(target):
        await m.answer(
            "В этом фрагменте не найден обычный emoji. Отправьте фразу вместе с emoji, который нужно заменить.\n"
            "Например: <code>🛍 Каталог</code>"
        )
        return
    row = await db.upsert_premium_emoji_placement(
        item_id,
        str(item["custom_emoji_id"]),
        str(item["fallback_text"]),
        target,
        mode,
    )
    premium_load_placements(await db.premium_emoji_placements())
    await db.audit(m.from_user.id, "premium_emoji_placement_set", f"item={item_id},mode={mode},target={target[:120]}")
    await db.mark_premium_emoji_recent(m.from_user.id, item_id)
    await state.clear()
    await m.answer("✅ Premium emoji настроен. Если выбран режим замены, обычный emoji больше не показывается. Изменение применяется без перезапуска бота.", reply_markup=main_menu(True))
    await render_premium_emoji_pack_item(m, item_id, pack_page)


@router.callback_query(F.data.startswith("premiumemoji:pdel:"))
async def premium_emoji_place_delete(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, rule_id, item_id, page = c.data.split(":", 4)
    await db.delete_premium_emoji_placement(int(rule_id))
    premium_load_placements(await db.premium_emoji_placements())
    await db.audit(c.from_user.id, "premium_emoji_placement_delete", f"rule={rule_id}")
    await c.answer("Расстановка удалена ✅")
    await render_premium_emoji_pack_item(c.message, int(item_id), int(page))


@router.callback_query(F.data.startswith("premiumemoji:bdel:"))
async def premium_emoji_button_delete_from_item(c: CallbackQuery, bot: Bot):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, button_id, item_id, page = c.data.split(":", 4)
    button = await db.reset_ui_button_emoji(int(button_id))
    if button:
        ui_set_custom_icon(str(button["button_key"]), "")
        await db.audit(c.from_user.id, "button_premium_emoji_reset", f"button={button_id};from_item={item_id}")
    await c.answer("Premium emoji с кнопки удалён ✅ Обычный emoji возвращён.")
    await render_premium_emoji_pack_item(c.message, int(item_id), int(page))


@router.callback_query(F.data.startswith("premiumemoji:clearask:"))
async def premium_emoji_clear_everywhere_ask(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, item_id, page = c.data.split(":", 3)
    item = await db.premium_emoji_pack_item(int(item_id))
    if not item:
        await c.answer("Emoji не найден", show_alert=True); return
    await c.answer()
    await render_screen(
        c.message,
        "🧹 <b>Удалить этот Premium emoji отовсюду?</b>\n\n"
        "Будут удалены все его назначения в текстах и на кнопках. Сам emoji останется в импортированном наборе и его можно будет назначить снова.\n\n"
        "У кнопок автоматически вернутся обычные emoji из их стандартных подписей.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить отовсюду", callback_data=f"premiumemoji:clearyes:{item_id}:{page}")],
            [InlineKeyboardButton(text="❌ Нет", callback_data=f"premiumemoji:pi:{item_id}:{page}")],
        ]),
    )


@router.callback_query(F.data.startswith("premiumemoji:clearyes:"))
async def premium_emoji_clear_everywhere_yes(c: CallbackQuery, bot: Bot):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, item_id, page = c.data.split(":", 3)
    item = await db.premium_emoji_pack_item(int(item_id))
    if not item:
        await c.answer("Emoji не найден", show_alert=True); return
    emoji_id = str(item["custom_emoji_id"] or "")

    buttons = await db.reset_ui_buttons_by_custom_emoji(emoji_id)
    for button in buttons:
        ui_set_custom_icon(str(button["button_key"]), "")

    text_rules = await db.delete_premium_emoji_placements_by_custom_emoji(emoji_id)
    single_rules = await db.delete_premium_emoji_rules_by_custom_emoji(emoji_id)
    for rule in single_rules:
        premium_remove_rule(str(rule["fallback_text"] or ""))
    premium_load_placements(await db.premium_emoji_placements())

    await db.audit(
        c.from_user.id,
        "premium_emoji_clear_everywhere",
        f"item={item_id},buttons={len(buttons)},placements={len(text_rules)},single_rules={len(single_rules)}",
    )
    await c.answer("Premium emoji удалён из всех назначений ✅", show_alert=True)
    if buttons or single_rules:
        await force_refresh_main_menu(c.message, c.from_user.id, "✅ Premium emoji удалён отовсюду. Меню обновлено 👇")
    await render_premium_emoji_pack_item(c.message, int(item_id), int(page))


async def render_premium_emoji_button_groups(message: Message, item_id: int) -> None:
    item = await db.premium_emoji_pack_item(item_id)
    if not item:
        await render_premium_emoji_panel(message); return
    await sync_ui_button_registry()
    groups = {r["group_name"]: int(r["c"] or 0) for r in await db.ui_button_groups()}
    rows: list[list[InlineKeyboardButton]] = []
    for group in UI_BUTTON_GROUP_TITLES:
        count = groups.get(group, 0)
        if count:
            rows.append([InlineKeyboardButton(
                text=f"{UI_BUTTON_GROUP_TITLES.get(group, group)} · {count}",
                callback_data=f"premiumemoji:bg:{item_id}:{group}:0",
            )])
    rows += [
        [InlineKeyboardButton(text=f"📋 Все кнопки · {await db.ui_button_count()}", callback_data=f"premiumemoji:bg:{item_id}:all:0")],
        [InlineKeyboardButton(text="⬅️ К emoji", callback_data=f"premiumemoji:pi:{item_id}:0")],
    ]
    await render_screen(
        message,
        "🔘 <b>Выберите раздел кнопок</b>\n\nВыбранный Premium emoji станет настоящей иконкой Telegram у выбранной кнопки.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("premiumemoji:buttons:"))
async def premium_emoji_buttons_start(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    item_id = int(c.data.rsplit(":", 1)[1])
    await c.answer()
    await render_premium_emoji_button_groups(c.message, item_id)


async def render_premium_emoji_button_list(message: Message, item_id: int, group: str, page: int) -> None:
    item = await db.premium_emoji_pack_item(item_id)
    if not item:
        await render_premium_emoji_panel(message); return
    await sync_ui_button_registry()
    db_group = "" if group == "all" else group
    total = await db.ui_button_count(db_group)
    max_page = max(0, (total - 1) // UI_BUTTON_PAGE_SIZE)
    page = max(0, min(page, max_page))
    rows: list[list[InlineKeyboardButton]] = []
    for button in await db.ui_buttons(db_group, UI_BUTTON_PAGE_SIZE, page * UI_BUTTON_PAGE_SIZE):
        rows.append([InlineKeyboardButton(
            text=_ui_button_value(button)[:64],
            callback_data=f"premiumemoji:bu:{item_id}:{button['id']}:{group}:{page}",
        )])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"premiumemoji:bg:{item_id}:{group}:{page-1}"))
    if page < max_page:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"premiumemoji:bg:{item_id}:{group}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⬅️ К разделам", callback_data=f"premiumemoji:buttons:{item_id}")])
    await render_screen(
        message,
        f"🔘 <b>Выберите кнопку</b>\n\nСтраница <b>{page+1}/{max_page+1}</b>. После нажатия emoji сразу будет назначен этой кнопке.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("premiumemoji:bg:"))
async def premium_emoji_button_group_open(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, item_id, group, page = c.data.split(":", 4)
    await c.answer()
    await render_premium_emoji_button_list(c.message, int(item_id), group, int(page))


@router.callback_query(F.data.startswith("premiumemoji:bu:"))
async def premium_emoji_button_use(c: CallbackQuery, bot: Bot):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, item_id, button_id, group, page = c.data.split(":", 5)
    item = await db.premium_emoji_pack_item(int(item_id))
    button = await db.ui_button(int(button_id))
    if not item or not button:
        await c.answer("Emoji или кнопка не найдены", show_alert=True); return

    emoji_id = str(item["custom_emoji_id"])
    button = await db.set_ui_button_custom_emoji(int(button_id), emoji_id)
    ui_set_custom_icon(button["button_key"], emoji_id)

    # IMPORTANT: this is the path used from the global Premium emoji library.
    # Previously it only saved the ID to SQLite, so a persistent ReplyKeyboard
    # already visible in Telegram did not change. Verify the exact ID against
    # Telegram and force-refresh the keyboard just like the button editor does.
    ok, error = await verify_and_preview_button_icon(c.message, button, emoji_id, c.from_user.id)
    if not ok:
        button = await db.reset_ui_button_emoji(int(button_id))
        if button:
            ui_set_custom_icon(button["button_key"], "")
        await db.audit(
            c.from_user.id,
            "button_premium_emoji_rejected_from_library",
            f"button={button_id},item={item_id},emoji={emoji_id},error={error}",
        )
        await c.answer("Telegram отклонил Premium emoji", show_alert=True)
        await c.message.answer(
            "❌ <b>Telegram не принял этот Premium emoji для кнопки.</b>\n\n"
            f"Причина API: <code>{html.escape(error)}</code>\n\n"
            "Назначение отменено, обычный emoji сохранён.",
            reply_markup=main_menu(True),
        )
        await render_premium_emoji_button_list(c.message, int(item_id), group, int(page))
        return

    await db.audit(
        c.from_user.id,
        "button_premium_emoji_set_from_library",
        f"button={button_id},item={item_id},emoji={emoji_id}",
    )
    await db.mark_premium_emoji_recent(c.from_user.id, int(item_id))
    await c.answer("Premium emoji установлен ✅ Покупателям ничего не отправлено.", show_alert=True)
    await render_premium_emoji_button_list(c.message, int(item_id), group, int(page))


async def render_premium_emoji_placements(message: Message, page: int = 0) -> None:
    total = await db.premium_emoji_placement_count()
    max_page = max(0, (total - 1) // PREMIUM_PLACEMENT_PAGE_SIZE)
    page = max(0, min(page, max_page))
    rules = await db.premium_emoji_placements(PREMIUM_PLACEMENT_PAGE_SIZE, page * PREMIUM_PLACEMENT_PAGE_SIZE)
    rows: list[list[InlineKeyboardButton]] = []
    for rule in rules:
        mode = {"before":"перед", "after":"после", "replace":"вместо", "replace_emoji":"замена emoji"}.get(str(rule["position"]), str(rule["position"]))
        anchor = str(rule["match_text"]).replace("\n", " ↵ ")
        rows.append([InlineKeyboardButton(
            text=f"{mode}: {anchor}"[:64],
            callback_data=f"premiumemoji:pr:{rule['id']}:{page}",
            icon_custom_emoji_id=str(rule["custom_emoji_id"]),
        )])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"premiumemoji:placements:{page-1}"))
    if page < max_page:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"premiumemoji:placements:{page+1}"))
    if nav:
        rows.append(nav)
    if total:
        rows.append([InlineKeyboardButton(text="🗑 Удалить все расстановки", callback_data="premiumemoji:placementsresetask")])
    rows.append([InlineKeyboardButton(text="⬅️ К Premium emoji", callback_data="adm:premiumemoji")])
    await render_screen(
        message,
        f"🎯 <b>Расстановки Premium emoji</b>\n\nВсего: <b>{total}</b> · страница <b>{page+1}/{max_page+1}</b>.\n\nЗдесь собраны все места в текстах/подписях, куда вы назначили emoji из импортированных наборов.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("premiumemoji:placements:"))
async def premium_emoji_placements_open(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    page = int(c.data.rsplit(":", 1)[1])
    await c.answer()
    await render_premium_emoji_placements(c.message, page)


@router.callback_query(F.data.startswith("premiumemoji:pr:"))
async def premium_emoji_placement_open(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, rule_id, page = c.data.split(":", 3)
    rule = await db.premium_emoji_placement(int(rule_id))
    if not rule:
        await c.answer("Расстановка не найдена", show_alert=True); return
    mode = {"before":"Перед фразой", "after":"После фразы", "replace":"Вместо всей фразы", "replace_emoji":"Заменить обычный emoji"}.get(str(rule["position"]), str(rule["position"]))
    await c.answer()
    await render_screen(
        c.message,
        "🎯 <b>Расстановка Premium emoji</b>\n\n"
        f"Режим: <b>{mode}</b>\n"
        f"Фрагмент: <code>{html.escape(str(rule['match_text']))}</code>\n"
        f"custom_emoji_id: <code>{html.escape(str(rule['custom_emoji_id']))}</code>",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"premiumemoji:prd:{rule_id}:{page}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"premiumemoji:placements:{page}")],
        ]),
    )


@router.callback_query(F.data.startswith("premiumemoji:prd:"))
async def premium_emoji_placement_delete_global(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    _, _, rule_id, page = c.data.split(":", 3)
    await db.delete_premium_emoji_placement(int(rule_id))
    premium_load_placements(await db.premium_emoji_placements())
    await db.audit(c.from_user.id, "premium_emoji_placement_delete", f"rule={rule_id}")
    await c.answer("Удалено ✅")
    await render_premium_emoji_placements(c.message, int(page))


@router.callback_query(F.data == "premiumemoji:placementsresetask")
async def premium_emoji_placements_reset_ask(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    await c.answer()
    await render_screen(
        c.message,
        "🗑 <b>Удалить все расстановки Premium emoji в текстах?</b>\n\nИконки кнопок и импортированные наборы останутся.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да", callback_data="premiumemoji:placementsresetyes")],
            [InlineKeyboardButton(text="❌ Нет", callback_data="premiumemoji:placements:0")],
        ]),
    )


@router.callback_query(F.data == "premiumemoji:placementsresetyes")
async def premium_emoji_placements_reset_yes(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    await db.reset_premium_emoji_placements()
    premium_load_placements([])
    await db.audit(c.from_user.id, "premium_emoji_placements_reset_all")
    await c.answer("Все расстановки удалены ✅", show_alert=True)
    await render_premium_emoji_panel(c.message)


async def render_premium_emoji_single_rules(message: Message) -> None:
    rules = await db.premium_emoji_rules()
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ Добавить отдельные Premium emoji", callback_data="premiumemoji:add")],
    ]
    for rule in rules[:40]:
        fallback = str(rule["fallback_text"] or "").replace("\n", " ")
        emoji_id = str(rule["custom_emoji_id"] or "")
        rows.append([InlineKeyboardButton(
            text=f"💎 {fallback} · …{emoji_id[-8:]}"[:64],
            callback_data=f"premiumemoji:item:{rule['id']}",
            icon_custom_emoji_id=emoji_id,
        )])
    if rules:
        rows.append([InlineKeyboardButton(text="🗑 Удалить все одиночные замены", callback_data="premiumemoji:resetall:ask")])
    rows.append([InlineKeyboardButton(text="⬅️ К Premium emoji", callback_data="adm:premiumemoji")])
    await render_screen(
        message,
        "🌍 <b>Глобальные замены</b>\n\n"
        "Эти правила заменяют обычный Unicode-emoji во всех новых исходящих текстах, подписях и кнопках бота. Их видят покупатели, а не только администратор.\n\n"
        f"Сейчас правил: <b>{len(rules)}</b>.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "premiumemoji:singles")
async def premium_emoji_singles(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    await c.answer()
    await render_premium_emoji_single_rules(c.message)


@router.callback_query(F.data == "premiumemoji:add")
async def premium_emoji_add_start(c: CallbackQuery, state: FSMContext):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    await state.clear()
    await state.set_state(AdminPremiumEmoji.waiting)
    await c.answer()
    await c.message.answer(
        "💎 <b>Добавление отдельных Premium emoji</b>\n\n"
        "Отправьте сообщением <b>один или несколько</b> Premium/custom emoji. Бот запомнит их ID и будет заменять соответствующие обычные Unicode-emoji во всех исходящих текстах и подписях.\n\n"
        "Для целого набора удобнее вернуться назад и выбрать «🔗 Добавить набор по ссылке».",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminPremiumEmoji.waiting)
async def premium_emoji_add_save(m: Message, state: FSMContext):
    if await admin_role(m.from_user.id) != "owner":
        await state.clear(); await m.answer("⛔ Нет доступа."); return
    if ui_text_matches(m.text, "❌ Отмена"):
        await state.clear(); await m.answer("Добавление отменено.", reply_markup=main_menu(True)); return
    pairs = extract_custom_emoji_pairs(m.text, m.entities)
    if not pairs:
        await m.answer("Не нашёл custom emoji. Отправьте именно Premium emoji из Telegram.")
        return
    saved = 0
    for fallback, emoji_id in pairs:
        row = await db.upsert_premium_emoji_rule(fallback, emoji_id)
        if row:
            premium_set_rule(fallback, emoji_id)
            saved += 1
    await db.audit(m.from_user.id, "premium_emoji_rules_add", f"count={saved}")
    await state.clear()
    await m.answer(f"✅ Добавлено/обновлено Premium emoji: <b>{saved}</b>.", reply_markup=main_menu(True))
    await render_premium_emoji_single_rules(m)


@router.callback_query(F.data.startswith("premiumemoji:item:"))
async def premium_emoji_item(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    rule_id = int(c.data.rsplit(":", 1)[1])
    rule = await db.premium_emoji_rule(rule_id)
    if not rule:
        await c.answer("Правило не найдено", show_alert=True)
        await render_premium_emoji_single_rules(c.message)
        return
    fallback = str(rule["fallback_text"] or "")
    emoji_id = str(rule["custom_emoji_id"] or "")
    await c.answer()
    await render_screen(
        c.message,
        "💎 <b>Глобальная замена</b>\n\n"
        f"Обычный символ: <b>{html.escape(fallback)}</b>\n"
        f"custom_emoji_id: <code>{html.escape(emoji_id)}</code>\n\n"
        f"Тест: {html.escape(fallback)}",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить это правило", callback_data=f"premiumemoji:delete:{rule_id}")],
            [InlineKeyboardButton(text="⬅️ К одиночным заменам", callback_data="premiumemoji:singles")],
        ]),
    )


@router.callback_query(F.data.startswith("premiumemoji:delete:"))
async def premium_emoji_delete(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    rule_id = int(c.data.rsplit(":", 1)[1])
    row = await db.delete_premium_emoji_rule(rule_id)
    if row:
        fallback = str(row["fallback_text"] or "")
        premium_remove_rule(fallback)
        await db.audit(c.from_user.id, "premium_emoji_rule_delete", f"rule={rule_id}")
    await c.answer("Удалено ✅")
    await render_premium_emoji_single_rules(c.message)


@router.callback_query(F.data == "premiumemoji:resetall:ask")
async def premium_emoji_reset_all_ask(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    await c.answer()
    await render_screen(
        c.message,
        "🗑 <b>Удалить все глобальные Premium emoji замены?</b>\n\nИмпортированные наборы, расстановки и иконки кнопок останутся.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить все", callback_data="premiumemoji:resetall:yes")],
            [InlineKeyboardButton(text="❌ Нет", callback_data="premiumemoji:singles")],
        ]),
    )


@router.callback_query(F.data == "premiumemoji:resetall:yes")
async def premium_emoji_reset_all_yes(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True); return
    await db.reset_premium_emoji_rules()
    premium_clear_rules()
    await db.audit(c.from_user.id, "premium_emoji_rules_reset_all")
    await c.answer("Все глобальные замены удалены ✅", show_alert=True)
    await render_premium_emoji_single_rules(c.message)


async def notify_admin_receipt(bot:Bot,oid:int):
    o=await db.order(oid);items=await db.order_items(oid);admins=await db.get_admins();text=(f"💳 <b>Оплата на проверку · заказ {order_ref(o)}</b>\n\n"+order_delivery_summary(o)+"\n\n"+"\n".join(f"• {html.escape(i['product_name'])} {html.escape(i['color'] or '')}/{html.escape(i['size'])} × {i['qty']}" for i in items)+f"\n\nИтого: <b>{money(o['total'])}</b>")
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Подтвердить оплату",callback_data=f"payok:{oid}")],[InlineKeyboardButton(text="❌ Отклонить чек",callback_data=f"payno:{oid}")]])
    for a in admins:
        if a["role"] not in ("owner","manager"):continue
        try:
            if o["receipt_type"]=="document":await bot.send_document(chat_id=a["user_id"],document=o["receipt_file_id"],caption=text,reply_markup=kb)
            else:await bot.send_photo(chat_id=a["user_id"],photo=o["receipt_file_id"],caption=text,reply_markup=kb)
        except Exception:logging.exception("admin receipt notify")


async def confirm_paid_order(bot:Bot,oid:int,method:str,actor_id:int)->tuple[bool,str]:
    o=await db.order(oid)
    if not o:return False,"Заказ не найден"
    ok,error,already=await db.consume_reservation_and_confirm(oid,method,actor_id)
    if not ok:return False,error
    o=await db.order(oid)

    # All financial side effects are idempotent. Re-running this function after a
    # crash safely finishes work that may have been committed only partially.
    benefits=await db.apply_paid_benefits(o)
    inviter=await db.reward_referral(o["user_id"],oid)

    # Avoid duplicate buyer confirmations when an admin double-clicks the action.
    if await db.claim_order_notification(oid,"payment"):
        paid_text=f"✅ <b>Оплата подтверждена!</b>\nЗаказ {order_ref(o)} принят в работу."
        if benefits.get("cashback"):
            paid_text+=f"\n\n🎁 Начислено <b>{benefits['cashback']} бонусов</b>.\nВаш баланс: <b>{benefits['balance']} бонусов</b>."
        try:
            await bot.send_message(
                chat_id=o["user_id"],
                text=paid_text,
                reply_markup=customer_order_keyboard(oid),
            )
        except Exception:
            await db.reset_order_notification(oid,"payment")
            logging.exception("buyer payment confirmation notify")

    if inviter:
        try:
            await bot.send_message(chat_id=inviter,text=f"🎁 Ваш приглашённый друг сделал первую покупку. Начислено {settings.referral_bonus_points} бонусов!")
        except Exception:
            logging.exception("referral reward notify")

    # Low-stock alert is emitted at most once for this paid order.
    if await db.claim_order_notification(oid,"low_stock"):
        try:
            low=await db.fetchall("""SELECT v.*,p.name FROM product_variants v JOIN products p ON p.id=v.product_id WHERE v.stock<=? ORDER BY v.stock LIMIT 30""",(settings.low_stock_threshold,))
            if low:
                text="⚠️ <b>Низкие остатки</b>\n\n"+"\n".join(f"• {html.escape(x['name'])} · {html.escape(x['color'])}/{html.escape(x['size'])} — {x['stock']} шт." for x in low)
                for a in await db.get_admins():
                    if a["role"] in ("owner","manager"):
                        try:await bot.send_message(chat_id=a["user_id"],text=text)
                        except Exception:logging.exception("low stock notify")
        except Exception:
            await db.reset_order_notification(oid,"low_stock")
            logging.exception("low stock alert build")
    return True,""


@router.callback_query(F.data.startswith("payok:"))
async def pay_ok(c:CallbackQuery,bot:Bot):
    if (await admin_role(c.from_user.id)) not in ("owner","manager"):
        await c.answer("Нет доступа",show_alert=True);return
    oid=int(c.data.split(":")[1])
    await c.answer("Подтверждаю оплату…")
    ok,error=await confirm_paid_order(bot,oid,"manual",c.from_user.id)
    if not ok:
        await c.message.answer(f"❌ Не удалось подтвердить оплату: {html.escape(error)}",reply_markup=admin_order_quick_keyboard(oid));return
    await db.audit(c.from_user.id,"payment_confirm",f"order={oid}")
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        logging.exception("Could not clear payment buttons")
    o=await db.order(oid)
    if not o:
        return
    # Payment confirmation is the end of this admin step. The order is parked in
    # the shipping queue; no FSM/tracking prompt is opened automatically.
    await c.message.answer(
        f"✅ <b>Оплата подтверждена · {order_ref(o)}</b>\n\n"
        f"💰 {money(o['total'])}\n"
        f"🚚 Заказ перемещён в <b>К отправке</b>.\n\n"
        "Когда посылка будет готова, откройте её в разделе «К отправке» и добавьте трек-номер.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚚 Открыть «К отправке»",callback_data="adm:shipping")],
            [InlineKeyboardButton(text="📦 Открыть этот заказ",callback_data=f"admorderq:shipping:{oid}")],
            [InlineKeyboardButton(text="💳 Следующая оплата",callback_data="adm:payments")],
        ]),
    )


@router.callback_query(F.data.startswith("payno:"))
async def pay_no(c:CallbackQuery,bot:Bot):
    if (await admin_role(c.from_user.id)) not in ("owner","manager"):await c.answer("Нет доступа",show_alert=True);return
    oid=int(c.data.split(":")[1]);o=await db.order(oid)
    if not o:await c.answer("Заказ не найден",show_alert=True);return
    await c.answer("Отклоняю чек…")
    await db.set_order_status(oid,"Чек отклонён",c.from_user.id);await db.audit(c.from_user.id,"payment_reject",f"order={oid}")
    reservation_error = ""
    try:
        await db.ensure_order_reservation(oid, minutes=settings.reservation_minutes)
    except ValueError as exc:
        # The old hold may already have expired and another buyer may have taken
        # the stock. Rejecting the receipt must still succeed; simply do not
        # promise the customer a reservation that no longer exists.
        reservation_error = str(exc)
        logging.warning("Could not renew reservation for rejected order %s: %s", oid, exc)
    try:await c.message.edit_reply_markup(reply_markup=None)
    except Exception:logging.exception("Suppressed exception")
    # Отдельное заметное сообщение админу после отклонения оплаты.
    retry_note = (
        "Покупатель сможет отправить чек повторно."
        if not reservation_error
        else f"⚠️ Повторный резерв не создан: {html.escape(reservation_error)}"
    )
    await c.message.answer(
        f"❌ <b>Оплата заказа {order_ref(o)} отклонена</b>\n\n"
        f"Заказ не подтверждён. {retry_note}\n"
        f"💰 Сумма: <b>{money(o['total'])}</b>\n"
        f"📦 Статус: <b>Чек отклонён</b>",
        reply_markup=admin_order_quick_keyboard(oid, next_queue="payments"),
    )
    buyer_text = f"❌ Чек по заказу {order_ref(o)} не подтверждён. Проверьте перевод и отправьте чек повторно."
    allow_receipt = not bool(reservation_error)
    if reservation_error:
        buyer_text = (
            f"❌ Чек по заказу {order_ref(o)} не подтверждён.\n\n"
            "⚠️ Пока чек проверялся, резерв товара закончился, и сейчас заново зарезервировать его не удалось. "
            "Свяжитесь с поддержкой, чтобы уточнить наличие."
        )
    try:await bot.send_message(
        chat_id=o["user_id"],
        text=buyer_text,
        reply_markup=customer_order_keyboard(oid, allow_receipt=allow_receipt),
    )
    except Exception:logging.exception("Suppressed exception")


# -----------------------------
# ADMIN ORDERS / TRACKS / NOTES / STATUS
# -----------------------------
ADMIN_QUEUE_META = {
    "payments": ("status='На проверке оплаты'", "💳 Оплаты на проверке"),
    # Legacy assembly statuses are intentionally included here. Old orders do not
    # need to be clicked through those stages anymore.
    "assembly": ("status IN ('Подтверждён','Собирается','Собран','Передан в доставку')", "🚚 К отправке"),
    "shipping": ("status IN ('Подтверждён','Собирается','Собран','Передан в доставку')", "🚚 К отправке"),
    "finish": ("status IN ('Отправлен','Получен')", "🏁 Завершение"),
    "active": ("status IN ('Подтверждён','Собирается','Собран','Передан в доставку','Отправлен','Получен')", "⚡ Активные заказы"),
    "all": ("1=1", "📋 Все заказы"),
}


def admin_queue_callback(queue: str) -> str:
    return {
        "payments": "adm:payments",
        "assembly": "adm:shipping",
        "shipping": "adm:shipping",
        "finish": "adm:queue:finish",
        "active": "adm:active",
        "all": "adm:queue:all",
    }.get(queue, "adm:orders")


def order_queue_hint(status: str) -> str:
    return {
        "На проверке оплаты": "проверить оплату",
        "Подтверждён": "добавить трек",
        "Собирается": "добавить трек",
        "Собран": "добавить трек",
        "Передан в доставку": "добавить трек",
        "Отправлен": "ожидает получения",
        "Получен": "завершить",
    }.get(status, "открыть")


async def admin_queue_rows(queue: str, limit: int = 50):
    where, _ = ADMIN_QUEUE_META.get(queue, ADMIN_QUEUE_META["active"])
    order_sql = "id DESC" if queue == "all" else "id ASC"
    return await db.fetchall(f"SELECT * FROM orders WHERE {where} ORDER BY {order_sql} LIMIT ?", (limit,))


def _admin_customer_short(o) -> str:
    value=(o['customer_name'] or (('@'+o['username']) if o['username'] else str(o['user_id'])) or '').strip()
    return value[:18] or 'Покупатель'


async def admin_queue_screen(message: Message, queue: str):
    if queue == "assembly":
        queue = "shipping"
    if queue not in ADMIN_QUEUE_META:
        queue = "active"
    rows = await admin_queue_rows(queue)
    _, title = ADMIN_QUEUE_META[queue]
    buttons=[]
    for o in rows:
        if queue == "payments":
            label=f"{order_ref(o)} · {money(o['total'])} · {_admin_customer_short(o)}"
        elif queue == "shipping":
            delivery=(o['delivery_method'] or 'Доставка').replace('Доставка ', '').strip()
            label=f"{order_ref(o)} · {delivery} · {money(o['total'])}"
        elif queue == "finish":
            label=f"{order_ref(o)} · {o['status']} · {money(o['total'])}"
        else:
            label=f"{order_ref(o)} · {o['status']} · {_admin_customer_short(o)}"
        buttons.append([InlineKeyboardButton(text=label[:64],callback_data=f"admorderq:{queue}:{o['id']}")])
    if not buttons:
        buttons.append([InlineKeyboardButton(text="✅ Здесь пока пусто", callback_data="adm:orders")])
    buttons.append([InlineKeyboardButton(text="⬅️ Заказы", callback_data="adm:orders"),
                    InlineKeyboardButton(text="⚙️ Панель", callback_data="adm:home")])
    helper = {
        "payments": "Откройте заказ, проверьте чек и подтвердите или отклоните оплату.",
        "shipping": "Здесь только оплаченные заказы. Откройте заказ, когда посылка готова, и добавьте трек-номер.",
        "finish": "Отправленные заказы: здесь можно отметить получение и завершить заказ.",
        "active": "Все заказы, которые сейчас находятся в работе.",
        "all": "Полная история заказов.",
    }.get(queue, "")
    await render_screen(message, f"{title} · <b>{len(rows)}</b>\n\n{helper}", InlineKeyboardMarkup(inline_keyboard=buttons))


async def next_admin_order_id(queue: str, current_id: int) -> int | None:
    if queue not in ADMIN_QUEUE_META:
        queue="active"
    where,_=ADMIN_QUEUE_META[queue]
    row=await db.fetchone(f"SELECT id FROM orders WHERE {where} AND id<>? ORDER BY id ASC LIMIT 1", (current_id,))
    return int(row["id"]) if row else None


async def admin_order_card(message:Message,oid:int,queue:str="active"):
    if queue == "assembly":
        queue = "shipping"
    o=await db.order(oid);items=await db.order_items(oid)
    role=await admin_role(message.chat.id)
    if not o:
        await render_screen(message,"Заказ не найден.");return

    customer=(o['customer_name'] or '').strip() or ((('@'+o['username']) if o['username'] else str(o['user_id'])))
    phone=(o['phone'] or '').strip() or '—'
    text=(
        f"📦 <b>Заказ {order_ref(o)}</b>\n"
        f"Статус: <b>{html.escape(o['status'])}</b>\n\n"
        f"👤 Покупатель: <b>{html.escape(customer)}</b>\n"
        f"📱 Телефон: <code>{html.escape(phone)}</code>\n\n"
        f"{order_delivery_summary(o)}\n\n"
        "<b>Состав заказа</b>\n" +
        "\n".join(f"• {html.escape(i['product_name'])} · {html.escape(i['color'] or '')}/{html.escape(i['size'])} × {i['qty']} — {money(i['price']*i['qty'])}" for i in items) +
        f"\n\n💰 Итого: <b>{money(o['total'])}</b>"
    )
    if o['tracking_number']:
        text+=f"\n🚚 Трек: <code>{html.escape(o['tracking_number'])}</code>"
    if o['admin_note']:
        text+=f"\n📝 Заметка: {html.escape(o['admin_note'])}"

    rows=[]
    if o['status']=="На проверке оплаты":
        rows.append([InlineKeyboardButton(text="✅ Подтвердить оплату",callback_data=f"payok:{oid}"),
                     InlineKeyboardButton(text="❌ Отклонить",callback_data=f"payno:{oid}")])
    elif o['status'] in PRE_SHIPPING_STATUSES:
        rows.append([InlineKeyboardButton(text="🚚 Добавить трек-номер",callback_data=f"trackq:shipping:{oid}")])
        text += "\n\n<b>Следующий шаг:</b> когда посылка передана службе доставки, добавьте трек-номер. Остальные промежуточные статусы нажимать не нужно."
    elif o['status']=="Отправлен":
        rows.append([InlineKeyboardButton(text="📬 Отметить полученным",callback_data=f"statusq:finish:{oid}:Получен")])
    elif o['status']=="Получен":
        rows.append([InlineKeyboardButton(text="🏁 Завершить заказ",callback_data=f"statusq:finish:{oid}:Завершён")])
    elif o['status'] in ("Ожидает оплаты","Чек отклонён"):
        text += "\n\n⌛ Сейчас действие за покупателем — ожидаем оплату или новый чек."
    elif o['status']=="Завершён":
        text += "\n\n✅ Заказ полностью завершён."

    service=[]
    if o['receipt_file_id']:
        service.append(InlineKeyboardButton(text="🧾 Чек",callback_data=f"admreceiptq:{queue}:{oid}"))
    if o['tracking_number']:
        service.append(InlineKeyboardButton(text="✏️ Изменить трек",callback_data=f"trackq:{queue}:{oid}"))
    if service:
        rows.append(service)
    rows.append([InlineKeyboardButton(text="📝 Внутренняя заметка",callback_data=f"noteq:{queue}:{oid}")])
    rows.append([InlineKeyboardButton(text="⬅️ К списку",callback_data=admin_queue_callback(queue)),
                 InlineKeyboardButton(text="➡️ Следующий",callback_data=f"admnext:{queue}:{oid}")])
    rows.append([InlineKeyboardButton(text="📦 Все очереди заказов",callback_data="adm:orders")])
    await render_screen(message,text,InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("admextra:"))
async def adm_order_extra(c:CallbackQuery):
    # Старые кнопки «Ещё» из предыдущей версии продолжают работать,
    # но теперь открывают полную карточку заказа без скрытых действий.
    if (await admin_role(c.from_user.id)) not in ("owner","manager","warehouse"):await c.answer("Нет доступа",show_alert=True);return
    _,queue,oid_s=c.data.split(":",2);await c.answer();await admin_order_card(c.message,int(oid_s),queue)


async def send_admin_receipt_view(c:CallbackQuery, oid:int, queue:str):
    o=await db.order(oid)
    if not o or not o["receipt_file_id"]:
        await c.answer("Чек не найден",show_alert=True);return
    await c.answer("Открываю чек…")
    rows=[]
    if o["status"]=="На проверке оплаты":
        rows += [[InlineKeyboardButton(text="✅ Подтвердить оплату",callback_data=f"payok:{oid}")],
                 [InlineKeyboardButton(text="❌ Отклонить чек",callback_data=f"payno:{oid}")]]
    rows.append([InlineKeyboardButton(text="⬅️ К заказу",callback_data=f"admorderq:{queue}:{oid}"),
                 InlineKeyboardButton(text="➡️ Следующий",callback_data=f"admnext:{queue}:{oid}")])
    kb=InlineKeyboardMarkup(inline_keyboard=rows)
    caption=f"🧾 <b>Чек по заказу {order_ref(o)}</b>\n💰 Сумма: <b>{money(o['total'])}</b>\n📦 Статус: <b>{html.escape(o['status'])}</b>"
    try:
        if o["receipt_type"]=="document":await c.message.answer_document(document=o["receipt_file_id"],caption=caption,reply_markup=kb)
        else:await c.message.answer_photo(photo=o["receipt_file_id"],caption=caption,reply_markup=kb)
    except Exception:
        logging.exception("admin receipt view")
        await c.message.answer("Не удалось открыть файл чека.",reply_markup=admin_order_quick_keyboard(oid,next_queue=queue))


@router.callback_query(F.data.startswith("admreceiptq:"))
async def adm_receipt_view_q(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager"):
        await c.answer("Нет доступа",show_alert=True);return
    _,queue,oid_s=c.data.split(":",2)
    await send_admin_receipt_view(c,int(oid_s),queue)


@router.callback_query(F.data.startswith("admreceipt:"))
async def adm_receipt_view(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager"):
        await c.answer("Нет доступа",show_alert=True);return
    await send_admin_receipt_view(c,int(c.data.split(":")[1]),"payments")


@router.callback_query(F.data=="adm:orders")
async def adm_orders(c:CallbackQuery):
    role=await admin_role(c.from_user.id)
    if role not in ("owner","manager","warehouse"):
        await c.answer("Нет доступа",show_alert=True);return
    await c.answer()
    pending=await db.fetchone("SELECT COUNT(*) c FROM orders WHERE status='На проверке оплаты'")
    shipping=await db.fetchone("SELECT COUNT(*) c FROM orders WHERE status IN ('Подтверждён','Собирается','Собран','Передан в доставку')")
    finish=await db.fetchone("SELECT COUNT(*) c FROM orders WHERE status IN ('Отправлен','Получен')")
    active=await db.fetchone("SELECT COUNT(*) c FROM orders WHERE status IN ('Подтверждён','Собирается','Собран','Передан в доставку','Отправлен','Получен')")
    total=await db.fetchone("SELECT COUNT(*) c FROM orders")
    rows=[]
    if role in ("owner","manager"):
        rows.append([InlineKeyboardButton(text=f"💳 Проверить оплаты · {int(pending['c'] or 0)}",callback_data="adm:payments")])
    rows.append([InlineKeyboardButton(text=f"🚚 К отправке · {int(shipping['c'] or 0)}",callback_data="adm:shipping")])
    rows.append([InlineKeyboardButton(text=f"🏁 Завершение · {int(finish['c'] or 0)}",callback_data="adm:queue:finish"),
                 InlineKeyboardButton(text=f"⚡ Активные · {int(active['c'] or 0)}",callback_data="adm:active")])
    rows.append([InlineKeyboardButton(text=f"📋 Все заказы · {int(total['c'] or 0)}",callback_data="adm:queue:all")])
    rows.append([InlineKeyboardButton(text="⬅️ Панель",callback_data="adm:home")])
    await render_screen(
        c.message,
        "📦 <b>Заказы</b>\n\n"
        "Рабочий процесс теперь простой:\n"
        "<b>1.</b> Проверить и подтвердить оплату.\n"
        "<b>2.</b> Заказ автоматически появится в «К отправке».\n"
        "<b>3.</b> Когда отправили посылку — открыть заказ и ввести трек.\n\n"
        "Никаких отдельных кнопок «Сборка / Собран / Передан в доставку» больше не требуется.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data=="adm:active")
async def adm_active(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","warehouse"):await c.answer("Нет доступа",show_alert=True);return
    await c.answer();await admin_queue_screen(c.message,"active")


@router.callback_query(F.data=="adm:queue:assembly")
async def adm_queue_assembly(c:CallbackQuery):
    # Legacy buttons from old admin messages now open the new unified shipping queue.
    if (await admin_role(c.from_user.id)) not in ("owner","manager","warehouse"):
        await c.answer("Нет доступа",show_alert=True);return
    await c.answer();await admin_queue_screen(c.message,"shipping")


@router.callback_query(F.data=="adm:queue:finish")
async def adm_queue_finish(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","warehouse"):await c.answer("Нет доступа",show_alert=True);return
    await c.answer();await admin_queue_screen(c.message,"finish")


@router.callback_query(F.data=="adm:queue:all")
async def adm_queue_all(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","warehouse"):await c.answer("Нет доступа",show_alert=True);return
    await c.answer();await admin_queue_screen(c.message,"all")


@router.callback_query(F.data.startswith("admorderq:"))
async def adm_order_q(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","warehouse"):await c.answer("Нет доступа",show_alert=True);return
    _,queue,oid_s=c.data.split(":",2);await c.answer();await admin_order_card(c.message,int(oid_s),queue)


@router.callback_query(F.data.startswith("admorder:"))
async def adm_order(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","warehouse"):await c.answer("Нет доступа",show_alert=True);return
    await c.answer();await admin_order_card(c.message,int(c.data.split(":")[1]),"active")


@router.callback_query(F.data=="adm:payments")
async def adm_payments(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager"):await c.answer("Нет доступа",show_alert=True);return
    await c.answer();await admin_queue_screen(c.message,"payments")


@router.callback_query(F.data=="adm:shipping")
async def adm_shipping(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","warehouse"):await c.answer("Нет доступа",show_alert=True);return
    await c.answer();await admin_queue_screen(c.message,"shipping")


@router.callback_query(F.data.startswith("admnext:"))
async def adm_next(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","warehouse"):await c.answer("Нет доступа",show_alert=True);return
    _,queue,current_s=c.data.split(":",2);current=int(current_s);await c.answer();nxt=await next_admin_order_id(queue,current)
    if not nxt:
        await admin_queue_screen(c.message,queue);return
    await admin_order_card(c.message,nxt,queue)


async def apply_admin_status(c:CallbackQuery,bot:Bot,oid:int,status:str,queue:str):
    o=await db.order(oid)
    if not o:await c.answer("Заказ не найден",show_alert=True);return
    if o["status"] in PRE_SHIPPING_STATUSES and status in ("Собирается","Собран","Передан в доставку"):
        await c.answer("Промежуточные этапы больше не нужны — добавьте трек в «К отправке»",show_alert=True)
        await admin_order_card(c.message,oid,"shipping")
        return
    allowed=STATUS_NEXT.get(o["status"])
    if not allowed or allowed[0]!=status:await c.answer("Недопустимый переход",show_alert=True);return
    await c.answer("Готово ✅")
    await db.set_order_status(oid,status,c.from_user.id);await db.audit(c.from_user.id,"order_status",f"order={oid}, status={status}")
    try:
        if status=="Завершён":
            # Финальный шаг: после полного завершения клиент получает кнопку отзыва.
            # Она появляется только здесь и ведёт на настроенную публикацию отзывов.
            review_url=await configured_review_url()
            final_kb=review_url_keyboard(review_url)
            await bot.send_message(
                chat_id=o["user_id"],
                text=f"📦 <b>Заказ {order_ref(o)}</b>\n{STATUS_USER_TEXT.get(status,status)}",
                reply_markup=final_kb,
            )
        else:
            await bot.send_message(
                chat_id=o["user_id"],
                text=f"📦 <b>Заказ {order_ref(o)}</b>\n{STATUS_USER_TEXT.get(status,status)}",
                reply_markup=customer_order_keyboard(oid),
            )
    except Exception:logging.exception("Suppressed exception")
    await admin_order_card(c.message,oid,queue)


@router.callback_query(F.data.startswith("statusq:"))
async def adm_status_q(c:CallbackQuery,bot:Bot):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","warehouse"):await c.answer("Нет доступа",show_alert=True);return
    _,queue,oid_s,status=c.data.split(":",3);await apply_admin_status(c,bot,int(oid_s),status,queue)


# Совместимость со старыми сообщениями/кнопками из предыдущей версии.
@router.callback_query(F.data.startswith("status:"))
async def adm_status(c:CallbackQuery,bot:Bot):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","warehouse"):await c.answer("Нет доступа",show_alert=True);return
    _,oid_s,status=c.data.split(":",2);o=await db.order(int(oid_s))
    queue="active"
    if o:
        if o["status"] in PRE_SHIPPING_STATUSES:queue="shipping"
        elif o["status"] in ("Отправлен","Получен"):queue="finish"
    await apply_admin_status(c,bot,int(oid_s),status,queue)


@router.callback_query(F.data.startswith("trackq:"))
async def track_start_q(c:CallbackQuery,state:FSMContext):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","warehouse"):
        await c.answer("Нет доступа",show_alert=True);return
    _,queue,oid_s=c.data.split(":",2);oid=int(oid_s);o=await db.order(oid)
    if not o:
        await c.answer("Заказ не найден",show_alert=True);return
    await state.set_state(AdminTracking.waiting_track)
    await state.update_data(order_id=oid,queue=("shipping" if queue=="assembly" else queue))
    await c.answer()
    current=f"\nТекущий трек: <code>{html.escape(o['tracking_number'])}</code>" if o['tracking_number'] else ""
    await c.message.answer(
        f"🚚 <b>Трек-номер · {order_ref(o)}</b>\n\n"
        f"Служба: <b>{html.escape(o['delivery_method'] or 'не указана')}</b>{current}\n\n"
        "Отправьте трек-номер одним сообщением. После сохранения заказ автоматически станет «Отправлен», а покупатель получит трек.",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(F.data.startswith("track:"))
async def track_start(c:CallbackQuery,state:FSMContext):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","warehouse"):
        await c.answer("Нет доступа",show_alert=True);return
    oid=int(c.data.split(":")[1]);o=await db.order(oid)
    if not o:
        await c.answer("Заказ не найден",show_alert=True);return
    await state.set_state(AdminTracking.waiting_track);await state.update_data(order_id=oid,queue="shipping");await c.answer()
    await c.message.answer(
        f"🚚 <b>Трек-номер · {order_ref(o)}</b>\n\n"
        f"Служба: <b>{html.escape(o['delivery_method'] or 'не указана')}</b>\n\n"
        "Отправьте трек-номер одним сообщением.",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminTracking.waiting_track,F.text)
async def track_input(m:Message,state:FSMContext,bot:Bot):
    track=m.text.strip();data=await state.get_data();oid=int(data.get("order_id") or 0);queue=str(data.get("queue") or "shipping")
    if len(track)<4 or "\n" in track:
        await m.answer("Введите корректный трек-номер одной строкой.");return
    o=await db.order(oid)
    if not o:
        await state.clear();await m.answer("Заказ не найден.");return
    await db.set_tracking(oid,track,m.from_user.id)
    await db.audit(m.from_user.id,"tracking",f"order={oid}, track={track}")
    await state.clear();url=tracking_url(o["delivery_method"])
    try:
        await bot.send_message(
            chat_id=o["user_id"],
            text=(
                f"🚚 <b>Заказ {order_ref(o)} передан в службу доставки</b>\n\n"
                f"Служба: <b>{html.escape(o['delivery_method'] or 'не указана')}</b>\n"
                f"Трек-номер: <code>{html.escape(track)}</code>\n\n"
                "Заказ отправлен. Отслеживание доступно по кнопке ниже."
            ),
            reply_markup=customer_order_keyboard(oid,tracking_url_value=url or ""),
        )
    except Exception:
        logging.exception("buyer tracking notify")
    await m.answer(
        f"✅ <b>{order_ref(o)} отправлен</b>\n\n"
        f"Трек сохранён: <code>{html.escape(track)}</code>\n"
        "Покупатель получил сообщение с трек-номером.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➡️ Следующий «К отправке»",callback_data=f"admnext:shipping:{oid}")],
            [InlineKeyboardButton(text="🚚 К отправке",callback_data="adm:shipping"),
             InlineKeyboardButton(text="📦 Заказы",callback_data="adm:orders")],
        ]),
    )


@router.callback_query(F.data.startswith("noteq:"))
async def note_start_q(c:CallbackQuery,state:FSMContext):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","warehouse"):await c.answer("Нет доступа",show_alert=True);return
    _,queue,oid_s=c.data.split(":",2);oid=int(oid_s);await state.set_state(AdminNote.text);await state.update_data(order_id=oid,queue=queue);await c.answer();await c.message.answer("📝 Введите внутренний комментарий (его не увидит покупатель):",reply_markup=cancel_keyboard())


@router.callback_query(F.data.startswith("note:"))
async def note_start(c:CallbackQuery,state:FSMContext):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","warehouse"):await c.answer("Нет доступа",show_alert=True);return
    oid=int(c.data.split(":")[1]);await state.set_state(AdminNote.text);await state.update_data(order_id=oid,queue="active");await c.answer();await c.message.answer("📝 Введите внутренний комментарий (его не увидит покупатель):",reply_markup=cancel_keyboard())


@router.message(AdminNote.text,F.text)
async def note_input(m:Message,state:FSMContext):
    data=await state.get_data();oid=int(data["order_id"]);queue=str(data.get("queue") or "active");await db.set_order_note(oid,m.text.strip());await db.audit(m.from_user.id,"order_note",f"order={oid}");await state.clear();role=await admin_role(m.from_user.id);await m.answer("✅ Комментарий сохранён.",reply_markup=admin_order_quick_keyboard(oid,include_payments=role in ("owner","manager"),next_queue=queue))


# -----------------------------
# ADMIN CATEGORIES / CATEGORY MEDIA
# -----------------------------
def category_media_upload_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Готово")],
            [KeyboardButton(text="⬅️ Назад"), KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )


async def admin_categories_screen(message: Message):
    cats = await db.category_records()
    rows: list[list[InlineKeyboardButton]] = []
    for cat in cats:
        label = f"🗂 {cat['name']} · {int(cat['product_count'] or 0)} тов. · {int(cat['media_count'] or 0)} медиа"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"admcat:{cat['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ К каталогу", callback_data="adm:section:catalog")])
    text = (
        "🗂 <b>Категории</b>\n\n"
        "Выберите категорию. Для каждой можно изменить название и добавить любое количество фото/видео."
        if cats else
        "🗂 <b>Категории</b>\n\nКатегорий пока нет. Они появятся после добавления товара."
    )
    await render_screen(message, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def admin_category_card(message: Message, category_id: int):
    cat = await db.category_record(category_id)
    if not cat:
        await message.answer("Категория не найдена.")
        return
    media = await db.category_media(category_id)
    photos = sum(1 for x in media if (x["media_type"] or "photo") == "photo")
    videos = sum(1 for x in media if (x["media_type"] or "photo") == "video")
    text = (
        f"🗂 <b>{html.escape(cat['name'])}</b>\n\n"
        f"👕 Товаров: <b>{int(cat['product_count'] or 0)}</b>\n"
        f"🎞 Медиа: <b>{len(media)}</b> · фото {photos} · видео {videos}\n\n"
        "Медиа категории показывается покупателю при открытии этой категории в каталоге. "
        "Если ничего не загружено, категория работает как обычный текстовый список товаров."
    )
    rows = [
        [InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"admcatname:{category_id}")],
        [InlineKeyboardButton(text="➕ Добавить фото / видео", callback_data=f"admcatmediaadd:{category_id}")],
        [InlineKeyboardButton(text=f"🎞 Управление медиа · {len(media)}", callback_data=f"admcatmedia:{category_id}")],
        [InlineKeyboardButton(text="👁 Предпросмотр категории", callback_data=f"admcatpreview:{category_id}")],
        [InlineKeyboardButton(text="⬅️ Категории", callback_data="adm:categories")],
    ]
    await render_screen(message, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def admin_category_media_screen(message: Message, category_id: int):
    cat = await db.category_record(category_id)
    if not cat:
        await message.answer("Категория не найдена.")
        return
    media = await db.category_media(category_id)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="➕ Добавить фото / видео", callback_data=f"admcatmediaadd:{category_id}")]
    ]
    for index, item in enumerate(media[:100], 1):
        mt = (item["media_type"] or "photo")
        icon = "🖼" if mt == "photo" else "🎬"
        rows.append([InlineKeyboardButton(
            text=f"🗑 {icon} {MEDIA_TYPE_LABELS.get(mt, mt)} #{index}",
            callback_data=f"admcatmediadel:{category_id}:{item['id']}",
        )])
    if media:
        rows.append([InlineKeyboardButton(text="🧹 Удалить все медиа", callback_data=f"admcatmediaclearask:{category_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ К категории", callback_data=f"admcat:{category_id}")])
    await render_screen(
        message,
        f"🎞 <b>Медиа категории «{html.escape(cat['name'])}»</b>\n\n"
        "Можно загружать фотографии и видео. Несколько файлов показываются покупателю как медиагруппа/альбом. "
        "Нажмите на строку с корзиной, чтобы удалить конкретный файл.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "adm:categories")
async def adm_categories(c: CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner", "manager", "content"):
        await c.answer("Нет доступа", show_alert=True); return
    await c.answer(); await admin_categories_screen(c.message)


@router.callback_query(F.data.startswith("admcat:"))
async def adm_category_open(c: CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner", "manager", "content"):
        await c.answer("Нет доступа", show_alert=True); return
    await c.answer(); await admin_category_card(c.message, int(c.data.split(":", 1)[1]))


@router.callback_query(F.data.startswith("admcatpreview:"))
async def adm_category_preview(c: CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner", "manager", "content"):
        await c.answer("Нет доступа", show_alert=True); return
    await c.answer(); await send_category_screen(c.message, int(c.data.split(":", 1)[1]))


@router.callback_query(F.data.startswith("admcatname:"))
async def adm_category_rename_start(c: CallbackQuery, state: FSMContext):
    if (await admin_role(c.from_user.id)) not in ("owner", "manager", "content"):
        await c.answer("Нет доступа", show_alert=True); return
    category_id = int(c.data.split(":", 1)[1]); cat = await db.category_record(category_id)
    if not cat:
        await c.answer("Категория не найдена", show_alert=True); return
    await state.clear(); await state.set_state(AdminCategoryEdit.name); await state.update_data(category_id=category_id)
    await c.answer()
    await c.message.answer(
        f"✏️ Текущее название: <b>{html.escape(cat['name'])}</b>\n\nВведите новое название категории:",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminCategoryEdit.name, F.text)
async def adm_category_rename_save(m: Message, state: FSMContext):
    value = m.text.strip()
    if ui_text_matches(value, "⬅️ Назад") or ui_text_matches(value, "❌ Отмена"):
        return
    if len(value) < 1 or len(value) > 80:
        await m.answer("Название должно содержать от 1 до 80 символов."); return
    data = await state.get_data(); category_id = int(data.get("category_id") or 0)
    try:
        cat = await db.rename_category(category_id, value)
    except ValueError as exc:
        await m.answer(f"❌ {html.escape(str(exc))}"); return
    await db.audit(m.from_user.id, "category_rename", f"category={category_id}, name={value}")
    await state.clear()
    await m.answer("✅ Категория переименована.", reply_markup=ReplyKeyboardRemove())
    await admin_category_card(m, int(cat["id"]))


@router.callback_query(F.data.startswith("admcatmediaadd:"))
async def adm_category_media_add_start(c: CallbackQuery, state: FSMContext):
    if (await admin_role(c.from_user.id)) not in ("owner", "manager", "content"):
        await c.answer("Нет доступа", show_alert=True); return
    category_id = int(c.data.split(":", 1)[1]); cat = await db.category_record(category_id)
    if not cat:
        await c.answer("Категория не найдена", show_alert=True); return
    await state.clear(); await state.set_state(AdminCategoryEdit.media)
    await state.update_data(category_id=category_id, category_media_added=0)
    await c.answer()
    await c.message.answer(
        f"🎞 <b>Медиа категории «{html.escape(cat['name'])}»</b>\n\n"
        "Отправьте фото или видео. Можно отправить несколько файлов подряд или сразу альбом.\n"
        "Когда закончите — нажмите <b>✅ Готово</b>.",
        reply_markup=category_media_upload_keyboard(),
    )


@router.message(AdminCategoryEdit.media, F.photo | F.video)
async def adm_category_media_receive(m: Message, state: FSMContext):
    media = extract_message_media(m)
    if not media or media[0] not in {"photo", "video"}:
        return
    data = await state.get_data(); category_id = int(data.get("category_id") or 0)
    await db.add_category_media(category_id, media[1], media[0])
    count = int(data.get("category_media_added") or 0) + 1
    await state.update_data(category_media_added=count)
    # Для Telegram-альбома не отвечаем на каждый его элемент, чтобы не засорять админский чат.
    if not m.media_group_id:
        await m.answer(f"✅ Добавлено: {count}. Можно отправить ещё или нажать «✅ Готово».", reply_markup=category_media_upload_keyboard())


@router.message(AdminCategoryEdit.media, UIButtonText("✅ Готово"))
async def adm_category_media_done(m: Message, state: FSMContext):
    data = await state.get_data(); category_id = int(data.get("category_id") or 0); count = int(data.get("category_media_added") or 0)
    await state.clear()
    await db.audit(m.from_user.id, "category_media_add", f"category={category_id}, added={count}")
    await m.answer(f"✅ Загрузка завершена. Добавлено файлов: {count}.", reply_markup=ReplyKeyboardRemove())
    await admin_category_card(m, category_id)


@router.message(AdminCategoryEdit.media)
async def adm_category_media_wrong(m: Message):
    await m.answer("Отправьте именно фотографию или видео, либо нажмите «✅ Готово».", reply_markup=category_media_upload_keyboard())


@router.callback_query(F.data.startswith("admcatmedia:"))
async def adm_category_media_open(c: CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner", "manager", "content"):
        await c.answer("Нет доступа", show_alert=True); return
    await c.answer(); await admin_category_media_screen(c.message, int(c.data.split(":", 1)[1]))


@router.callback_query(F.data.startswith("admcatmediadel:"))
async def adm_category_media_delete(c: CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner", "manager", "content"):
        await c.answer("Нет доступа", show_alert=True); return
    _, category_id_s, media_id_s = c.data.split(":", 2)
    await db.delete_category_media(int(media_id_s))
    await db.audit(c.from_user.id, "category_media_delete", f"category={category_id_s}, media={media_id_s}")
    await c.answer("Удалено")
    await admin_category_media_screen(c.message, int(category_id_s))


@router.callback_query(F.data.startswith("admcatmediaclearask:"))
async def adm_category_media_clear_ask(c: CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner", "manager", "content"):
        await c.answer("Нет доступа", show_alert=True); return
    category_id = int(c.data.split(":", 1)[1])
    await c.answer()
    await render_screen(c.message, "⚠️ Удалить <b>все фото и видео</b> этой категории?", InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить всё", callback_data=f"admcatmediaclear:{category_id}")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admcatmedia:{category_id}")],
    ]))


@router.callback_query(F.data.startswith("admcatmediaclear:"))
async def adm_category_media_clear(c: CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner", "manager", "content"):
        await c.answer("Нет доступа", show_alert=True); return
    category_id = int(c.data.split(":", 1)[1])
    await db.clear_category_media(category_id)
    await db.audit(c.from_user.id, "category_media_clear", f"category={category_id}")
    await c.answer("Все медиа удалены")
    await admin_category_media_screen(c.message, category_id)


# -----------------------------
# ADMIN PRODUCTS / COLORS / PHOTOS / STOCK
# -----------------------------
def color_mode_keyboard(prefix:str,pid:int|None=None) -> InlineKeyboardMarkup:
    suffix=f":{pid}" if pid is not None else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1️⃣ Один цвет",callback_data=f"{prefix}:single{suffix}")],
        [InlineKeyboardButton(text="🎨 Несколько цветов",callback_data=f"{prefix}:multi{suffix}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:fsm_back")],
    ])


def product_photo_step_keyboard(multi:bool) -> ReplyKeyboardMarkup:
    done="✅ Медиа этого цвета загружены" if multi else "✅ Готово"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=done)],
            [KeyboardButton(text="➡️ Без медиа этого цвета")],
            [KeyboardButton(text="⬅️ Назад")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


async def prompt_product_photo_color(message:Message,state:FSMContext):
    d=await state.get_data();colors=list(d.get("photo_colors",[]));idx=int(d.get("photo_color_index",0))
    if not colors:
        await finish_add_product(message,state);return
    color=colors[idx];multi=len(colors)>1
    media_by_color=dict(d.get("media_by_color",{}));count=len(media_by_color.get(color,[]))
    await message.answer(
        f"8/8 🎞 Мультимедиа для цвета <b>{html.escape(color)}</b>.\n\n"
        "Отправьте фото, видео, GIF/анимацию, документ или аудиофайл. Можно отправлять несколько файлов по одному.\n"
        + (f"Уже загружено для этого цвета: <b>{count}</b>.\n" if count else "")
        + ("Когда закончите этот цвет — нажмите «✅ Медиа этого цвета загружены»." if multi else "Когда загрузите всё — нажмите «✅ Готово»."),
        reply_markup=product_photo_step_keyboard(multi),
    )


@router.callback_query(F.data=="adm:add")
async def adm_add(c:CallbackQuery,state:FSMContext):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","content"):await c.answer("Нет доступа",show_alert=True);return
    await state.clear();await state.set_state(AdminAddProduct.category);await c.answer();await c.message.answer("1/8 Категория товара:",reply_markup=cancel_keyboard())


@router.message(AdminAddProduct.category,F.text)
async def add_cat(m:Message,state:FSMContext):await state.update_data(category=m.text.strip());await state.set_state(AdminAddProduct.name);await m.answer("2/8 Название. Можно использовать форматирование Telegram — оно сохранится в карточке товара:")


@router.message(AdminAddProduct.name,F.text)
async def add_name(m:Message,state:FSMContext):
    await state.update_data(name=m.text.strip(), name_html=message_rich_html(m))
    await state.set_state(AdminAddProduct.price)
    await m.answer("3/8 Цена, ₽:")


@router.message(AdminAddProduct.price,F.text)
async def add_price(m:Message,state:FSMContext):
    try:p=int(m.text.replace(" ",""));assert p>0
    except Exception:await m.answer("Введите положительное число.");return
    await state.update_data(price=p);await state.set_state(AdminAddProduct.description);await m.answer("4/8 Описание:")


@router.message(AdminAddProduct.description,F.text)
async def add_desc(m:Message,state:FSMContext):
    await state.update_data(description=m.text.strip(), description_html=message_rich_html(m))
    await state.set_state(AdminAddProduct.weight)
    await m.answer(f"5/8 Вес одной вещи в граммах (например {settings.default_product_weight}):")


@router.message(AdminAddProduct.weight,F.text)
async def add_weight(m:Message,state:FSMContext):
    try:w=max(1,int(m.text.strip()))
    except ValueError:await m.answer("Введите число.");return
    await state.update_data(weight=w);await state.set_state(AdminAddProduct.color_mode);await m.answer(
        "6/8 🎨 <b>Сколько цветов у этого товара?</b>\n\n"
        "• «Один цвет» — например товар выпускается только в белом цвете.\n"
        "• «Несколько цветов» — например одна футболка есть в белом и чёрном цветах.\n\n"
        "Для каждого цвета можно будет загрузить отдельный альбом.",
        reply_markup=color_mode_keyboard("addmode"),
    )


@router.callback_query(F.data=="addmode:single")
async def add_mode_single(c:CallbackQuery,state:FSMContext):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","content"):await c.answer("Нет доступа",show_alert=True);return
    await state.update_data(color_mode="single");await state.set_state(AdminAddProduct.single_color);await c.answer();await c.message.answer(
        "7/8 ✍️ Введите цвет товара вручную.\nМожно написать абсолютно любое название, например: <code>Графит</code>, <code>Молочный</code>, <code>Washed Black</code> или своё.",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(F.data=="addmode:multi")
async def add_mode_multi(c:CallbackQuery,state:FSMContext):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","content"):await c.answer("Нет доступа",show_alert=True);return
    await state.update_data(color_mode="multi");await state.set_state(AdminAddProduct.variants);await c.answer();await c.message.answer(
        "7/8 ✍️ Введите цвета вручную, затем размеры и остатки.\nНазвания цветов придумываете сами — фиксированного списка нет.\nФормат:\n<code>Графит:S=5,M=8,L=3; Молочный:S=2,M=4,L=0</code>",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminAddProduct.single_color,F.text)
async def add_single_color(m:Message,state:FSMContext):
    color=m.text.strip()
    if not color:await m.answer("Введите название цвета.");return
    await state.update_data(single_color=color);await state.set_state(AdminAddProduct.variants);await m.answer(
        "7/8 Теперь укажите размеры и остатки для этого цвета.\nФормат: <code>S=5,M=8,L=3,XL=2</code>",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminAddProduct.variants,F.text)
async def add_variants(m:Message,state:FSMContext):
    d=await state.get_data();mode=d.get("color_mode","multi")
    raw=m.text.strip()
    if mode=="single":raw=f"{d.get('single_color','Основной')}:{raw}"
    spec=parse_variant_text(raw)
    if not spec:
        await m.answer("Не удалось распознать остатки. Попробуйте ещё раз.");return
    colors=[]
    for color,_size in spec:
        if color not in colors:colors.append(color)
    if mode=="single" and len(colors)!=1:
        await m.answer("Для режима «Один цвет» должен быть указан только один цвет.");return
    if mode=="multi" and len(colors)<2:
        await m.answer("Для режима «Несколько цветов» укажите минимум два цвета, например Чёрный и Белый.");return
    await state.update_data(variants=spec,photo_colors=colors,photo_color_index=0,media_by_color={});await state.set_state(AdminAddProduct.photo)
    await prompt_product_photo_color(m,state)


@router.message(AdminAddProduct.photo,F.photo)
@router.message(AdminAddProduct.photo,F.video)
@router.message(AdminAddProduct.photo,F.animation)
@router.message(AdminAddProduct.photo,F.document)
@router.message(AdminAddProduct.photo,F.audio)
async def add_product_photo(m:Message,state:FSMContext):
    payload=extract_message_media(m)
    if not payload:return
    media_type,file_id=payload
    lock=admin_product_photo_locks.setdefault(m.from_user.id,asyncio.Lock())
    async with lock:
        d=await state.get_data();colors=list(d.get("photo_colors",[]));idx=int(d.get("photo_color_index",0))
        if not colors:return
        color=colors[idx];media_by_color=dict(d.get("media_by_color",{}));items=list(media_by_color.get(color,[]))
        if not any(x.get("file_id")==file_id and x.get("media_type")==media_type for x in items):
            items.append({"media_type":media_type,"file_id":file_id})
        media_by_color[color]=items;await state.update_data(media_by_color=media_by_color);count=len(items)
    await m.answer(
        f"✅ {MEDIA_TYPE_LABELS.get(media_type,'Файл')} для цвета <b>{html.escape(color)}</b> добавлен. Всего: <b>{count}</b>.\n"
        "Можно отправить ещё файлы или закончить этот цвет.",
        reply_markup=product_photo_step_keyboard(len(colors)>1),
    )


@router.message(AdminAddProduct.photo, UIButtonText("✅ Готово"))
@router.message(AdminAddProduct.photo, UIButtonText("✅ Медиа этого цвета загружены"))
@router.message(AdminAddProduct.photo, UIButtonText("➡️ Без медиа этого цвета"))
@router.message(AdminAddProduct.photo, UIButtonText("✅ Фото этого цвета загружены"))
@router.message(AdminAddProduct.photo, UIButtonText("➡️ Без фото этого цвета"))
async def add_product_photo_next(m:Message,state:FSMContext):
    d=await state.get_data();colors=list(d.get("photo_colors",[]));idx=int(d.get("photo_color_index",0))
    if idx+1<len(colors):
        await state.update_data(photo_color_index=idx+1);await prompt_product_photo_color(m,state);return
    await finish_add_product(m,state)


async def finish_add_product(m:Message,state:FSMContext):
    d=await state.get_data();media_by_color=dict(d.get("media_by_color",{}))
    pid=await db.add_product(
        d["category"], d["name"], d["price"], d["description"], d["weight"], d["variants"], "",
        d.get("description_html", ""), d.get("name_html", ""), "in_stock"
    )
    total_media=0
    for color in d.get("photo_colors",[]):
        for item in media_by_color.get(color,[]):
            await db.add_media(pid,item["file_id"],item["media_type"],color);total_media+=1
    await db.audit(m.from_user.id,"product_add",f"product={pid}");await state.clear();await m.answer(
        f"✅ Товар #{pid} добавлен. Цветов: {len(d.get('photo_colors',[]))}. Мультимедиа: {total_media}.",
        reply_markup=main_menu(True),
    )


@router.callback_query(F.data=="adm:products")
async def adm_products(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","content"):await c.answer("Нет доступа",show_alert=True);return
    rows=await db.products("1=1",(),100);buttons=[[InlineKeyboardButton(text=f"{'🟠' if product_status_value(p)=='preorder' else '🟢'} #{p['id']} {p['name']} · {money(p['price'])}"[:60],callback_data=f"admprod:{p['id']}")] for p in rows];buttons.append([InlineKeyboardButton(text="⬅️ К каталогу",callback_data="adm:section:catalog")]);await c.answer();await render_screen(c.message,"👕 <b>Товары</b>",InlineKeyboardMarkup(inline_keyboard=buttons))


async def admin_product_card(message:Message,pid:int):
    p=await db.product(pid);vars_=await db.variants(pid);media=await db.product_media(pid)
    if not p:await message.answer("Товар не найден.");return
    colors=unique_variant_colors(vars_);mode="Несколько цветов" if len(colors)>1 else "Один цвет"
    media_counts={}
    type_counts={}
    for item in media:
        key=item["color"] or "Общие/старые"
        media_counts[key]=media_counts.get(key,0)+1
        mt=(item["media_type"] or "photo")
        type_counts[mt]=type_counts.get(mt,0)+1
    media_text=", ".join(f"{html.escape(k)}: {v}" for k,v in media_counts.items()) or "нет"
    types_text=", ".join(f"{MEDIA_TYPE_LABELS.get(k,k)}: {v}" for k,v in type_counts.items()) or "нет"
    text=(
        f"👕 <b>#{pid}</b> {stored_product_name_html(p)}\n"
        f"Статус товара: {product_status_badge_html(p)}\n"
        f"Категория: {html.escape(p['category'])}\n"
        f"Цена: {money(p['price'])}"+(f" (старая {money(p['old_price'])})" if p['old_price'] else "")+
        f"\nВес: {p['weight_grams']} г\n"
        f"🎨 Режим цветов: <b>{mode}</b>\n"
        f"✨ Новинка: {'да' if p['is_new'] else 'нет'} · 🔥 Хит: {'да' if p['is_hit'] else 'нет'}\n"
        f"🎞 Мультимедиа: {len(media)} ({media_text})\n"
        f"Типы: {html.escape(types_text)}\n\n"+
        "\n".join(f"• {html.escape(v['color'])} / {html.escape(v['size'])}: <b>{v['stock']} шт.</b>" for v in vars_)
    )
    rows=[
        [InlineKeyboardButton(text=f"📦 Статус: {product_status_short(p)}",callback_data=f"prodstatus:{pid}")],
        [InlineKeyboardButton(text="💰 Цена/скидка",callback_data=f"editprod:{pid}:price")],
        [InlineKeyboardButton(text="🎨 Цветовой режим / размеры / остатки",callback_data=f"variantsedit:{pid}")],
        [InlineKeyboardButton(text="🎞 Управление мультимедиа",callback_data=f"productmedia:{pid}")],
        [InlineKeyboardButton(text=("✨ Убрать Новинку" if p['is_new'] else "✨ Сделать Новинкой"),callback_data=f"toggleflag:{pid}:is_new"),InlineKeyboardButton(text=("🔥 Убрать Хит" if p['is_hit'] else "🔥 Сделать Хитом"),callback_data=f"toggleflag:{pid}:is_hit")],
        [InlineKeyboardButton(text="✏️ Название",callback_data=f"editprod:{pid}:name"),InlineKeyboardButton(text="📝 Описание",callback_data=f"editprod:{pid}:description")],
        [InlineKeyboardButton(text="👁 Предпросмотр товара",callback_data=f"prodpreview:{pid}")],
        [InlineKeyboardButton(text="➕ Товар в эту категорию",callback_data=f"addincat:{p['category']}")],
        [InlineKeyboardButton(text="🗂 Категория",callback_data=f"editprod:{pid}:category"),InlineKeyboardButton(text="⚖️ Вес",callback_data=f"editprod:{pid}:weight_grams")],
        [InlineKeyboardButton(text="🗑 Удалить",callback_data=f"delprod:{pid}")],
        [InlineKeyboardButton(text="⬅️ Товары",callback_data="adm:products")],
    ]
    await message.answer(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def admin_product_media(message:Message,pid:int):
    media=await db.product_media(pid)
    rows=[[InlineKeyboardButton(text="➕ Добавить фото / видео / GIF / файл",callback_data=f"addphoto:{pid}")]]
    for item in media[:50]:
        mt=(item["media_type"] or "photo")
        color=item["color"] or "Общие"
        label=f"🗑 {MEDIA_TYPE_LABELS.get(mt,mt)} · {color}"[:60]
        rows.append([InlineKeyboardButton(text=label,callback_data=f"delmedia:{pid}:{item['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ К товару",callback_data=f"admprod:{pid}")])
    text=(f"🎞 <b>Мультимедиа товара #{pid}</b>\n\n"
          "Можно добавлять фото, видео, GIF/анимации, документы и аудиофайлы. "
          "Нажатие на строку с корзиной удаляет конкретный файл.")
    await message.answer(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("admprod:"))
async def adm_prod_card(c:CallbackQuery):await c.answer();await admin_product_card(c.message,int(c.data.split(":")[1]))

@router.callback_query(F.data.startswith("prodpreview:"))
async def admin_product_preview(c:CallbackQuery):
    pid=int(c.data.split(":")[1])
    await c.answer()
    await send_product(c.message,pid)


@router.callback_query(F.data.startswith("addincat:"))
async def add_product_in_category(c:CallbackQuery,state:FSMContext):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","content"):
        await c.answer("Нет доступа",show_alert=True)
        return
    category=c.data.split(":",1)[1]
    await state.clear()
    await state.update_data(category=category, category_locked=True)
    await state.set_state(AdminAddProduct.name)
    await c.answer()
    await c.message.answer(f"➕ Новый товар будет добавлен в категорию <b>{html.escape(category)}</b>.\n\n1/7 Название. Можно использовать форматирование Telegram — оно сохранится в карточке товара:",reply_markup=cancel_keyboard())


@router.callback_query(F.data.startswith("productmedia:"))
async def product_media_admin(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","content"):await c.answer("Нет доступа",show_alert=True);return
    pid=int(c.data.split(":")[1]);await c.answer();await admin_product_media(c.message,pid)

@router.callback_query(F.data.startswith("delmedia:"))
async def product_media_delete(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","content"):await c.answer("Нет доступа",show_alert=True);return
    _,pid_s,mid_s=c.data.split(":");pid=int(pid_s);mid=int(mid_s);item=await db.product_media_item(mid)
    if not item or int(item["product_id"])!=pid:await c.answer("Файл не найден",show_alert=True);return
    await db.delete_media(mid);await db.audit(c.from_user.id,"product_media_delete",f"product={pid}, media={mid}");await c.answer("Удалено ✅");await admin_product_media(c.message,pid)
@router.callback_query(F.data.startswith("toggleflag:"))
async def toggle_flag(c:CallbackQuery):
    _,pid_s,field=c.data.split(":");pid=int(pid_s);p=await db.product(pid);await db.update_product(pid,field,0 if p[field] else 1);await db.audit(c.from_user.id,"product_flag",f"product={pid},{field}");await c.answer("Готово");await admin_product_card(c.message,pid)


@router.callback_query(F.data.startswith("prodstatus:"))
async def product_status_menu(c: CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner", "manager", "content"):
        await c.answer("Нет доступа", show_alert=True); return
    pid=int(c.data.split(":")[1]); p=await db.product(pid)
    if not p:
        await c.answer("Товар не найден", show_alert=True); return
    rows=[
        [InlineKeyboardButton(text="🟢 В НАЛИЧИИ", callback_data=f"setprodstatus:{pid}:in_stock")],
        [InlineKeyboardButton(text="🟠 ПРЕДЗАКАЗ", callback_data=f"setprodstatus:{pid}:preorder")],
        [InlineKeyboardButton(text="⬅️ К товару", callback_data=f"admprod:{pid}")],
    ]
    await c.answer()
    await c.message.answer(
        f"📦 <b>Статус товара #{pid}</b>\n\nСейчас: {product_status_badge_html(p)}\n\n"
        "Выберите пометку. Она будет крупно показана в самом верху карточки товара.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("setprodstatus:"))
async def product_status_set(c: CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner", "manager", "content"):
        await c.answer("Нет доступа", show_alert=True); return
    _, pid_s, value = c.data.split(":", 2); pid=int(pid_s)
    if value not in {"in_stock", "preorder"}:
        await c.answer("Неизвестный статус", show_alert=True); return
    await db.update_product(pid, "availability_status", value)
    await db.audit(c.from_user.id, "product_availability_status", f"product={pid},status={value}")
    await c.answer("Статус обновлён ✅", show_alert=True)
    await admin_product_card(c.message, pid)


@router.callback_query(F.data.startswith("editprod:"))
async def edit_prod_start(c:CallbackQuery,state:FSMContext):
    _,pid_s,field=c.data.split(":");await state.set_state(AdminEditValue.value);await state.update_data(product_id=int(pid_s),field=field);await c.answer()
    prompt="Введите новое значение:"
    if field=="price":prompt="Введите текущую цену и старую цену через пробел. Например: <code>4990 5990</code>. Если скидки нет: <code>4990 0</code>"
    if field=="weight_grams":prompt="Введите вес одной единицы товара в граммах:"
    if field=="name":
        p=await db.product(int(pid_s))
        current=stored_product_name_html(p) if p else "—"
        prompt=(
            "Введите новое название товара. <b>Форматирование Telegram сохранится</b>: "
            "жирный, курсив, подчёркивание, зачёркивание, спойлер, код и ссылки.\n\n"
            f"<b>Текущее название:</b>\n{current}"
        )
    if field=="description":
        p=await db.product(int(pid_s))
        current=stored_product_description_html(p) if p else "—"
        prompt=(
            "Введите новое описание товара. <b>Форматирование Telegram сохранится</b>: "
            "жирный, курсив, подчёркивание, зачёркивание, спойлер, код, ссылки и цитаты.\n\n"
            f"<b>Текущее описание:</b>\n{current}"
        )
    await c.message.answer(prompt,reply_markup=cancel_keyboard())
@router.message(AdminEditValue.value,F.text)
async def edit_prod_value(m:Message,state:FSMContext):
    d=await state.get_data();pid=d["product_id"];field=d["field"]
    if field=="price":
        try:parts=m.text.replace(" "," ").split();price=int(parts[0]);old=int(parts[1]) if len(parts)>1 else 0
        except Exception:await m.answer("Пример: 4990 5990");return
        await db.update_product(pid,"price",price);await db.update_product(pid,"old_price",old)
    elif field=="weight_grams":
        try:value=max(1,int(m.text.strip()))
        except ValueError:await m.answer("Введите вес числом в граммах.");return
        await db.update_product(pid,field,value)
    elif field=="description":
        await db.update_product(pid,"description",m.text.strip())
        await db.update_product(pid,"description_html",message_rich_html(m))
    elif field=="name":
        await db.update_product(pid,"name",m.text.strip())
        await db.update_product(pid,"name_html",message_rich_html(m))
    else:await db.update_product(pid,field,m.text.strip())
    await db.audit(m.from_user.id,"product_edit",f"product={pid}, field={field}");await state.clear();await m.answer("✅ Обновлено.");await admin_product_card(m,pid)


@router.callback_query(F.data.startswith("variantsedit:"))
async def variants_edit_start(c:CallbackQuery,state:FSMContext):
    pid=int(c.data.split(":")[1]);vars_=await db.variants(pid);colors=unique_variant_colors(vars_);current="; ".join(f"{v['color']}:{v['size']}={v['stock']}" for v in vars_)
    await state.clear();await state.update_data(product_id=pid,current_variants=current);await c.answer();await c.message.answer(
        f"🎨 <b>Цветовой режим товара</b>\n\nСейчас: <b>{'Несколько цветов' if len(colors)>1 else 'Один цвет'}</b>\n\n"
        "Выберите, должен ли товар иметь один цвет или несколько. После выбора бот попросит полный список размеров и остатков.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить размер",callback_data=f"sizeadd:{pid}"), InlineKeyboardButton(text="➖ Удалить размер",callback_data=f"sizedel:{pid}")],
            [InlineKeyboardButton(text="🎨 Изменить все размеры и остатки",callback_data=f"sizeall:{pid}")],
            [InlineKeyboardButton(text="1 цвет / несколько цветов",callback_data=f"editmode:single:{pid}")],
            [InlineKeyboardButton(text="⬅️ К товару",callback_data=f"admprod:{pid}")]
        ]),
    )


@router.callback_query(F.data.startswith("sizeall:"))
async def size_all_edit(c:CallbackQuery,state:FSMContext):
    pid=int(c.data.split(":")[1]);vars_=await db.variants(pid);current="; ".join(f"{v['color']}:{v['size']}={v['stock']}" for v in vars_)
    await state.set_state(AdminVariantEdit.text);await state.update_data(product_id=pid,variant_mode="multi");await c.answer();await c.message.answer("Введите полный список размеров. Лишние размеры будут удалены.\nПример: <code>Черный:S=5,M=3,L=1</code>\n\nТекущие:\n"+html.escape(current),reply_markup=cancel_keyboard())


@router.callback_query(F.data.startswith("sizeadd:"))
async def size_add_start(c:CallbackQuery,state:FSMContext):
    pid=int(c.data.split(":")[1]);await state.set_state(AdminVariantEdit.text);await state.update_data(product_id=pid,variant_action="add_size");await c.answer();await c.message.answer("Введите размер и остаток. Можно несколько. Пример: <code>XL=10,XXL=5</code>",reply_markup=cancel_keyboard())


@router.callback_query(F.data.startswith("sizedel:"))
async def size_del_start(c:CallbackQuery,state:FSMContext):
    pid=int(c.data.split(":")[1]);await state.set_state(AdminVariantEdit.text);await state.update_data(product_id=pid,variant_action="del_size");await c.answer();await c.message.answer("Введите размеры для удаления через запятую. Пример: <code>XL,XXL</code>",reply_markup=cancel_keyboard())


@router.callback_query(F.data.startswith("editmode:single:"))
async def variants_mode_single(c:CallbackQuery,state:FSMContext):
    pid=int(c.data.split(":")[2]);vars_=await db.variants(pid);current="; ".join(f"{v['color']}:{v['size']}={v['stock']}" for v in vars_)
    await state.set_state(AdminVariantEdit.text);await state.update_data(product_id=pid,variant_mode="single");await c.answer();await c.message.answer(
        "Введите <b>любой цвет вручную</b> и полный список размеров/остатков.\n"
        "Например: <code>Графит:S=5,M=3,L=1</code>\n\nТекущие:\n"+html.escape(current),
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(F.data.startswith("editmode:multi:"))
async def variants_mode_multi(c:CallbackQuery,state:FSMContext):
    pid=int(c.data.split(":")[2]);vars_=await db.variants(pid);current="; ".join(f"{v['color']}:{v['size']}={v['stock']}" for v in vars_)
    await state.set_state(AdminVariantEdit.text);await state.update_data(product_id=pid,variant_mode="multi");await c.answer();await c.message.answer(
        "Введите <b>минимум два цвета вручную</b> и полный список размеров/остатков.\n"
        "Названия цветов могут быть любыми. Например: <code>Графит:S=5,M=3; Молочный:S=2,M=4</code>\n\nТекущие:\n"+html.escape(current),
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminVariantEdit.text,F.text)
async def variants_edit(m:Message,state:FSMContext,bot:Bot):
    d=await state.get_data()
    action=d.get("variant_action")
    pid=d.get("product_id")
    if action=="add_size":
        add={}
        for part in m.text.split(','):
            if '=' in part:
                name,qty=part.split('=',1)
                try:add[name.strip()]=max(0,int(qty.strip()))
                except ValueError:pass
        vars_=await db.variants(pid)
        spec={(v['color'],v['size']):v['stock'] for v in vars_}
        colors=[v['color'] for v in vars_] or ['Основной']
        for color in dict.fromkeys(colors):
            for size,stock in add.items(): spec[(color,size)]=stock
        try:
            await db.set_variants(pid,spec)
        except ValueError as exc:
            await m.answer(f"⚠️ {html.escape(str(exc))}");return
        await state.clear();await m.answer("✅ Размеры добавлены.");await admin_product_card(m,pid);return
    if action=="del_size":
        remove={x.strip() for x in m.text.split(',') if x.strip()}
        vars_=await db.variants(pid)
        spec={(v['color'],v['size']):v['stock'] for v in vars_ if v['size'] not in remove}
        if spec:
            try:
                await db.set_variants(pid,spec)
            except ValueError as exc:
                await m.answer(f"⚠️ {html.escape(str(exc))}");return
            await state.clear();await m.answer("✅ Размеры удалены.");await admin_product_card(m,pid)
        else:
            await m.answer("Нельзя удалить все размеры товара.")
        return
    spec=parse_variant_text(m.text.strip())
    if not spec:await m.answer("Формат не распознан.");return
    mode=d.get("variant_mode","multi");colors=[]
    for color,_size in spec:
        if color not in colors:colors.append(color)
    if mode=="single" and len(colors)!=1:
        await m.answer("Для режима «Один цвет» оставьте только один цвет.");return
    if mode=="multi" and len(colors)<2:
        await m.answer("Для режима «Несколько цветов» нужно минимум два цвета.");return
    pid=d["product_id"]
    try:
        restocked=await db.set_variants(pid,spec)
    except ValueError as exc:
        await m.answer(f"⚠️ {html.escape(str(exc))}");return
    await db.audit(m.from_user.id,"stock_edit",f"product={pid},mode={mode}");await state.clear();await m.answer("✅ Цветовой режим и остатки обновлены.")
    for vid in restocked:
        v=await db.variant(vid);watchers=await db.restock_watchers(vid)
        for uid in watchers:
            try:await bot.send_message(chat_id=uid,text=f"🔔 <b>Снова в наличии!</b>\n{html.escape(v['name'])} · {html.escape(v['color'])}/{html.escape(v['size'])}\nОстаток: {v['stock']} шт.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛍 Открыть товар",callback_data=f"product:{v['product_id']}")]]))
            except Exception:logging.exception("Suppressed exception")
        await db.clear_restock_watchers(vid)
    await admin_product_card(m,pid)


async def prompt_extra_photo_upload(message:Message,state:FSMContext):
    d=await state.get_data();color=d.get("photo_color","");pid=d.get("product_id")
    await message.answer(
        f"🎞 Добавление мультимедиа\nТовар #{pid} · цвет <b>{html.escape(color or 'Общие')}</b>\n\n"
        "Отправьте фото, видео, GIF/анимацию, документ или аудиофайл. Можно добавить несколько файлов. Когда закончите — нажмите «✅ Готово».",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="✅ Готово")],[KeyboardButton(text="⬅️ Назад")],[KeyboardButton(text="❌ Отмена")]],
            resize_keyboard=True,
            one_time_keyboard=False,
        ),
    )


@router.callback_query(F.data.startswith("addphoto:"))
async def add_photo_start(c:CallbackQuery,state:FSMContext):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","content"):await c.answer("Нет доступа",show_alert=True);return
    pid=int(c.data.split(":")[1]);vars_=await db.variants(pid);colors=unique_variant_colors(vars_)
    await state.clear();await state.update_data(product_id=pid)
    if len(colors)<=1:
        color=colors[0] if colors else "Основной";await state.set_state(AdminAddPhoto.photo);await state.update_data(photo_color=color);await c.answer();await prompt_extra_photo_upload(c.message,state);return
    await state.set_state(AdminAddPhoto.color);await c.answer();buttons=[]
    for i in range(0,len(colors),3):
        buttons.append([InlineKeyboardButton(text=f"🎨 {colors[j]}",callback_data=f"addphotocolor:{pid}:{j}") for j in range(i,min(i+3,len(colors)))])
    buttons.append([InlineKeyboardButton(text="⬅️ К товару",callback_data=f"admprod:{pid}")])
    await c.message.answer("Для какого цвета добавить мультимедиа?",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("addphotocolor:"))
async def add_photo_color_selected(c:CallbackQuery,state:FSMContext):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","content"):await c.answer("Нет доступа",show_alert=True);return
    _,pid_s,idx_s=c.data.split(":");pid=int(pid_s);vars_=await db.variants(pid);colors=unique_variant_colors(vars_)
    try:color=colors[int(idx_s)]
    except Exception:await c.answer("Цвет не найден",show_alert=True);return
    await state.set_state(AdminAddPhoto.photo);await state.update_data(product_id=pid,photo_color=color);await c.answer();await prompt_extra_photo_upload(c.message,state)


@router.message(AdminAddPhoto.photo,F.photo)
@router.message(AdminAddPhoto.photo,F.video)
@router.message(AdminAddPhoto.photo,F.animation)
@router.message(AdminAddPhoto.photo,F.document)
@router.message(AdminAddPhoto.photo,F.audio)
async def add_photo_input(m:Message,state:FSMContext):
    if (await admin_role(m.from_user.id)) not in ("owner","manager","content"):await state.clear();await m.answer("⛔ Нет доступа.");return
    payload=extract_message_media(m)
    if not payload:return
    media_type,file_id=payload
    lock=admin_product_photo_locks.setdefault(m.from_user.id,asyncio.Lock())
    async with lock:
        d=await state.get_data();pid=d["product_id"];color=d.get("photo_color","");await db.add_media(pid,file_id,media_type,color)
    await db.audit(m.from_user.id,"product_media",f"product={pid},color={color},type={media_type}");await m.answer(
        f"✅ {MEDIA_TYPE_LABELS.get(media_type,'Файл')} добавлен для цвета <b>{html.escape(color or 'Общие')}</b>. Можно отправить ещё или нажать «✅ Готово».",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="✅ Готово")],[KeyboardButton(text="⬅️ Назад")],[KeyboardButton(text="❌ Отмена")]],resize_keyboard=True,one_time_keyboard=False),
    )


@router.message(AdminAddPhoto.photo, UIButtonText("✅ Готово"))
async def add_photo_done(m:Message,state:FSMContext):
    d=await state.get_data();pid=d.get("product_id");await state.clear();await m.answer("✅ Мультимедиа обновлено.",reply_markup=main_menu(True));
    if pid:await admin_product_card(m,pid)


@router.callback_query(F.data.startswith("delprod:"))
async def del_product(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager","content"):await c.answer("Нет доступа",show_alert=True);return
    pid=int(c.data.split(":")[1])
    try:
        await db.delete_product(pid)
    except ValueError as exc:
        await c.answer(str(exc),show_alert=True);return
    await db.audit(c.from_user.id,"product_delete",f"product={pid}");await c.answer("Удалено");await c.message.answer("✅ Товар удалён.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К товарам",callback_data="adm:products")]]))


# -----------------------------
# ADMIN PROMOS / STATS / CUSTOMERS / REVIEWS
# -----------------------------
@router.callback_query(F.data=="adm:promos")
async def adm_promos(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","content"):await c.answer("Нет доступа",show_alert=True);return
    rows=await db.promos();buttons=[[InlineKeyboardButton(text="➕ Создать промокод",callback_data="promo:add")]]
    for p in rows:
        st=await db.promo_stats(p["id"])
        buttons.append([InlineKeyboardButton(text=f"{'✅' if p['active'] else '⛔'} {p['code']} · {st['purchases']} покупок · −{int(st['discount_sum'] or 0)} ₽"[:64],callback_data=f"promo:view:{p['id']}")])
    buttons.append([InlineKeyboardButton(text="⬅️ К маркетингу",callback_data="adm:section:marketing")])
    await c.answer();await render_screen(c.message,"🎟 <b>Промокоды</b>\n\nНажмите на промокод, чтобы увидеть статистику оплаченных покупок и общую сумму скидки.",InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("promo:view:"))
async def promo_view(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","content"):await c.answer("Нет доступа",show_alert=True);return
    pid=int(c.data.split(":")[2]);p=await db.promo_by_id(pid)
    if not p:await c.answer("Промокод не найден",show_alert=True);return
    st=await db.promo_stats(pid);max_uses="без лимита" if not int(p["max_uses"] or 0) else str(p["max_uses"])
    text=(f"🎟 <b>{html.escape(p['code'])}</b>\n\n"
          f"Скидка: <b>{p['percent']}%</b>\n"
          f"Статус: <b>{'включён' if p['active'] else 'выключен'}</b>\n"
          f"Минимальная сумма заказа: <b>{money(p['min_order'])}</b>\n"
          f"Лимит использований: <b>{max_uses}</b>\n\n"
          f"🛒 Оплаченных покупок: <b>{st['purchases']}</b>\n"
          f"👥 Покупателей: <b>{st['customers']}</b>\n"
          f"💰 Сумма заказов после скидок: <b>{money(st['orders_sum'])}</b>\n"
          f"🏷 Общая сумма скидки по промокоду: <b>{money(st['discount_sum'])}</b>\n\n"
          f"💸 <b>Сумма к выплате по этому промокоду: {money(st['discount_sum'])}</b>")
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📋 Покупки по промокоду",callback_data=f"promo:orders:{pid}")],[InlineKeyboardButton(text=("⛔ Выключить" if p['active'] else "✅ Включить"),callback_data=f"promo:toggle:{pid}")],[InlineKeyboardButton(text="⬅️ К промокодам",callback_data="adm:promos")]])
    await c.answer();await c.message.answer(text,reply_markup=kb)

@router.callback_query(F.data.startswith("promo:orders:"))
async def promo_orders(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","content"):await c.answer("Нет доступа",show_alert=True);return
    pid=int(c.data.split(":")[2]);p=await db.promo_by_id(pid)
    if not p:await c.answer("Промокод не найден",show_alert=True);return
    rows=await db.promo_orders(pid,20)
    if rows:
        body="\n\n".join(f"• Заказ {order_ref(o)} · {html.escape(o['customer_name'] or ('@'+o['username'] if o['username'] else str(o['user_id'])))}\nСумма: {money(o['total'])} · скидка: <b>{money(o['discount_amount'])}</b>" for o in rows)
    else:body="Оплаченных покупок по этому промокоду пока нет."
    await c.answer();await c.message.answer(f"📋 <b>Покупки · {html.escape(p['code'])}</b>\n\n{body}",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад",callback_data=f"promo:view:{pid}")]]))

@router.callback_query(F.data=="promo:add")
async def promo_add_start(c:CallbackQuery,state:FSMContext):await state.set_state(AdminPromo.code);await c.answer();await c.message.answer("Код промокода:",reply_markup=cancel_keyboard())
@router.message(AdminPromo.code,F.text)
async def promo_code(m:Message,state:FSMContext):await state.update_data(code=m.text.strip().upper());await state.set_state(AdminPromo.percent);await m.answer("Скидка в процентах (1–90):",reply_markup=cancel_keyboard())
@router.message(AdminPromo.percent,F.text)
async def promo_percent(m:Message,state:FSMContext):
    try:p=int(m.text);assert 1<=p<=90
    except Exception:await m.answer("Введите 1–90.");return
    await state.update_data(percent=p);await state.set_state(AdminPromo.min_order);await m.answer("Минимальная сумма заказа, ₽ (0 — без ограничения):",reply_markup=cancel_keyboard())
@router.message(AdminPromo.min_order,F.text)
async def promo_min(m:Message,state:FSMContext):
    try:v=max(0,int(m.text))
    except ValueError:await m.answer("Введите число.");return
    await state.update_data(min_order=v);await state.set_state(AdminPromo.max_uses);await m.answer("Максимум использований (0 — без лимита):",reply_markup=cancel_keyboard())
@router.message(AdminPromo.max_uses,F.text)
async def promo_max(m:Message,state:FSMContext):
    try:v=max(0,int(m.text))
    except ValueError:await m.answer("Введите число.");return
    d=await state.get_data()
    try:pid=await db.add_promo(d["code"],d["percent"],d["min_order"],v)
    except Exception:await m.answer("Такой код уже существует.");return
    await db.audit(m.from_user.id,"promo_add",d["code"]);await state.clear();await m.answer("✅ Промокод создан.",reply_markup=main_menu(True));await m.answer("Вернуться к промокодам:",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К промокодам",callback_data="adm:promos")]]))

@router.callback_query(F.data.startswith("promo:toggle:"))
async def promo_toggle(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","content"):await c.answer("Нет доступа",show_alert=True);return
    pid=int(c.data.split(":")[2]);await db.toggle_promo(pid);await db.audit(c.from_user.id,"promo_toggle",f"promo={pid}");await c.answer("Готово");p=await db.promo_by_id(pid)
    await c.message.answer(f"Промокод <b>{html.escape(p['code'])}</b> {'включён ✅' if p['active'] else 'выключен ⛔'}.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📊 Открыть статистику",callback_data=f"promo:view:{pid}")],[InlineKeyboardButton(text="⬅️ К промокодам",callback_data="adm:promos")]]))

@router.callback_query(F.data.startswith("promotoggle:"))
async def promo_toggle_legacy(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","content"):await c.answer("Нет доступа",show_alert=True);return
    pid=int(c.data.split(":")[1]);await db.toggle_promo(pid);await db.audit(c.from_user.id,"promo_toggle",f"promo={pid}");await c.answer("Готово")


@router.callback_query(F.data=="adm:stats")
async def adm_stats(c:CallbackQuery):
    role=await admin_role(c.from_user.id)
    if role not in ("owner","manager","content"):await c.answer("Нет доступа",show_alert=True);return
    today=datetime.now().strftime("%Y-%m-%d");month=datetime.now().strftime("%Y-%m")
    paid="('Подтверждён','Собирается','Собран','Передан в доставку','Отправлен','Получен','Завершён')"
    allr=await db.fetchone(f"SELECT COUNT(*) c,COALESCE(SUM(total),0) s,COALESCE(AVG(total),0) a FROM orders WHERE status IN {paid}");tr=await db.fetchone(f"SELECT COUNT(*) c,COALESCE(SUM(total),0) s FROM orders WHERE status IN {paid} AND created_at LIKE ?",(today+"%",));mr=await db.fetchone(f"SELECT COUNT(*) c,COALESCE(SUM(total),0) s FROM orders WHERE status IN {paid} AND created_at LIKE ?",(month+"%",));users=(await db.user_count_stats())["total"]
    top=await db.fetchall(f"SELECT product_name,SUM(qty) q FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE o.status IN {paid} GROUP BY product_name ORDER BY q DESC LIMIT 5");sizes=await db.fetchall(f"SELECT size,SUM(qty) q FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE o.status IN {paid} GROUP BY size ORDER BY q DESC LIMIT 5")
    text=(f"📊 <b>Статистика магазина</b>\n\nСегодня: {tr['c']} заказов · {money(tr['s'])}\nМесяц: {mr['c']} · {money(mr['s'])}\nВсего оплачено: {allr['c']} · {money(allr['s'])}\nСредний чек: {money(allr['a'])}\nПользователей: {users}\n\n🔥 Топ товаров:\n"+"\n".join(f"• {html.escape(x['product_name'])} — {x['q']} шт." for x in top)+"\n\n📏 Популярные размеры:\n"+"\n".join(f"• {html.escape(x['size'])} — {x['q']} шт." for x in sizes))
    await c.answer();await c.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home")]]))


@router.callback_query(F.data=="adm:customers")
async def adm_customers(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager"):await c.answer("Нет доступа",show_alert=True);return
    rows=await db.customers(50);buttons=[[InlineKeyboardButton(text=f"{u['full_name'] or u['username'] or u['user_id']} · 🎁 {u['bonus_balance']}"[:64],callback_data=f"customer:{u['user_id']}")] for u in rows]
    buttons.append([InlineKeyboardButton(text="🎁 Бонусы покупателей",callback_data="adm:bonuses")]);buttons.append([InlineKeyboardButton(text="⬅️ К клиентам",callback_data="adm:section:clients")]);await c.answer();await render_screen(c.message,"👥 <b>Клиенты</b>",InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("customer:"))
async def customer_card(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager"):await c.answer("Нет доступа",show_alert=True);return
    uid=int(c.data.split(":")[1]);u=await db.user_row(uid)
    if not u:await c.answer("Покупатель не найден",show_alert=True);return
    orders=await db.user_orders(uid,20);spend=await db.lifetime_spend(uid);last=orders[0] if orders else None
    history="\n".join(f"• {order_ref(o)} · {o['status']} · {money(o['total'])}" for o in orders[:8]) or "Нет заказов"
    phone=(last['phone'] if last else '') or '—';recipient=(last['recipient_full_name'] if last else '') or '—'
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Начислить бонусы",callback_data=f"bonusadd:{uid}"),InlineKeyboardButton(text="➖ Списать бонусы",callback_data=f"bonussub:{uid}")],[InlineKeyboardButton(text="⬅️ К клиентам",callback_data="adm:customers")]])
    await c.answer();await c.message.answer(f"👤 <b>{html.escape(u['full_name'] or '')}</b>\nID: <code>{uid}</code>\nUsername: @{html.escape(u['username'] or '')}\nТелефон из последнего заказа: {html.escape(phone)}\nПолучатель: {html.escape(recipient)}\nРегистрация: {u['first_started_at']}\nПоследний вход: {u['last_seen_at']}\nПокупок: {len(orders)}\nСумма оплаченных: {money(spend)}\n🎁 <b>Бонусы: {u['bonus_balance']}</b>\n\n<b>Последние заказы:</b>\n{history}",reply_markup=kb)

@router.callback_query(F.data=="adm:bonuses")
async def adm_bonuses(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager"):await c.answer("Нет доступа",show_alert=True);return
    rows=await db.bonus_customers(50);buttons=[]
    for u in rows:
        name=u['full_name'] or (('@'+u['username']) if u['username'] else str(u['user_id']))
        buttons.append([InlineKeyboardButton(text=f"🎁 {u['bonus_balance']} · {name}"[:64],callback_data=f"bonususer:{u['user_id']}")])
    buttons.append([InlineKeyboardButton(text="⬅️ К клиентам",callback_data="adm:section:clients")]);await c.answer();await render_screen(c.message,"🎁 <b>Бонусы покупателей</b>\n\nНажмите на покупателя, чтобы посмотреть баланс, начислить или списать бонусы.",InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("bonususer:"))
async def bonus_user(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager"):await c.answer("Нет доступа",show_alert=True);return
    uid=int(c.data.split(":")[1]);u=await db.user_row(uid)
    if not u:await c.answer("Покупатель не найден",show_alert=True);return
    spend=await db.lifetime_spend(uid);orders=await db.user_orders(uid,100);paid={'Подтверждён','Собирается','Собран','Передан в доставку','Отправлен','Получен','Завершён'};paid_count=sum(1 for o in orders if o['status'] in paid)
    name=u['full_name'] or (('@'+u['username']) if u['username'] else str(uid));kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Начислить",callback_data=f"bonusadd:{uid}"),InlineKeyboardButton(text="➖ Списать",callback_data=f"bonussub:{uid}")],[InlineKeyboardButton(text="👤 Карточка клиента",callback_data=f"customer:{uid}")],[InlineKeyboardButton(text="⬅️ К бонусам",callback_data="adm:bonuses")]])
    await c.answer();await c.message.answer(f"🎁 <b>Бонусы покупателя</b>\n\n👤 {html.escape(name)}\nID: <code>{uid}</code>\nБаланс: <b>{u['bonus_balance']} бонусов</b>\nОплаченных покупок: <b>{paid_count}</b>\nСумма покупок: <b>{money(spend)}</b>",reply_markup=kb)

@router.callback_query(F.data.startswith("bonusadd:"))
async def bonus_add_start(c:CallbackQuery,state:FSMContext):
    if (await admin_role(c.from_user.id)) not in ("owner","manager"):await c.answer("Нет доступа",show_alert=True);return
    uid=int(c.data.split(":")[1]);u=await db.user_row(uid)
    if not u:await c.answer("Покупатель не найден",show_alert=True);return
    await state.update_data(bonus_user_id=uid,bonus_mode="add");await state.set_state(AdminBonus.amount);await c.answer();await c.message.answer(f"➕ <b>Начисление бонусов</b>\n\nПокупатель: {html.escape(u['full_name'] or u['username'] or str(uid))}\nТекущий баланс: <b>{u['bonus_balance']}</b>\n\nВведите количество бонусов:",reply_markup=cancel_keyboard())

@router.callback_query(F.data.startswith("bonussub:"))
async def bonus_subtract_start(c:CallbackQuery,state:FSMContext):
    if (await admin_role(c.from_user.id)) not in ("owner","manager"):await c.answer("Нет доступа",show_alert=True);return
    uid=int(c.data.split(":")[1]);u=await db.user_row(uid)
    if not u:await c.answer("Покупатель не найден",show_alert=True);return
    await state.update_data(bonus_user_id=uid,bonus_mode="subtract");await state.set_state(AdminBonus.amount);await c.answer();await c.message.answer(f"➖ <b>Списание бонусов</b>\n\nПокупатель: {html.escape(u['full_name'] or u['username'] or str(uid))}\nТекущий баланс: <b>{u['bonus_balance']}</b>\n\nВведите количество бонусов для списания:",reply_markup=cancel_keyboard())

@router.message(AdminBonus.amount,F.text)
async def bonus_add_amount(m:Message,state:FSMContext,bot:Bot):
    if (await admin_role(m.from_user.id)) not in ("owner","manager"):await state.clear();await m.answer("⛔ Нет доступа.");return
    try:
        amount=int(m.text.strip())
        if amount<=0 or amount>1000000:raise ValueError
    except ValueError:
        await m.answer("Введите целое число от 1 до 1 000 000.");return
    data=await state.get_data();uid=int(data.get('bonus_user_id') or 0);mode=data.get('bonus_mode','add')
    delta=amount if mode=='add' else -amount
    try:new_balance=await db.adjust_bonus(uid,delta)
    except ValueError as e:await m.answer(f"⚠️ {html.escape(str(e))}");return
    action='bonus_add' if mode=='add' else 'bonus_subtract';verb='Начислено' if mode=='add' else 'Списано';symbol='➕' if mode=='add' else '➖'
    await db.audit(m.from_user.id,action,f"user={uid}; amount={amount}; balance={new_balance}");await state.clear();delivered=True
    try:
        if mode=='add':
            await bot.send_message(chat_id=uid,text=f"🎁 <b>Вам начислено {amount} бонусов!</b>\n\nВаш новый баланс: <b>{new_balance} бонусов</b>.")
        else:
            await bot.send_message(chat_id=uid,text=f"🎁 <b>С вашего бонусного баланса списано {amount} бонусов.</b>\n\nВаш новый баланс: <b>{new_balance} бонусов</b>.")
    except TelegramForbiddenError:delivered=False;await db.mark_blocked(uid)
    except Exception:delivered=False;logging.exception("bonus notification failed")
    note="" if delivered else "\n⚠️ Баланс изменён, но уведомление пользователю не доставлено."
    await m.answer(f"✅ {symbol} {verb} <b>{amount} бонусов</b>.\nНовый баланс: <b>{new_balance}</b>.{note}",reply_markup=main_menu(True))


@router.callback_query(F.data=="adm:reviews")
async def adm_reviews(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager"):await c.answer("Нет доступа",show_alert=True);return
    url=await configured_review_url()
    current=f"<code>{html.escape(url)}</code>" if url else "<i>не указана</i>"
    buttons=[[InlineKeyboardButton(text="✏️ Указать / изменить ссылку",callback_data="reviewcfg:set")]]
    if url:
        buttons.append([InlineKeyboardButton(text="💬 Открыть пост с отзывами",url=url)])
        buttons.append([InlineKeyboardButton(text="🗑 Убрать ссылку",callback_data="reviewcfg:clear")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад",callback_data="adm:section:clients")])
    await c.answer();await c.message.answer(
        "⭐ <b>Настройка отзывов</b>\n\n"
        "Укажите ссылку на <b>конкретный пост Telegram-канала</b>, под которым включены комментарии. "
        "Кнопка «Оставить отзыв» появится у покупателя только после полного завершения заказа.\n\n"
        f"Текущая ссылка: {current}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data=="reviewcfg:set")
async def review_cfg_start(c:CallbackQuery,state:FSMContext):
    if (await admin_role(c.from_user.id)) not in ("owner","manager"):await c.answer("Нет доступа",show_alert=True);return
    await state.set_state(AdminReviewLink.url);await c.answer();await c.message.answer(
        "Отправьте ссылку на <b>пост</b> в Telegram-канале, где покупатели должны писать отзывы в комментариях.\n\n"
        "Пример: <code>https://t.me/my_shop_reviews/15</code>\n\n"
        "Важно: у этого поста должны быть включены комментарии.",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminReviewLink.url,F.text)
async def review_cfg_save(m:Message,state:FSMContext):
    if (await admin_role(m.from_user.id)) not in ("owner","manager"):
        await state.clear();return
    url=normalize_review_post_url(m.text)
    if not url:
        await m.answer(
            "❌ Нужна ссылка именно на пост Telegram-канала.\n"
            "Например: <code>https://t.me/my_shop_reviews/15</code>"
        )
        return
    await db.set_setting(REVIEW_POST_SETTING,url);await db.audit(m.from_user.id,"review_post_url",url);await state.clear()
    await m.answer(
        "✅ Ссылка для отзывов сохранена. Кнопка «⭐ Оставить отзыв» появится у покупателя только после полного завершения заказа.",
        reply_markup=main_menu(await is_admin(m.from_user.id)),
    )


@router.callback_query(F.data=="reviewcfg:clear")
async def review_cfg_clear(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","manager"):await c.answer("Нет доступа",show_alert=True);return
    await db.set_setting(REVIEW_POST_SETTING,"");await db.audit(c.from_user.id,"review_post_url_clear");await c.answer("Ссылка удалена ✅",show_alert=True)
    await c.message.answer("⭐ Ссылка для отзывов удалена. Новые кнопки отзывов не будут показываться, пока вы не укажете новую ссылку.")


# -----------------------------
# BROADCAST
# -----------------------------
def unsubscribe_kb():return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔕 Не получать рассылку",callback_data="broadcast:off")]])


@router.callback_query(F.data=="adm:broadcast")
async def broadcast_start(c:CallbackQuery,state:FSMContext):
    if (await admin_role(c.from_user.id)) not in ("owner","content"):await c.answer("Нет доступа",show_alert=True);return
    if broadcast_lock.locked():await c.answer("Рассылка уже выполняется",show_alert=True);return
    users=await db.broadcast_user_ids();await state.clear();await state.set_state(AdminBroadcast.waiting_message);await c.answer();await c.message.answer(f"📣 Отправьте <b>одно готовое сообщение</b> для рассылки.\nПолучателей сейчас: {len(users)}\n\nПоддерживаются текст, фото+подпись, видео, GIF, документ, аудио/голос. После этого будет предпросмотр.",reply_markup=cancel_keyboard())


@router.message(AdminBroadcast.waiting_message)
async def broadcast_preview(m:Message,state:FSMContext,bot:Bot):
    if m.media_group_id:await m.answer("Фотоальбом из нескольких сообщений не поддерживается. Отправьте одно сообщение.");return
    try:await bot.copy_message(chat_id=m.chat.id,from_chat_id=m.chat.id,message_id=m.message_id)
    except TelegramBadRequest:await m.answer("Этот тип сообщения нельзя скопировать. Используйте текст/фото/видео/GIF/документ/аудио.");return
    users=await db.broadcast_user_ids();await state.update_data(source_chat=m.chat.id,source_message=m.message_id);await state.set_state(AdminBroadcast.confirm);await m.answer(f"👆 Предпросмотр. Получателей: <b>{len(users)}</b>",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 Отправить всем",callback_data="broadcast:send")],[InlineKeyboardButton(text="⬅️ Изменить сообщение",callback_data="nav:fsm_back")],[InlineKeyboardButton(text="❌ Отмена",callback_data="broadcast:cancel")]]))


@router.callback_query(F.data=="broadcast:cancel")
async def broadcast_cancel(c:CallbackQuery,state:FSMContext):await state.clear();await c.answer();await c.message.answer("Рассылка отменена.")
@router.callback_query(F.data=="broadcast:send")
async def broadcast_send(c:CallbackQuery,state:FSMContext,bot:Bot):
    if broadcast_lock.locked():await c.answer("Рассылка уже идёт",show_alert=True);return
    data=await state.get_data();uids=await db.broadcast_user_ids();await c.answer("Запущено");progress=await c.message.answer(f"📣 Рассылка: 0/{len(uids)}")
    sent=failed=blocked=0
    async with broadcast_lock:
        for n,uid in enumerate(uids,1):
            try:await bot.copy_message(chat_id=uid,from_chat_id=data["source_chat"],message_id=data["source_message"],reply_markup=unsubscribe_kb());sent+=1
            except TelegramRetryAfter as e:
                await asyncio.sleep(float(e.retry_after)+.2)
                try:await bot.copy_message(chat_id=uid,from_chat_id=data["source_chat"],message_id=data["source_message"],reply_markup=unsubscribe_kb());sent+=1
                except Exception:failed+=1
            except TelegramForbiddenError:blocked+=1;failed+=1;await db.mark_blocked(uid)
            except (TelegramBadRequest,TelegramNetworkError):failed+=1
            except Exception:failed+=1;logging.exception("broadcast")
            await asyncio.sleep(.05)
            if n%50==0 or n==len(uids):
                try:await progress.edit_text(f"📣 Рассылка: {n}/{len(uids)}\n✅ {sent} · ❌ {failed}")
                except Exception:logging.exception("Suppressed exception")
    await db.save_broadcast_log(data["source_message"],len(uids),sent,failed,blocked);await db.audit(c.from_user.id,"broadcast",f"total={len(uids)}, sent={sent}, failed={failed}");await state.clear();await progress.edit_text(f"✅ <b>Рассылка завершена</b>\nВсего: {len(uids)}\nДоставлено: {sent}\nОшибок: {failed}\nНедоступны: {blocked}")


@router.callback_query(F.data=="adm:broadcast_stats")
async def broadcast_stats(c:CallbackQuery):
    if (await admin_role(c.from_user.id)) not in ("owner","content"):await c.answer("Нет доступа",show_alert=True);return
    logs=await db.broadcast_logs(10);ust=await db.user_count_stats();text=(f"📈 <b>Рассылки</b>\n\nБаза: {ust['total']}\nПолучают: {ust['active']}\nОтключили: {ust['off']}\nНедоступны: {ust['blocked']}\n\nПоследние кампании:\n"+"\n".join(f"{x['created_at']} — ✅ {x['sent']}/{x['total']}, ошибок {x['failed']}" for x in logs));await c.answer();await c.message.answer(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад",callback_data="adm:section:marketing")]]))


# -----------------------------
# OWNER TOOLS: EXCEL/BACKUP/PRIVACY/ADMINS/LOGS/SIZE CHART
# -----------------------------
@router.callback_query(F.data=="adm:export")
async def adm_export(c:CallbackQuery):
    if await admin_role(c.from_user.id)!="owner":await c.answer("Только владелец",show_alert=True);return
    path=os.path.join(tempfile.gettempdir(),f"orders_{datetime.now():%Y%m%d_%H%M%S}.xlsx");await export_orders_xlsx(path);await c.answer();await c.message.answer_document(document=FSInputFile(path),caption="📤 Экспорт заказов");await db.audit(c.from_user.id,"export_orders")
    try:os.remove(path)
    except OSError:pass
@router.callback_query(F.data=="adm:backup")
async def adm_backup(c:CallbackQuery):
    if await admin_role(c.from_user.id)!="owner":await c.answer("Только владелец",show_alert=True);return
    path=os.path.join(tempfile.gettempdir(),f"shop_backup_{datetime.now():%Y%m%d_%H%M%S}.db");await make_backup(path);await c.answer();await c.message.answer_document(document=FSInputFile(path),caption="💾 Резервная копия базы данных");await db.audit(c.from_user.id,"backup")
    try:os.remove(path)
    except OSError:pass



_CLEANUP_ACTIONS = {
    "completed_orders": (
        "🏁 Завершённые заказы",
        "Будут удалены заказы со статусом «Получен» или «Завершён» вместе с их товарами, историей статусов, отзывами и использованием промокодов.",
    ),
    "paid_orders": (
        "💳 ВСЕ оплаченные заказы",
        "⚠️ Будут удалены ВСЕ оплаченные заказы, включая те, которые сейчас собираются, отправляются или находятся в доставке. Также удалятся связанные позиции, история статусов, отзывы и использования промокодов.",
    ),
    "unpaid_orders": (
        "⏳ Неоплаченные / отклонённые",
        "Будут удалены заказы «Ожидает оплаты» и «Чек отклонён». Заказы «На проверке оплаты» специально НЕ удаляются.",
    ),
    "delivery_profiles": (
        "📍 Сохранённые профили доставки",
        "Будут удалены сохранённые покупателями адреса и данные доставки. Аккаунты и заказы останутся.",
    ),
    "customer_profiles": (
        "👥 Профили покупателей",
        "⚠️ Будут удалены профили всех покупателей, кроме администраторов: сохранённые адреса, корзины, согласия и другие персональные данные. История заказов сохранится, но будет обезличена. Активные покупатели потеряют привязку к своим текущим заказам.",
    ),
    "customer_activity": (
        "🛒 Корзины / ожидания",
        "Будут очищены корзины и подписки на появление товара. Профили покупателей и заказы останутся.",
    ),
    "broadcast_logs": (
        "📣 История рассылок",
        "Будет удалена статистика прошлых рассылок. Сами пользователи и их настройка подписки останутся.",
    ),
    "audit_logs": (
        "🧾 Журнал действий",
        "Будет полностью очищен журнал действий администраторов.",
    ),
    "all": (
        "🔥 Полная очистка данных",
        "⚠️ Будут очищены заказы, профили покупателей, корзины, доставка, рассылки и журналы. Товары, настройки магазина и администраторы останутся.",
    ),
}



@router.callback_query(F.data=="cleanup:single")
async def cleanup_single_menu(c:CallbackQuery):
    if await admin_role(c.from_user.id)!="owner":
        await c.answer("Только владелец",show_alert=True); return
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Заказы",callback_data="single:list:orders")],
        [InlineKeyboardButton(text="👤 Пользователи",callback_data="single:list:users")],
        [InlineKeyboardButton(text="📍 Доставка",callback_data="single:list:delivery")],
        [InlineKeyboardButton(text="📣 Рассылки",callback_data="single:list:broadcast")],
        [InlineKeyboardButton(text="🧾 Логи",callback_data="single:list:audit")],
        [InlineKeyboardButton(text="⬅️ Назад",callback_data="adm:cleanup")]
    ])
    await c.message.answer("🗑 Удаление по одной записи:",reply_markup=kb)

@router.callback_query(F.data.startswith("single:list:"))
async def single_list(c:CallbackQuery):
    kind=c.data.split(":")[2]
    rows=await db.admin_cleanup_list(kind)
    buttons=[]
    for r in rows:
        rid=r[0]
        label=f"🗑 {rid}"
        if kind=="users": label=f"🗑 {rid} @{r[1] or ''}"
        buttons.append([InlineKeyboardButton(text=label,callback_data=f"single:del:{kind}:{rid}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад",callback_data="cleanup:single")])
    await c.message.answer("Выберите запись для удаления:",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("single:del:"))
async def single_del(c:CallbackQuery):
    if await admin_role(c.from_user.id)!="owner":
        await c.answer("Только владелец",show_alert=True); return
    _,_,kind,rid=c.data.split(":")
    await db.admin_delete_one(kind,int(rid))
    await db.audit(c.from_user.id,"single_delete",f"{kind}:{rid}")
    await c.answer("Удалено")
    await c.message.answer("✅ Запись удалена",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗑 Ещё",callback_data=f"single:list:{kind}")]]))

@router.callback_query(F.data=="adm:cleanup")
async def adm_cleanup(c:CallbackQuery):
    if await admin_role(c.from_user.id)!="owner":await c.answer("Только владелец",show_alert=True);return
    st=await db.cleanup_summary()
    rows=[
        [InlineKeyboardButton(text=f"🏁 Завершённые заказы · {st['completed_orders']}",callback_data="cleanup:completed_orders")],
        [InlineKeyboardButton(text=f"💳 ВСЕ оплаченные заказы · {st['paid_orders']}",callback_data="cleanup:paid_orders")],
        [InlineKeyboardButton(text=f"⏳ Неоплаченные / отклонённые · {st['unpaid_orders']}",callback_data="cleanup:unpaid_orders")],
        [InlineKeyboardButton(text=f"📍 Профили доставки · {st['delivery_profiles']}",callback_data="cleanup:delivery_profiles")],
        [InlineKeyboardButton(text=f"👥 Профили покупателей · {st['customer_profiles']}",callback_data="cleanup:customer_profiles")],
        [InlineKeyboardButton(text=f"🛒 Корзины / ожидания · {st['customer_activity']}",callback_data="cleanup:customer_activity")],
        [InlineKeyboardButton(text=f"📣 История рассылок · {st['broadcast_logs']}",callback_data="cleanup:broadcast_logs")],
        [InlineKeyboardButton(text=f"🧾 Журнал действий · {st['audit_logs']}",callback_data="cleanup:audit_logs")],
        [InlineKeyboardButton(text="🗑 Удаление по одной записи",callback_data="cleanup:single")],
        [InlineKeyboardButton(text="🔥 ОЧИСТИТЬ ВСЁ (кроме товаров и админов)",callback_data="cleanup:all")],
        [InlineKeyboardButton(text="💾 Сначала сделать резервную копию",callback_data="adm:backup")],
        [InlineKeyboardButton(text="⬅️ К настройкам",callback_data="adm:section:settings")],
    ]
    await c.answer()
    await c.message.answer(
        "🧹 <b>Очистка данных</b>\n\n"
        "Раздел доступен только владельцу. Перед удалением важных данных лучше сделать резервную копию. "
        "Каждое удаление требует отдельного подтверждения.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("cleanup:"))
async def cleanup_ask(c:CallbackQuery):
    if await admin_role(c.from_user.id)!="owner":await c.answer("Только владелец",show_alert=True);return
    kind=c.data.split(":",1)[1]
    info=_CLEANUP_ACTIONS.get(kind)
    if not info:await c.answer("Неизвестное действие",show_alert=True);return
    title,warning=info
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, очистить",callback_data=f"cleanup_confirm:{kind}")],
        [InlineKeyboardButton(text="❌ Нет, отмена",callback_data="adm:cleanup")],
    ])
    await c.answer()
    await c.message.answer(
        f"<b>{title}</b>\n\n{warning}\n\n<b>Отменить это действие после удаления нельзя.</b>",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("cleanup_confirm:"))
async def cleanup_confirm(c:CallbackQuery):
    if await admin_role(c.from_user.id)!="owner":await c.answer("Только владелец",show_alert=True);return
    kind=c.data.split(":",1)[1]
    if kind not in _CLEANUP_ACTIONS:await c.answer("Неизвестное действие",show_alert=True);return
    await c.answer("Подтверждено")
    if kind=="all":
        count=0
        count+=await db.cleanup_paid_orders()
        count+=await db.cleanup_unpaid_orders()
        count+=await db.cleanup_delivery_profiles()
        count+=await db.cleanup_customer_activity()
        count+=await db.cleanup_customer_profiles()
        count+=await db.cleanup_broadcast_logs()
        count+=await db.cleanup_audit_logs()
    elif kind=="completed_orders":count=await db.cleanup_completed_orders()
    elif kind=="paid_orders":count=await db.cleanup_paid_orders()
    elif kind=="unpaid_orders":count=await db.cleanup_unpaid_orders()
    elif kind=="delivery_profiles":count=await db.cleanup_delivery_profiles()
    elif kind=="customer_profiles":count=await db.cleanup_customer_profiles()
    elif kind=="customer_activity":count=await db.cleanup_customer_activity()
    elif kind=="broadcast_logs":count=await db.cleanup_broadcast_logs()
    elif kind=="audit_logs":count=await db.cleanup_audit_logs()
    else:return
    if kind!="audit_logs":
        await db.audit(c.from_user.id,"cleanup",f"type={kind}; removed={count}")
    await c.message.answer(
        f"✅ <b>Очистка завершена</b>\n\n{_CLEANUP_ACTIONS[kind][0]}\nУдалено/обработано записей: <b>{count}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🧹 Вернуться к очистке",callback_data="adm:cleanup")]]),
    )


@router.callback_query(F.data=="adm:privacy")
async def adm_privacy(c:CallbackQuery):
    if await admin_role(c.from_user.id)!="owner":await c.answer("Только владелец",show_alert=True);return
    rows=await db.privacy_requests();await c.answer()
    if not rows:await c.message.answer("✅ Нет новых запросов по данным.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад",callback_data="adm:section:settings")]]));return
    for r in rows:
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Анонимизировать",callback_data=f"privacyok:{r['id']}"),InlineKeyboardButton(text="❌ Отклонить",callback_data=f"privacyno:{r['id']}")],[InlineKeyboardButton(text="⬅️ Назад",callback_data="adm:section:settings")]])
        await c.message.answer(f"🛡 Запрос #{r['id']} · user <code>{r['user_id']}</code> · {r['created_at']}",reply_markup=kb)
@router.callback_query(F.data.startswith("privacyok:"))
async def privacy_ok(c:CallbackQuery,bot:Bot):
    if await admin_role(c.from_user.id)!="owner":await c.answer("Нет доступа",show_alert=True);return
    rid=int(c.data.split(":")[1]);r=await db.privacy_request(rid);active=await db.fetchone("SELECT COUNT(*) c FROM orders WHERE user_id=? AND status NOT IN ('Завершён','Получен')",(r["user_id"],))
    if active["c"]:await c.answer("Есть незавершённые заказы",show_alert=True);return
    uid=r["user_id"]
    try:await bot.send_message(chat_id=uid,text="🛡 Ваш запрос на удаление/анонимизацию данных выполнен.")
    except Exception:logging.exception("Suppressed exception")
    await db.anonymize_user(uid);await db.privacy_status(rid,"done");await db.audit(c.from_user.id,"privacy_delete",f"user={uid}");await c.answer("Выполнено")
@router.callback_query(F.data.startswith("privacyno:"))
async def privacy_no(c:CallbackQuery):rid=int(c.data.split(":")[1]);await db.privacy_status(rid,"rejected");await db.audit(c.from_user.id,"privacy_reject",str(rid));await c.answer("Отклонено")


async def send_admin_subscription_panel(message: Message):
    enabled, chat_ref, url = await required_subscription_config()
    raw_enabled = (await db.get_setting(REQUIRED_SUB_ENABLED_SETTING, "0")) == "1"
    status = "🟢 Включена" if enabled else ("🟠 Настроена некорректно" if raw_enabled else "🔴 Выключена")
    channel = html.escape(chat_ref or "не указан")
    link = html.escape(url or "не указан")
    buttons = [
        [InlineKeyboardButton(text="✏️ Указать / изменить канал", callback_data="subadm:set")],
    ]
    if chat_ref and url:
        buttons.append([InlineKeyboardButton(
            text="🔴 Отключить" if raw_enabled else "🟢 Включить",
            callback_data="subadm:toggle",
        )])
        buttons.append([InlineKeyboardButton(text="🧪 Проверить настройку", callback_data="subadm:test")])
    buttons.append([InlineKeyboardButton(text="⬅️ К настройкам", callback_data="adm:section:settings")])
    await message.answer(
        "📢 <b>Обязательная подписка</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Канал для проверки: <code>{channel}</code>\n"
        f"Ссылка кнопки подписки: <code>{link}</code>\n\n"
        "При включённой проверке пользователь сначала должен подписаться на канал и подтвердить подписку. "
        "Только после этого откроются документы и остальные функции бота.\n\n"
        "⚠️ Бот должен быть <b>администратором канала</b>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data=="adm:subscription")
async def adm_subscription(c: CallbackQuery):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True)
        return
    await c.answer()
    await send_admin_subscription_panel(c.message)


@router.callback_query(F.data=="subadm:set")
async def adm_subscription_set(c: CallbackQuery, state: FSMContext):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True)
        return
    await state.set_state(AdminRequiredChannel.channel)
    await c.answer()
    await c.message.answer(
        "📢 <b>Укажите канал обязательной подписки.</b>\n\n"
        "Для публичного канала отправьте один из вариантов:\n"
        "<code>@my_channel</code>\n"
        "или <code>https://t.me/my_channel</code>\n\n"
        "Для закрытого канала отправьте:\n"
        "<code>-1001234567890 | https://t.me/+ССЫЛКА_ПРИГЛАШЕНИЯ</code>\n\n"
        "Перед сохранением добавьте этого бота администратором канала.",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminRequiredChannel.channel, F.text)
async def adm_subscription_channel_input(m: Message, state: FSMContext, bot: Bot):
    if ui_text_matches((m.text or "").strip(), "❌ Отмена"):
        await state.clear()
        await m.answer("Действие отменено.", reply_markup=main_menu(True))
        return
    if await admin_role(m.from_user.id) != "owner":
        await state.clear()
        await m.answer("⛔ Нет доступа.")
        return
    parsed = parse_required_channel_input(m.text)
    if not parsed:
        await m.answer(
            "❌ Не удалось распознать канал.\n\n"
            "Публичный: <code>@channel</code> или <code>https://t.me/channel</code>\n"
            "Закрытый: <code>-100... | https://t.me/+...</code>"
        )
        return
    chat_ref, url = parsed
    ok, info = await validate_required_channel(bot, chat_ref)
    if not ok:
        await m.answer(f"❌ {html.escape(info)}\n\nИсправьте настройку и отправьте канал ещё раз.")
        return
    await db.set_setting(REQUIRED_SUB_CHAT_SETTING, chat_ref)
    await db.set_setting(REQUIRED_SUB_URL_SETTING, url)
    await db.audit(m.from_user.id, "required_subscription_channel", f"chat={chat_ref}")
    await state.clear()
    await m.answer(f"✅ Канал сохранён: <b>{html.escape(info)}</b>\nТеперь обязательную подписку можно включить.")
    await send_admin_subscription_panel(m)


@router.callback_query(F.data=="subadm:toggle")
async def adm_subscription_toggle(c: CallbackQuery, bot: Bot):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True)
        return
    raw_enabled = (await db.get_setting(REQUIRED_SUB_ENABLED_SETTING, "0")) == "1"
    chat_ref = (await db.get_setting(REQUIRED_SUB_CHAT_SETTING, "")).strip()
    url = (await db.get_setting(REQUIRED_SUB_URL_SETTING, "")).strip()
    if raw_enabled:
        await db.set_setting(REQUIRED_SUB_ENABLED_SETTING, "0")
        await db.audit(c.from_user.id, "required_subscription_disable")
        await c.answer("Обязательная подписка отключена ✅", show_alert=True)
    else:
        if not chat_ref or not url:
            await c.answer("Сначала укажите канал", show_alert=True)
            return
        ok, info = await validate_required_channel(bot, chat_ref)
        if not ok:
            await c.answer(info, show_alert=True)
            return
        await db.set_setting(REQUIRED_SUB_ENABLED_SETTING, "1")
        await db.audit(c.from_user.id, "required_subscription_enable", f"chat={chat_ref}")
        await c.answer(f"Включено: {info}", show_alert=True)
    await send_admin_subscription_panel(c.message)


@router.callback_query(F.data=="subadm:test")
async def adm_subscription_test(c: CallbackQuery, bot: Bot):
    if await admin_role(c.from_user.id) != "owner":
        await c.answer("Только владелец", show_alert=True)
        return
    chat_ref = (await db.get_setting(REQUIRED_SUB_CHAT_SETTING, "")).strip()
    ok, info = await validate_required_channel(bot, chat_ref)
    if not ok:
        await c.answer(info, show_alert=True)
        return
    owner_subscribed = await is_required_subscribed(bot, c.from_user.id) if (await db.get_setting(REQUIRED_SUB_ENABLED_SETTING, "0")) == "1" else True
    suffix = "" if owner_subscribed else " Канал доступен, но ваш аккаунт сейчас не определяется как подписчик."
    await c.answer(f"✅ Настройка рабочая: {info}.{suffix}", show_alert=True)


@router.callback_query(F.data=="adm:admins")
async def adm_admins(c:CallbackQuery):
    if await admin_role(c.from_user.id)!="owner":await c.answer("Только владелец",show_alert=True);return
    rows=await db.get_admins();buttons=[[InlineKeyboardButton(text=f"{a['user_id']} · {ROLE_NAMES.get(a['role'],a['role'])}",callback_data=f"adminremove:{a['user_id']}")] for a in rows if a['user_id']!=settings.admin_id];buttons.insert(0,[InlineKeyboardButton(text="➕ Добавить администратора",callback_data="admin:add")]);buttons.append([InlineKeyboardButton(text="⬅️ К настройкам",callback_data="adm:section:settings")]);await c.answer();await c.message.answer("👨‍💼 <b>Администраторы</b>\nНажатие на сотрудника удаляет доступ (владелец из .env не удаляется).",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
@router.callback_query(F.data=="admin:add")
async def admin_add_start(c:CallbackQuery,state:FSMContext):await state.set_state(AdminAddAdmin.user_id);await c.answer();await c.message.answer("Введите Telegram ID сотрудника:",reply_markup=cancel_keyboard())
@router.message(AdminAddAdmin.user_id,F.text)
async def admin_add_id(m:Message,state:FSMContext):
    try:uid=int(m.text.strip())
    except ValueError:await m.answer("Введите числовой Telegram ID.");return
    await state.update_data(user_id=uid);await state.set_state(AdminAddAdmin.role);kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Менеджер",callback_data="adminrole:manager")],[InlineKeyboardButton(text="Контент-менеджер",callback_data="adminrole:content")],[InlineKeyboardButton(text="Склад / отправка",callback_data="adminrole:warehouse")],[InlineKeyboardButton(text="⬅️ Назад",callback_data="nav:fsm_back")]]);await m.answer("Выберите роль:",reply_markup=kb)
@router.callback_query(AdminAddAdmin.role,F.data.startswith("adminrole:"))
async def admin_add_role(c:CallbackQuery,state:FSMContext):
    role=c.data.split(":")[1];d=await state.get_data();await db.set_admin(d["user_id"],role);await db.audit(c.from_user.id,"admin_add",f"user={d['user_id']},role={role}");await state.clear();await c.answer("Добавлен ✅",show_alert=True)
@router.callback_query(F.data.startswith("adminremove:"))
async def admin_remove(c:CallbackQuery):
    if await admin_role(c.from_user.id)!="owner":await c.answer("Нет доступа",show_alert=True);return
    uid=int(c.data.split(":")[1]);await db.delete_admin(uid);await db.audit(c.from_user.id,"admin_remove",f"user={uid}");await c.answer("Доступ удалён")


@router.callback_query(F.data=="adm:logs")
async def adm_logs(c:CallbackQuery):
    if await admin_role(c.from_user.id)!="owner":await c.answer("Только владелец",show_alert=True);return
    rows=await db.audit_logs(30);text="🧾 <b>Журнал действий</b>\n\n"+"\n".join(f"{r['created_at']} · <code>{r['admin_id']}</code> · {html.escape(r['action'])} · {html.escape(r['details'] or '')}" for r in rows);await c.answer();await c.message.answer(text[:4000],reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К настройкам",callback_data="adm:section:settings")]]))
def content_admin_definition(kind:str) -> dict[str,str]:
    if kind=="welcome":
        return {
            "title":"🖼 Приветственное сообщение",
            "text_key":"welcome_text",
            "html_key":"welcome_text_html",
            "media_type_key":"welcome_media_type",
            "media_file_key":"welcome_media_file_id",
            "default_text":"Привет, {first_name}! 👋\n\nВыбирайте товары и оформляйте доставку прямо в боте.",
            "prefix":"",
        }
    if kind=="sizechart":
        return {
            "title":"📏 Размерная сетка",
            "text_key":"size_chart",
            "html_key":"size_chart_html",
            "media_type_key":"size_chart_media_type",
            "media_file_key":"size_chart_media_file_id",
            "default_text":settings.size_chart_text,
            "prefix":"📏 <b>Таблица размеров</b>",
        }
    raise ValueError("unknown content kind")


def can_edit_content(role:str|None) -> bool:
    return role in {"owner","content"}


async def show_admin_content(message:Message,kind:str):
    cfg=content_admin_definition(kind)
    text=await db.get_setting(cfg["text_key"],cfg["default_text"])
    text_html=await db.get_setting(cfg["html_key"],"")
    if kind=="sizechart":
        chart_media=await db.size_chart_media()
        photos=sum(1 for item in chart_media if item["media_type"]=="photo")
        videos=sum(1 for item in chart_media if item["media_type"]=="video")
        parts=[]
        if photos: parts.append(f"фото: {photos}")
        if videos: parts.append(f"видео: {videos}")
        media_label=", ".join(parts) if parts else "нет"
    else:
        media_type=await db.get_setting(cfg["media_type_key"],"")
        media_file=await db.get_setting(cfg["media_file_key"],"")
        media_label=MEDIA_TYPE_LABELS.get(media_type,media_type) if media_type and media_file else "нет"
    preview_html=(text_html if text_html else html.escape(text)) if text else "—"
    if len(preview_html)>2600:
        preview_html=html.escape(text[:2400]+"…")
    note="\n\nМожно использовать <code>{first_name}</code> — при первом запуске он заменится на имя пользователя." if kind=="welcome" else ""
    media_button="🎞 Добавить фото / видео" if kind=="sizechart" else "🎞 Загрузить / заменить мультимедиа"
    delete_button="🗑 Удалить все мультимедиа" if kind=="sizechart" else "🗑 Удалить мультимедиа"
    rows=[
        [InlineKeyboardButton(text="✏️ Изменить текст",callback_data=f"content:text:{kind}")],
        [InlineKeyboardButton(text=media_button,callback_data=f"content:media:{kind}")],
        [InlineKeyboardButton(text=delete_button,callback_data=f"content:delete:{kind}")],
        [InlineKeyboardButton(text="👁 Предпросмотр",callback_data=f"content:preview:{kind}")],
        [InlineKeyboardButton(text="⬅️ К маркетингу",callback_data="adm:section:marketing")],
    ]
    await message.answer(
        f"{cfg['title']}\n\nТекущее мультимедиа: <b>{html.escape(media_label)}</b>\n\n"
        f"<b>Текущий текст:</b>\n{preview_html}{note}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data=="adm:welcome")
async def adm_welcome(c:CallbackQuery):
    if not can_edit_content(await admin_role(c.from_user.id)):await c.answer("Нет доступа",show_alert=True);return
    await c.answer();await show_admin_content(c.message,"welcome")


@router.callback_query(F.data=="adm:sizechart")
async def adm_sizechart(c:CallbackQuery):
    if not can_edit_content(await admin_role(c.from_user.id)):await c.answer("Нет доступа",show_alert=True);return
    await c.answer();await show_admin_content(c.message,"sizechart")


@router.callback_query(F.data.startswith("content:text:"))
async def admin_content_text_start(c:CallbackQuery,state:FSMContext):
    if not can_edit_content(await admin_role(c.from_user.id)):await c.answer("Нет доступа",show_alert=True);return
    kind=c.data.split(":",2)[2];cfg=content_admin_definition(kind);current=await db.get_setting(cfg["text_key"],cfg["default_text"]);current_html=await db.get_setting(cfg["html_key"],"")
    rendered_current=current_html if current_html else html.escape(current)
    await state.clear();await state.set_state(AdminContentEdit.text);await state.update_data(content_kind=kind);await c.answer();await c.message.answer(
        f"✏️ Введите новый текст для «{html.escape(cfg['title'])}». <b>Форматирование Telegram сохранится</b>: жирный, курсив, подчёркивание, зачёркивание, спойлер, код, ссылки и цитаты.\n\nЧтобы оставить только мультимедиа без текста, отправьте <code>-</code>.\n\nТекущий текст:\n{rendered_current}",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminContentEdit.text,F.text)
async def admin_content_text_save(m:Message,state:FSMContext):
    if not can_edit_content(await admin_role(m.from_user.id)):await state.clear();await m.answer("⛔ Нет доступа.");return
    data=await state.get_data();kind=data.get("content_kind","");cfg=content_admin_definition(kind)
    value=m.text.strip();value="" if value=="-" else value
    value_html="" if value=="" else message_rich_html(m)
    await db.set_setting(cfg["text_key"],value);await db.set_setting(cfg["html_key"],value_html);await db.audit(m.from_user.id,f"{kind}_text_edit")
    await state.clear();await m.answer("✅ Текст обновлён.",reply_markup=main_menu(True));await show_admin_content(m,kind)


@router.callback_query(F.data.startswith("content:media:"))
async def admin_content_media_start(c:CallbackQuery,state:FSMContext):
    if not can_edit_content(await admin_role(c.from_user.id)):await c.answer("Нет доступа",show_alert=True);return
    kind=c.data.split(":",2)[2];cfg=content_admin_definition(kind)
    await state.clear();await state.set_state(AdminContentEdit.media);await state.update_data(content_kind=kind);await c.answer()
    if kind=="sizechart":
        await c.message.answer(
            "🎞 Отправьте одно или несколько <b>фото и/или видео</b> для размерной сетки.\n\n"
            "Можно отправлять файлы по одному или одним альбомом. После загрузки нажмите «✅ Готово».",
            reply_markup=cancel_keyboard(),
        )
    else:
        await c.message.answer(
            f"🎞 Отправьте новое мультимедиа для «{html.escape(cfg['title'])}»: фото, видео, GIF/анимацию, документ или аудиофайл.\n\nНовый файл заменит текущий.",
            reply_markup=cancel_keyboard(),
        )


@router.message(AdminContentEdit.media,F.photo)
@router.message(AdminContentEdit.media,F.video)
@router.message(AdminContentEdit.media,F.animation)
@router.message(AdminContentEdit.media,F.document)
@router.message(AdminContentEdit.media,F.audio)
async def admin_content_media_save(m:Message,state:FSMContext):
    if not can_edit_content(await admin_role(m.from_user.id)):await state.clear();await m.answer("⛔ Нет доступа.");return
    payload=extract_message_media(m)
    if not payload:return
    media_type,file_id=payload;data=await state.get_data();kind=data.get("content_kind","");cfg=content_admin_definition(kind)
    if kind=="sizechart":
        if media_type not in {"photo","video"}:
            await m.answer("Для размерной сетки можно добавлять только фото и видео.");return
        await db.add_size_chart_media(file_id,media_type)
        # Keep legacy single-media settings populated for backward compatibility with older code/backups.
        legacy_items=await db.size_chart_media()
        if legacy_items:
            first=legacy_items[0]
            await db.set_setting(cfg["media_type_key"],first["media_type"]);await db.set_setting(cfg["media_file_key"],first["file_id"])
            await db.set_setting("size_chart_photo",first["file_id"] if first["media_type"]=="photo" else "")
        await db.audit(m.from_user.id,"sizechart_media_add",f"type={media_type}")
        count=len(legacy_items)
        done_kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Готово",callback_data="content:media_done:sizechart")]])
        await m.answer(f"✅ Добавлено. Сейчас в размерной сетке файлов: <b>{count}</b>. Можно отправить ещё фото/видео или нажать «Готово».",reply_markup=done_kb)
        return
    await db.set_setting(cfg["media_type_key"],media_type);await db.set_setting(cfg["media_file_key"],file_id)
    await db.audit(m.from_user.id,f"{kind}_media_edit",f"type={media_type}");await state.clear()
    await m.answer(f"✅ {MEDIA_TYPE_LABELS.get(media_type,'Файл')} сохранён.",reply_markup=main_menu(True));await show_admin_content(m,kind)


@router.callback_query(AdminContentEdit.media,F.data=="content:media_done:sizechart")
async def admin_sizechart_media_done(c:CallbackQuery,state:FSMContext):
    if not can_edit_content(await admin_role(c.from_user.id)):await state.clear();await c.answer("Нет доступа",show_alert=True);return
    data=await state.get_data()
    if data.get("content_kind")!="sizechart":await c.answer("Неверный режим",show_alert=True);return
    await state.clear();await c.answer("Готово ✅")
    await show_admin_content(c.message,"sizechart")


@router.callback_query(F.data.startswith("content:delete:"))
async def admin_content_media_delete(c:CallbackQuery):
    if not can_edit_content(await admin_role(c.from_user.id)):await c.answer("Нет доступа",show_alert=True);return
    kind=c.data.split(":",2)[2];cfg=content_admin_definition(kind)
    await db.set_setting(cfg["media_type_key"],"");await db.set_setting(cfg["media_file_key"],"")
    if kind=="sizechart":
        await db.clear_size_chart_media();await db.set_setting("size_chart_photo","")
    await db.audit(c.from_user.id,f"{kind}_media_delete");await c.answer("Мультимедиа удалено ✅");await show_admin_content(c.message,kind)


@router.callback_query(F.data.startswith("content:preview:"))
async def admin_content_preview(c:CallbackQuery):
    if not can_edit_content(await admin_role(c.from_user.id)):await c.answer("Нет доступа",show_alert=True);return
    kind=c.data.split(":",2)[2];cfg=content_admin_definition(kind)
    text=await db.get_setting(cfg["text_key"],cfg["default_text"]);text_html=await db.get_setting(cfg["html_key"],"")
    await c.answer()
    if kind=="sizechart":
        await send_size_chart_content(c.message,text,rich_html=(text_html if text_html else None))
        return
    media_type=await db.get_setting(cfg["media_type_key"],"");media_file=await db.get_setting(cfg["media_file_key"],"")
    await send_custom_content(c.message,text,media_type,media_file,prefix_html=cfg["prefix"],first_name=c.from_user.first_name,rich_html=(text_html if text_html else None))


# -----------------------------
# FALLBACK
# -----------------------------
@router.message()
async def fallback(m:Message):await m.answer("Используйте кнопки меню 👇",reply_markup=main_menu(await is_admin(m.from_user.id)))


# -----------------------------
# RUN
# -----------------------------
async def main():
    configure_logging(settings.log_dir)
    lock=SingleInstanceLock(settings.bot_token).acquire()
    try:
        await run_bot(
            router=router,
            legal_gate=LegalGateMiddleware(),
            premium_request_middleware=premium_emoji_request_middleware,
            abandoned_cart_worker=abandoned_cart_worker,
            build=BOT_BUILD,
        )
    finally:
        lock.release()


if __name__=="__main__":asyncio.run(main())
