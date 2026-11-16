"""Предупреждение о второй копии бота с тем же токеном.

Если бот с этим же токеном запущен ещё где-то (на ноутбуке разработчика,
на старом сервере), Telegram делит обновления между копиями. Со стороны
это выглядит как «сообщение устарело» на кнопках и пропавшие заявки:
у каждой копии свои шаги диалогов и, как правило, своя база. Сам бот видит
только ошибку Conflict в журнале — поэтому сообщаем о ней владельцу.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable, List, Optional

log = logging.getLogger(__name__)

TEXT = (
    "⚠️ <b>Запущена вторая копия бота с этим же токеном.</b>\n\n"
    "Telegram делит нажатия между копиями: часть кнопок отвечает "
    "«сообщение устарело», а заявки, созданные через другую копию, "
    "не попадают в рабочую базу.\n\n"
    "Остановите лишний запуск (например, <code>python -m uccp_bot.bot</code> "
    "на своём компьютере или другом сервере). Для проверок заведите "
    "отдельного тестового бота в @BotFather."
)


class ConflictAlert(logging.Handler):
    """Ловит TelegramConflictError в журнале aiogram и пишет владельцам."""

    COOLDOWN = 6 * 3600     # не чаще раза в 6 часов
    # При деплое старый и новый контейнер пару секунд работают вместе —
    # это один-два конфликта. Вторая копия даёт конфликт каждые ~10 секунд.
    THRESHOLD = 6
    WINDOW = 180

    def __init__(self, bot, admin_ids: Iterable[int]) -> None:
        super().__init__(level=logging.ERROR)
        self.bot = bot
        self.admin_ids = list(admin_ids)
        self._last: Optional[float] = None
        self._seen: List[float] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if "TelegramConflictError" not in record.getMessage():
                return
            now = time.monotonic()
            self._seen = [t for t in self._seen if now - t < self.WINDOW] + [now]
            if len(self._seen) < self.THRESHOLD:
                return
            if self._last is not None and now - self._last < self.COOLDOWN:
                return
            self._last = now
            asyncio.get_running_loop().create_task(self._notify())
        except Exception:       # журнал не должен ронять бота
            pass

    async def _notify(self) -> None:
        for chat_id in self.admin_ids:
            try:
                await self.bot.send_message(chat_id, TEXT)
            except Exception as exc:
                log.warning("Не удалось предупредить %s о второй копии: %s", chat_id, exc)
