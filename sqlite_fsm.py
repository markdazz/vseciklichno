from __future__ import annotations

import asyncio
import pickle
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import aiosqlite
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey


class SQLiteStorage(BaseStorage):
    """Persistent aiogram FSM storage backed by SQLite.

    Uses pickle intentionally because the existing bot stores tuple-keyed dicts and
    nested Telegram-independent Python values in FSM data that cannot be represented
    faithfully as JSON. The database is local/trusted application state.
    """

    def __init__(self, path: str):
        self.path = str(Path(path))
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def _ensure(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            async with aiosqlite.connect(self.path, timeout=30) as db:
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("PRAGMA busy_timeout=30000")
                await db.execute(
                    """CREATE TABLE IF NOT EXISTS fsm_storage(
                        storage_key TEXT PRIMARY KEY,
                        state TEXT,
                        data BLOB,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )"""
                )
                await db.commit()
            self._initialized = True

    @staticmethod
    def _key(key: StorageKey) -> str:
        parts = [
            str(getattr(key, "bot_id", "")),
            str(getattr(key, "business_connection_id", "") or ""),
            str(getattr(key, "chat_id", "")),
            str(getattr(key, "user_id", "")),
            str(getattr(key, "thread_id", "") or ""),
            str(getattr(key, "destiny", "default") or "default"),
        ]
        return ":".join(parts)

    @staticmethod
    def _state_value(state: str | State | None) -> str | None:
        if state is None:
            return None
        if isinstance(state, State):
            return state.state
        return str(state)

    async def set_state(self, key: StorageKey, state: str | State | None = None) -> None:
        await self._ensure()
        skey = self._key(key)
        value = self._state_value(state)
        async with aiosqlite.connect(self.path, timeout=30) as db:
            await db.execute("PRAGMA busy_timeout=30000")
            await db.execute(
                """INSERT INTO fsm_storage(storage_key,state,data,updated_at)
                   VALUES (?,?,NULL,CURRENT_TIMESTAMP)
                   ON CONFLICT(storage_key) DO UPDATE SET state=excluded.state,updated_at=CURRENT_TIMESTAMP""",
                (skey, value),
            )
            if value is None:
                cur = await db.execute("SELECT data FROM fsm_storage WHERE storage_key=?", (skey,))
                row = await cur.fetchone()
                if row and row[0] is None:
                    await db.execute("DELETE FROM fsm_storage WHERE storage_key=?", (skey,))
            await db.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        await self._ensure()
        async with aiosqlite.connect(self.path, timeout=30) as db:
            await db.execute("PRAGMA busy_timeout=30000")
            cur = await db.execute("SELECT state FROM fsm_storage WHERE storage_key=?", (self._key(key),))
            row = await cur.fetchone()
            return row[0] if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        if not isinstance(data, Mapping):
            raise TypeError(f"FSM data must be mapping, got {type(data).__name__}")
        await self._ensure()
        skey = self._key(key)
        payload = pickle.dumps(dict(data), protocol=pickle.HIGHEST_PROTOCOL) if data else None
        async with aiosqlite.connect(self.path, timeout=30) as db:
            await db.execute("PRAGMA busy_timeout=30000")
            await db.execute(
                """INSERT INTO fsm_storage(storage_key,state,data,updated_at)
                   VALUES (?,NULL,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(storage_key) DO UPDATE SET data=excluded.data,updated_at=CURRENT_TIMESTAMP""",
                (skey, payload),
            )
            if payload is None:
                cur = await db.execute("SELECT state FROM fsm_storage WHERE storage_key=?", (skey,))
                row = await cur.fetchone()
                if row and row[0] is None:
                    await db.execute("DELETE FROM fsm_storage WHERE storage_key=?", (skey,))
            await db.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        await self._ensure()
        async with aiosqlite.connect(self.path, timeout=30) as db:
            await db.execute("PRAGMA busy_timeout=30000")
            cur = await db.execute("SELECT data FROM fsm_storage WHERE storage_key=?", (self._key(key),))
            row = await cur.fetchone()
        if not row or row[0] is None:
            return {}
        value = pickle.loads(row[0])
        return dict(value) if isinstance(value, Mapping) else {}

    async def close(self) -> None:
        return None
