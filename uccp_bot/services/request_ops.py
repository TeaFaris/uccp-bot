"""Действия над заявкой — одни и те же для бота и панели.

Каждая функция проверяет право и статус, меняет заявку, пишет историю
и рассылает уведомления. Коммит — за вызывающим (как в `flow.py`).
"""
from __future__ import annotations

import datetime as dt
from typing import Optional, Set

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Category, Comment, CostStatus, Request, Status, User
from ..utils import esc, fmt_dt, fmt_money, utcnow
from . import flow, history, notify
from .ops import ActionError, clean_reason, require

__all__ = ["ActionError", "available_actions"]


def _is_admin(user: Optional[User]) -> bool:
    return user is not None and user.is_approved and user.is_admin


def _require_open(request: Request) -> None:
    require(
        request.status not in Status.FINAL,
        f"Заявка {request.number} уже в статусе «{Status.title(request.status)}» — "
        "действие недоступно.",
    )


def available_actions(request: Request, user: Optional[User]) -> Set[str]:
    """Какие действия показать администратору — те же, что кнопки `admin_kb`."""
    if not _is_admin(user):
        return set()
    acts: Set[str] = set()
    if request.status not in Status.FINAL:
        acts |= {"assign", "change_category", "change_due"}
    if request.cost_status == CostStatus.PENDING and user.can_approve_cost:
        acts |= {"cost_ok", "cost_no"}
    if request.cost_status in (CostStatus.APPROVED, CostStatus.PENDING) and user.can_edit_locked:
        acts.add("edit_cost")
    if request.status == Status.DONE and user.can_confirm:
        acts |= {"confirm", "return"}
    if request.status in Status.OPEN:
        acts |= {"comment", "cancel"}
    return acts


# --------------------------------------------------------------------------- #
# Исполнитель, категория, срок
# --------------------------------------------------------------------------- #
async def assign(
    session: AsyncSession, bot: Bot, request: Request, executor: Optional[User], actor: User
) -> bool:
    """Назначить или перенаправить. True — если мастер получил уведомление."""
    require(_is_admin(actor))
    _require_open(request)
    require(
        executor is not None and executor.is_executor and executor.is_active,
        "Выберите активного исполнителя.",
    )
    return await flow.assign_executor(session, bot, request, executor, actor)


async def change_category(
    session: AsyncSession, bot: Bot, request: Request, category: Optional[Category], actor: User
) -> str:
    """Смена категории с повторной маршрутизацией: ok | unregistered | none."""
    require(_is_admin(actor))
    _require_open(request)
    require(category is not None and category.is_active, "Выберите действующую категорию.")
    old_label = request.category.label
    request.category_id = category.id
    request.category = category
    await history.log(
        session, request, "category_changed", user=actor,
        details=f"{old_label} → {category.label}",
    )
    await session.flush()
    return await flow.dispatch_request(session, bot, request, actor=actor)


async def change_due(
    session: AsyncSession, bot: Bot, request: Request, new_due: dt.datetime, actor: User
) -> None:
    """Новый срок (UTC) от администратора."""
    require(_is_admin(actor))
    _require_open(request)
    require(new_due >= utcnow(), "Новый срок не может быть в прошлом.")
    old_due = request.due_at
    request.due_at = new_due
    request.is_overdue = False
    await history.log(
        session, request, "due_changed", user=actor,
        details=f"{fmt_dt(old_due)} → {fmt_dt(request.due_at)}",
    )
    await session.flush()
    note = (
        f"📅 Срок по заявке <b>{esc(request.number)}</b> изменён администратором: "
        f"{fmt_dt(old_due)} → <b>{fmt_dt(request.due_at)}</b>"
    )
    await notify.notify_request_watchers(
        session, bot, request, note, to_author=True, to_executor=True,
        exclude_user_id=actor.id,
    )


# --------------------------------------------------------------------------- #
# Отмена и комментарий
# --------------------------------------------------------------------------- #
async def cancel(
    session: AsyncSession, bot: Bot, request: Request, actor: User, reason: str
) -> None:
    """Отменить может администратор (заявка в работе) или автор, пока её не взяли."""
    require(actor is not None and actor.is_approved)
    by_admin = actor.is_admin and request.status in Status.OPEN
    by_author = request.author_id == actor.id and request.status in (
        Status.NEW, Status.AWAITING_ASSIGNMENT,
    )
    _require_open(request)
    require(by_admin or by_author)
    reason = clean_reason(reason, "причину отмены")
    await flow.cancel_request(session, bot, request, actor, reason)


async def add_comment(
    session: AsyncSession, bot: Bot, request: Request, actor: User, text: str
) -> None:
    require(actor is not None and actor.is_approved and not actor.is_viewer)
    # как в боте: кнопка комментария есть только у заявки в работе
    require(
        request.status in Status.OPEN,
        f"Заявка {request.number} уже в статусе «{Status.title(request.status)}» — "
        "действие недоступно.",
    )
    text = (text or "").strip()
    require(bool(text), "Напишите текст комментария.")
    session.add(Comment(request_id=request.id, user_id=actor.id, text=text))
    if actor.is_executor:
        request.executor_comment = text
    elif actor.can_confirm:
        request.manager_comment = text
    await history.log(session, request, "comment", user=actor, details=text)
    await session.flush()
    note = (
        f"💬 Новый комментарий к заявке <b>{esc(request.number)}</b>\n"
        f"{esc(actor.full_name)}: <i>{esc(text)}</i>"
    )
    await notify.notify_request_watchers(
        session, bot, request, note, to_author=True, to_executor=True,
        exclude_user_id=actor.id,
    )


