"""Общее для слоя действий: бот и панель вызывают одни и те же функции.

Функция действия сама проверяет право и допустимость по статусу. Отказ —
`ActionError` с текстом, который можно показать человеку как есть.
"""
from __future__ import annotations

import asyncio
import weakref

from .. import texts

# Бот и панель работают в одном процессе. Действие над одним объектом
# (заявкой, пользователем, справочником) выполняется строго по очереди:
# двойной клик или одновременное нажатие в боте и в панели не применят его дважды.
_locks: "weakref.WeakValueDictionary[tuple, asyncio.Lock]" = weakref.WeakValueDictionary()


def entity_lock(kind: str, entity_id: object = 0) -> asyncio.Lock:
    """Блокировка на объект; держать от загрузки объекта до коммита."""
    key = (kind, entity_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


class ActionError(Exception):
    """Действие недоступно — текст объясняет почему."""


def require(condition: bool, message: str = texts.NO_ACCESS) -> None:
    if not condition:
        raise ActionError(message)


def clean_reason(raw: str, what: str = "причину") -> str:
    """Причины и комментарии для журнала — не короче трёх символов."""
    text = (raw or "").strip()
    require(len(text) >= 3, f"Укажите {what} подробнее — не короче трёх символов.")
    return text
