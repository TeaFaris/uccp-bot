"""Мелочи интерфейса: чтобы у пользователя не оставалось «мёртвых» кнопок.

Если человек бросил заполнение заявки (нажал /start, /menu или «Отменить»),
у старого сообщения остаётся клавиатура шага. Нажатие на неё раньше
приводило в тупик: «Это сообщение устарело». Теперь клавиатура снимается.
"""
from __future__ import annotations

import logging
from typing import Optional

from aiogram import Bot
from aiogram.fsm.context import FSMContext

log = logging.getLogger(__name__)


async def remember_step(state: FSMContext, chat_id: int, message_id: int) -> None:
    """Запоминаем сообщение с клавиатурой текущего шага."""
    await state.update_data(step_chat_id=chat_id, step_message_id=message_id)


async def drop_step_keyboard(bot: Bot, state: FSMContext) -> None:
    """Снимаем клавиатуру с незавершённого шага, если он был."""
    data = await state.get_data()
    chat_id = data.get("step_chat_id")
    message_id = data.get("step_message_id")
    if not chat_id or not message_id:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except Exception as exc:        # сообщение могли удалить или оно слишком старое
        log.debug("Не удалось снять клавиатуру шага: %s", exc)
