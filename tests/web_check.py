"""Панель: вход по коду, области видимости, защита форм правки.

Запуск: .venv/bin/python tests/web_check.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BOT_TOKEN"] = "test:token"
os.environ["DB_URL"] = "sqlite+aiosqlite:///" + os.path.join(tempfile.mkdtemp(), "w.db")
os.environ["WEB_SECRET"] = "test-secret"

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from sqlalchemy import select  # noqa: E402

from uccp_bot.db.base import SessionMaker, init_db  # noqa: E402
from uccp_bot.db.models import (  # noqa: E402
    Brand, Category, CostStatus, Outlet, Priority, Request, RoleCode, Status,
    User, UserScope, UserStatus,
)
from uccp_bot.db.seed import seed  # noqa: E402
from uccp_bot.services import history  # noqa: E402
from uccp_bot.services.numbering import new_external_id, next_request_number  # noqa: E402
from uccp_bot.utils import utcnow  # noqa: E402
from uccp_bot.web import auth  # noqa: E402
from uccp_bot.web.app import build_app  # noqa: E402
from uccp_bot.utils import utc_from_local  # noqa: E402
from uccp_bot.db.models import RequestStatusHistory  # noqa: E402

from fixtures import FakeBot  # noqa: E402


def check(condition: bool, label: str) -> None:
    print(("✅ " if condition else "❌ ") + label)
    if not condition:
        raise AssertionError(label)


def csrf(client: TestClient) -> str:
    """Токен формы для текущей сессии — тот же, что панель кладёт в формы."""
    cookie = client.session.cookie_jar.filter_cookies(client.make_url("/")).get(auth.COOKIE_NAME)
    return auth.csrf_for(cookie.value if cookie else "")


async def login(client: TestClient, user_id: int) -> None:
    code = auth.issue_code(user_id)
    resp = await client.post("/login", data={"code": code}, allow_redirects=False)
    assert resp.status in (302, 303), resp.status


async def main() -> None:
    await init_db()
    async with SessionMaker() as session:
        await seed(session)
        moose = await session.scalar(select(Brand).where(Brand.name == "Moose Café"))
        hd = await session.scalar(select(Brand).where(Brand.name == "Hotdogger"))
        opera = await session.scalar(select(Outlet).where(Outlet.name == "Moose Opera"))
        airport = await session.scalar(select(Outlet).where(Outlet.name == "Hotdogger Аэропорт"))
        electric = await session.scalar(select(Category).where(Category.code == "electric"))
        master = await session.scalar(select(User).where(User.full_name == "Назрулло"))
        master.tg_id = 900
        await session.flush()

        def make(tg, name, role, brand=None, outlet=None, status=UserStatus.ACTIVE):
            u = User(tg_id=tg, full_name=name, last_name=name, role_code=role,
                     brand_id=brand.id if brand else None,
                     outlet_id=outlet.id if outlet else None,
                     status=status, is_registered=True, position_text="должность")
            u.sync_flags()
            session.add(u)
            return u

        admin = make(1, "Главный админ", RoleCode.ADMIN)
        chief = make(2, "Главный опердир", RoleCode.OPS_CHIEF)
        ops_hd = make(3, "Опердир Hotdogger", RoleCode.OPS_DIRECTOR, hd)
        staff = make(4, "Сотрудник точки", RoleCode.STAFF, moose, opera)
        blocked = make(5, "Отключённый", RoleCode.ADMIN, status=UserStatus.BLOCKED)
        master2 = make(901, "Второй мастер", RoleCode.EXECUTOR)
        await session.flush()

        async def add(outlet, author, cost=None, status=Status.CLOSED):
            req = Request(
                number=await next_request_number(session), external_id=new_external_id(),
                author_id=author.id, brand_id=outlet.brand_id, outlet_id=outlet.id,
                category_id=electric.id, executor_id=master.id,
                description=f"Заявка по точке {outlet.name}",
                priority=Priority.HIGH, status=status,
                due_at=utcnow() + dt.timedelta(hours=4),
                done_at=utcnow(), closed_at=utcnow(),
                work_cost=cost, material_cost=0,
                cost_status=CostStatus.APPROVED if cost else CostStatus.NONE,
            )
            session.add(req)
            await session.flush()
            await history.log(session, req, "created", user=author,
                              new_status=Status.NEW)
            return req

        r_moose = await add(opera, staff, 250)
        r_hd = await add(airport, staff, 400)
        r_open = await add(opera, staff, status=Status.NEW)
        await session.commit()
        moose_no, hd_no = r_moose.number, r_hd.number
        ids = {"moose": r_moose.id, "hd": r_hd.id, "open": r_open.id}

    bot = FakeBot()
    app = build_app(SessionMaker, bot)
    async with TestClient(TestServer(app)) as client:
        print("=== Вход ===")
        resp = await client.get("/", allow_redirects=False)
        check(resp.status == 302, "Без входа панель закрыта (редирект на /login)")

        resp = await client.post("/login", data={"code": "000000"})
        check(resp.status == 401, "Неверный код не пускает")

        code = auth.issue_code(admin.id)
        resp = await client.post("/login", data={"code": code}, allow_redirects=False)
        check(resp.status == 302, "Правильный код пускает")
        resp = await client.post("/login", data={"code": code})
        check(resp.status == 401, "Код одноразовый — повторно не работает")

        print("\n=== Главный администратор видит всё ===")
        text = await (await client.get("/requests")).text()
        check(moose_no in text and hd_no in text, "В списке обе заявки")
        check("Сводка" in text and "Отчёты" in text, "Меню панели на месте")

        card = await (await client.get(f"/requests/{ids['moose']}")).text()
        check(moose_no in card, "Карточка заявки открывается")
        check("История действий" in card and "создал заявку" in card,
              "В карточке есть журнал действий")
        check("250" in card and "согласована" in card,
              "Видны стоимость и статус согласования")

        print("\n=== Правка: защита форм ===")
        action_url = f"/requests/{ids['moose']}/do/comment"
        resp = await client.post(action_url, data={"text": "без токена"}, allow_redirects=False)
        check(resp.status == 403, "POST без CSRF-токена отклоняется")
        resp = await client.post(action_url, data={"text": "x", "csrf": "чужой"},
                                 allow_redirects=False)
        check(resp.status == 403, "POST с чужим CSRF-токеном отклоняется")
        resp = await client.post(f"/requests/{ids['moose']}/do/nonexistent",
                                 data={"csrf": csrf(client)}, allow_redirects=False)
        check(resp.status == 404, "Неизвестное действие — 404")
        for method, path in (("post", "/requests"), ("delete", f"/requests/{ids['moose']}")):
            resp = await getattr(client, method)(path)
            check(resp.status in (404, 405), f"{method.upper()} {path} — такого маршрута нет")

        await client.get("/logout")
        await login(client, chief.id)
        resp = await client.post(action_url, data={"text": "руководитель", "csrf": csrf(client)},
                                 allow_redirects=False)
        check(resp.status == 403, "Не-админ с верным токеном не может ничего изменить")
        await client.get("/logout")
        await login(client, admin.id)

        print("\n=== Действия с заявкой из панели ===")
        open_url = f"/requests/{ids['open']}"
        card = await (await client.get(open_url)).text()
        check("Назначить исполнителя" in card and "Изменить срок" in card
              and "Отменить заявку" in card, "В карточке открытой заявки есть действия")
        check('name="csrf"' in card, "Формы защищены токеном")
        closed_card = await (await client.get(f"/requests/{ids['moose']}")).text()
        check("Назначить исполнителя" not in closed_card, "У закрытой заявки действий над заявкой нет")

        async def act(name, **fields):
            resp = await client.post(f"{open_url}/do/{name}", data={"csrf": csrf(client), **fields})
            return await resp.text()

        bot.clear()
        page = await act("assign", executor_id=str(master2.id))
        async with SessionMaker() as session:
            req = await session.get(Request, ids["open"])
            last = await session.scalar(select(RequestStatusHistory).where(
                RequestStatusHistory.request_id == req.id).order_by(RequestStatusHistory.id.desc()))
        check(req.executor_id == master2.id, "Исполнитель назначен из панели")
        check("Исполнитель назначен" in page, "После действия показано сообщение об итоге")
        check(any("Новая заявка" in t for t in bot.to(901)), "Мастер получил заявку в Telegram")
        check(last.details.endswith("через панель"), "В журнале пометка «через панель»")
        page2 = await (await client.get(open_url)).text()
        check("Исполнитель назначен" not in page2, "Сообщение об итоге показывается один раз")

        due_local = (dt.datetime.now() + dt.timedelta(days=3)).replace(hour=15, minute=0,
                                                                     second=0, microsecond=0)
        await act("change_due", due=due_local.strftime("%Y-%m-%dT%H:%M"))
        async with SessionMaker() as session:
            req = await session.get(Request, ids["open"])
        check(req.due_at == utc_from_local(due_local), "Срок изменён из панели (время — местное)")
        page = await act("change_due", due="вчера")
        check("Укажите дату и время" in page, "Неверная дата — понятная ошибка")

        await act("comment", text="Проверьте щиток в зале")
        check(any("Проверьте щиток" in t for t in bot.to(4)), "Комментарий дошёл до инициатора")

        await act("cancel", reason="Дубль заявки")
        async with SessionMaker() as session:
            req = await session.get(Request, ids["open"])
        check(req.status == Status.CANCELLED, "Заявка отменена из панели")
        page = await act("assign", executor_id=str(master2.id))
        check("недоступно" in page, "Действие над отменённой заявкой — понятный отказ")

        await client.get("/logout")
        await login(client, chief.id)
        card = await (await client.get(f"/requests/{ids['moose']}")).text()
        check("/do/" not in card, "Руководителю формы действий не показываются")
        await client.get("/logout")
        await login(client, admin.id)

        print("\n=== Перебор кода входа ===")
        await client.get("/logout")
        auth.GLOBAL_FAIL_LIMIT = 5
        for i in range(5):
            await client.post("/login", data={"code": "000000"},
                              headers={"X-Forwarded-For": f"10.0.0.{i}"})
        good = auth.issue_code(admin.id)
        resp = await client.post("/login", data={"code": good},
                                 headers={"X-Forwarded-For": "10.9.9.9"}, allow_redirects=False)
        check(resp.status == 401, "Смена X-Forwarded-For не обходит общий лимит ошибок")
        auth.GLOBAL_FAIL_LIMIT = 30
        auth._global_failures.clear()
        await login(client, admin.id)

        print("\n=== Область видимости ===")
        await client.get("/logout")
        await login(client, chief.id)
        text = await (await client.get("/requests")).text()
        check(moose_no in text and hd_no in text,
              "Главный операционный директор видит оба бренда")
        resp = await client.get("/users")
        check(resp.status == 403, "Пользователи — только главному администратору")

        await client.get("/logout")
        await login(client, ops_hd.id)
        text = await (await client.get("/requests")).text()
        check(hd_no in text and moose_no not in text,
              "Операционный директор видит только свой бренд")
        resp = await client.get(f"/requests/{ids['moose']}")
        check(resp.status == 404, "Чужая заявка по прямой ссылке недоступна")

        async with SessionMaker() as session:
            session.add(UserScope(user_id=ops_hd.id, outlet_id=opera.id,
                                  brand_id=opera.brand_id, created_by_id=admin.id))
            await session.commit()
        text = await (await client.get("/requests")).text()
        check(moose_no in text, "После закрепления объекта заявка появляется")
        resp = await client.get(f"/requests/{ids['moose']}")
        check(resp.status == 200, "И карточка становится доступна")

        await client.get("/logout")
        await login(client, staff.id)
        resp = await client.get("/reports")
        check(resp.status == 403, "Сотруднику точки отчёты закрыты")
        text = await (await client.get("/requests")).text()
        check(moose_no in text and hd_no in text, "Автор видит свои заявки")

        print("\n=== Отчёты ===")
        await client.get("/logout")
        await login(client, admin.id)
        text = await (await client.get("/reports?period=all")).text()
        check("Эффективность исполнителей" in text, "Блок эффективности есть")
        check("Назрулло" in text, "Исполнитель в таблице эффективности")
        check("По брендам" in text and "По точкам" in text, "Разрезы отчёта на месте")

        print("\n=== Отключённый пользователь ===")
        await client.get("/logout")
        await login(client, blocked.id)
        resp = await client.get("/", allow_redirects=False)
        check(resp.status == 302, "Заблокированного в панель не пускает")

        print("\n=== Служебное ===")
        resp = await client.get("/healthz")
        check(resp.status == 200, "Проверка живости отвечает")

    print("\n🎉 Панель просмотра работает и соблюдает права доступа.")


if __name__ == "__main__":
    asyncio.run(main())
