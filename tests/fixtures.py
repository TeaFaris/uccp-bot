"""Общие заготовки для тестов слоя действий и панели (только для тестов)."""
from __future__ import annotations

import datetime as dt
from typing import List, Tuple

from sqlalchemy import select

from uccp_bot.db.models import (
    Brand, Category, CostStatus, Outlet, Priority, Request, RoleCode, Status,
    User, UserStatus,
)
from uccp_bot.services import history
from uccp_bot.services.numbering import new_external_id, next_request_number
from uccp_bot.utils import utcnow


class FakeBot:
    """Записывает всё, что бот отправил бы в Telegram."""

    def __init__(self) -> None:
        self.sent: List[Tuple[int, str]] = []
        self.files = {}          # file_id -> (bytes, file_size)

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text))

    async def send_photo(self, chat_id, *args, **kwargs):
        self.sent.append((chat_id, "[photo]"))

    async def send_video(self, chat_id, *args, **kwargs):
        self.sent.append((chat_id, "[video]"))

    async def send_document(self, chat_id, *args, **kwargs):
        self.sent.append((chat_id, "[document]"))

    async def send_media_group(self, chat_id, *args, **kwargs):
        self.sent.append((chat_id, "[media_group]"))

    def to(self, chat_id: int) -> List[str]:
        return [text for cid, text in self.sent if cid == chat_id]

    def clear(self) -> None:
        self.sent.clear()


def check(condition: bool, label: str) -> None:
    print(("✅ " if condition else "❌ ") + label)
    if not condition:
        raise AssertionError(label)


async def make_world(session) -> dict:
    """Бренды и точки из seed + люди всех ролей + одна новая заявка."""
    moose = await session.scalar(select(Brand).where(Brand.name == "Moose Café"))
    hd = await session.scalar(select(Brand).where(Brand.name == "Hotdogger"))
    opera = await session.scalar(select(Outlet).where(Outlet.name == "Moose Opera"))
    airport = await session.scalar(select(Outlet).where(Outlet.name == "Hotdogger Аэропорт"))
    electric = await session.scalar(select(Category).where(Category.code == "electric"))
    master = await session.scalar(select(User).where(User.full_name == "Назрулло"))
    master.tg_id = 900

    def make(tg, name, role, brand=None, outlet=None, status=UserStatus.ACTIVE):
        u = User(tg_id=tg, full_name=name, last_name=name, role_code=role,
                 brand_id=brand.id if brand else None,
                 outlet_id=outlet.id if outlet else None,
                 status=status, is_registered=True, position_text="должность")
        u.sync_flags()
        session.add(u)
        return u

    world = dict(
        moose=moose, hd=hd, opera=opera, airport=airport, electric=electric, master=master,
        admin=make(1, "Главный админ", RoleCode.ADMIN),
        chief=make(2, "Главный опердир", RoleCode.OPS_CHIEF),
        ops_hd=make(3, "Опердир Hotdogger", RoleCode.OPS_DIRECTOR, hd),
        staff=make(4, "Сотрудник точки", RoleCode.STAFF, moose, opera),
        master2=make(901, "Второй мастер", RoleCode.EXECUTOR),
        pending=make(7, "Новичок", RoleCode.STAFF, status=UserStatus.PENDING),
    )
    await session.flush()
    world["req"] = await add_request(session, world, opera, world["staff"])
    return world


async def add_request(session, world, outlet, author, status=Status.NEW,
                      cost=None, cost_status=CostStatus.NONE) -> Request:
    req = Request(
        number=await next_request_number(session), external_id=new_external_id(),
        author_id=author.id, brand_id=outlet.brand_id, outlet_id=outlet.id,
        category_id=world["electric"].id, executor_id=world["master"].id,
        description=f"Заявка по точке {outlet.name}",
        priority=Priority.HIGH, status=status,
        due_at=utcnow() + dt.timedelta(hours=4),
        work_cost=cost, material_cost=0 if cost is not None else None,
        cost_status=cost_status,
    )
    session.add(req)
    await session.flush()
    await history.log(session, req, "created", user=author, new_status=Status.NEW)
    return req
