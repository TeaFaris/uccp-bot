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
from uccp_bot.db.models import Attachment, ExecutorAssignment, RequestStatusHistory  # noqa: E402

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
        newbie = make(6, "Новичок Регистрация", RoleCode.STAFF, status=UserStatus.PENDING)
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
        photo = Attachment(request_id=r_moose.id, file_id="PH1", media_type="photo", stage="after")
        big = Attachment(request_id=r_moose.id, file_id="BIG", media_type="video", stage="after")
        doc = Attachment(request_id=r_moose.id, file_id="DOC1", media_type="document", stage="before")
        session.add_all([photo, big, doc])
        r_done = await add(opera, staff, cost=300, status=Status.DONE)
        r_done.cost_status = CostStatus.PENDING
        await session.commit()
        media = {"photo": photo.id, "big": big.id, "doc": doc.id}
        done_id = r_done.id
        moose_no, hd_no = r_moose.number, r_hd.number
        ids = {"moose": r_moose.id, "hd": r_hd.id, "open": r_open.id}

    ids["done"] = done_id
    bot = FakeBot()
    bot.files["PH1"] = (b"\xff\xd8\xff-jpeg-bytes", 18)
    bot.files["DOC1"] = (b"<script>alert(1)</script>", 25, "documents/evil.html")
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

        print("\n=== Фото и видео ===")
        card = await (await client.get(f"/requests/{ids['moose']}")).text()
        check(f'src="/media/{media["photo"]}"' in card, "Фото показано превью в карточке")
        check(f'href="/media/{media["big"]}"' in card, "Видео открывается по ссылке")
        resp = await client.get(f"/media/{media['photo']}")
        body = await resp.read()
        check(resp.status == 200 and body == b"\xff\xd8\xff-jpeg-bytes", "Фото отдаётся из Telegram")
        check(resp.headers.get("Content-Type", "").startswith("image/jpeg"), "Тип файла — картинка")
        check("private" in resp.headers.get("Cache-Control", ""), "Кэш только в браузере")
        await client.get(f"/media/{media['photo']}")
        check(bot.get_file_calls == 1, "Повторный просмотр не тянет файл из Telegram заново")
        resp = await client.get(f"/media/{media['big']}")
        check(resp.status == 404 and "Telegram" in await resp.text(),
              "Файл больше 20 МБ — «смотреть в Telegram»")
        resp = await client.get("/media/999999")
        check(resp.status == 404, "Несуществующий файл — 404")
        resp = await client.get(f"/media/{media['doc']}")
        check(resp.headers.get("Content-Type", "").startswith("application/octet-stream")
              and "attachment" in resp.headers.get("Content-Disposition", ""),
              "Документ (в т.ч. .html) отдаётся только на скачивание, не открывается в панели")
        check(resp.headers.get("X-Content-Type-Options") == "nosniff"
              and "sandbox" in resp.headers.get("Content-Security-Policy", ""),
              "Файлы отдаются с nosniff и песочницей CSP")

        print("\n=== Экранирование ===")
        resp = await client.get('/requests?search=" autofocus onfocus="alert(1)')
        text = await resp.text()
        check('onfocus="alert(1)"' not in text, "Кавычки из поиска экранируются в атрибуте")
        resp = await client.get('/requests?search=x&page=1&z"><b>=1')
        check('"><b>' not in await resp.text(), "Ссылки постраничного вывода экранируются")
        csp = resp.headers.get("Content-Security-Policy", "")
        check("default-src 'none'" in csp and "frame-ancestors 'none'" in csp,
              "Страницы панели запрещают скрипты и встраивание (CSP)")

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
        page = await act("comment", text="Комментарий к отменённой")
        check("недоступно" in page, "Комментарий к отменённой заявке — понятный отказ")

        print("\n=== Двойная отправка формы ===")
        bot.clear()
        bot.delay = 0.2
        done_url = f"/requests/{ids['done']}/do/confirm"
        token = csrf(client)
        r1, r2 = await asyncio.gather(
            client.post(done_url, data={"csrf": token}),
            client.post(done_url, data={"csrf": token}),
        )
        bot.delay = 0.0
        async with SessionMaker() as session:
            closes = (await session.scalars(select(RequestStatusHistory.action).where(
                RequestStatusHistory.request_id == ids["done"],
                RequestStatusHistory.action == "closed"))).all()
        check(len(closes) == 1, f"Двойной клик закрывает заявку один раз (закрытий: {len(closes)})")

        await client.get("/logout")
        await login(client, chief.id)
        card = await (await client.get(f"/requests/{ids['moose']}")).text()
        check("/do/" not in card, "Руководителю формы действий не показываются")
        await client.get("/logout")
        await login(client, admin.id)

        print("\n=== Регистрации и пользователи ===")
        text = await (await client.get("/users?status=pending")).text()
        check("Новичок Регистрация" in text and f'href="/users/{newbie.id}"' in text,
              "Заявки на регистрацию видны списком со ссылками")
        check("Сотрудник точки" not in text, "Фильтр по статусу работает")
        card = await (await client.get(f"/users/{newbie.id}")).text()
        check("Одобрить регистрацию" in card and "Отклонить" in card, "Карточка заявки на регистрацию")

        async def user_act(uid, name, **fields):
            data = [("csrf", csrf(client))]
            for key, value in fields.items():
                for item in (value if isinstance(value, list) else [value]):
                    data.append((key, str(item)))
            resp = await client.post(f"/users/{uid}/do/{name}", data=data)
            return await resp.text()

        bot.clear()
        page = await user_act(newbie.id, "approve", role=RoleCode.EXECUTOR, brand_id=moose.id,
                              outlet_id="", category_ids=[electric.id])
        async with SessionMaker() as session:
            u = await session.get(User, newbie.id)
            dirs = (await session.scalars(select(ExecutorAssignment).where(
                ExecutorAssignment.user_id == newbie.id))).all()
        check(u.status == UserStatus.ACTIVE and u.role_code == RoleCode.EXECUTOR,
              "Регистрация одобрена из панели")
        check([d.category_id for d in dirs] == [electric.id], "Направления мастера сохранены")
        check(bot.to(6) and "подтверждена" in page, "Человек уведомлён, итог показан")

        await user_act(newbie.id, "role", role=RoleCode.STAFF)
        page = await user_act(newbie.id, "outlet", outlet_id=opera.id)
        await user_act(newbie.id, "phone", phone='"><b>x')
        card = await (await client.get(f"/users/{newbie.id}")).text()
        check('"><b>x' not in card, "Телефон с кавычками экранируется в поле формы")
        await user_act(newbie.id, "phone", phone="+992 900 00 00 01")
        async with SessionMaker() as session:
            u = await session.get(User, newbie.id)
        check(u.role_code == RoleCode.STAFF and u.outlet_id == opera.id and "900" in (u.phone or ""),
              "Роль, точка и телефон меняются из панели")
        page = await user_act(admin.id, "status", status=UserStatus.BLOCKED)
        check("самого себя" in page, "Себя заблокировать нельзя — понятная ошибка")
        await user_act(newbie.id, "status", status=UserStatus.BLOCKED)
        async with SessionMaker() as session:
            check((await session.get(User, newbie.id)).status == UserStatus.BLOCKED,
                  "Пользователь заблокирован из панели")

        card = await (await client.get(f"/users/{ops_hd.id}")).text()
        check("Закреплённые объекты" in card, "Опердиректору можно закрепить объекты")
        await user_act(ops_hd.id, "scope_add", target=f"o{opera.id}")
        async with SessionMaker() as session:
            row = await session.scalar(select(UserScope).where(UserScope.user_id == ops_hd.id))
        check(row is not None and row.outlet_id == opera.id, "Объект закреплён из панели")
        await user_act(ops_hd.id, "scope_del", scope_id=row.id)
        async with SessionMaker() as session:
            check(await session.get(UserScope, row.id) is None, "Объект откреплён из панели")

        card = await (await client.get(f"/users/{master2.id}")).text()
        check("Направления мастера" in card and "Оплата" in card, "У мастера — направления и оплата")
        await user_act(master2.id, "dir_add", category_id=electric.id, target=f"b{moose.id}")
        await user_act(master2.id, "payment", fixed="1")
        async with SessionMaker() as session:
            d = await session.scalar(select(ExecutorAssignment).where(
                ExecutorAssignment.user_id == master2.id))
            m2 = await session.get(User, master2.id)
        check(d is not None and d.brand_id == moose.id and m2.fixed_payment,
              "Направление и оплата мастера меняются из панели")
        await user_act(master2.id, "dir_del", assignment_id=d.id)

        await client.get("/logout")
        await login(client, chief.id)
        resp = await client.get(f"/users/{newbie.id}")
        check(resp.status == 403, "Карточка пользователя — только главному администратору")
        await client.get("/logout")
        await login(client, admin.id)

        print("\n=== Справочники ===")
        text = await (await client.get("/catalog")).text()
        check("Точки" in text and "Категории" in text and "Мастера" in text,
              "Страница справочников открывается")
        check('href="/catalog"' in text, "Пункт «Справочники» в меню админа")

        async def cat_act(act_name, **fields):
            resp = await client.post(f"/catalog/do/{act_name}", data={"csrf": csrf(client), **fields})
            return await resp.text()

        await cat_act("outlet_add", brand_id=moose.id, name="Moose Панель")
        async with SessionMaker() as session:
            o = await session.scalar(select(Outlet).where(Outlet.name == "Moose Панель"))
        check(o is not None and o.brand_id == moose.id, "Точка добавлена из панели")
        page = await cat_act("outlet_add", brand_id=moose.id, name="Moose Панель")
        check("уже есть" in page, "Дубль точки — понятная ошибка")
        await cat_act("outlet_rename", outlet_id=o.id, name="Moose Панель-2")
        await cat_act("outlet_brand", outlet_id=o.id, brand_id=hd.id)
        await cat_act("outlet_toggle", outlet_id=o.id)
        async with SessionMaker() as session:
            o = await session.get(Outlet, o.id)
        check(o.name == "Moose Панель-2" and o.brand_id == hd.id and not o.is_active,
              "Точка переименована, перенесена и выключена из панели")

        await cat_act("cat_add", name="Лифты", emoji="🛗")
        async with SessionMaker() as session:
            c = await session.scalar(select(Category).where(Category.name == "Лифты"))
        check(c is not None and c.emoji == "🛗", "Категория добавлена из панели")
        await cat_act("cat_rename", category_id=c.id, name="Лифты и подъёмники")
        await cat_act("cat_toggle", category_id=c.id, field="photo_required")
        async with SessionMaker() as session:
            c = await session.get(Category, c.id)
        check(c.name == "Лифты и подъёмники" and c.photo_required, "Категория изменена из панели")

        await cat_act("exec_add", full_name="Мастер Из Панели", phone="-", username="panel_master")
        async with SessionMaker() as session:
            m = await session.scalar(select(User).where(User.username == "panel_master"))
        check(m is not None and m.is_executor, "Мастер заведён из панели")
        text = await (await client.get("/catalog")).text()
        check(f'href="/users/{m.id}"' in text, "Мастер в списке со ссылкой на карточку")

        await client.get("/logout")
        await login(client, chief.id)
        resp = await client.get("/catalog")
        check(resp.status == 403, "Справочники — только главному администратору")
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
        resp = await client.get(f"/media/{media['photo']}")
        check(resp.status == 404, "Фото чужой заявки по прямой ссылке недоступно")

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
