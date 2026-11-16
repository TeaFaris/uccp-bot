"""Управление в панели: регистрации и пользователи, справочники.

Только главный администратор. Все изменения — через общий слой
(`services/user_ops.py`, `services/catalog_ops.py`), как в боте.
"""
from __future__ import annotations

import re
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from aiohttp import web
from multidict import MultiDictProxy
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    Brand, Category, ExecutorAssignment, Outlet, RoleCode, User, UserScope, UserStatus,
)
from ..services import catalog_ops, user_ops
from ..services.ops import ActionError, entity_lock
from ..utils import fmt_dt
from . import auth, views
from .views import esc
from .app import _admin_post, _back, _bot, _guard, _maker

routes = web.RouteTableDef()

STATUS_KIND = {UserStatus.ACTIVE: "green", UserStatus.PENDING: "amber",
               UserStatus.BLOCKED: "red", UserStatus.DISABLED: "red",
               UserStatus.REJECTED: "red"}
USER_TABS = (
    ("", "Все"),
    (UserStatus.PENDING, "Ожидают подтверждения"),
    (UserStatus.ACTIVE, "Активные"),
    (UserStatus.BLOCKED, "Заблокированные"),
    (UserStatus.REJECTED, "Отклонённые"),
)
SCOPE_ROLES = (RoleCode.OPS_DIRECTOR, RoleCode.OUTLET_ADMIN)   # как в боте


async def _manager(request: web.Request) -> User:
    user = await _guard(request)
    if not user.can_manage_users:
        raise web.HTTPForbidden(text="Раздел доступен только главному администратору")
    return user


def _opt_int(raw) -> Optional[int]:
    try:
        return int(raw) if str(raw or "").strip() else None
    except (TypeError, ValueError):
        raise ActionError("Неверное значение в форме — обновите страницу.")


def _target(raw: str) -> Tuple[Optional[int], Optional[int]]:
    """«b12» — бренд, «o5» — точка, пусто — ничего. Возвращает (brand_id, outlet_id)."""
    raw = str(raw or "")
    if raw.startswith("b"):
        return _opt_int(raw[1:]), None
    if raw.startswith("o"):
        return None, _opt_int(raw[1:])
    return None, None


def _plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


async def _catalog(session: AsyncSession):
    brands = list((await session.scalars(select(Brand).order_by(Brand.sort_order))).all())
    outlets = list((await session.scalars(
        select(Outlet).where(Outlet.is_active.is_(True)).order_by(Outlet.brand_id, Outlet.sort_order, Outlet.name)
    )).all())
    categories = list((await session.scalars(
        select(Category).where(Category.is_active.is_(True)).order_by(Category.sort_order)
    )).all())
    return brands, outlets, categories


def _target_options(brands, outlets, empty: Optional[str]) -> List[Tuple[str, str]]:
    names = {b.id: b.name for b in brands}
    opts: List[Tuple[str, str]] = [("", empty)] if empty else []
    opts += [(f"b{b.id}", f"{b.name} — весь бренд") for b in brands]
    opts += [(f"o{o.id}", f"{names.get(o.brand_id, '—')} · {o.name}") for o in outlets]
    return opts


# --------------------------------------------------------------------------- #
# Список пользователей
# --------------------------------------------------------------------------- #
@routes.get("/users")
async def users_page(request: web.Request) -> web.Response:
    user = await _manager(request)
    status = request.query.get("status", "")
    async with _maker(request)() as session:
        stmt = select(User).order_by(User.full_name)
        if status in UserStatus.TITLES:
            stmt = stmt.where(User.status == status)
        rows = list((await session.scalars(stmt)).all())
        pending = await session.scalar(
            select(func.count()).select_from(User).where(User.status == UserStatus.PENDING)
        )
        brands = {b.id: b.name for b in (await session.scalars(select(Brand))).all()}
        outlets = {o.id: o.name for o in (await session.scalars(select(Outlet))).all()}

    tabs = "".join(
        f'<a href="/users{"?status=" + code if code else ""}" class="{"on" if code == status else ""}">'
        f'{esc(title)}{f" ({pending})" if code == UserStatus.PENDING else ""}</a>'
        for code, title in USER_TABS
    )
    table_rows = [
        [
            f'<a href="/users/{u.id}">{esc(u.full_name)}</a>',
            esc(u.role_title),
            views.tag(UserStatus.TITLES.get(u.status, u.status), STATUS_KIND.get(u.status, "")),
            esc(u.position_text or "—"),
            esc(brands.get(u.brand_id, "—")),
            esc(outlets.get(u.outlet_id, "—")),
            esc(u.phone or "—"),
            "да" if u.tg_id else "нет",
        ]
        for u in rows
    ]
    body = (
        "<h1>Пользователи</h1>"
        f'<div class="tabs">{tabs}</div>'
        + views.table(
            ["Ф.И.О.", "Роль", "Статус", "Должность", "Бренд", "Точка", "Телефон", "Бот активирован"],
            table_rows,
        )
    )
    return web.Response(text=views.page("Пользователи", body, user, "users"),
                        content_type="text/html")


