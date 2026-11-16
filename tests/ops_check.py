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
from uccp_bot.db.models import (  # noqa: E402
    Brand, ExecutorAssignment, Outlet, RoleCode, User, UserScope, UserStatus,
)
from uccp_bot.services import catalog_ops, history, request_ops, user_ops  # noqa: E402
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
        check(await refused(request_ops.add_comment(session, bot, req, admin, "Поздно")),
              "Комментарий к закрытой заявке не принимается")


async def user_ops_checks(bot: FakeBot) -> None:
    async with SessionMaker() as session:
        admin = await session.scalar(select(User).where(User.full_name == "Главный админ"))
        chief = await session.scalar(select(User).where(User.full_name == "Главный опердир"))
        pending = await session.scalar(select(User).where(User.full_name == "Новичок"))
        ops_hd = await session.scalar(select(User).where(User.full_name == "Опердир Hotdogger"))
        electric = await session.scalar(select(Category).where(Category.code == "electric"))
        moose = await session.scalar(select(Brand).where(Brand.name == "Moose Café"))
        opera = await session.scalar(select(Outlet).where(Outlet.name == "Moose Opera"))

        print("\n=== Регистрации ===")
        check(await refused(user_ops.approve(
            session, bot, pending, chief, RoleCode.STAFF, moose.id, opera.id, [])),
              "Руководитель не одобряет регистрации")
        check(await refused(user_ops.approve(
            session, bot, pending, admin, RoleCode.EXECUTOR, None, None, [])),
              "Мастер без направлений не одобряется")
        check(await refused(user_ops.approve(
            session, bot, pending, admin, RoleCode.OUTLET_ADMIN, moose.id, None, [])),
              "Админ точки без точки не одобряется (как в боте)")
        check(await refused(user_ops.approve(
            session, bot, pending, admin, RoleCode.OPS_DIRECTOR, None, None, [])),
              "Опердиректор без бренда не одобряется (как в боте)")
        check(await refused(user_ops.approve(
            session, bot, pending, admin, RoleCode.EXECUTOR, moose.id, None, [999999])),
              "Несуществующее направление — понятный отказ, без падения")
        bot.clear()
        summary = await user_ops.approve(
            session, bot, pending, admin, RoleCode.EXECUTOR, moose.id, None, [electric.id])
        await session.commit()
        check(pending.status == UserStatus.ACTIVE and pending.role_code == RoleCode.EXECUTOR,
              "Регистрация одобрена с ролью мастера")
        rows = (await session.scalars(select(ExecutorAssignment).where(
            ExecutorAssignment.user_id == pending.id))).all()
        check([(r.category_id, r.brand_id) for r in rows] == [(electric.id, moose.id)],
              "Направление мастера привязано к бренду")
        check(bot.to(7) and "Электрика" in summary, "Человек уведомлён, итог содержит направление")
        check(await refused(user_ops.approve(
            session, bot, pending, admin, RoleCode.STAFF, moose.id, opera.id, [])),
              "Повторное одобрение — понятный отказ")

        print("\n=== Пользователь ===")
        check(await refused(user_ops.set_status(session, bot, admin, admin, UserStatus.BLOCKED)),
              "Нельзя заблокировать самого себя")
        await user_ops.set_status(session, bot, pending, admin, UserStatus.BLOCKED)
        check(pending.status == UserStatus.BLOCKED and not pending.is_active, "Пользователь заблокирован")
        await user_ops.set_status(session, bot, pending, admin, UserStatus.ACTIVE)
        warning = await user_ops.set_role(session, bot, pending, admin, RoleCode.STAFF)
        check(pending.role_code == RoleCode.STAFF and warning == "stale_directions",
              "Смена роли с мастера предупреждает об оставшихся направлениях")
        await user_ops.set_outlet(session, pending, admin, opera.id)
        check(pending.outlet_id == opera.id and pending.brand_id == opera.brand_id,
              "Точка задаёт и бренд")
        await user_ops.set_brand(session, pending, admin, ops_hd.brand_id)
        check(pending.outlet_id is None, "Смена бренда снимает точку чужого бренда")
        await user_ops.set_phone(session, pending, admin, "-")
        check(pending.phone is None, "Телефон очищается через «-»")
        await user_ops.set_fixed_payment(session, bot, pending, admin, True)
        check(pending.fixed_payment, "Фиксированная оплата включена")
        check(await refused(user_ops.set_phone(session, pending, chief, "+992900000000")),
              "Руководитель не меняет пользователей")

        print("\n=== Области видимости и направления ===")
        bot.clear()
        scope_row = await user_ops.add_scope(session, bot, ops_hd, admin, outlet_id=opera.id)
        check(scope_row.brand_id == opera.brand_id and bot.to(3), "Объект закреплён, человек уведомлён")
        check(await refused(user_ops.add_scope(session, bot, ops_hd, admin, outlet_id=opera.id)),
              "Повторное закрепление того же объекта — отказ")
        await user_ops.remove_scope(session, ops_hd, admin, scope_row.id)
        check(await session.get(UserScope, scope_row.id) is None, "Объект откреплён")
        master2 = await session.scalar(select(User).where(User.full_name == "Второй мастер"))
        row = await user_ops.add_direction(session, master2, admin, electric.id, outlet_id=opera.id)
        check(row.brand_id == opera.brand_id, "Направление по точке привязано к её бренду")
        check(await refused(user_ops.add_direction(session, ops_hd, admin, electric.id)),
              "Направление можно дать только мастеру")
        await user_ops.remove_direction(session, master2, admin, row.id)
        check(await session.get(ExecutorAssignment, row.id) is None, "Направление снято")

        print("\n=== Отклонение регистрации ===")
        newcomer = User(tg_id=8, full_name="Ещё один", last_name="Ещё один",
                        role_code=RoleCode.STAFF, status=UserStatus.PENDING)
        session.add(newcomer)
        await session.flush()
        check(await refused(user_ops.reject(session, bot, newcomer, admin, "")),
              "Отклонение без причины не принимается")
        await user_ops.reject(session, bot, newcomer, admin, "Нет такого сотрудника")
        check(newcomer.status == UserStatus.REJECTED and bot.to(8), "Регистрация отклонена, человек уведомлён")
        check(await refused(user_ops.approve(
            session, bot, newcomer, admin, RoleCode.STAFF, moose.id, opera.id, [])),
              "Отклонённую заявку одобрить нельзя (как в боте)")
        await session.commit()


