"""Действия администратора над пользователями — одни и те же для бота и панели.

Проверяют право (`can_manage_users`), меняют данные, уведомляют человека.
Коммит — за вызывающим.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import texts
from ..db.models import (
    Brand, Category, ExecutorAssignment, Outlet, RoleCode, User, UserScope, UserStatus,
)
from ..keyboards.common import main_menu
from ..utils import esc, normalize_phone, utcnow
from .ops import ActionError, clean_reason, require

__all__ = ["ActionError"]


def _require_manager(actor: Optional[User]) -> None:
    require(actor is not None and actor.can_manage_users)


async def _send(bot: Bot, user: User, text: str, **kwargs) -> None:
    if not user.tg_id or not text:
        return
    try:
        await bot.send_message(user.tg_id, text, **kwargs)
    except Exception:       # человек мог заблокировать бота — действие от этого не отменяется
        pass


async def direction_ids(session: AsyncSession, user_id: int) -> List[int]:
    return list((await session.scalars(
        select(ExecutorAssignment.category_id).where(ExecutorAssignment.user_id == user_id)
    )).all())


# --------------------------------------------------------------------------- #
# Регистрации
# --------------------------------------------------------------------------- #
async def finish_approval(session: AsyncSession, bot: Bot, target: User, actor: User) -> str:
    """Выдать доступ и уведомить человека. Возвращает итог для администратора."""
    _require_manager(actor)
    target.status = UserStatus.ACTIVE
    target.approved_at = utcnow()
    target.approved_by_id = actor.id
    target.reject_reason = None
    target.sync_flags()
    await session.flush()

    role = target.role_code
    brand = await session.get(Brand, target.brand_id) if target.brand_id else None
    outlet = await session.get(Outlet, target.outlet_id) if target.outlet_id else None

    names: List[str] = []
    if role == RoleCode.EXECUTOR:
        for cat_id in await direction_ids(session, target.id):
            category = await session.get(Category, cat_id)
            if category:
                names.append(category.name)

    scope_lines = {
        RoleCode.STAFF: f"Объект: {outlet.name if outlet else '—'}",
        RoleCode.OUTLET_ADMIN: f"Объект: {outlet.name if outlet else '—'}",
        RoleCode.OPS_DIRECTOR: f"Бренд: {brand.name if brand else '—'}",
        RoleCode.EXECUTOR: "Направления: " + (", ".join(names) if names else "—")
        + (f"\nБренд: {brand.name}" if brand else "\nБренды и объекты: все"),
        RoleCode.UCCP_STAFF: "Доступ: все обращения точек в УЦЦП",
        RoleCode.ADMIN: "Доступ: полный",
    }
    scope = scope_lines.get(role, "")

    role_label = target.role_title
    if role == RoleCode.EXECUTOR and names:
        role_label = f"Мастер — {names[0].lower()}" if len(names) == 1 else "Мастер"

    await _send(
        bot, target,
        texts.APPROVED_NOTICE.format(
            role=esc(role_label),
            scope=esc(scope),
            hint=(
                "Теперь вы будете получать назначенные вам заявки."
                if role == RoleCode.EXECUTOR
                else "Рабочее меню открыто — можно создавать заявки."
            ),
        ),
        reply_markup=main_menu(target),
    )
    await _send(bot, target, texts.ROLE_INSTRUCTIONS.get(role, ""))

    warn = "" if target.tg_id else "\n\n⚠️ Пользователь ещё не активировал бота."
    return (
        f"✅ Регистрация <b>№{target.id}</b> подтверждена.\n\n"
        f"{esc(target.full_name)}\n"
        f"Роль: <b>{esc(role_label)}</b>\n{esc(scope)}{warn}"
    )


async def approve(
    session: AsyncSession, bot: Bot, target: User, actor: User, role: str,
    brand_id: Optional[int], outlet_id: Optional[int], category_ids: Iterable[int],
) -> str:
    """Одобрение регистрации за один шаг (панель). Правила — как у мастера в боте."""
    _require_manager(actor)
    # как в боте: одобряется только ожидающая заявка
    require(target.status == UserStatus.PENDING,
            "Одобрить можно только заявку, которая ждёт подтверждения.")
    require(role in RoleCode.ALL, "Выберите роль.")
    category_ids = [int(c) for c in category_ids]

    if role in (RoleCode.ADMIN, RoleCode.UCCP_STAFF, RoleCode.OPS_CHIEF):
        brand_id = outlet_id = None
    elif role in (RoleCode.OPS_DIRECTOR, RoleCode.EXECUTOR):
        outlet_id = None
    if outlet_id:
        outlet = await session.get(Outlet, outlet_id)
        require(outlet is not None, "Точка не найдена.")
        brand_id = outlet.brand_id
    if brand_id:
        require(await session.get(Brand, brand_id) is not None, "Бренд не найден.")
    # те же обязательные шаги, что у мастера одобрения в боте
    if role in (RoleCode.STAFF, RoleCode.OUTLET_ADMIN):
        require(bool(outlet_id), "Для этой роли выберите точку.")
    if role == RoleCode.OPS_DIRECTOR:
        require(bool(brand_id), "Для операционного директора выберите бренд.")
    if role == RoleCode.EXECUTOR:
        require(bool(category_ids), "Отметьте хотя бы одно направление мастера.")
        for cat_id in category_ids:
            category = await session.get(Category, cat_id)
            require(category is not None and category.is_active,
                    "Направление не найдено — обновите страницу.")

    target.role_code = role
    target.brand_id = brand_id or None
    target.outlet_id = outlet_id or None
    if role == RoleCode.EXECUTOR:
        for old in (await session.scalars(
            select(ExecutorAssignment).where(ExecutorAssignment.user_id == target.id)
        )).all():
            await session.delete(old)
        for cat_id in category_ids:
            session.add(ExecutorAssignment(
                user_id=target.id, category_id=cat_id, brand_id=target.brand_id,
            ))
    await session.flush()
    return await finish_approval(session, bot, target, actor)


async def reject(session: AsyncSession, bot: Bot, target: User, actor: User, reason: str) -> None:
    _require_manager(actor)
    reason = clean_reason(reason, "причину отклонения")
    target.status = UserStatus.REJECTED
    target.reject_reason = reason
    target.sync_flags()
    await session.flush()
    await _send(bot, target, texts.REJECTED_HINT.format(reason=f"Причина: <i>{esc(reason)}</i>"))


# --------------------------------------------------------------------------- #
# Карточка пользователя
# --------------------------------------------------------------------------- #
async def set_role(
    session: AsyncSession, bot: Bot, target: User, actor: User, role: str
) -> Optional[str]:
    """Новая роль. Возвращает предупреждение: needs_directions | stale_directions | None."""
    _require_manager(actor)
    require(role in RoleCode.ALL, "Выберите роль.")
    target.role_code = role
    if target.status == UserStatus.PENDING:
        target.status = UserStatus.ACTIVE
        target.approved_at = utcnow()
        target.approved_by_id = actor.id
    target.sync_flags()
    await session.flush()

    has_directions = bool(await session.scalar(
        select(ExecutorAssignment.id).where(
            ExecutorAssignment.user_id == target.id,
            ExecutorAssignment.is_active.is_(True),
        )
    ))
    if role == RoleCode.EXECUTOR and not has_directions:
        return "needs_directions"
    await _send(
        bot, target,
        f"ℹ️ Ваша роль изменена на: <b>{target.role_title}</b>.\n"
        + texts.ROLE_INSTRUCTIONS.get(target.role_code, ""),
        reply_markup=main_menu(target),
    )
    if role != RoleCode.EXECUTOR and has_directions:
        return "stale_directions"
    return None


STATUS_NOTICES = {
    UserStatus.BLOCKED: texts.BLOCKED_HINT,
    UserStatus.DISABLED: texts.DISABLED_HINT,
    UserStatus.ACTIVE: "✅ Доступ к боту восстановлен.",
}


async def set_status(
    session: AsyncSession, bot: Bot, target: User, actor: User, status: str
) -> None:
    _require_manager(actor)
    require(status in STATUS_NOTICES, "Недопустимый статус.")
    require(
        not (target.id == actor.id and status != UserStatus.ACTIVE),
        "Нельзя заблокировать самого себя.",
    )
    target.status = status
    target.sync_flags()
    await session.flush()
    await _send(bot, target, STATUS_NOTICES[status], reply_markup=main_menu(target))


async def set_fixed_payment(
    session: AsyncSession, bot: Bot, target: User, actor: User, value: bool
) -> None:
    """Фиксированная месячная оплата: у мастера не спрашиваем стоимость."""
    _require_manager(actor)
    if bool(target.fixed_payment) == bool(value):
        return
    target.fixed_payment = bool(value)
    await session.flush()
    await _send(
        bot, target,
        "ℹ️ Условия оплаты обновлены: <b>фиксированная ежемесячная</b>.\n"
        "При завершении заявки укажите только выполненные работы и, "
        "при необходимости, фото — стоимость вводить не нужно."
        if target.fixed_payment
        else "ℹ️ Условия оплаты обновлены: <b>оплата по каждой заявке</b>.\n"
        "При завершении заявки снова потребуется указать стоимость работ "
        "и материалов.",
    )


async def set_brand(session: AsyncSession, target: User, actor: User, brand_id: Optional[int]) -> None:
    _require_manager(actor)
    target.brand_id = brand_id or None
    if target.outlet_id:
        outlet = await session.get(Outlet, target.outlet_id)
        if outlet and outlet.brand_id != target.brand_id:
            target.outlet_id = None
    await session.flush()


async def set_outlet(session: AsyncSession, target: User, actor: User, outlet_id: Optional[int]) -> None:
    """Точка задаёт и бренд пользователя."""
    _require_manager(actor)
    target.outlet_id = outlet_id or None
    if target.outlet_id:
        outlet = await session.get(Outlet, target.outlet_id)
        require(outlet is not None, "Точка не найдена.")
        target.brand_id = outlet.brand_id
    await session.flush()


async def set_phone(session: AsyncSession, target: User, actor: User, raw: str) -> None:
    """«-» или пусто — очистить."""
    _require_manager(actor)
    raw = (raw or "").strip()
    target.phone = None if raw in ("", "-") else (normalize_phone(raw) or raw)
    await session.flush()


# --------------------------------------------------------------------------- #
# Дополнительные объекты руководителя и направления мастера
# --------------------------------------------------------------------------- #
async def add_scope(
    session: AsyncSession, bot: Bot, target: User, actor: User,
    brand_id: Optional[int] = None, outlet_id: Optional[int] = None,
) -> UserScope:
    _require_manager(actor)
    require(bool(brand_id or outlet_id), "Выберите бренд или точку.")
    if outlet_id:
        outlet = await session.get(Outlet, outlet_id)
        require(outlet is not None, "Точка не найдена.")
        row = UserScope(user_id=target.id, outlet_id=outlet.id, brand_id=outlet.brand_id,
                        created_by_id=actor.id)
        label = outlet.name
        same = UserScope.outlet_id == outlet.id
    else:
        brand = await session.get(Brand, brand_id)
        require(brand is not None, "Бренд не найден.")
        row = UserScope(user_id=target.id, brand_id=brand.id, created_by_id=actor.id)
        label = f"{brand.name} (весь бренд)"
        same = (UserScope.brand_id == brand.id) & UserScope.outlet_id.is_(None)
    exists = await session.scalar(
        select(UserScope.id).where(UserScope.user_id == target.id, same)
    )
    require(exists is None, "Этот объект уже закреплён.")
    session.add(row)
    await session.flush()
    await _send(bot, target, f"ℹ️ Вам дополнительно открыт доступ к объекту: <b>{esc(label)}</b>.")
    return row


async def remove_scope(session: AsyncSession, target: User, actor: User, scope_id: int) -> None:
    _require_manager(actor)
    row = await session.get(UserScope, scope_id)
    if row is not None and row.user_id == target.id:
        await session.delete(row)
        await session.flush()


async def add_direction(
    session: AsyncSession, target: User, actor: User, category_id: int,
    brand_id: Optional[int] = None, outlet_id: Optional[int] = None,
) -> ExecutorAssignment:
    _require_manager(actor)
    require(target.is_executor, "Направления назначаются только мастерам.")
    require(await session.get(Category, category_id) is not None, "Категория не найдена.")
    if outlet_id:
        outlet = await session.get(Outlet, outlet_id)
        require(outlet is not None, "Точка не найдена.")
        brand_id = outlet.brand_id
    row = ExecutorAssignment(
        user_id=target.id, category_id=category_id,
        brand_id=brand_id or None, outlet_id=outlet_id or None,
    )
    session.add(row)
    await session.flush()
    return row


async def remove_direction(session: AsyncSession, target: User, actor: User, assignment_id: int) -> None:
    _require_manager(actor)
    row = await session.get(ExecutorAssignment, assignment_id)
    if row is not None and row.user_id == target.id:
        await session.delete(row)
        await session.flush()
