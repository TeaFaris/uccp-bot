"""Хранилище шагов диалогов (FSM) в отдельном файле SQLite.

Раньше шаги жили в памяти процесса (MemoryStorage) и пропадали при каждом
перезапуске бота — деплое или сбое. Человек нажимал кнопку календаря или
мастера заявки, а бот уже не помнил, на каком он шаге, и отвечал
«Это сообщение устарело». Теперь шаги переживают перезапуск.

Отдельный файл, а не таблица в основной базе: обработчик может держать
открытую транзакцию записи в основной базе и в это же время менять шаг —
в одном файле SQLite такая запись ждала бы саму себя до таймаута.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from typing import Any, Dict, Mapping, Optional

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

# брошенные на полпути диалоги старше этого срока удаляются при старте
KEEP_DAYS = 30


class SQLiteStorage(BaseStorage):
    def __init__(self, path: str) -> None:
        self.path = path
        self._db: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    # --- синхронная часть: выполняется в пуле потоков, не тормозит бота ---
    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            db = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS fsm ("
                " key TEXT PRIMARY KEY,"
                " state TEXT,"
                " data TEXT NOT NULL DEFAULT '{}',"
                " updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            db.execute(
                "DELETE FROM fsm WHERE updated_at < datetime('now', ?)",
                (f"-{KEEP_DAYS} days",),
            )
            db.commit()
            self._db = db
        return self._db

    def _write_sync(self, key: str, column: str, value: Optional[str]) -> None:
        with self._lock:
            db = self._conn()
            db.execute(
                f"INSERT INTO fsm (key, {column}) VALUES (?, ?) "
                f"ON CONFLICT(key) DO UPDATE SET {column} = excluded.{column}, "
                "updated_at = CURRENT_TIMESTAMP",
                (key, value),
            )
            # пустые записи не храним — таблица остаётся маленькой
            db.execute(
                "DELETE FROM fsm WHERE key = ? AND state IS NULL AND data = '{}'", (key,)
            )
            db.commit()

    def _read_sync(self, key: str, column: str) -> Optional[str]:
        with self._lock:
            row = self._conn().execute(
                f"SELECT {column} FROM fsm WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    # --- интерфейс aiogram ---
    @staticmethod
    def _key(key: StorageKey) -> str:
        parts = [
            key.bot_id, key.chat_id, key.user_id,
            key.thread_id or "", key.business_connection_id or "", key.destiny,
        ]
        if getattr(key, "is_guest", False):
            parts.append("guest")
        return ":".join(str(part) for part in parts)

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        await asyncio.to_thread(self._write_sync, self._key(key), "state", value)

    async def get_state(self, key: StorageKey) -> Optional[str]:
        return await asyncio.to_thread(self._read_sync, self._key(key), "state")

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        raw = json.dumps(dict(data), ensure_ascii=False)
        await asyncio.to_thread(self._write_sync, self._key(key), "data", raw)

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        raw = await asyncio.to_thread(self._read_sync, self._key(key), "data")
        return json.loads(raw) if raw else {}

    async def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
