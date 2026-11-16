"""Справочники: точки, категории, мастера — одни и те же действия для бота и панели."""
from __future__ import annotations

import secrets
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Category, Outlet, RoleCode, User, UserStatus
from ..utils import normalize_phone, utcnow
from .ops import ActionError, require

__all__ = ["ActionError"]

CATEGORY_FLAGS = ("is_active", "photo_required")


def _require_manager(actor: Optional[User]) -> None:
    require(actor is not None and actor.can_manage_users)


def _name(raw: str) -> str:
    name = (raw or "").strip()
    require(bool(name), "Введите название.")
    return name


# --- точки ---
async def add_outlet(session: AsyncSession, actor: User, brand_id: int, name: str) -> Outlet:
    _require_manager(actor)
    name = _name(name)
    exists = await session.scalar(
        select(Outlet.id).where(Outlet.brand_id == brand_id, Outlet.name == name)
    )
    require(exists is None, "Такая точка уже есть. Введите другое название.")
    outlet = Outlet(brand_id=brand_id, name=name)
    session.add(outlet)
    await session.flush()
    return outlet


async def rename_outlet(session: AsyncSession, actor: User, outlet: Outlet, name: str) -> None:
    _require_manager(actor)
    outlet.name = _name(name)
    await session.flush()


async def set_outlet_brand(session: AsyncSession, actor: User, outlet: Outlet, brand_id: int) -> None:
    _require_manager(actor)
    outlet.brand_id = brand_id
    await session.flush()


async def toggle_outlet(session: AsyncSession, actor: User, outlet: Outlet) -> None:
    _require_manager(actor)
    outlet.is_active = not outlet.is_active
    await session.flush()


# --- категории ---
async def add_category(session: AsyncSession, actor: User, name: str, emoji: str) -> Category:
    _require_manager(actor)
    name = _name(name)
    emoji = (emoji or "").strip()
    if emoji in ("", "-") or len(emoji) > 4:
        emoji = "📦"
    code = f"custom_{secrets.randbelow(100000)}"
    while await session.scalar(select(Category.id).where(Category.code == code)):
        code = f"custom_{secrets.randbelow(100000)}"
    max_sort = await session.scalar(select(func.max(Category.sort_order))) or 0
    category = Category(code=code, name=name, emoji=emoji, sort_order=max_sort + 10)
    session.add(category)
    await session.flush()
    return category


async def rename_category(session: AsyncSession, actor: User, category: Category, name: str) -> None:
    _require_manager(actor)
    category.name = _name(name)
    await session.flush()


async def toggle_category(session: AsyncSession, actor: User, category: Category, field: str) -> None:
    _require_manager(actor)
    require(field in CATEGORY_FLAGS, "Неизвестный параметр категории.")
    setattr(category, field, not getattr(category, field))
    await session.flush()


# --- мастера ---
async def add_executor(
    session: AsyncSession, actor: User, full_name: str, phone: str, username: str
) -> User:
    """Мастер, заведённый админом: доступ выдан сразу, бота он активирует позже."""
    _require_manager(actor)
    full_name = _name(full_name)
    phone = (phone or "").strip()
    raw = (username or "").strip().lstrip("@")
    username = None if raw in ("", "-") else raw
    if username:
        exists = await session.scalar(
            select(User).where(func.lower(User.username) == username.lower())
        )
        require(
            exists is None,
            f"Пользователь @{username} уже есть в системе ({exists.full_name if exists else ''}).",
        )
    executor = User(
        full_name=full_name,
        last_name=full_name,
        phone=None if phone in ("", "-") else normalize_phone(phone),
        username=username,
        position_text="Мастер (заведён администратором)",
        role_code=RoleCode.EXECUTOR,
        status=UserStatus.ACTIVE,
        is_active=True,
        is_registered=False,
        approved_at=utcnow(),
    )
    session.add(executor)
    await session.flush()
    return executor