# --------------------------------------------------------------------------- #
# Карточка пользователя
# --------------------------------------------------------------------------- #
@routes.get("/users/{uid}")
async def user_card(request: web.Request) -> web.Response:
    user = await _manager(request)
    uid = int(request.match_info["uid"])
    async with _maker(request)() as session:
        target = await session.get(User, uid)
        if target is None:
            raise web.HTTPNotFound(text="Пользователь не найден")
        brands, outlets, categories = await _catalog(session)
        all_outlets = {o.id: o for o in (await session.scalars(select(Outlet))).all()}
        brand_names = {b.id: b.name for b in brands}
        cat_names = {c.id: c.label for c in (await session.scalars(select(Category))).all()}
        scopes = list((await session.scalars(
            select(UserScope).where(UserScope.user_id == uid).order_by(UserScope.id)
        )).all())
        directions = list((await session.scalars(
            select(ExecutorAssignment).where(ExecutorAssignment.user_id == uid)
            .order_by(ExecutorAssignment.id)
        )).all())

    csrf = auth.csrf_token(request)
    url = f"/users/{uid}/do"

    def box(action, title, fields, button, danger=False):
        return views.action_box(f"{url}/{action}", title, fields, button, csrf, danger)

    def place(brand_id, outlet_id) -> str:
        if outlet_id and outlet_id in all_outlets:
            o = all_outlets[outlet_id]
            return f"{brand_names.get(o.brand_id, '—')} · {o.name}"
        if brand_id:
            return f"{brand_names.get(brand_id, '—')} — весь бренд"
        return "все бренды и точки"

    role_opts = [(code, RoleCode.TITLES[code]) for code in RoleCode.ALL]
    brand_opts = [("", "— без бренда —")] + [(b.id, b.name) for b in brands]
    outlet_opts = [("", "— без точки —")] + [
        (o.id, f"{brand_names.get(o.brand_id, '—')} · {o.name}") for o in outlets
    ]

    pairs = [
        ("Статус", views.tag(UserStatus.TITLES.get(target.status, target.status),
                             STATUS_KIND.get(target.status, ""))),
        ("Роль", esc(target.role_title)),
        ("Должность", esc(target.position_text or "—")),
        ("Бренд", esc(brand_names.get(target.brand_id, "—"))),
        ("Точка", esc(all_outlets[target.outlet_id].name if target.outlet_id in all_outlets else "—")),
        ("Телефон", esc(target.phone or "—")),
        ("Telegram", esc(("@" + target.username) if target.username else "—")
         + ("" if target.tg_id else " · бот не активирован")),
        ("Заявка подана", esc(fmt_dt(target.applied_at)) if target.applied_at else ""),
        ("Доступ выдан", esc(fmt_dt(target.approved_at)) if target.approved_at else ""),
        ("Причина отклонения", esc(target.reject_reason or "")),
    ]

    blocks: List[str] = []
    if target.status == UserStatus.PENDING:
        checks = "".join(
            f'<label><input type="checkbox" name="category_ids" value="{c.id}"> {esc(c.label)}</label>'
            for c in categories
        )
        blocks.append(box(
            "approve", "Одобрить регистрацию",
            f'<select name="role">{views.options_html(role_opts, target.role_code)}</select>'
            f'<select name="brand_id">{views.options_html(brand_opts, target.brand_id or "")}</select>'
            f'<select name="outlet_id">{views.options_html(outlet_opts, target.outlet_id or "")}</select>'
            '<span class="muted">Сотруднику и админу точки — точка, опердиректору — бренд, '
            'мастеру — направления (минимум одно).</span>'
            f'<div class="checks">{checks}</div>',
            "✅ Одобрить",
        ))
        blocks.append(box(
            "reject", "Отклонить заявку",
            '<textarea name="reason" required placeholder="Причину увидит пользователь"></textarea>',
            "❌ Отклонить", danger=True,
        ))
    else:
        blocks.append(box("role", "Роль",
                          f'<select name="role">{views.options_html(role_opts, target.role_code)}</select>',
                          "Сохранить роль"))
        status_opts = [(code, UserStatus.TITLES[code]) for code in
                       (UserStatus.ACTIVE, UserStatus.BLOCKED, UserStatus.DISABLED)]
        blocks.append(box("status", "Доступ",
                          f'<select name="status">{views.options_html(status_opts, target.status)}</select>',
                          "Сохранить доступ"))
        blocks.append(box("brand", "Бренд",
                          f'<select name="brand_id">{views.options_html(brand_opts, target.brand_id or "")}</select>',
                          "Сохранить бренд"))
        blocks.append(box("outlet", "Точка",
                          f'<select name="outlet_id">{views.options_html(outlet_opts, target.outlet_id or "")}</select>'
                          '<span class="muted">Точка задаёт и бренд.</span>',
                          "Сохранить точку"))
        blocks.append(box("phone", "Телефон",
                          f'<input name="phone" value="{esc(target.phone or "")}" placeholder="«-» — очистить">',
                          "Сохранить телефон"))
        if target.role_code == RoleCode.EXECUTOR:
            blocks.append(box(
                "payment", "Оплата мастера",
                f'<select name="fixed">{views.options_html([("0", "По каждой заявке"), ("1", "Фиксированная ежемесячная")], "1" if target.fixed_payment else "0")}</select>',
                "Сохранить оплату",
            ))

    extra = ""
    if target.role_code in SCOPE_ROLES and target.status == UserStatus.ACTIVE:
        rows = [
            [esc(place(r.brand_id, r.outlet_id)),
             views.inline_button(f"{url}/scope_del", "снять", csrf, [("scope_id", r.id)])]
            for r in scopes
        ]
        extra += (
            "<h2>Закреплённые объекты</h2>"
            + (views.table(["Объект", ""], rows) if rows else '<p class="muted">Нет</p>')
            + '<div class="actions">' + box(
                "scope_add", "Закрепить объект",
                f'<select name="target">{views.options_html(_target_options(brands, outlets, None))}</select>',
                "🏢 Закрепить") + "</div>"
        )
    if target.role_code == RoleCode.EXECUTOR and target.status == UserStatus.ACTIVE:
        rows = [
            [esc(cat_names.get(d.category_id, "—")), esc(place(d.brand_id, d.outlet_id)),
             views.inline_button(f"{url}/dir_del", "снять", csrf, [("assignment_id", d.id)])]
            for d in directions
        ]
        cat_opts = [(c.id, c.label) for c in categories]
        extra += (
            "<h2>Направления мастера</h2>"
            + (views.table(["Направление", "Где", ""], rows) if rows
               else '<p class="muted">Нет — заявки мастеру не приходят</p>')
            + '<div class="actions">' + box(
                "dir_add", "Добавить направление",
                f'<select name="category_id">{views.options_html(cat_opts)}</select>'
                f'<select name="target">{views.options_html(_target_options(brands, outlets, "Все бренды и точки"))}</select>',
                "🗂 Добавить") + "</div>"
        )

    body = (
        f"<h1>{esc(target.full_name)}</h1>"
        '<p><a href="/users">← ко всем пользователям</a></p>'
        + views.definition_list(pairs)
        + f'<h2>Действия</h2><div class="actions">{"".join(blocks)}</div>'
        + extra
    )
    return web.Response(text=views.page(target.full_name, body, user, "users"),
                        content_type="text/html")


