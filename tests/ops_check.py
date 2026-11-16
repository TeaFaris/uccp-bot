"""Общий слой действий: те же правила для бота и панели.

Запуск: .venv/bin/python tests/ops_check.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BOT_TOKEN"] = "123456789:AAEsTtEsTtEsTtEsTtEsTtEsTtEsTtEsTtEs"
os.environ["DB_URL"] = "sqlite+aiosqlite:///" + os.path.join(tempfile.mkdtemp(), "ops.db")

from sqlalchemy import select  # noqa: E402

from uccp_bot.db.base import SessionMaker, init_db  # noqa: E402
from uccp_bot.db.models import (  # noqa: E402
    Category, Comment, CostStatus, Request, RequestStatusHistory, Status,
)
from uccp_bot.db.seed import seed  # noqa: E402
from uccp_bot.services import history, request_ops  # noqa: E402
from uccp_bot.services.cards import load_request  # noqa: E402
from uccp_bot.services.ops import ActionError  # noqa: E402
from uccp_bot.utils import utcnow  # noqa: E402

from fixtures import FakeBot, add_request, check, make_world  # noqa: E402


async def refused(coro) -> bool:
    try:
        await coro
    except ActionError:
        return True
    return False


async def last_event(session, req) -> RequestStatusHistory:
    return await session.scalar(
        select(RequestStatusHistory)
        .where(RequestStatusHistory.request_id == req.id)
        .order_by(RequestStatusHistory.id.desc())
    )


async def request_ops_checks(bot: FakeBot) -> None:
    async with SessionMaker() as session:
        await seed(session)
        w = await make_world(session)
        await session.commit()
        ids = {k: v.id for k, v in w.items() if hasattr(v, "id")}

    print("\n=== Назначение исполнителя ===")
    async with SessionMaker() as session:
        req = await load_request(session, ids["req"])
        admin, chief = await session.get(type(w["admin"]), ids["admin"]), await session.get(type(w["admin"]), ids["chief"])
        master2 = await session.get(type(w["admin"]), ids["master2"])
        check(await refused(request_ops.assign(session, bot, req, master2, chief)),
              "Руководитель не может назначать исполнителя")
        bot.clear()
        sent = await request_ops.assign(session, bot, req, master2, admin)
        await session.commit()
        check(req.executor_id == master2.id and req.status == Status.NEW, "Исполнитель сменён")
        check(sent and any("Новая заявка" in t for t in bot.to(901)), "Новый мастер уведомлён")
        check(any("передана другому" in t for t in bot.to(900)), "Прежний мастер уведомлён")
        check((await last_event(session, req)).action == "reassigned", "Перенаправление в истории")

    print("\n=== Срок ===")
    async with SessionMaker() as session:
        req = await load_request(session, ids["req"])
        admin = await session.get(type(w["admin"]), ids["admin"])
        chief = await session.get(type(w["admin"]), ids["chief"])
        new_due = (utcnow() + dt.timedelta(days=2)).replace(microsecond=0)
        check(await refused(request_ops.change_due(session, bot, req, new_due, chief)),
              "Руководитель не может менять срок")
        check(await refused(request_ops.change_due(
            session, bot, req, utcnow() - dt.timedelta(hours=1), admin)),
              "Срок в прошлом не принимается")
        bot.clear()
        req.is_overdue = True
        await request_ops.change_due(session, bot, req, new_due, admin)
        await session.commit()
        check(req.due_at == new_due and not req.is_overdue, "Срок изменён, просрочка снята")
        check(any("изменён администратором" in t for t in bot.to(4)), "Инициатор уведомлён о сроке")
        check((await last_event(session, req)).action == "due_changed", "Смена срока в истории")

    print("\n=== Категория ===")
    async with SessionMaker() as session:
        req = await load_request(session, ids["req"])
        admin = await session.get(type(w["admin"]), ids["admin"])
        plumbing = await session.scalar(select(Category).where(Category.code != "electric"))
        reason = await request_ops.change_category(session, bot, req, plumbing, admin)
        await session.commit()
        check(req.category_id == plumbing.id, "Категория изменена")
        check(reason in ("ok", "unregistered", "none"), f"Маршрутизация выполнена: {reason}")
        events = (await session.scalars(select(RequestStatusHistory.action).where(
            RequestStatusHistory.request_id == req.id))).all()
        check("category_changed" in events, "Смена категории в истории")

    print("\n=== Комментарий ===")
    async with SessionMaker() as session:
        req = await load_request(session, ids["req"])
        admin = await session.get(type(w["admin"]), ids["admin"])
        check(await refused(request_ops.add_comment(session, bot, req, admin, "  ")),
              "Пустой комментарий не принимается")
        bot.clear()
        await request_ops.add_comment(session, bot, req, admin, "Проверьте щиток")
        await session.commit()
        check(await session.scalar(select(Comment).where(Comment.request_id == req.id)) is not None,
              "Комментарий сохранён")
        check(any("Проверьте щиток" in t for t in bot.to(4)), "Инициатор видит комментарий")
        check(not bot.to(1), "Автору комментария сообщение не дублируется")

    print("\n=== Подтверждение, возврат, стоимость ===")
    async with SessionMaker() as session:
        w2 = {"electric": await session.get(Category, ids["electric"]),
              "master": await session.get(type(w["admin"]), ids["master"])}
        staff = await session.get(type(w["admin"]), ids["staff"])
        opera = await session.get(type(w["opera"]), ids["opera"])
        done = await add_request(session, w2, opera, staff, status=Status.DONE,
                                 cost=300, cost_status=CostStatus.PENDING)
        await session.commit()
        done_id = done.id

    async with SessionMaker() as session:
        req = await load_request(session, done_id)
        admin = await session.get(type(w["admin"]), ids["admin"])
        chief = await session.get(type(w["admin"]), ids["chief"])
        master = await session.get(type(w["admin"]), ids["master"])
        acts = request_ops.available_actions(req, admin)
        check({"confirm", "return", "cost_ok", "cost_no", "edit_cost"} <= acts,
              f"Для выполненной заявки доступны подтверждение и стоимость: {sorted(acts)}")
        check(request_ops.available_actions(req, chief) == set(), "Руководителю действий нет")
        check(await refused(request_ops.approve_cost(session, bot, req, chief)),
              "Руководитель не согласует стоимость")
        check(await refused(request_ops.reject_cost(session, bot, req, admin, "")),
              "Отклонение стоимости без причины не принимается")
        check(await refused(request_ops.confirm(session, bot, req, master)),
              "Исполнитель не подтверждает свою заявку")
        check(await refused(request_ops.return_for_rework(session, bot, req, admin, "ок")),
              "Возврат с причиной короче 3 символов не принимается")
        bot.clear()
        await request_ops.approve_cost(session, bot, req, admin)
        await session.commit()
        check(req.cost_status == CostStatus.APPROVED, "Стоимость согласована")
        check(any("согласована" in t for t in bot.to(900)), "Мастер уведомлён о согласовании")
        check(await refused(request_ops.approve_cost(session, bot, req, admin)),
              "Повторное согласование — понятный отказ")
        check(await refused(request_ops.edit_cost(session, bot, req, admin, -5, 0, "ошибка")),
              "Отрицательная стоимость не принимается")
        await request_ops.edit_cost(session, bot, req, admin, 200, 50, "Скидка по договору")
        await session.commit()
        check(float(req.total_cost) == 250, "Стоимость изменена админом")
        check("Было: работы" in (await last_event(session, req)).details,
              "Первоначальные суммы в истории")
        await request_ops.return_for_rework(session, bot, req, admin, "Не убран мусор")
        await session.commit()
        check(req.status == Status.RETURNED, "Возвращена на доработку")
        check(await refused(request_ops.confirm(session, bot, req, admin)),
              "Подтвердить можно только выполненную заявку")

    print("\n=== Отмена ===")
    async with SessionMaker() as session:
        req = await load_request(session, ids["req"])
        admin = await session.get(type(w["admin"]), ids["admin"])
        check(await refused(request_ops.cancel(session, bot, req, admin, "")),
              "Отмена без причины не принимается")
        history.SOURCE.set("панель")
        try:
            await request_ops.cancel(session, bot, req, admin, "Дубль заявки")
        finally:
            history.SOURCE.set(None)
        await session.commit()
        check(req.status == Status.CANCELLED, "Заявка отменена")
        check((await last_event(session, req)).details.endswith("· через панель"),
              "Действие из панели помечено в истории")
        check(await refused(request_ops.assign(
            session, bot, req, await session.get(type(w["admin"]), ids["master"]), admin)),
              "С отменённой заявкой действия запрещены")
        check(request_ops.available_actions(req, admin) == set(),
              "У отменённой заявки нет действий")


async def main() -> None:
    await init_db()
    bot = FakeBot()
    await request_ops_checks(bot)
    print("\n🎉 Слой действий работает.")


if __name__ == "__main__":
    asyncio.run(main())
