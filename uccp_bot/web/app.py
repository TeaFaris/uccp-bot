"""Панель в браузере: просмотр для всех ролей, правка — для администратора.

Правка идёт только через общий слой действий (`services/*_ops.py`) — тот же,
что вызывает бот, поэтому уведомления в Telegram и журнал работают так же.

Область видимости берётся из тех же правил, что и в боте
(`services/scope.py`), поэтому руководитель видит ровно свои объекты.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import mimetypes
from collections import OrderedDict
from typing import Awaitable, Callable, Dict, Optional, Tuple

from aiohttp import web
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import config
from ..db.models import (
    Attachment,
    Brand,
    Category,
    CostStatus,
    OrgRequest,
    OrgStatus,
    Outlet,
    Priority,
    Request,
    RequestStatusHistory,
    Status,
    User,
    UserStatus,
)
from ..services import history, request_ops, routing
from ..services import org as org_service
from ..services import reports as reports_service
from ..services import scope
from ..services.cards import REQUEST_LOAD_OPTIONS, load_request
from ..services.ops import ActionError
from ..services.history import ACTION_TITLES
from ..utils import (
    esc, fmt_dt, fmt_money, local_from_utc, parse_amount, parse_date, utc_from_local,
)
from . import auth, views

PAGE_SIZE = 25
routes = web.RouteTableDef()


def _maker(request: web.Request) -> async_sessionmaker:
    return request.app["session_maker"]


async def _guard(request: web.Request) -> Optional[User]:
    user = await auth.current_user(request)
    if user is None:
        raise web.HTTPFound("/login")
    return user


# --------------------------------------------------------------------------- #
# Правка: формы, сообщения после действия
# --------------------------------------------------------------------------- #
FLASH_COOKIE = "uccp_flash"


async def _admin_post(request: web.Request) -> Tuple[User, dict]:
    """Вход + верный токен формы + право администратора — иначе 403."""
    user = await _guard(request)
    form = dict(await request.post())
    if not auth.check_csrf(request, form.get("csrf", "")):
        raise web.HTTPForbidden(text="Форма устарела — обновите страницу и повторите.")
    if not user.can_manage_users:
        raise web.HTTPForbidden(text="Изменения доступны только главному администратору")
    return user, form


def _back(location: str, message: str, ok: bool = True) -> web.HTTPSeeOther:
    """Редирект обратно с одноразовым сообщением об итоге действия."""
    response = web.HTTPSeeOther(location)
    payload = json.dumps({"ok": ok, "text": message}, ensure_ascii=False).encode()
    response.set_cookie(
        FLASH_COOKIE, base64.urlsafe_b64encode(payload).decode(),
        max_age=60, httponly=True, samesite="Lax", secure=config.web_https,
    )
    return response


def _read_flash(raw: Optional[str]) -> Optional[dict]:
    try:
        return json.loads(base64.urlsafe_b64decode(raw.encode())) if raw else None
    except Exception:
        return None


@web.middleware
async def panel_middleware(request: web.Request, handler):
    """Действия из панели помечаются в журнале; сообщение об итоге показывается один раз."""
    token = history.SOURCE.set("панель")
    try:
        response = await handler(request)
    finally:
        history.SOURCE.reset(token)
    flash = _read_flash(request.cookies.get(FLASH_COOKIE))
    if (flash and isinstance(response, web.Response) and response.text
            and response.content_type == "text/html"):
        response.text = response.text.replace(
            "<main>", "<main>" + views.flash(flash.get("text", ""), flash.get("ok", True)), 1
        )
        response.del_cookie(FLASH_COOKIE)
    return response


# --------------------------------------------------------------------------- #
# Вход
# --------------------------------------------------------------------------- #
@routes.get("/login")
async def login_form(request: web.Request) -> web.Response:
    if await auth.current_user(request) is not None:
        raise web.HTTPFound("/")
    return web.Response(text=views.login_page(), content_type="text/html")


@routes.post("/login")
async def login_submit(request: web.Request) -> web.Response:
    data = await request.post()
    code = str(data.get("code", ""))
    user_id = auth.check_code(code, auth.client_ip(request))
    if user_id is None:
        return web.Response(
            text=views.login_page("Код неверный или истёк. Запросите новый: /panel"),
            content_type="text/html",
            status=401,
        )
    response = web.HTTPFound("/")
    auth.set_cookie(response, user_id)
    return response


@routes.get("/logout")
async def logout(request: web.Request) -> web.Response:
    response = web.HTTPFound("/login")
    auth.drop_cookie(response)
    return response


# --------------------------------------------------------------------------- #
# Сводка
# --------------------------------------------------------------------------- #
@routes.get("/")
async def dashboard(request: web.Request) -> web.Response:
    user = await _guard(request)
    async with _maker(request)() as session:
        condition = await scope.request_condition(session, user)

        async def count(*extra):
            stmt = select(func.count(Request.id))
            if condition is not None:
                stmt = stmt.where(condition)
            for clause in extra:
                stmt = stmt.where(clause)
            return int(await session.scalar(stmt) or 0)

        total = await count()
        open_count = await count(Request.status.in_(Status.OPEN))
        overdue = await count(Request.is_overdue.is_(True), Request.status.in_(Status.OPEN))
        closed = await count(Request.status.in_((Status.CLOSED, Status.CONFIRMED)))
        awaiting = await count(Request.status == Status.AWAITING_ASSIGNMENT)
        cost_pending = await count(Request.cost_status == CostStatus.PENDING)

        stmt = select(Request).options(*REQUEST_LOAD_OPTIONS)
        if condition is not None:
            stmt = stmt.where(condition)
        latest = list(
            (await session.scalars(stmt.order_by(Request.created_at.desc()).limit(10))).all()
        )

        org_condition = await scope.org_condition(session, user)
        org_stmt = select(func.count(OrgRequest.id)).where(
            OrgRequest.status.in_(OrgStatus.OPEN)
        )
        if org_condition is not None:
            org_stmt = org_stmt.where(org_condition)
        org_open = int(await session.scalar(org_stmt) or 0)

    cards = "".join([
        views.stat_card(total, "всего заявок"),
        views.stat_card(open_count, "в работе"),
        views.stat_card(overdue, "просрочено", "warn" if overdue else ""),
        views.stat_card(closed, "закрыто", "ok"),
        views.stat_card(awaiting, "ждут назначения", "warn" if awaiting else ""),
        views.stat_card(cost_pending, "стоимость на согласовании"),
        views.stat_card(org_open, "обращений в УЦЦП"),
    ])
    body = (
        f"<h1>Сводка</h1><div class=cards>{cards}</div>"
        "<h2>Последние заявки</h2>"
        + views.table(
            ["Номер", "Создана", "Точка", "Категория", "Описание", "Исполнитель",
             "Приоритет", "Статус", "Срок", "Стоимость"],
            [views.request_row(r) for r in latest],
        )
        + '<p class="note">Панель работает только на просмотр. Назначение, '
        "статусы, согласование стоимости и подтверждение выполняются в Telegram.</p>"
    )
    return web.Response(
        text=views.page("Сводка", body, user, "home"), content_type="text/html"
    )


# --------------------------------------------------------------------------- #
# Заявки
# --------------------------------------------------------------------------- #
@routes.get("/requests")
async def requests_list(request: web.Request) -> web.Response:
    user = await _guard(request)
    q = request.query
    page_no = max(0, int(q.get("page", 0) or 0))

    async with _maker(request)() as session:
        condition = await scope.request_condition(session, user)
        stmt = select(Request).options(*REQUEST_LOAD_OPTIONS)
        if condition is not None:
            stmt = stmt.where(condition)

        if q.get("brand"):
            stmt = stmt.where(Request.brand_id == int(q["brand"]))
        if q.get("outlet"):
            stmt = stmt.where(Request.outlet_id == int(q["outlet"]))
        if q.get("category"):
            stmt = stmt.where(Request.category_id == int(q["category"]))
        if q.get("executor"):
            stmt = stmt.where(Request.executor_id == int(q["executor"]))
        if q.get("status"):
            stmt = stmt.where(Request.status == q["status"])
        if q.get("priority"):
            stmt = stmt.where(Request.priority == q["priority"])
        if q.get("overdue") == "1":
            stmt = stmt.where(Request.is_overdue.is_(True))
        if q.get("search"):
            like = f"%{q['search'].strip()}%"
            stmt = stmt.where(
                Request.description.ilike(like) | Request.number.ilike(like)
            )
        date_from = parse_date(q.get("from", ""))
        date_to = parse_date(q.get("to", ""))
        if date_from:
            stmt = stmt.where(Request.created_at >= dt.datetime.combine(date_from, dt.time.min))
        if date_to:
            stmt = stmt.where(Request.created_at <= dt.datetime.combine(date_to, dt.time.max))

        rows = list((await session.scalars(
            stmt.order_by(Request.created_at.desc())
            .limit(PAGE_SIZE + 1).offset(page_no * PAGE_SIZE)
        )).all())
        has_next = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]

        brands = list((await session.scalars(select(Brand).order_by(Brand.sort_order))).all())
        outlets = list((await session.scalars(select(Outlet).order_by(Outlet.name))).all())
        categories = list((await session.scalars(
            select(Category).order_by(Category.sort_order))).all())
        executors = list((await session.scalars(
            select(User).where(User.role_code == "executor").order_by(User.full_name))).all())

    filters = (
        '<form class="filters" method="get">'
        + views.select_field("brand", "Бренд",
                             [("", "все")] + [(str(b.id), b.name) for b in brands],
                             q.get("brand", ""))
        + views.select_field("outlet", "Точка",
                             [("", "все")] + [(str(o.id), o.name) for o in outlets],
                             q.get("outlet", ""))
        + views.select_field("category", "Категория",
                             [("", "все")] + [(str(c.id), c.name) for c in categories],
                             q.get("category", ""))
        + views.select_field("executor", "Исполнитель",
                             [("", "все")] + [(str(e.id), e.full_name) for e in executors],
                             q.get("executor", ""))
        + views.select_field("status", "Статус",
                             [("", "все")] + [(k, v) for k, v in Status.TITLES.items()],
                             q.get("status", ""))
        + views.select_field("priority", "Приоритет",
                             [("", "все")] + [(k, Priority.TITLES[k]) for k in Priority.ORDER],
                             q.get("priority", ""))
        + f'<div><label>С даты</label><input name="from" placeholder="дд.мм.гггг" '
          f'value="{esc(q.get("from", ""))}"></div>'
        + f'<div><label>По дату</label><input name="to" placeholder="дд.мм.гггг" '
          f'value="{esc(q.get("to", ""))}"></div>'
        + f'<div><label>Поиск</label><input name="search" placeholder="номер или текст" '
          f'value="{esc(q.get("search", ""))}"></div>'
        + '<div><label>Только просроченные</label>'
          f'<select name="overdue"><option value="">нет</option>'
          f'<option value="1"{" selected" if q.get("overdue") == "1" else ""}>да</option>'
          "</select></div>"
        + "<button type=submit>Показать</button>"
        + '<a class="tag" href="/requests">сбросить</a>'
        + "</form>"
    )

    pager = ""
    if page_no or has_next:
        base = "&".join(f"{k}={esc(v)}" for k, v in q.items() if k != "page")
        prev_link = (f'<a class="tag" href="/requests?{base}&page={page_no - 1}">← назад</a>'
                     if page_no else "")
        next_link = (f'<a class="tag" href="/requests?{base}&page={page_no + 1}">вперёд →</a>'
                     if has_next else "")
        pager = f'<div class="pager">{prev_link}{next_link}' \
                f'<span class="muted">страница {page_no + 1}</span></div>'

    body = (
        f"<h1>Заявки</h1>{filters}"
        + views.table(
            ["Номер", "Создана", "Точка", "Категория", "Описание", "Исполнитель",
             "Приоритет", "Статус", "Срок", "Стоимость"],
            [views.request_row(r) for r in rows],
        )
        + pager
    )
    return web.Response(
        text=views.page("Заявки", body, user, "requests"), content_type="text/html"
    )


@routes.get("/requests/{rid}")
async def request_card(request: web.Request) -> web.Response:
    user = await _guard(request)
    rid = int(request.match_info["rid"])
    async with _maker(request)() as session:
        req = await session.scalar(
            select(Request).where(Request.id == rid).options(*REQUEST_LOAD_OPTIONS)
        )
        if req is None or not await scope.can_view_request(session, user, req):
            raise web.HTTPNotFound(text="Заявка не найдена или недоступна")
        events = list((await session.scalars(
            select(RequestStatusHistory)
            .where(RequestStatusHistory.request_id == rid)
            .order_by(RequestStatusHistory.created_at)
        )).all())
        authors = {
            u.id: u.full_name
            for u in (await session.scalars(select(User))).all()
        }
        actions_html = ""
        acts = request_ops.available_actions(req, user)
        if acts:
            preferred = await routing.find_executors_for_category(session, req.category_id)
            executors = preferred + [
                e for e in await routing.all_executors(session) if e not in preferred
            ]
            categories = list((await session.scalars(
                select(Category).where(Category.is_active.is_(True)).order_by(Category.sort_order)
            )).all())
            actions_html = _request_actions_html(
                req, acts, executors, categories, auth.csrf_token(request)
            )

    cost_block = "—"
    if req.cost_exempt:
        cost_block = "входит в фиксированную месячную оплату мастера"
    elif req.work_cost is not None or req.material_cost is not None:
        cost_block = (
            f"работы {esc(fmt_money(req.work_cost))}, "
            f"материалы {esc(fmt_money(req.material_cost))}, "
            f"итого <b>{esc(fmt_money(req.total_cost))}</b> — "
            f"{esc(req.cost_status_title)}"
        )

    pairs = [
        ("Статус", views.tag(Status.title(req.status), views.STATUS_TAG.get(req.status, ""))
         + (' <span class="tag red">просрочена</span>' if req.is_overdue else "")),
        ("Бренд", esc(req.brand.name if req.brand else "—")),
        ("Точка", esc(req.outlet.name if req.outlet else "—")),
        ("Категория", esc(req.category.label if req.category else "—")),
        ("Описание", esc(req.description)),
        ("Приоритет", views.tag(Priority.title(req.priority),
                                views.PRIORITY_TAG.get(req.priority, ""))),
        ("Инициатор", esc(req.author.full_name if req.author else "—")),
        ("Исполнитель", esc(req.executor.full_name if req.executor else "не назначен")),
        ("Создана", esc(fmt_dt(req.created_at))),
        ("Срок", esc(fmt_dt(req.due_at))),
        ("Выполнена", esc(fmt_dt(req.done_at)) if req.done_at else ""),
        ("Закрыта", esc(fmt_dt(req.closed_at)) if req.closed_at else ""),
        ("Стоимость", cost_block),
        ("Комментарий исполнителя", esc(req.executor_comment or "")),
        ("Комментарий менеджера", esc(req.manager_comment or "")),
        ("Причина возврата", esc(req.return_reason or "")),
        ("Фото и файлы", f"{len(req.attachments)} шт. — ниже" if req.attachments else "нет"),
    ]

    timeline = "".join(
        f'<div class="ev"><b>{esc(fmt_dt(e.created_at))}</b> — '
        f"{esc(authors.get(e.user_id, 'Система'))}: "
        f"{esc(ACTION_TITLES.get(e.action, e.action))}"
        + (f"<br><span class=muted>{esc(e.details)}</span>" if e.details else "")
        + "</div>"
        for e in events
    ) or '<div class="muted">Записей нет</div>'

    body = (
        f'<h1>Заявка {esc(req.number)}</h1>'
        f'<p><a href="/requests">← ко всем заявкам</a></p>'
        + views.definition_list(pairs)
        + _media_html(req.attachments)
        + actions_html
        + "<h2>История действий</h2>"
        + f'<div class="timeline">{timeline}</div>'
    )
    return web.Response(
        text=views.page(req.number, body, user, "requests"), content_type="text/html"
    )


def _request_actions_html(req, acts, executors, categories, csrf: str) -> str:
    """Блок действий администратора — набор совпадает с кнопками бота."""
    url = f"/requests/{req.id}/do"

    def form(action: str, title: str, fields: str, button: str, danger: bool = False) -> str:
        cls = ' class="danger"' if danger else ""
        return (
            f'<div class="act"><h3>{esc(title)}</h3>'
            f'<form method="post" action="{url}/{action}">{views.csrf_input(csrf)}'
            f'{fields}<button type="submit"{cls}>{esc(button)}</button></form></div>'
        )

    def textarea(name: str, placeholder: str) -> str:
        return f'<textarea name="{name}" required placeholder="{esc(placeholder)}"></textarea>'

    blocks = []
    if "assign" in acts:
        options = "".join(
            f'<option value="{e.id}"{" selected" if e.id == req.executor_id else ""}>'
            f'{esc(e.full_name)}{"" if e.tg_id else " ⚠️ бот не активирован"}</option>'
            for e in executors
        )
        blocks.append(form(
            "assign", "Назначить исполнителя" + (" / перенаправить" if req.executor_id else ""),
            f'<select name="executor_id">{options}</select>', "👤 Назначить",
        ))
    if "change_due" in acts:
        local = local_from_utc(req.due_at)
        current = local.strftime("%Y-%m-%dT%H:%M") if local else ""
        blocks.append(form("change_due", "Изменить срок",
                           f'<input type="datetime-local" name="due" value="{current}" required>',
                           "📅 Сохранить срок"))
    if "change_category" in acts:
        options = "".join(
            f'<option value="{c.id}"{" selected" if c.id == req.category_id else ""}>'
            f'{esc(c.label)}</option>'
            for c in categories
        )
        blocks.append(form("change_category", "Изменить категорию",
                           f'<select name="category_id">{options}</select>'
                           '<span class="muted">Заявка уйдёт исполнителю нового направления.</span>',
                           "✏️ Сменить категорию"))
    if "confirm" in acts:
        blocks.append(form("confirm", "Подтвердить выполнение", "", "✅ Подтвердить и закрыть"))
    if "return" in acts:
        blocks.append(form("return", "Вернуть на доработку",
                           textarea("reason", "Причина возврата — её увидит исполнитель"),
                           "🔄 Вернуть"))
    if "cost_ok" in acts:
        blocks.append(form("cost_ok", "Согласовать стоимость",
                           f'<span>Итого {esc(fmt_money(req.total_cost))}</span>', "✅ Согласовать"))
    if "cost_no" in acts:
        blocks.append(form("cost_no", "Отклонить стоимость",
                           textarea("reason", "Причина — исполнитель заявит сумму заново"),
                           "❌ Отклонить", danger=True))
    if "edit_cost" in acts:
        work = "" if req.work_cost is None else req.work_cost
        material = "" if req.material_cost is None else req.material_cost
        blocks.append(form(
            "edit_cost", "Изменить стоимость",
            f'<input name="work" inputmode="decimal" required placeholder="Работы" '
            f'value="{esc(str(work))}">'
            f'<input name="material" inputmode="decimal" required placeholder="Материалы" '
            f'value="{esc(str(material))}">'
            + textarea("reason", "Причина изменения — сохранится в истории"),
            "💰 Сохранить стоимость"))
    if "comment" in acts:
        blocks.append(form("comment", "Комментарий",
                           textarea("text", "Увидят инициатор и исполнитель"), "💬 Отправить"))
    if "cancel" in acts:
        blocks.append(form("cancel", "Отменить заявку",
                           textarea("reason", "Причина отмены"), "🚫 Отменить заявку",
                           danger=True))
    if not blocks:
        return ""
    return "<h2>Действия</h2>" + f'<div class="actions">{"".join(blocks)}</div>'


def _form_int(form: dict, name: str, what: str) -> int:
    try:
        return int(form.get(name, ""))
    except (TypeError, ValueError):
        raise ActionError(f"Выберите {what}.")


def _form_amount(form: dict, name: str) -> float:
    amount = parse_amount(str(form.get(name, "")))
    if amount is None:
        raise ActionError("Введите сумму числом, например: 250 или 250.50")
    return amount


def _bot(request: web.Request):
    return request.app["bot"]


# --------------------------------------------------------------------------- #
# Фото и видео заявки — скачиваются через бота
# --------------------------------------------------------------------------- #
MEDIA_STAGES = (("before", "При создании"), ("after", "Результат работы"))
MEDIA_TYPES = {"photo": "image/jpeg", "video": "video/mp4"}
MEDIA_CACHE_LIMIT = 100 * 1024 * 1024       # не больше 100 МБ файлов в памяти
MEDIA_TOO_BIG = (
    "Файл недоступен для просмотра в браузере: Telegram отдаёт ботам файлы "
    "до 20 МБ. Посмотреть его можно в Telegram — кнопка «📎 Файлы» в карточке заявки."
)
_media_cache: "OrderedDict[str, Tuple[bytes, str]]" = OrderedDict()


def _media_cache_put(file_id: str, data: bytes, content_type: str) -> None:
    if len(data) > MEDIA_CACHE_LIMIT // 4:
        return
    _media_cache[file_id] = (data, content_type)
    while sum(len(d) for d, _ in _media_cache.values()) > MEDIA_CACHE_LIMIT:
        _media_cache.popitem(last=False)


def _media_html(attachments) -> str:
    if not attachments:
        return ""
    out = ["<h2>Фото и файлы</h2>"]
    for stage, title in MEDIA_STAGES:
        items = [a for a in attachments if a.stage == stage]
        if not items:
            continue
        parts = []
        for number, att in enumerate(items, 1):
            url = f"/media/{att.id}"
            if att.media_type == "photo":
                parts.append(
                    f'<a href="{url}" target="_blank"><img src="{url}" loading="lazy" '
                    f'alt="Фото {number}: не открылось — смотреть в Telegram"></a>'
                )
            else:
                label = "🎬 Видео" if att.media_type == "video" else "📄 Файл"
                parts.append(f'<a href="{url}" target="_blank">{label} {number}</a>')
        out.append(f'<h3>{esc(title)}</h3><div class="media">{"".join(parts)}</div>')
    out.append('<p class="muted">Файлы больше 20 МБ браузеру недоступны — '
               "их можно посмотреть в Telegram.</p>")
    return "".join(out)


@routes.get("/media/{aid}")
async def media_file(request: web.Request) -> web.Response:
    """Файл заявки — тем, кому видна сама заявка."""
    user = await _guard(request)
    try:
        aid = int(request.match_info["aid"])
    except ValueError:
        raise web.HTTPNotFound(text="Файл не найден")
    async with _maker(request)() as session:
        att = await session.get(Attachment, aid)
        req = None
        if att is not None:
            req = await session.scalar(
                select(Request).where(Request.id == att.request_id).options(*REQUEST_LOAD_OPTIONS)
            )
        if req is None or not await scope.can_view_request(session, user, req):
            raise web.HTTPNotFound(text="Файл не найден или недоступен")
        file_id, media_type = att.file_id, att.media_type

    cached = _media_cache.get(file_id)
    if cached is not None:
        _media_cache.move_to_end(file_id)
        data, content_type = cached
    else:
        bot = request.app["bot"]
        try:
            info = await bot.get_file(file_id)
            data = (await bot.download_file(info.file_path)).getvalue()
        except Exception:
            raise web.HTTPNotFound(text=MEDIA_TOO_BIG)
        content_type = (
            MEDIA_TYPES.get(media_type)
            or mimetypes.guess_type(info.file_path or "")[0]
            or "application/octet-stream"
        )
        _media_cache_put(file_id, data, content_type)
    return web.Response(
        body=data, content_type=content_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )


RequestAction = Callable[[web.Request, AsyncSession, Request, User, dict], Awaitable[str]]
# действие -> обработчик, возвращающий текст об успехе; заполняется ниже
REQUEST_ACTIONS: Dict[str, RequestAction] = {}


def request_action_handler(name: str):
    def register(func: RequestAction) -> RequestAction:
        REQUEST_ACTIONS[name] = func
        return func
    return register


@request_action_handler("assign")
async def _do_assign(request, session, req, actor, form) -> str:
    executor = await session.get(User, _form_int(form, "executor_id", "исполнителя"))
    sent = await request_ops.assign(session, _bot(request), req, executor, actor)
    note = "" if sent else " Мастер ещё не активировал бота — уведомление не доставлено."
    return f"Исполнитель назначен: {executor.full_name}.{note}"


@request_action_handler("change_category")
async def _do_change_category(request, session, req, actor, form) -> str:
    category = await session.get(Category, _form_int(form, "category_id", "категорию"))
    reason = await request_ops.change_category(session, _bot(request), req, category, actor)
    return {
        "ok": "Категория изменена, заявка направлена исполнителю нового направления.",
        "unregistered": "Категория изменена. Исполнитель ещё не активировал бота — "
                        "заявка передана администратору.",
        "none": "Категория изменена. Исполнителя по новому направлению нет — назначьте вручную.",
    }[reason]


@request_action_handler("change_due")
async def _do_change_due(request, session, req, actor, form) -> str:
    try:
        local = dt.datetime.fromisoformat(str(form.get("due", "")))
    except ValueError:
        raise ActionError("Укажите дату и время нового срока.")
    await request_ops.change_due(session, _bot(request), req, utc_from_local(local), actor)
    return f"Срок изменён: {fmt_dt(req.due_at)}."


@request_action_handler("comment")
async def _do_comment(request, session, req, actor, form) -> str:
    await request_ops.add_comment(session, _bot(request), req, actor, str(form.get("text", "")))
    return "Комментарий отправлен."


@request_action_handler("cancel")
async def _do_cancel(request, session, req, actor, form) -> str:
    await request_ops.cancel(session, _bot(request), req, actor, str(form.get("reason", "")))
    return f"Заявка {req.number} отменена."


@request_action_handler("confirm")
async def _do_confirm(request, session, req, actor, form) -> str:
    await request_ops.confirm(session, _bot(request), req, actor)
    return f"Заявка {req.number} подтверждена и закрыта."


@request_action_handler("return")
async def _do_return(request, session, req, actor, form) -> str:
    await request_ops.return_for_rework(
        session, _bot(request), req, actor, str(form.get("reason", ""))
    )
    return "Заявка возвращена на доработку, исполнитель уведомлён."


@request_action_handler("cost_ok")
async def _do_cost_ok(request, session, req, actor, form) -> str:
    await request_ops.approve_cost(session, _bot(request), req, actor)
    return f"Стоимость согласована: {fmt_money(req.total_cost)}."


@request_action_handler("cost_no")
async def _do_cost_no(request, session, req, actor, form) -> str:
    await request_ops.reject_cost(session, _bot(request), req, actor, str(form.get("reason", "")))
    return "Стоимость отклонена, исполнитель заявит её заново."


@request_action_handler("edit_cost")
async def _do_edit_cost(request, session, req, actor, form) -> str:
    await request_ops.edit_cost(
        session, _bot(request), req, actor,
        _form_amount(form, "work"), _form_amount(form, "material"), str(form.get("reason", "")),
    )
    return f"Стоимость изменена: {fmt_money(req.total_cost)}. Исходные суммы — в истории."


@routes.post("/requests/{rid}/do/{action}")
async def request_action(request: web.Request) -> web.Response:
    user, form = await _admin_post(request)
    action = REQUEST_ACTIONS.get(request.match_info["action"])
    if action is None:
        raise web.HTTPNotFound(text="Неизвестное действие")
    rid = int(request.match_info["rid"])
    back = f"/requests/{rid}"
    async with _maker(request)() as session:
        req = await load_request(session, rid)
        if req is None:
            raise web.HTTPNotFound(text="Заявка не найдена")
        actor = await session.get(User, user.id)
        try:
            message = await action(request, session, req, actor, form)
        except ActionError as exc:
            await session.rollback()
            return _back(back, str(exc), ok=False)
        await session.commit()
    return _back(back, message)


# --------------------------------------------------------------------------- #
# Заявки в УЦЦП
# --------------------------------------------------------------------------- #
@routes.get("/org")
async def org_list(request: web.Request) -> web.Response:
    user = await _guard(request)
    q = request.query
    page_no = max(0, int(q.get("page", 0) or 0))

    async with _maker(request)() as session:
        condition = await scope.org_condition(session, user)
        stmt = select(OrgRequest).options(*org_service.ORG_LOAD_OPTIONS)
        if condition is not None:
            stmt = stmt.where(condition)
        if q.get("status"):
            stmt = stmt.where(OrgRequest.status == q["status"])
        if q.get("type"):
            stmt = stmt.where(OrgRequest.request_type == q["type"])
        rows = list((await session.scalars(
            stmt.order_by(OrgRequest.created_at.desc())
            .limit(PAGE_SIZE + 1).offset(page_no * PAGE_SIZE)
        )).all())
        has_next = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]

    from ..db.models import OrgType

    filters = (
        '<form class="filters" method="get">'
        + views.select_field("type", "Тип обращения",
                             [("", "все")] + [(k, v) for k, v in OrgType.TITLES.items()],
                             q.get("type", ""))
        + views.select_field("status", "Статус",
                             [("", "все")] + [(k, v) for k, v in OrgStatus.TITLES.items()],
                             q.get("status", ""))
        + "<button type=submit>Показать</button>"
        + '<a class="tag" href="/org">сбросить</a></form>'
    )
    table_rows = [
        [
            f'<a href="/org/{r.id}"><b>{esc(r.number)}</b></a>',
            esc(fmt_dt(r.created_at)),
            esc(r.object_name),
            esc(r.type_title),
            esc((r.description or "")[:60]),
            esc(r.author.full_name if r.author else "—"),
            esc(r.assignee.full_name if r.assignee else "не назначен"),
            views.tag(OrgStatus.title(r.status), views.STATUS_TAG.get(r.status, "")),
            esc(fmt_dt(r.due_at)),
        ]
        for r in rows
    ]
    pager = ""
    if page_no or has_next:
        base = "&".join(f"{k}={esc(v)}" for k, v in q.items() if k != "page")
        pager = '<div class="pager">' + (
            f'<a class="tag" href="/org?{base}&page={page_no - 1}">← назад</a>' if page_no else ""
        ) + (
            f'<a class="tag" href="/org?{base}&page={page_no + 1}">вперёд →</a>' if has_next else ""
        ) + "</div>"

    body = (
        "<h1>Заявки в УЦЦП</h1>"
        '<p class="muted">Обращения торговых точек в центр — отдельно от '
        "технических заявок мастерам.</p>" + filters
        + views.table(
            ["Номер", "Создано", "Объект", "Тип", "Запрос", "Автор",
             "Ответственный", "Статус", "Срок"],
            table_rows,
        )
        + pager
    )
    return web.Response(text=views.page("Заявки в УЦЦП", body, user, "org"),
                        content_type="text/html")


@routes.get("/org/{rid}")
async def org_card(request: web.Request) -> web.Response:
    user = await _guard(request)
    rid = int(request.match_info["rid"])
    async with _maker(request)() as session:
        req = await org_service.load(session, rid)
        if req is None or not org_service.can_view(user, req):
            raise web.HTTPNotFound(text="Обращение не найдено или недоступно")
        text_card = await org_service.render_history(session, rid)

    pairs = [
        ("Статус", views.tag(OrgStatus.title(req.status),
                             views.STATUS_TAG.get(req.status, ""))),
        ("Тип", esc(req.type_title)),
        ("Бренд", esc(req.brand.name if req.brand else "—")),
        ("Объект", esc(req.object_name)),
        ("Автор", esc(req.author.full_name if req.author else "—")),
        ("Должность", esc(req.author_position or "—")),
        ("Запрос", esc(req.description)),
        ("Количество", esc(req.quantity or "")),
        ("Обоснование", esc(req.reason or "")),
        ("Срок", esc(fmt_dt(req.due_at))),
        ("Принял в УЦЦП", esc(req.accepted_by.full_name) if req.accepted_by else ""),
        ("Ответственный", esc(req.assignee.full_name) if req.assignee else "не назначен"),
        ("Запрос уточнения", esc(req.clarify_question or "")),
        ("Ответ автора", esc(req.clarify_answer or "")),
        ("Результат", esc(req.result_comment or "")),
        ("Файлы", f"{len(req.attachments)} шт. — смотреть в Telegram"
         if req.attachments else "нет"),
    ]
    body = (
        f"<h1>Обращение {esc(req.number)}</h1>"
        f'<p><a href="/org">← ко всем обращениям</a></p>'
        + views.definition_list(pairs)
        + "<h2>История действий</h2>"
        + '<div class="timeline">'
        + text_card.replace("🕓 <b>История изменений</b>", "").replace("\n", "<br>")
        + "</div>"
    )
    return web.Response(text=views.page(req.number, body, user, "org"),
                        content_type="text/html")


# --------------------------------------------------------------------------- #
# Отчёты
# --------------------------------------------------------------------------- #
@routes.get("/reports")
async def reports_page(request: web.Request) -> web.Response:
    user = await _guard(request)
    if not user.can_see_reports:
        raise web.HTTPForbidden(text="Отчёты вам недоступны")

    q = request.query
    period = q.get("period", "month")
    date_from, date_to, title = reports_service.period_range(period)
    filters = reports_service.ReportFilters(
        date_from=date_from, date_to=date_to, period_title=title
    )
    # та же область видимости, что и в боте
    if not (user.is_admin or user.is_ops_chief or user.role_code == "uccp_staff"):
        if user.is_ops_director:
            filters.brand_id = user.brand_id
        elif user.is_outlet_admin:
            filters.outlet_id = user.outlet_id

    async with _maker(request)() as session:
        data = await reports_service.build_report(session, filters)
        org_data = await reports_service.build_org_report(session, filters)

    cards = "".join([
        views.stat_card(data.total, "заявок за период"),
        views.stat_card(data.closed, "закрыто", "ok"),
        views.stat_card(data.in_work, "в работе"),
        views.stat_card(data.overdue, "просрочено", "warn" if data.overdue else ""),
        views.stat_card(esc(fmt_money(data.total_cost)), "расходы"),
        views.stat_card(org_data.total, "обращений в УЦЦП"),
    ])

    def breakdown(title_text: str, store) -> str:
        rows = [
            [esc(name), str(count), esc(fmt_money(cost)) if cost else "—"]
            for name, (count, cost) in sorted(
                store.items(), key=lambda kv: (-kv[1][0], kv[0])
            )[:15]
        ]
        return f"<h2>{esc(title_text)}</h2>" + views.table(
            ["Наименование", "Заявок", "Расходы"], rows
        )

    eff_rows = [
        [
            esc(s.name), str(s.total), str(s.closed),
            f"{s.on_time_share}%" if (s.on_time + s.overdue) else "—",
            str(s.overdue),
            f"{s.avg_hours:.1f} ч" if s.avg_hours else "—",
            esc(fmt_money(s.cost)) if s.cost else "—",
        ]
        for s in sorted(
            (v for k, v in data.executors.items() if k != "не назначен"),
            key=lambda s: -s.total,
        )
    ]

    periods = [("today", "Сегодня"), ("yesterday", "Вчера"), ("week", "Неделя"),
               ("month", "Месяц"), ("all", "Всё время")]
    switch = " ".join(
        f'<a class="tag {"blue" if code == period else ""}" '
        f'href="/reports?period={code}">{esc(label)}</a>'
        for code, label in periods
    )

    body = (
        f"<h1>Отчёт {esc(title)}</h1><p>{switch}</p>"
        f"<div class=cards>{cards}</div>"
        + "<h2>Эффективность исполнителей</h2>"
        + views.table(
            ["Исполнитель", "Заявок", "Закрыто", "В срок", "Просрочено",
             "Среднее время", "Расходы"],
            eff_rows,
        )
        + breakdown("По брендам", data.by_brand)
        + breakdown("По точкам", data.by_outlet)
        + breakdown("По категориям", data.by_category)
        + '<p class="note">Полная выгрузка в Excel и Google Sheets — '
        "в боте: 📊 Отчёты.</p>"
    )
    return web.Response(text=views.page("Отчёты", body, user, "reports"),
                        content_type="text/html")


# --------------------------------------------------------------------------- #
# Пользователи (только главный администратор)
# --------------------------------------------------------------------------- #
@routes.get("/users")
async def users_page(request: web.Request) -> web.Response:
    user = await _guard(request)
    if not user.can_manage_users:
        raise web.HTTPForbidden(text="Раздел доступен только главному администратору")
    async with _maker(request)() as session:
        rows = list((await session.scalars(select(User).order_by(User.full_name))).all())
        brands = {b.id: b.name for b in (await session.scalars(select(Brand))).all()}
        outlets = {o.id: o.name for o in (await session.scalars(select(Outlet))).all()}

    status_kind = {UserStatus.ACTIVE: "green", UserStatus.PENDING: "amber",
                   UserStatus.BLOCKED: "red", UserStatus.DISABLED: "red",
                   UserStatus.REJECTED: "red"}
    table_rows = [
        [
            esc(u.full_name),
            esc(u.role_title),
            views.tag(UserStatus.TITLES.get(u.status, u.status),
                      status_kind.get(u.status, "")),
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
        '<p class="muted">Изменение ролей и прав — в боте: '
        "⚙️ Администрирование → 👥 Пользователи.</p>"
        + views.table(
            ["Ф.И.О.", "Роль", "Статус", "Должность", "Бренд", "Точка",
             "Телефон", "Бот активирован"],
            table_rows,
        )
    )
    return web.Response(text=views.page("Пользователи", body, user, "users"),
                        content_type="text/html")


@routes.get("/healthz")
async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def build_app(session_maker: async_sessionmaker, bot=None) -> web.Application:
    app = web.Application(middlewares=[panel_middleware])
    app["session_maker"] = session_maker
    app["bot"] = bot            # для уведомлений в Telegram из действий панели
    app.add_routes(routes)
    return app


async def start_panel(session_maker: async_sessionmaker, bot=None) -> Optional[web.AppRunner]:
    """Поднимает панель рядом с ботом в том же процессе."""
    if not config.web_enabled:
        return None
    app = build_app(session_maker, bot)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, config.web_host, config.web_port)
    await site.start()
    return runner