# --------------------------------------------------------------------------- #
# Действия над пользователем
# --------------------------------------------------------------------------- #
UserAction = Callable[[web.Request, AsyncSession, User, User, "MultiDictProxy"], Awaitable[str]]
USER_ACTIONS: Dict[str, UserAction] = {}


def user_action(name: str):
    def register(func: UserAction) -> UserAction:
        USER_ACTIONS[name] = func
        return func
    return register


@user_action("approve")
async def _approve(request, session, target, actor, form) -> str:
    summary = await user_ops.approve(
        session, _bot(request), target, actor, str(form.get("role", "")),
        _opt_int(form.get("brand_id")), _opt_int(form.get("outlet_id")),
        [_opt_int(c) for c in form.getall("category_ids", []) if str(c).strip()],
    )
    return _plain(summary)


@user_action("reject")
async def _reject(request, session, target, actor, form) -> str:
    await user_ops.reject(session, _bot(request), target, actor, str(form.get("reason", "")))
    return "Регистрация отклонена, пользователь уведомлён."


@user_action("role")
async def _role(request, session, target, actor, form) -> str:
    warning = await user_ops.set_role(session, _bot(request), target, actor, str(form.get("role", "")))
    return {
        "needs_directions": "Роль изменена. Добавьте направления — без них заявки мастеру "
                            "приходить не будут.",
        "stale_directions": "Роль изменена. Внимание: у человека остались направления мастера, "
                            "но заявки по ним ему больше не пойдут.",
    }.get(warning, "Роль изменена.")