# --------------------------------------------------------------------------- #
# Подтверждение и возврат
# --------------------------------------------------------------------------- #
def _require_can_confirm(actor: Optional[User], request: Request) -> None:
    require(actor is not None and actor.can_confirm)
    require(not actor.is_executor, "Исполнитель не может закрывать свою заявку.")
    require(request.status == Status.DONE, "Заявка не в статусе «Выполнена».")


async def confirm(session: AsyncSession, bot: Bot, request: Request, actor: User) -> None:
    _require_can_confirm(actor, request)
    await flow.close_request(session, bot, request, actor)


async def return_for_rework(
    session: AsyncSession, bot: Bot, request: Request, actor: User, reason: str
) -> None:
    _require_can_confirm(actor, request)
    reason = clean_reason(reason, "причину возврата")
    await flow.return_request(session, bot, request, actor, reason)


# --------------------------------------------------------------------------- #
# Стоимость — право главного администратора
# --------------------------------------------------------------------------- #
COST_ONLY_ADMIN = "Согласовать стоимость может только главный администратор."


def _require_cost_pending(actor: Optional[User], request: Request) -> None:
    require(actor is not None and actor.can_approve_cost, COST_ONLY_ADMIN)
    require(
        request.cost_status == CostStatus.PENDING,
        f"Стоимость уже {request.cost_status_title}.",
    )


async def approve_cost(session: AsyncSession, bot: Bot, request: Request, actor: User) -> None:
    _require_cost_pending(actor, request)
    request.cost_status = CostStatus.APPROVED
    request.cost_decided_at = utcnow()
    request.cost_decided_by_id = actor.id
    await history.log(
        session, request, "cost_approved", user=actor,
        details=f"Согласована стоимость {fmt_money(request.total_cost)}",
    )
    await session.flush()
    await notify.send_to_user(
        bot, request.executor,
        f"✅ Стоимость по заявке <b>{esc(request.number)}</b> согласована: "
        f"{fmt_money(request.total_cost)}",
    )


async def reject_cost(
    session: AsyncSession, bot: Bot, request: Request, actor: User, reason: str
) -> None:
    _require_cost_pending(actor, request)
    reason = clean_reason(reason, "причину отклонения стоимости")
    request.cost_status = CostStatus.REJECTED
    request.cost_decided_at = utcnow()
    request.cost_decided_by_id = actor.id
    request.cost_reject_reason = reason
    await history.log(
        session, request, "cost_rejected", user=actor,
        details=f"Отклонена стоимость {fmt_money(request.total_cost)}. Причина: {reason}",
    )
    await session.flush()
    await notify.send_to_user(
        bot, request.executor,
        f"❌ Стоимость по заявке <b>{esc(request.number)}</b> не согласована.\n"
        f"Причина: <i>{esc(reason)}</i>\n\n"
        "Укажите стоимость заново: откройте заявку и нажмите «💰 Заявить стоимость».",
    )


async def edit_cost(
    session: AsyncSession, bot: Bot, request: Request, actor: User,
    work: float, material: float, reason: str,
) -> None:
    """Правка зафиксированной стоимости: сразу согласована, исходные суммы — в истории."""
    require(
        actor is not None and actor.can_edit_locked,
        "Изменять зафиксированные данные вправе только главный администратор.",
    )
    require(
        request.cost_status in (CostStatus.APPROVED, CostStatus.PENDING),
        "Стоимость по этой заявке ещё не заявлена.",
    )
    require(
        work is not None and material is not None and work >= 0 and material >= 0,
        "Суммы должны быть числами не меньше нуля.",
    )
    reason = clean_reason(reason, "причину изменения")
    old_work, old_material = request.work_cost, request.material_cost
    request.work_cost = work
    request.material_cost = material
    request.cost_status = CostStatus.APPROVED
    request.cost_decided_at = utcnow()
    request.cost_decided_by_id = actor.id
    await history.log(
        session, request, "cost_edited", user=actor,
        details=(
            f"Было: работы {fmt_money(old_work)}, материалы {fmt_money(old_material)}. "
            f"Стало: работы {fmt_money(request.work_cost)}, "
            f"материалы {fmt_money(request.material_cost)}. "
            f"Причина: {reason}"
        ),
    )
    await session.flush()
    await notify.send_to_user(
        bot, request.executor,
        f"ℹ️ Главный администратор изменил стоимость по заявке "
        f"<b>{esc(request.number)}</b>: {fmt_money(request.total_cost)}\n"
        f"Причина: <i>{esc(reason)}</i>",
    )
