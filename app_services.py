from __future__ import annotations

import hashlib
import html
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

import db
from config import settings

log = logging.getLogger(__name__)


def money(value: int | float) -> str:
    return f"{int(round(value)):,}".replace(",", " ") + " ₽"


def effective_price(product) -> int:
    price = int(product["price"] or 0)
    old_price = int(product["old_price"] or 0) if "old_price" in product.keys() else 0
    # db V5 stores current price in price and crossed-out previous price in old_price.
    return price


def price_text(product) -> str:
    price = int(product["price"] or 0)
    old_price = int(product["old_price"] or 0) if "old_price" in product.keys() else 0
    if old_price > price > 0:
        return f"<s>{money(old_price)}</s> <b>{money(price)}</b>"
    return f"<b>{money(price)}</b>"


def parse_variant_text(text: str) -> dict[tuple[str, str], int]:
    """Черный:S=5,M=3; Белый:S=2,M=0 -> {(color,size): stock}."""
    return db.parse_variant_spec(text)


def delivery_address(data: dict[str, Any]) -> str:
    method = data.get("delivery_method", "")
    region = str(data.get("region", "") or "").strip()
    city = str(data.get("city", "") or "").strip()
    if method == "Почта России":
        parts = [
            str(data.get("postal_code", "") or "").strip(), region, city,
            str(data.get("street", "") or "").strip(),
            (f"д. {data.get('house')}" if data.get("house") else ""),
            (f"корп./стр. {data.get('building')}" if data.get("building") else ""),
            (f"кв./офис {data.get('apartment')}" if data.get("apartment") else ""),
        ]
        return ", ".join(p for p in parts if p)
    if method == "СДЭК" and data.get("cdek_type") == "ПВЗ":
        return ", ".join(p for p in [region, city, f"ПВЗ СДЭК: {data.get('cdek_point','')}"] if p)
    parts = [
        region, city, str(data.get("street", "") or "").strip(),
        (f"д. {data.get('house')}" if data.get("house") else ""),
        (f"корп./стр. {data.get('building')}" if data.get("building") else ""),
        (f"кв./офис {data.get('apartment')}" if data.get("apartment") else ""),
    ]
    return ", ".join(p for p in parts if p)


def delivery_summary(data: dict[str, Any]) -> str:
    method = data.get("delivery_method", "Не указан")
    lines = [
        f"🚚 Способ получения: <b>{html.escape(str(method))}</b>",
        f"👤 Получатель: <b>{html.escape(str(data.get('recipient_full_name','')))}</b>",
        f"📱 Телефон: {html.escape(str(data.get('phone','')))}",
    ]
    if method == "Почта России":
        lines.append(f"🏷 Индекс: <b>{html.escape(str(data.get('postal_code','')))}</b>")
    if method == "СДЭК":
        lines.append(f"📦 Получение: <b>{html.escape(str(data.get('cdek_type','')))}</b>")
    lines.append(f"📍 Адрес: {html.escape(delivery_address(data))}")
    if data.get("delivery_comment"):
        lines.append(f"📝 Комментарий: {html.escape(str(data['delivery_comment']))}")
    return "\n".join(lines)


def order_delivery_summary(order) -> str:
    data = {k: (order[k] if k in order.keys() else "") for k in [
        "recipient_full_name", "phone", "delivery_method", "postal_code", "region", "city",
        "street", "house", "building", "apartment", "cdek_type", "cdek_point", "delivery_comment",
    ]}
    data["recipient_full_name"] = data.get("recipient_full_name") or order["customer_name"]
    return delivery_summary(data)


async def cart_weight(user_id: int) -> int:
    items = await db.cart(user_id)
    return sum(max(1, int(i["weight_grams"] or settings.default_product_weight)) * int(i["qty"]) for i in items)


# ---------------- CDEK ----------------
_cdek_cache: tuple[str, float] | None = None


async def _cdek_token() -> str:
    global _cdek_cache
    if not (settings.cdek_client_id and settings.cdek_client_secret):
        raise RuntimeError("CDEK API не настроен")
    if _cdek_cache and _cdek_cache[1] > time.time() + 60:
        return _cdek_cache[0]
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            "https://api.cdek.ru/v2/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": settings.cdek_client_id,
                "client_secret": settings.cdek_client_secret,
            },
        )
        r.raise_for_status()
        payload = r.json()
    token = payload["access_token"]
    _cdek_cache = (token, time.time() + int(payload.get("expires_in", 3600)))
    return token


async def cdek_city(city: str, region: str = "") -> dict[str, Any] | None:
    if not (settings.cdek_client_id and settings.cdek_client_secret):
        return None
    token = await _cdek_token()
    params: dict[str, Any] = {"city": city, "size": 10}
    if region:
        params["region"] = region
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            "https://api.cdek.ru/v2/location/cities",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        rows = r.json() or []
    return rows[0] if rows else None