@user_action("status")
async def _status(request, session, target, actor, form) -> str:
    await user_ops.set_status(session, _bot(request), target, actor, str(form.get("status", "")))
    return f"Доступ: {UserStatus.TITLES.get(target.status, target.status)}."


@user_action("brand")
async def _brand(request, session, target, actor, form) -> str:
    await user_ops.set_brand(session, target, actor, _opt_int(form.get("brand_id")))
    return "Бренд изменён."


@user_action("outlet")
async def _outlet(request, session, target, actor, form) -> str:
    await user_ops.set_outlet(session, target, actor, _opt_int(form.get("outlet_id")))
    return "Точка изменена."


@user_action("phone")
async def _phone(request, session, target, actor, form) -> str:
    await user_ops.set_phone(session, target, actor, str(form.get("phone", "")))
    return f"Телефон: {target.phone or '—'}."


@user_action("payment")
async def _payment(request, session, target, actor, form) -> str:
    await user_ops.set_fixed_payment(session, _bot(request), target, actor, form.get("fixed") == "1")
    return f"Оплата: {target.payment_title}."


@user_action("scope_add")
async def _scope_add(request, session, target, actor, form) -> str:
    brand_id, outlet_id = _target(form.get("target"))
    await user_ops.add_scope(session, _bot(request), target, actor,
                             brand_id=brand_id, outlet_id=outlet_id)
    return "Объект закреплён, пользователь уведомлён."


@user_action("scope_del")
async def _scope_del(request, session, target, actor, form) -> str:
    await user_ops.remove_scope(session, target, actor, _opt_int(form.get("scope_id")))
    return "Объект откреплён."


@user_action("dir_add")
async def _dir_add(request, session, target, actor, form) -> str:
    brand_id, outlet_id = _target(form.get("target"))
    category_id = _opt_int(form.get("category_id"))
    if category_id is None:
        raise ActionError("Выберите направление.")
    await user_ops.add_direction(session, target, actor, category_id,
                                 brand_id=brand_id, outlet_id=outlet_id)
    return "Направление добавлено."


@user_action("dir_del")
async def _dir_del(request, session, target, actor, form) -> str:
    await user_ops.remove_direction(session, target, actor, _opt_int(form.get("assignment_id")))
    return "Направление снято."


@routes.post("/users/{uid}/do/{action}")
async def user_do(request: web.Request) -> web.Response:
    user, form = await _admin_post(request)
    action = USER_ACTIONS.get(request.match_info["action"])
    if action is None:
        raise web.HTTPNotFound(text="Неизвестное действие")
    uid = int(request.match_info["uid"])
    back = f"/users/{uid}"
    async with entity_lock("user", uid), _maker(request)() as session:
        target = await session.get(User, uid)
        if target is None:
            raise web.HTTPNotFound(text="Пользователь не найден")
        actor = await session.get(User, user.id)
        try:
            message = await action(request, session, target, actor, form)
        except ActionError as exc:
            await session.rollback()
            return _back(back, str(exc), ok=False)
        await session.commit()
    return _back(back, message)


