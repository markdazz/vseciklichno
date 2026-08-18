from __future__ import annotations

from typing import Any

import httpx

from config import settings


async def raw_bot_api(method_name: str, payload: dict[str, Any]) -> tuple[bool, str, Any]:
    """Call api.telegram.org directly, bypassing aiogram models/middleware.

    Used only from the owner-only diagnostics section. The bot token and full
    URL are never returned in diagnostics text.
    """
    url = f"https://api.telegram.org/bot{settings.bot_token}/{method_name}"
    try:
        async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
            response = await client.post(url, json=payload)
        try:
            data = response.json()
        except Exception:
            return False, f"HTTP {response.status_code}: не-JSON ответ Telegram", None
        if bool(data.get("ok")):
            return True, "OK", data.get("result")
        description = str(data.get("description") or f"HTTP {response.status_code}")
        return False, description, data
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", None


async def full_system_diagnostics() -> list[tuple[str, bool, str]]:
    """Run owner-facing production health checks without exposing secrets."""
    from datetime import datetime
    from pathlib import Path
    import os
    import shutil

    import db
    from production_runtime import disk_status, process_uptime_seconds

    results: list[tuple[str, bool, str]] = []

    ok, detail, _ = await raw_bot_api("getMe", {})
    results.append(("Telegram API", ok, "доступен" if ok else detail[:160]))

    try:
        health = await db.database_health()
        db_ok = str(health.get("quick_check", "")).lower() == "ok" and str(health.get("journal_mode", "")).lower() == "wal"
        results.append(("SQLite", db_ok, f"quick_check={health.get('quick_check')}, WAL={health.get('journal_mode')}, {int(health.get('size_bytes',0))/1024/1024:.1f} MB"))
    except Exception as exc:
        results.append(("SQLite", False, f"{type(exc).__name__}: {exc}"[:160]))

    try:
        test_key = "diagnostic_write_probe"
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await db.set_setting(test_key, stamp)
        read_back = await db.get_setting(test_key, "")
        results.append(("Запись в БД", read_back == stamp, "чтение/запись работают" if read_back == stamp else "значение не совпало"))
    except Exception as exc:
        results.append(("Запись в БД", False, f"{type(exc).__name__}: {exc}"[:160]))

    try:
        total, used, free = disk_status()
        free_gb = free / 1024 / 1024 / 1024
        results.append(("Диск", free_gb >= 0.25, f"свободно {free_gb:.2f} GB из {total/1024/1024/1024:.2f} GB"))
    except Exception as exc:
        results.append(("Диск", False, f"{type(exc).__name__}: {exc}"[:160]))

    last_backup = await db.get_setting("last_auto_backup", "")
    results.append(("Автобэкап", bool(last_backup), last_backup or "ещё не создавался"))

    try:
        uptime = process_uptime_seconds()
        hours, rem = divmod(uptime, 3600)
        minutes, seconds = divmod(rem, 60)
        results.append(("Аптайм", True, f"{hours}ч {minutes}м {seconds}с"))
        active_res = await db.fetchone("SELECT COUNT(*) c FROM inventory_reservations WHERE expires_at>?", (db.NOW(),))
        results.append(("Активные резервы", True, str(int(active_res['c'] or 0))))
    except Exception as exc:
        results.append(("Runtime", False, f"{type(exc).__name__}: {exc}"[:160]))

    try:
        rows = await db.premium_emoji_all_items()
        if rows:
            emoji_id = str(rows[0]["custom_emoji_id"])
            ok, detail, _ = await raw_bot_api("getCustomEmojiStickers", {"custom_emoji_ids": [emoji_id]})
            results.append(("Premium emoji", ok, "Telegram распознаёт custom emoji" if ok else detail[:160]))
        else:
            results.append(("Premium emoji", True, "библиотека пуста — проверка пропущена"))
    except Exception as exc:
        results.append(("Premium emoji", False, f"{type(exc).__name__}: {exc}"[:160]))

    async def endpoint_probe(name: str, url: str, configured: bool) -> None:
        if not configured:
            results.append((name, True, "API не настроен — используется fallback/фиксированная стоимость"))
            return
        try:
            async with httpx.AsyncClient(timeout=8.0, trust_env=False, follow_redirects=True) as client:
                response = await client.get(url)
            reachable = response.status_code < 500
            results.append((name, reachable, f"endpoint отвечает HTTP {response.status_code}"))
        except Exception as exc:
            results.append((name, False, f"{type(exc).__name__}: {exc}"[:160]))

    await endpoint_probe("СДЭК API", "https://api.cdek.ru/v2/location/cities?size=1", bool(settings.cdek_client_id and settings.cdek_client_secret))
    await endpoint_probe("Почта России API", "https://otpravka-api.pochta.ru/", bool(settings.russian_post_token and settings.russian_post_user_auth))

    return results
