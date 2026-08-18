from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

import db
from app_services import make_backup
from config import settings

T = TypeVar("T")
log = logging.getLogger(__name__)
PROCESS_STARTED_AT = datetime.now()


async def telegram_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    name: str,
    forever: bool = False,
    max_attempts: int = 6,
) -> T | None:
    """Retry transient Telegram/network operations without killing the process."""
    attempt = 0
    delays = (2, 5, 10, 20, 30, 60)
    while True:
        attempt += 1
        try:
            return await operation()
        except TelegramRetryAfter as exc:
            delay = max(1, int(exc.retry_after))
            log.warning("%s: Telegram FloodWait, retry in %ss", name, delay)
        except (TelegramNetworkError, OSError, asyncio.TimeoutError) as exc:
            delay = delays[min(attempt - 1, len(delays) - 1)]
            log.warning("%s: network unavailable (%s), retry in %ss", name, exc, delay)
        if not forever and attempt >= max_attempts:
            log.error("%s: retry limit reached; continuing without startup operation", name)
            return None
        await asyncio.sleep(delay)


async def wait_for_telegram(bot) -> None:
    await telegram_retry(lambda: bot.get_me(), name="Telegram API readiness", forever=True)


async def pre_migration_backup() -> Path | None:
    """Create a verified safety copy before startup migrations touch an existing DB."""
    source = Path(settings.db_path)
    if not source.exists() or source.stat().st_size == 0:
        return None
    backup_dir = Path(settings.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = backup_dir / f"pre_migration_{stamp}.db"

    def _copy_and_verify() -> str:
        import sqlite3
        src = sqlite3.connect(source, timeout=30)
        dst = sqlite3.connect(path, timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close(); src.close()
        con = sqlite3.connect(path, timeout=30)
        try:
            return str(con.execute("PRAGMA quick_check").fetchone()[0])
        finally:
            con.close()

    result = await asyncio.to_thread(_copy_and_verify)
    if result.lower() != "ok":
        path.unlink(missing_ok=True)
        raise RuntimeError(f"pre-migration backup quick_check failed: {result}")

    # Keep only a few startup safety copies in addition to normal daily backups.
    old = sorted(backup_dir.glob("pre_migration_*.db"), key=lambda x: x.stat().st_mtime, reverse=True)
    for stale in old[3:]:
        try:
            stale.unlink()
        except OSError:
            log.exception("Could not delete old pre-migration backup %s", stale)
    log.info("Pre-migration safety backup created: %s", path)
    return path


async def backup_once() -> Path:
    backup_dir = Path(settings.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = backup_dir / f"shop_{stamp}.db"
    await make_backup(str(path))

    # Verify the produced copy before considering it healthy.
    import sqlite3
    con = sqlite3.connect(path)
    try:
        result = con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()
    if str(result).lower() != "ok":
        path.unlink(missing_ok=True)
        raise RuntimeError(f"backup quick_check failed: {result}")

    backups = sorted(backup_dir.glob("shop_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[max(1, settings.backup_keep):]:
        try:
            old.unlink()
        except OSError:
            log.exception("Could not delete old backup %s", old)
    await db.set_setting("last_auto_backup", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return path


async def automatic_backup_worker() -> None:
    # Small initial delay avoids competing with startup migrations.
    await asyncio.sleep(30)
    while True:
        try:
            last = await db.get_setting("last_auto_backup", "")
            due = True
            if last:
                try:
                    dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                    due = datetime.now() - dt >= timedelta(hours=settings.backup_interval_hours)
                except ValueError:
                    pass
            if due:
                path = await backup_once()
                log.info("Automatic database backup created: %s", path)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("automatic_backup_worker")
        await asyncio.sleep(3600)


async def reservation_cleanup_worker() -> None:
    await asyncio.sleep(10)
    while True:
        try:
            released = await db.release_expired_reservations()
            if released:
                log.info("Released %s expired stock reservations", released)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reservation_cleanup_worker")
        await asyncio.sleep(60)


def disk_status() -> tuple[int, int, int]:
    total, used, free = shutil.disk_usage(Path(settings.db_path).resolve().parent)
    return int(total), int(used), int(free)


def process_uptime_seconds() -> int:
    return max(0, int((datetime.now() - PROCESS_STARTED_AT).total_seconds()))