async def cdek_points(city: str, region: str = "", limit: int = 12) -> list[dict[str, str]]:
    city_row = await cdek_city(city, region)
    if not city_row:
        return []
    token = await _cdek_token()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            "https://api.cdek.ru/v2/deliverypoints",
            params={"city_code": city_row["code"], "type": "PVZ"},
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        rows = r.json() or []
    out = []
    for p in rows[:limit]:
        loc = p.get("location") or {}
        out.append({
            "code": str(p.get("code") or ""),
            "name": str(p.get("name") or "ПВЗ СДЭК"),
            "address": str(loc.get("address_full") or loc.get("address") or ""),
        })
    return out


async def cdek_shipping_cost(city: str, region: str, weight_g: int) -> int | None:
    if not (settings.cdek_client_id and settings.cdek_client_secret):
        return settings.cdek_fixed_cost or None
    to_city = await cdek_city(city, region)
    if not to_city:
        return settings.cdek_fixed_cost or None
    token = await _cdek_token()
    from_location: dict[str, Any] = {}
    if settings.cdek_origin_postal:
        from_location["postal_code"] = settings.cdek_origin_postal
    if settings.cdek_origin_city:
        from_location["city"] = settings.cdek_origin_city
    if settings.cdek_origin_region:
        from_location["region"] = settings.cdek_origin_region
    payload = {
        "type": 1,
        "from_location": from_location,
        "to_location": {"code": to_city["code"]},
        "packages": [{"weight": max(1, int(weight_g)), "length": 30, "width": 25, "height": 12}],
    }
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.post(
                "https://api.cdek.ru/v2/calculator/tarifflist",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            tariffs = (r.json() or {}).get("tariff_codes") or []
        prices = [float(t["delivery_sum"]) for t in tariffs if t.get("delivery_sum") is not None]
        return int(round(min(prices))) if prices else (settings.cdek_fixed_cost or None)
    except Exception:
        return settings.cdek_fixed_cost or None


# ---------------- Russian Post ----------------
async def post_shipping_cost(postal_code: str, weight_g: int) -> int | None:
    if not (settings.russian_post_token and settings.russian_post_user_auth and settings.post_origin_postal):
        return settings.post_fixed_cost or None
    payload = {
        "index-from": settings.post_origin_postal,
        "index-to": postal_code,
        "mail-category": "ORDINARY",
        "mail-type": "POSTAL_PARCEL",
        "mass": max(1, int(weight_g)),
    }
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.post(
                "https://otpravka-api.pochta.ru/1.0/tariff",
                json=payload,
                headers={
                    "Authorization": f"AccessToken {settings.russian_post_token}",
                    "X-User-Authorization": f"Basic {settings.russian_post_user_auth}",
                    "Content-Type": "application/json;charset=UTF-8",
                },
            )
            r.raise_for_status()
            data = r.json() or {}
        # API amounts are commonly returned in kopecks.
        cents = data.get("total-rate") or data.get("total-rate-wo-vat") or data.get("rate")
        if cents is None:
            return settings.post_fixed_cost or None
        return max(0, int(round(float(cents) / 100)))
    except Exception:
        return settings.post_fixed_cost or None


async def shipping_cost(delivery: dict[str, Any], user_id: int) -> tuple[int, str]:
    weight = await cart_weight(user_id)
    method = delivery.get("delivery_method", "")
    if method == "СДЭК":
        cost = await cdek_shipping_cost(delivery.get("city", ""), delivery.get("region", ""), weight)
        if cost is None:
            return 0, "СДЭК: стоимость уточнит продавец"
        return cost, "СДЭК: рассчитано автоматически" if settings.cdek_client_id else "СДЭК: фиксированная стоимость"
    if method == "Почта России":
        cost = await post_shipping_cost(delivery.get("postal_code", ""), weight)
        if cost is None:
            return 0, "Почта России: стоимость уточнит продавец"
        return cost, "Почта России: рассчитано автоматически" if settings.russian_post_token else "Почта России: фиксированная стоимость"
    return 0, "Доставка не рассчитана"


# ---------------- Price / loyalty / promo ----------------
async def calculate_pricing(user_id: int, delivery: dict[str, Any], promo_code: str = "", use_points: bool = False) -> dict[str, Any]:
    items = await db.cart(user_id)
    subtotal = sum(int(i["price"]) * int(i["qty"]) for i in items)
    promo_discount = 0
    promo_row = None
    promo_code = (promo_code or "").strip().upper()
    if promo_code and use_points:
        raise ValueError("Можно использовать либо промокод, либо бонусы — одновременно нельзя")
    if promo_code:
        p = await db.promo(promo_code)
        if not p or not int(p["active"]):
            raise ValueError("Промокод не найден или отключён")
        if subtotal < int(p["min_order"] or 0):
            raise ValueError(f"Промокод действует от суммы {money(p['min_order'])}")
        used = int(await db.promo_usage_count(p["id"]))
        if int(p["max_uses"] or 0) and used >= int(p["max_uses"]):
            raise ValueError("Лимит использований промокода исчерпан")
        if await db.promo_used_by(p["id"], user_id):
            raise ValueError("Вы уже использовали этот промокод")
        promo_row = p
        promo_discount = subtotal * int(p["percent"]) // 100

    spend = await db.lifetime_spend(user_id)
    loyalty_percent = settings.loyalty_discount_percent if spend >= settings.loyalty_threshold else 0
    after_promo = max(0, subtotal - promo_discount)
    loyalty_discount = after_promo * loyalty_percent // 100
    after_discounts = max(0, after_promo - loyalty_discount)

    points_used = 0
    if use_points:
        balance = await db.bonus_balance(user_id)
        max_by_order = after_discounts * settings.max_points_percent // 100
        points_used = min(balance, max_by_order)

    ship_cost, ship_note = await shipping_cost(delivery, user_id)
    total = max(0, after_discounts - points_used) + ship_cost
    return {
        "subtotal": subtotal,
        "promo_discount": promo_discount,
        "loyalty_discount": loyalty_discount,
        "loyalty_percent": loyalty_percent,
        "points_used": points_used,
        "shipping_cost": ship_cost,
        "shipping_note": ship_note,
        "total": total,
        "promo_code": promo_code if promo_row else "",
        "promo": promo_row,
    }


def pricing_text(pricing: dict[str, Any]) -> str:
    lines = [f"Товары: {money(pricing['subtotal'])}"]
    if pricing["promo_discount"]:
        lines.append(f"Промокод: −{money(pricing['promo_discount'])}")
    if pricing["loyalty_discount"]:
        lines.append(f"Скидка постоянного клиента ({pricing['loyalty_percent']}%): −{money(pricing['loyalty_discount'])}")
    if pricing["points_used"]:
        lines.append(f"Бонусы: −{money(pricing['points_used'])}")
    lines.append(f"Доставка: {money(pricing['shipping_cost'])}")
    lines.append(f"<b>Итого: {money(pricing['total'])}</b>")
    return "\n".join(lines)


def tracking_url(method: str) -> str:
    if method == "Почта России":
        return "https://www.pochta.ru/tracking"
    if method == "СДЭК":
        return "https://www.cdek.ru/ru/tracking/"
    return ""


async def export_orders_xlsx(path: str):
    orders = await db.admin_orders(None, 100000)
    wb = Workbook()
    ws = wb.active
    ws.title = "Заказы"
    headers = [
        "Номер заказа", "Дата", "Статус", "Telegram ID", "Username", "ФИО", "Телефон", "Доставка",
        "Индекс", "Регион", "Город", "Адрес", "Товары", "Сумма товаров", "Скидка", "Бонусы",
        "Доставка ₽", "Итого ₽", "Промокод", "Оплата", "Трек", "Комментарий админа",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")
    for o in orders:
        address = delivery_address({k: o[k] for k in o.keys()})
        items = await db.order_items(o["id"])
        items_text = "\n".join(
            f"{i['product_name']} · {i['color'] or 'Основной'} / {i['size']} × {i['qty']}"
            for i in items
        )
        ws.append([
            db.public_order_ref(o), o["created_at"], o["status"], o["user_id"], o["username"],
            o["recipient_full_name"] or o["customer_name"], o["phone"], o["delivery_method"],
            o["postal_code"], o["region"], o["city"], address, items_text, o["subtotal"],
            int(o["discount_amount"] or 0) + int(o["loyalty_discount"] or 0), o["bonus_used"],
            o["shipping_cost"], o["total"], o["promo_code"], o["payment_method"], o["tracking_number"], o["admin_note"],
        ])
    for column in ws.columns:
        letter = column[0].column_letter
        ws.column_dimensions[letter].width = min(55, max(12, max(len(str(c.value or "")) for c in column) + 2))
    wb.save(path)


async def make_backup(path: str):
    import sqlite3
    src = sqlite3.connect(settings.db_path)
    dst = sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()


class SingleInstanceLock:
    def __init__(self, token: str):
        digest = hashlib.sha256(token.encode()).hexdigest()[:20]
        self.path = os.path.join(tempfile.gettempdir(), f"telegram_clothes_v5_{digest}.lock")
        self.file = None

    def acquire(self):
        self.file = open(self.path, "a+b")
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b"1"); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            self.file.close(); self.file = None
            raise RuntimeError("Этот бот уже запущен в другом окне/процессе.")
        return self

    def release(self):
        if not self.file:
            return
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        except Exception:
            log.debug("Failed to unlock single-instance lock cleanly", exc_info=True)
        try:
            self.file.close()
        finally:
            self.file = None
