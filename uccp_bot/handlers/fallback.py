"""Ответ на непонятые сообщения и на нажатия по устаревшим кнопкам (п.42).

Тупиков быть не должно: если кнопка больше не работает, бот объясняет
почему и сразу предлагает, что делать дальше.
"""
from __future__ import annotations

from typing import Optional

from aiogram import F, Router, types
from aiogram.filters import StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import texts
from ..db.models import User
from ..keyboards.common import BTN_NEW, main_menu

router = Router(name="fallback")

# префиксы шагов мастеров: по ним понятно, что человек нажал кнопку
# незавершённой заявки, а не просто старое служебное сообщение
WIZARD_PREFIXES = ("wz:", "og:", "cal:")


@router.message(StateFilter(None))
async def unknown_message(message: types.Message, user: Optional[User]) -> None:
    if user is None or not user.is_registered:
        await message.answer(
            "Я вас пока не знаю. Отправьте /start — это займёт минуту, "
            "после этого можно создавать заявки."
        )
        return
    await message.answer(
        "Не понял команду. Выберите действие кнопкой меню ниже "
        "или откройте 📖 Инструкцию / Помощь.",
        reply_markup=main_menu(user),
    )


@router.callback_query()
async def stale_callback(call: types.CallbackQuery, user: Optional[User]) -> None:
    data = call.data or ""

    # кнопка брошенного шага заявки
    if data.startswith(WIZARD_PREFIXES):
        await call.answer("Эта заявка уже не заполняется", show_alert=False)
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await call.message.answer(
            "ℹ️ Заполнение этой заявки было прервано — например, вы отправили "
            "/start или нажали «Отменить». Старые кнопки больше не работают.\n\n"
            f"Чтобы оформить заявку, нажмите «{BTN_NEW}» в меню ниже.",
            reply_markup=main_menu(user),
        )
        return

    await call.answer(
        "Это сообщение устарело — откройте нужный раздел через меню.",
        show_alert=True,
    )
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