async def catalog_ops_checks() -> None:
    async with SessionMaker() as session:
        admin = await session.scalar(select(User).where(User.full_name == "Главный админ"))
        chief = await session.scalar(select(User).where(User.full_name == "Главный опердир"))
        moose = await session.scalar(select(Brand).where(Brand.name == "Moose Café"))
        hd = await session.scalar(select(Brand).where(Brand.name == "Hotdogger"))

        print("\n=== Справочники ===")
        check(await refused(catalog_ops.add_outlet(session, chief, moose.id, "Moose Новая")),
              "Руководитель не меняет справочники")
        outlet = await catalog_ops.add_outlet(session, admin, moose.id, "Moose Новая")
        check(outlet.id and outlet.brand_id == moose.id, "Точка добавлена")
        check(await refused(catalog_ops.add_outlet(session, admin, moose.id, "Moose Новая")),
              "Дубль точки в бренде — отказ")
        await catalog_ops.rename_outlet(session, admin, outlet, "Moose Центр")
        await catalog_ops.set_outlet_brand(session, admin, outlet, hd.id)
        await catalog_ops.toggle_outlet(session, admin, outlet)
        check(outlet.name == "Moose Центр" and outlet.brand_id == hd.id and not outlet.is_active,
              "Точка переименована, перенесена и выключена")
        check(await refused(catalog_ops.rename_outlet(session, admin, outlet, " ")),
              "Пустое название не принимается")

        cat = await catalog_ops.add_category(session, admin, "Вентиляция", "-")
        check(cat.emoji == "📦" and cat.code.startswith("custom_"), "Категория добавлена с эмодзи по умолчанию")
        await catalog_ops.rename_category(session, admin, cat, "Вентиляция и кондиционеры")
        await catalog_ops.toggle_category(session, admin, cat, "photo_required")
        check(cat.name == "Вентиляция и кондиционеры" and cat.photo_required, "Категория изменена")

        ex = await catalog_ops.add_executor(session, admin, "Сантехник Али", "-", "@ali_fix")
        check(ex.is_executor and ex.username == "ali_fix" and ex.status == UserStatus.ACTIVE,
              "Мастер заведён администратором")
        check(await refused(catalog_ops.add_executor(session, admin, "Двойник", "-", "ALI_FIX")),
              "Дубль username — отказ")
        await session.commit()


async def main() -> None:
    await init_db()
    bot = FakeBot()
    await request_ops_checks(bot)
    await user_ops_checks(bot)
    await catalog_ops_checks()
    print("\n🎉 Слой действий работает.")


if __name__ == "__main__":
    asyncio.run(main())