# --------------------------------------------------------------------------- #
# Справочники: точки, категории, мастера
# --------------------------------------------------------------------------- #
@routes.get("/catalog")
async def catalog_page(request: web.Request) -> web.Response:
    user = await _manager(request)
    async with _maker(request)() as session:
        brands = list((await session.scalars(select(Brand).order_by(Brand.sort_order))).all())
        outlets = list((await session.scalars(
            select(Outlet).order_by(Outlet.brand_id, Outlet.sort_order, Outlet.name)
        )).all())
        categories = list((await session.scalars(select(Category).order_by(Category.sort_order))).all())
        executors = list((await session.scalars(
            select(User).where(User.role_code == RoleCode.EXECUTOR).order_by(User.full_name)
        )).all())
        dir_counts = dict((await session.execute(
            select(ExecutorAssignment.user_id, func.count()).group_by(ExecutorAssignment.user_id)
        )).all())

    csrf = auth.csrf_token(request)
    url = "/catalog/do"
    brand_opts = [(b.id, b.name) for b in brands]

    def rename_form(action: str, id_field: str, obj_id: int, value: str) -> str:
        return (
            f'<form class="inline" method="post" action="{url}/{action}">{views.csrf_input(csrf)}'
            f'<input type="hidden" name="{id_field}" value="{obj_id}">'
            f'<input name="name" value="{esc(value)}" required style="width:200px">'
            "<button type=submit>Переименовать</button></form>"
        )

    outlet_rows = [
        [
            rename_form("outlet_rename", "outlet_id", o.id, o.name),
            f'<form class="inline" method="post" action="{url}/outlet_brand">{views.csrf_input(csrf)}'
            f'<input type="hidden" name="outlet_id" value="{o.id}">'
            f'<select name="brand_id">{views.options_html(brand_opts, o.brand_id)}</select>'
            "<button type=submit>Сменить</button></form>",
            views.tag("работает", "green") if o.is_active else views.tag("выключена", "red"),
            views.inline_button(f"{url}/outlet_toggle", "выключить" if o.is_active else "включить",
                                csrf, [("outlet_id", o.id)]),
        ]
        for o in outlets
    ]
    category_rows = [
        [
            esc(c.emoji or ""),
            rename_form("cat_rename", "category_id", c.id, c.name),
            views.tag("действует", "green") if c.is_active else views.tag("выключена", "red"),
            ("фото обязательно" if c.photo_required else "фото по желанию"),
            views.inline_button(f"{url}/cat_toggle", "выключить" if c.is_active else "включить",
                                csrf, [("category_id", c.id), ("field", "is_active")])
            + " "
            + views.inline_button(f"{url}/cat_toggle",
                                  "фото по желанию" if c.photo_required else "фото обязательно",
                                  csrf, [("category_id", c.id), ("field", "photo_required")]),
        ]
        for c in categories
    ]
    executor_rows = [
        [
            f'<a href="/users/{e.id}">{esc(e.full_name)}</a>',
            esc(UserStatus.TITLES.get(e.status, e.status)),
            str(dir_counts.get(e.id, 0)),
            esc(e.phone or "—"),
            esc(("@" + e.username) if e.username else "—"),
            "да" if e.tg_id else "нет",
        ]
        for e in executors
    ]
    body = (
        "<h1>Справочники</h1>"
        "<h2>Точки</h2>"
        + views.table(["Название", "Бренд", "Статус", ""], outlet_rows)
        + '<div class="actions">' + views.action_box(
            f"{url}/outlet_add", "Добавить точку",
            f'<select name="brand_id">{views.options_html(brand_opts)}</select>'
            '<input name="name" required placeholder="Название точки">',
            "🏬 Добавить", csrf) + "</div>"
        + "<h2>Категории</h2>"
        + views.table(["", "Название", "Статус", "Фото", ""], category_rows)
        + '<div class="actions">' + views.action_box(
            f"{url}/cat_add", "Добавить категорию",
            '<input name="name" required placeholder="Название категории">'
            '<input name="emoji" maxlength="4" placeholder="Эмодзи (необязательно)">'
            '<span class="muted">После добавления назначьте мастеров на это направление.</span>',
            "📦 Добавить", csrf) + "</div>"
        + "<h2>Мастера</h2>"
        + views.table(["Ф.И.О.", "Доступ", "Направлений", "Телефон", "Telegram", "Бот активирован"],
                      executor_rows)
        + '<div class="actions">' + views.action_box(
            f"{url}/exec_add", "Добавить мастера",
            '<input name="full_name" required placeholder="Ф.И.О.">'
            '<input name="phone" placeholder="Телефон (необязательно)">'
            '<input name="username" placeholder="@username в Telegram (необязательно)">'
            '<span class="muted">С username бот узнает мастера, когда тот нажмёт /start.</span>',
            "👷 Добавить", csrf) + "</div>"
    )
    return web.Response(text=views.page("Справочники", body, user, "catalog"),
                        content_type="text/html")


