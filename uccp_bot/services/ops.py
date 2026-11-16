"""Общее для слоя действий: бот и панель вызывают одни и те же функции.

Функция действия сама проверяет право и допустимость по статусу. Отказ —
`ActionError` с текстом, который можно показать человеку как есть.
"""
from __future__ import annotations

from .. import texts


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
