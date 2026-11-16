"""Предупреждение владельцу, когда с тем же токеном запущена вторая копия бота.

Запуск: .venv/bin/python tests/conflict_check.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "123456789:AAEsTtEsTtEsTtEsTtEsTtEsTtEsTtEsTtEs")

from uccp_bot.services.watchdog import ConflictAlert  # noqa: E402

CONFLICT = (
    "Failed to fetch updates - TelegramConflictError: Telegram server says - "
    "Conflict: terminated by other getUpdates request; make sure that only one "
    "bot instance is running"
)


class FakeBot:
    def __init__(self) -> None:
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


def check(condition: bool, label: str) -> None:
    print(("✅ " if condition else "❌ ") + label)
    if not condition:
        raise AssertionError(label)


async def main() -> None:
    bot = FakeBot()
    logger = logging.getLogger("aiogram.dispatcher")
    handler = ConflictAlert(bot, [111, 222])
    logger.addHandler(handler)
    try:
        logger.error("Failed to fetch updates - TelegramNetworkError: timeout")
        await asyncio.sleep(0.05)
        check(bot.sent == [], "Обычная сетевая ошибка не вызывает предупреждения")

        for _ in range(3):
            logger.error(CONFLICT)
        await asyncio.sleep(0.05)
        check(bot.sent == [], "Пара-тройка конфликтов (перекрытие при деплое) не тревожит владельца")
        for _ in range(3):
            logger.error(CONFLICT)
        await asyncio.sleep(0.05)
        check(sorted(c for c, _ in bot.sent) == [111, 222],
              "Устойчивые конфликты — предупреждение получил каждый владелец")
        check("вторая копия" in bot.sent[0][1], "В тексте понятно, что случилось")

        logger.error(CONFLICT)
        logger.error(CONFLICT)
        await asyncio.sleep(0.05)
        check(len(bot.sent) == 2, "Повторные конфликты не засыпают сообщениями")
    finally:
        logger.removeHandler(handler)
    print("\n🎉 Предупреждение о второй копии работает.")


if __name__ == "__main__":
    asyncio.run(main())