CatalogAction = Callable[[AsyncSession, User, "MultiDictProxy"], Awaitable[str]]
CATALOG_ACTIONS: Dict[str, CatalogAction] = {}


def catalog_action(name: str):
    def register(func: CatalogAction) -> CatalogAction:
        CATALOG_ACTIONS[name] = func
        return func
    return register


async def _get_or_fail(session: AsyncSession, model, raw, what: str):
    obj = await session.get(model, _opt_int(raw)) if _opt_int(raw) else None
    if obj is None:
        raise ActionError(f"{what} не найдена — обновите страницу.")
    return obj


@catalog_action("outlet_add")
async def _outlet_add(session, actor, form) -> str:
    brand_id = _opt_int(form.get("brand_id"))
    if brand_id is None or await session.get(Brand, brand_id) is None:
        raise ActionError("Выберите бренд.")
    outlet = await catalog_ops.add_outlet(session, actor, brand_id, str(form.get("name", "")))
    return f"Точка «{outlet.name}» добавлена."


@catalog_action("outlet_rename")
async def _outlet_rename(session, actor, form) -> str:
    outlet = await _get_or_fail(session, Outlet, form.get("outlet_id"), "Точка")
    await catalog_ops.rename_outlet(session, actor, outlet, str(form.get("name", "")))
    return f"Точка переименована: «{outlet.name}»."


@catalog_action("outlet_brand")
async def _outlet_brand(session, actor, form) -> str:
    outlet = await _get_or_fail(session, Outlet, form.get("outlet_id"), "Точка")
    brand_id = _opt_int(form.get("brand_id"))
    if brand_id is None or await session.get(Brand, brand_id) is None:
        raise ActionError("Выберите бренд.")
    await catalog_ops.set_outlet_brand(session, actor, outlet, brand_id)
    return f"Точка «{outlet.name}» перенесена в другой бренд."


@catalog_action("outlet_toggle")
async def _outlet_toggle(session, actor, form) -> str:
    outlet = await _get_or_fail(session, Outlet, form.get("outlet_id"), "Точка")
    await catalog_ops.toggle_outlet(session, actor, outlet)
    return f"Точка «{outlet.name}» {'включена' if outlet.is_active else 'выключена'}."


@catalog_action("cat_add")
async def _cat_add(session, actor, form) -> str:
    category = await catalog_ops.add_category(
        session, actor, str(form.get("name", "")), str(form.get("emoji", ""))
    )
    return f"Категория «{category.label}» добавлена. Назначьте мастеров на это направление."


@catalog_action("cat_rename")
async def _cat_rename(session, actor, form) -> str:
    category = await _get_or_fail(session, Category, form.get("category_id"), "Категория")
    await catalog_ops.rename_category(session, actor, category, str(form.get("name", "")))
    return f"Категория переименована: «{category.label}»."


@catalog_action("cat_toggle")
async def _cat_toggle(session, actor, form) -> str:
    category = await _get_or_fail(session, Category, form.get("category_id"), "Категория")
    await catalog_ops.toggle_category(session, actor, category, str(form.get("field", "")))
    return f"Категория «{category.label}» обновлена."


@catalog_action("exec_add")
async def _exec_add(session, actor, form) -> str:
    executor = await catalog_ops.add_executor(
        session, actor, str(form.get("full_name", "")), str(form.get("phone", "")),
        str(form.get("username", "")),
    )
    return (f"Мастер «{executor.full_name}» добавлен. Откройте его карточку и назначьте "
            "направления.")


@routes.post("/catalog/do/{action}")
async def catalog_do(request: web.Request) -> web.Response:
    user, form = await _admin_post(request)
    action = CATALOG_ACTIONS.get(request.match_info["action"])
    if action is None:
        raise web.HTTPNotFound(text="Неизвестное действие")
    async with entity_lock("catalog"), _maker(request)() as session:
        actor = await session.get(User, user.id)
        try:
            message = await action(session, actor, form)
        except ActionError as exc:
            await session.rollback()
            return _back("/catalog", str(exc), ok=False)
        await session.commit()
    return _back("/catalog", message)
