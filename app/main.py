"""Servio web app: agent workspace, self-service portal, approvals, knowledge, admin."""

from __future__ import annotations

import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import config, db, markdown, services
from .auth import hash_password, verify_password
from .config import TYPES
from .services import ValidationError

BASE = Path(__file__).resolve().parent


def _secret_key() -> str:
    if key := os.environ.get("SERVIO_SECRET"):
        return key
    path = db.ROOT / ".servio_secret"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
    return path.read_text().strip()


@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    yield
    db.close_pool()


app = FastAPI(title="Servio", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=_secret_key(), same_site="lax",
                   max_age=60 * 60 * 12)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


# ---------------------------------------------------------------- template helpers

def _local(value: str | None):
    dt = services.parse_dt(value)
    return dt.astimezone() if dt else None


def fmt_dt(value: str | None) -> str:
    dt = _local(value)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def fmt_dtlocal(value: str | None) -> str:
    dt = _local(value)
    return dt.strftime("%Y-%m-%dT%H:%M") if dt else ""


def fmt_ago(value: str | None) -> str:
    dt = services.parse_dt(value)
    if not dt:
        return ""
    secs = (db.now() - dt).total_seconds()
    if secs < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return ""


def qs(params: dict, **overrides) -> str:
    """Query string from current params with overrides; None/'' values are dropped."""
    merged = {k: v for k, v in {**params, **overrides}.items() if v not in (None, "")}
    return "?" + urlencode(merged)


templates.env.filters.update(dt=fmt_dt, dtlocal=fmt_dtlocal, ago=fmt_ago, md=markdown.render,
                             slug=lambda s: re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-"))
templates.env.globals.update(TYPES=TYPES, PRIORITY=config.PRIORITY, sla_info=services.sla_info,
                             qs=qs)


# ---------------------------------------------------------------- auth & deps

class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


@app.exception_handler(Redirect)
async def _redirect(_request, exc: Redirect):
    return RedirectResponse(exc.url, status_code=303)


def get_con():
    con = db.connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def current_user(request: Request, con=Depends(get_con)) -> dict:
    uid = request.session.get("uid")
    row = con.execute("SELECT * FROM users WHERE id = ? AND active = 1", (uid,)).fetchone() \
        if uid else None
    if not row:
        raise Redirect(f"/login?next={quote(request.url.path)}")
    return dict(row)


def agent_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] == "requester":
        raise Redirect("/portal")
    return user


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admins only")
    return user


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append({"kind": kind, "text": message})


def render(request: Request, con, user: dict | None, name: str, **ctx):
    if user:
        ctx["pending_approvals"] = con.execute(
            "SELECT COUNT(*) FROM approvals WHERE approver_id = ? AND state = 'requested'",
            (user["id"],)).fetchone()[0]
    ctx.update(request=request, user=user, flashes=request.session.pop("flash", []))
    return templates.TemplateResponse(request, name, ctx)


def done(con, url: str) -> RedirectResponse:
    con.commit()  # commit before the browser follows the redirect
    return RedirectResponse(url, status_code=303)


def check_type(rtype: str) -> dict:
    if rtype not in TYPES:
        raise HTTPException(404, "Unknown record type")
    return TYPES[rtype]


def load_record(con, number: str) -> dict:
    rec = services.get_record_by_number(con, number)
    if not rec:
        raise HTTPException(404, f"{number} not found")
    return rec


def can_view(con, user: dict, rec: dict) -> bool:
    if user["role"] != "requester" or rec["caller_id"] == user["id"]:
        return True
    return con.execute("SELECT 1 FROM approvals WHERE record_id = ? AND approver_id = ?",
                       (rec["id"], user["id"])).fetchone() is not None


# ---------------------------------------------------------------- login

@app.get("/login")
def login_page(request: Request, next: str = "/", con=Depends(get_con)):
    return render(request, con, None, "login.html", next=next, error=None)


@app.post("/login")
async def login(request: Request, con=Depends(get_con)):
    form = await request.form()
    target = str(form.get("next") or "/")
    if not target.startswith("/") or target.startswith("//"):
        target = "/"
    row = con.execute("SELECT * FROM users WHERE username = ? AND active = 1",
                      (str(form.get("username", "")).strip().lower(),)).fetchone()
    if not row or not verify_password(str(form.get("password", "")), row["password_hash"]):
        return render(request, con, None, "login.html", next=target,
                      error="Wrong username or password.")
    request.session.clear()
    request.session["uid"] = row["id"]
    return RedirectResponse(target, status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/")
def home(user=Depends(current_user)):
    return RedirectResponse("/dashboard" if user["role"] != "requester" else "/portal", 303)


# ---------------------------------------------------------------- dashboard

@app.get("/dashboard")
def dashboard(request: Request, user=Depends(agent_user), con=Depends(get_con)):
    now_iso = db.iso(db.now())
    active = ("state NOT IN ('Resolved','Closed','Canceled','Closed Complete',"
              "'Closed Incomplete','Rejected')")
    my_groups = [r[0] for r in con.execute(
        "SELECT group_id FROM group_members WHERE user_id = ?", (user["id"],))]
    group_marks = ",".join("?" * len(my_groups)) or "NULL"

    def count(sql, *args):
        return con.execute(f"SELECT COUNT(*) FROM records WHERE {active} AND {sql}",
                           args).fetchone()[0]

    cards = [
        ("Assigned to me", count("assigned_to_id = ?", user["id"]), "/list/incident?assigned=me"),
        ("Unassigned in my groups",
         count(f"assigned_to_id IS NULL AND assignment_group_id IN ({group_marks})", *my_groups),
         "/list/incident?assigned=mygroups_unassigned"),
        ("Critical & high incidents", count("type = 'incident' AND priority <= 2"),
         "/list/incident?priority=2"),
        ("SLA breached", count("type = 'incident' AND sla_due < ?", now_iso),
         "/list/incident?sla=breached"),
    ]

    by_priority = {p: 0 for p in config.PRIORITY}
    for p, n in con.execute(f"SELECT priority, COUNT(*) FROM records WHERE type = 'incident' "
                            f"AND {active} GROUP BY priority"):
        if p:
            by_priority[p] = n
    by_group = con.execute(
        f"SELECT COALESCE(g.name, 'Unassigned'), COUNT(*) FROM records r "
        f"LEFT JOIN groups g ON g.id = r.assignment_group_id WHERE r.{active} "
        f"GROUP BY 1 ORDER BY 2 DESC").fetchall()
    days = []
    today = datetime.now().astimezone().date()
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        start = datetime.combine(day, datetime.min.time()).astimezone().astimezone(timezone.utc)
        n = con.execute("SELECT COUNT(*) FROM records WHERE type = 'incident' AND opened_at >= ? "
                        "AND opened_at < ?",
                        (db.iso(start), db.iso(start + timedelta(days=1)))).fetchone()[0]
        days.append((day.strftime("%a"), n))

    my_work = list_query(con, user, None, {"assigned": "me", "active": "1", "sort": "priority",
                                           "dir": "asc"}, limit=10)[0]
    return render(request, con, user, "dashboard.html", cards=cards, by_priority=by_priority,
                  by_group=by_group, days=days, my_work=my_work)


# ---------------------------------------------------------------- lists

SORTABLE = {"number", "priority", "state", "updated_at", "opened_at", "sla_due", "planned_start",
            "short_description"}


def list_query(con, user, rtype: str | None, params: dict, limit: int = 50, offset: int = 0):
    where, args = [], []
    if rtype:
        where.append("r.type = ?")
        args.append(rtype)
    if params.get("active", "1") == "1":
        closed = sorted({s for t in TYPES.values() for s in t["closed"]})
        where.append(f"r.state NOT IN ({','.join('?' * len(closed))})")
        args += closed
    if params.get("state"):
        where.append("r.state = ?")
        args.append(params["state"])
    if params.get("priority"):
        where.append("r.priority <= ?")
        args.append(int(params["priority"]))
    assigned = params.get("assigned")
    if assigned == "me":
        where.append("r.assigned_to_id = ?")
        args.append(user["id"])
    elif assigned == "unassigned":
        where.append("r.assigned_to_id IS NULL")
    elif assigned in ("mygroups", "mygroups_unassigned"):
        where.append("r.assignment_group_id IN "
                     "(SELECT group_id FROM group_members WHERE user_id = ?)")
        args.append(user["id"])
        if assigned == "mygroups_unassigned":
            where.append("r.assigned_to_id IS NULL")
    if params.get("sla") == "breached":
        where.append("r.sla_due < ?")
        args.append(db.iso(db.now()))
    if params.get("caller") == "me":
        where.append("r.caller_id = ?")
        args.append(user["id"])
    if q := (params.get("q") or "").strip():
        where.append("(r.number ILIKE ? OR r.short_description ILIKE ? OR r.description ILIKE ?)")
        args += [f"%{q}%"] * 3

    sort = params.get("sort") if params.get("sort") in SORTABLE else "updated_at"
    direction = "ASC" if params.get("dir") == "asc" else "DESC"
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = con.execute(f"SELECT COUNT(*) FROM records r {clause}", args).fetchone()[0]
    rows = con.execute(f"""
        SELECT r.*, c.name AS caller_name, a.name AS assignee_name, g.name AS group_name,
               ci.name AS item_name,
               (SELECT COUNT(*) FROM records x WHERE x.problem_id = r.id) AS incident_count
        FROM records r
        LEFT JOIN users c ON c.id = r.caller_id
        LEFT JOIN users a ON a.id = r.assigned_to_id
        LEFT JOIN groups g ON g.id = r.assignment_group_id
        LEFT JOIN catalog_items ci ON ci.id = r.catalog_item_id
        {clause} ORDER BY r.{sort} IS NULL, r.{sort} {direction}, r.id DESC
        LIMIT ? OFFSET ?""", [*args, limit, offset]).fetchall()
    return [dict(r) for r in rows], total


@app.get("/list/{rtype}")
def list_view(request: Request, rtype: str, user=Depends(agent_user), con=Depends(get_con)):
    cfg = check_type(rtype)
    params = dict(request.query_params)
    params.setdefault("active", "1")
    page = max(int(params.get("page", 1) or 1), 1)
    rows, total = list_query(con, user, rtype, params, offset=(page - 1) * 50)
    return render(request, con, user, "list.html", rtype=rtype, cfg=cfg, rows=rows, total=total,
                  page=page, pages=max((total + 49) // 50, 1), params=params,
                  columns=config.COLUMNS[rtype])


# ---------------------------------------------------------------- records

def form_choices(con, rtype: str) -> dict:
    users = con.execute("SELECT id, name FROM users WHERE active = 1 ORDER BY name").fetchall()
    agents = con.execute("SELECT id, name FROM users WHERE active = 1 AND role != 'requester' "
                         "ORDER BY name").fetchall()
    problems = con.execute("SELECT id, number || ' - ' || short_description FROM records "
                           "WHERE type = 'problem' AND state NOT IN ('Closed') "
                           "ORDER BY number DESC").fetchall()
    as_pairs = lambda rows: [(r[0], r[1]) for r in rows]  # noqa: E731
    same = lambda values: [(v, v) for v in values]  # noqa: E731
    return {
        "users": as_pairs(users),
        "agents": as_pairs(agents),
        "groups": as_pairs(con.execute("SELECT id, name FROM groups ORDER BY name")),
        "problems": as_pairs(problems),
        "states": same(TYPES[rtype]["states"]),
        "categories": same(config.CATEGORY_GROUPS),
        "impact": list(config.IMPACT.items()),
        "urgency": list(config.URGENCY.items()),
        "resolution": same(config.RESOLUTION_CODES),
        "change_types": same(config.CHANGE_TYPES),
        "risks": same(config.RISKS),
    }


def record_context(con, rec: dict) -> dict:
    journal = con.execute(
        "SELECT j.*, u.name AS author FROM journal j LEFT JOIN users u ON u.id = j.author_id "
        "WHERE j.record_id = ? ORDER BY j.created_at DESC, j.id DESC", (rec["id"],)).fetchall()
    approvals = con.execute(
        "SELECT a.*, u.name AS approver FROM approvals a JOIN users u ON u.id = a.approver_id "
        "WHERE a.record_id = ? ORDER BY a.id", (rec["id"],)).fetchall()
    incidents = con.execute(
        "SELECT number, short_description, state, priority FROM records "
        "WHERE problem_id = ? ORDER BY number", (rec["id"],)).fetchall()
    item = con.execute("SELECT name FROM catalog_items WHERE id = ?",
                       (rec["catalog_item_id"],)).fetchone()
    shown = {**rec, "catalog_item": item[0] if item else None, "approval": rec["approval"].title()}
    return {"rec": rec, "shown": shown, "journal": journal, "approvals": approvals,
            "incidents": incidents, "cfg": TYPES[rec["type"]], "fields": config.FIELDS[rec["type"]],
            "choices": form_choices(con, rec["type"]), "sla": services.sla_info(rec)}


@app.get("/new/{rtype}")
def new_form(request: Request, rtype: str, user=Depends(agent_user), con=Depends(get_con)):
    cfg = check_type(rtype)
    if rtype == "request":
        return RedirectResponse("/portal/catalog", 303)
    blank = {f["name"]: None for f in config.FIELDS[rtype]}
    blank.update(number="(assigned on save)", state=cfg["states"][0], impact=3, urgency=3,
                 caller_id=user["id"], approval="Not requested")
    if rtype == "change":
        blank["change_type"] = "Normal"
    return render(request, con, user, "new.html", rtype=rtype, cfg=cfg, shown=blank,
                  fields=config.FIELDS[rtype], choices=form_choices(con, rtype), errors=[])


@app.post("/new/{rtype}")
async def create(request: Request, rtype: str, user=Depends(agent_user), con=Depends(get_con)):
    cfg = check_type(rtype)
    data = dict(await request.form())
    data.pop("state", None)
    try:
        rec = services.create_record(con, rtype, data, actor_id=user["id"])
    except ValidationError as e:
        shown = {f["name"]: data.get(f["name"]) for f in config.FIELDS[rtype]}
        shown.update(number="(assigned on save)", state=cfg["states"][0],
                     approval="Not requested")
        return render(request, con, user, "new.html", rtype=rtype, cfg=cfg, shown=shown,
                      fields=config.FIELDS[rtype], choices=form_choices(con, rtype),
                      errors=e.errors)
    flash(request, f"{rec['number']} created.")
    return done(con, f"/r/{rec['number']}")


@app.get("/r/{number}")
def record_view(request: Request, number: str, user=Depends(agent_user), con=Depends(get_con)):
    rec = load_record(con, number)
    return render(request, con, user, "record.html", errors=[], **record_context(con, rec))


@app.post("/r/{number}")
async def record_save(request: Request, number: str, user=Depends(agent_user),
                      con=Depends(get_con)):
    rec = load_record(con, number)
    form = dict(await request.form())
    note = (form.pop("journal_body", "") or "").strip()
    kind = form.pop("journal_kind", "comment")
    try:
        services.update_record(con, rec, form, actor_id=user["id"])
    except ValidationError as e:
        ctx = record_context(con, rec)
        ctx["shown"] = {**ctx["shown"], **{k: v for k, v in form.items() if v != ""}}
        return render(request, con, user, "record.html", errors=e.errors, **ctx)
    if note:
        services.add_journal(con, rec["id"], user["id"],
                             "work_note" if kind == "work_note" else "comment", note)
    flash(request, f"{number} saved.")
    return done(con, f"/r/{number}")


@app.post("/r/{number}/assign-to-me")
def assign_to_me(request: Request, number: str, user=Depends(agent_user), con=Depends(get_con)):
    rec = load_record(con, number)
    services.update_record(con, rec, {"assigned_to_id": user["id"]}, actor_id=user["id"])
    flash(request, f"{number} assigned to you.")
    return done(con, f"/r/{number}")


# ---------------------------------------------------------------- approvals

@app.get("/approvals")
def approvals_view(request: Request, user=Depends(current_user), con=Depends(get_con)):
    rows = con.execute("""
        SELECT a.*, r.number, r.type, r.short_description, r.description, r.opened_at,
               c.name AS caller_name
        FROM approvals a JOIN records r ON r.id = a.record_id
        LEFT JOIN users c ON c.id = r.caller_id
        WHERE a.approver_id = ? ORDER BY a.state = 'requested' DESC, a.created_at DESC
        LIMIT 100""", (user["id"],)).fetchall()
    layout = "base.html" if user["role"] != "requester" else "portal/base.html"
    return render(request, con, user, "approvals.html", rows=rows, layout=layout)


@app.post("/approvals/{approval_id}")
async def approvals_decide(request: Request, approval_id: int, user=Depends(current_user),
                           con=Depends(get_con)):
    form = await request.form()
    try:
        rec = services.decide_approval(con, approval_id, form.get("decision") == "approve", user,
                                       (form.get("comments") or "").strip() or None)
        flash(request, f"{rec['number']} {rec['approval']}.")
    except ValidationError as e:
        flash(request, " ".join(e.errors), "error")
    return done(con, "/approvals")


# ---------------------------------------------------------------- knowledge

def kb_search(con, q: str | None, category: str | None = None):
    where, args = ["published = 1"], []
    if q:
        where.append("(title ILIKE ? OR body ILIKE ? OR number ILIKE ?)")
        args += [f"%{q}%"] * 3
    if category:
        where.append("category = ?")
        args.append(category)
    return con.execute(f"SELECT * FROM kb_articles WHERE {' AND '.join(where)} "
                       f"ORDER BY views DESC, title", args).fetchall()


@app.get("/kb")
def kb_list(request: Request, q: str = "", category: str = "", user=Depends(agent_user),
            con=Depends(get_con)):
    return render(request, con, user, "kb_list.html", articles=kb_search(con, q, category), q=q,
                  category=category, categories=config.KB_CATEGORIES)


@app.get("/kb/new")
def kb_new(request: Request, user=Depends(agent_user), con=Depends(get_con)):
    return render(request, con, user, "kb_edit.html", article=None,
                  categories=config.KB_CATEGORIES)


@app.post("/kb/new")
async def kb_create(request: Request, user=Depends(agent_user), con=Depends(get_con)):
    form = await request.form()
    number = services.create_kb_article(con, form["title"].strip(), form["category"],
                                        form["body"], user["id"])
    flash(request, f"{number} published.")
    return done(con, f"/kb/{number}")


def load_article(con, number: str):
    row = con.execute("SELECT k.*, u.name AS author FROM kb_articles k "
                      "LEFT JOIN users u ON u.id = k.author_id WHERE k.number = ?",
                      (number.upper(),)).fetchone()
    if not row:
        raise HTTPException(404, "Article not found")
    return row


@app.get("/kb/{number}")
def kb_view(request: Request, number: str, user=Depends(agent_user), con=Depends(get_con)):
    con.execute("UPDATE kb_articles SET views = views + 1 WHERE number = ?", (number.upper(),))
    return render(request, con, user, "kb_article.html", article=load_article(con, number))


@app.get("/kb/{number}/edit")
def kb_edit(request: Request, number: str, user=Depends(agent_user), con=Depends(get_con)):
    return render(request, con, user, "kb_edit.html", article=load_article(con, number),
                  categories=config.KB_CATEGORIES)


@app.post("/kb/{number}/edit")
async def kb_update(request: Request, number: str, user=Depends(agent_user),
                    con=Depends(get_con)):
    form = await request.form()
    load_article(con, number)
    con.execute("UPDATE kb_articles SET title = ?, category = ?, body = ?, published = ?, "
                "updated_at = ? WHERE number = ?",
                (form["title"].strip(), form["category"], form["body"],
                 1 if form.get("published") else 0, db.iso(db.now()), number.upper()))
    flash(request, f"{number.upper()} updated.")
    return done(con, f"/kb/{number.upper()}")


# ---------------------------------------------------------------- search

@app.get("/search")
def search(request: Request, q: str = "", user=Depends(agent_user), con=Depends(get_con)):
    q = q.strip()
    if re.fullmatch(r"(?i)(INC|REQ|CHG|PRB)\d{7}", q) and services.get_record_by_number(con, q):
        return RedirectResponse(f"/r/{q.upper()}", 303)
    if re.fullmatch(r"(?i)KB\d{7}", q):
        return RedirectResponse(f"/kb/{q.upper()}", 303)
    rows = list_query(con, user, None, {"q": q, "active": "0"}, limit=50)[0] if q else []
    return render(request, con, user, "search.html", q=q, rows=rows,
                  articles=kb_search(con, q) if q else [])


# ---------------------------------------------------------------- portal

@app.get("/portal")
def portal_home(request: Request, user=Depends(current_user), con=Depends(get_con)):
    tickets = list_query(con, user, None, {"caller": "me", "active": "1"}, limit=5)[0]
    popular = con.execute("SELECT number, title FROM kb_articles WHERE published = 1 "
                          "ORDER BY views DESC LIMIT 5").fetchall()
    return render(request, con, user, "portal/home.html", tickets=tickets, popular=popular)


@app.get("/portal/issue")
def portal_issue_form(request: Request, user=Depends(current_user), con=Depends(get_con)):
    return render(request, con, user, "portal/issue.html", errors=[], form={},
                  categories=config.CATEGORY_GROUPS)


@app.post("/portal/issue")
async def portal_issue(request: Request, user=Depends(current_user), con=Depends(get_con)):
    form = dict(await request.form())
    urgency = {"blocked": 1, "slowed": 2, "minor": 3}.get(form.get("how_bad"), 3)
    try:
        rec = services.create_record(con, "incident", {
            "short_description": form.get("short_description"),
            "description": form.get("description"),
            "category": form.get("category") or "Inquiry / Help",
            "impact": 3, "urgency": urgency, "caller_id": user["id"],
        }, actor_id=user["id"])
    except ValidationError as e:
        return render(request, con, user, "portal/issue.html", errors=e.errors, form=form,
                      categories=config.CATEGORY_GROUPS)
    flash(request, f"Thanks! {rec['number']} has been logged and routed to the right team.")
    return done(con, f"/portal/ticket/{rec['number']}")


@app.get("/portal/catalog")
def portal_catalog(request: Request, user=Depends(current_user), con=Depends(get_con)):
    items = con.execute("SELECT * FROM catalog_items WHERE active = 1 "
                        "ORDER BY category, name").fetchall()
    return render(request, con, user, "portal/catalog.html", items=items)


def load_item(con, item_id: int):
    item = con.execute("SELECT * FROM catalog_items WHERE id = ? AND active = 1",
                       (item_id,)).fetchone()
    if not item:
        raise HTTPException(404, "Catalog item not found")
    return item


@app.get("/portal/catalog/{item_id}")
def portal_item(request: Request, item_id: int, user=Depends(current_user),
                con=Depends(get_con)):
    item = load_item(con, item_id)
    return render(request, con, user, "portal/item.html", item=item,
                  variables=json.loads(item["variables"]), errors=[], form={})


@app.post("/portal/catalog/{item_id}")
async def portal_order(request: Request, item_id: int, user=Depends(current_user),
                       con=Depends(get_con)):
    item = load_item(con, item_id)
    form = dict(await request.form())
    try:
        rec = services.order_catalog_item(con, item_id, form, requester_id=user["id"])
    except ValidationError as e:
        return render(request, con, user, "portal/item.html", item=item,
                      variables=json.loads(item["variables"]), errors=e.errors, form=form)
    msg = "sent for approval" if rec["approval"] == "requested" else "submitted"
    flash(request, f"{rec['number']} {msg}.")
    return done(con, f"/portal/ticket/{rec['number']}")


@app.get("/portal/tickets")
def portal_tickets(request: Request, show: str = "open", user=Depends(current_user),
                   con=Depends(get_con)):
    rows = list_query(con, user, None, {"caller": "me", "active": "1" if show == "open" else "0",
                                        "sort": "opened_at"}, limit=200)[0]
    return render(request, con, user, "portal/tickets.html", rows=rows, show=show)


def load_portal_record(con, user, number):
    rec = load_record(con, number)
    if not can_view(con, user, rec):
        raise HTTPException(404, f"{number} not found")
    return rec


@app.get("/portal/ticket/{number}")
def portal_ticket(request: Request, number: str, user=Depends(current_user),
                  con=Depends(get_con)):
    rec = load_portal_record(con, user, number)
    ctx = record_context(con, rec)
    ctx["journal"] = [j for j in ctx["journal"] if j["kind"] == "comment"]
    assignee = con.execute("SELECT name FROM users WHERE id = ?",
                           (rec["assigned_to_id"],)).fetchone()
    return render(request, con, user, "portal/ticket.html",
                  assignee=assignee[0] if assignee else None, **ctx)


@app.post("/portal/ticket/{number}/comment")
async def portal_comment(request: Request, number: str, user=Depends(current_user),
                         con=Depends(get_con)):
    rec = load_portal_record(con, user, number)
    body = str((await request.form()).get("body", "")).strip()
    if body:
        services.add_journal(con, rec["id"], user["id"], "comment", body)
        if rec["type"] == "incident" and rec["state"] == "Resolved" \
                and rec["caller_id"] == user["id"]:
            services.update_record(con, rec, {"state": "In Progress"}, actor_id=user["id"])
            flash(request, "Your reply reopened this ticket.")
    return done(con, f"/portal/ticket/{number}")


@app.post("/portal/ticket/{number}/close")
def portal_close(request: Request, number: str, user=Depends(current_user),
                 con=Depends(get_con)):
    rec = load_portal_record(con, user, number)
    if rec["type"] == "incident" and rec["state"] == "Resolved" \
            and rec["caller_id"] == user["id"]:
        services.update_record(con, rec, {"state": "Closed"}, actor_id=user["id"])
        flash(request, "Thanks for confirming. Ticket closed.")
    return done(con, f"/portal/ticket/{number}")


@app.get("/portal/kb")
def portal_kb(request: Request, q: str = "", category: str = "", user=Depends(current_user),
              con=Depends(get_con)):
    return render(request, con, user, "portal/kb.html", articles=kb_search(con, q, category),
                  q=q, category=category, categories=config.KB_CATEGORIES)


@app.get("/portal/kb/{number}")
def portal_article(request: Request, number: str, user=Depends(current_user),
                   con=Depends(get_con)):
    article = load_article(con, number)
    if not article["published"]:
        raise HTTPException(404, "Article not found")
    con.execute("UPDATE kb_articles SET views = views + 1 WHERE number = ?", (number.upper(),))
    return render(request, con, user, "portal/article.html", article=article)


# ---------------------------------------------------------------- admin

@app.get("/admin")
def admin_view(request: Request, tab: str = "users", user=Depends(admin_user),
               con=Depends(get_con)):
    users = con.execute("SELECT u.*, m.name AS manager_name FROM users u "
                        "LEFT JOIN users m ON m.id = u.manager_id ORDER BY u.name").fetchall()
    groups = con.execute("""
        SELECT g.*, m.name AS manager_name,
               (SELECT STRING_AGG(u.name, ', ') FROM group_members gm
                JOIN users u ON u.id = gm.user_id WHERE gm.group_id = g.id) AS members
        FROM groups g LEFT JOIN users m ON m.id = g.manager_id ORDER BY g.name""").fetchall()
    memberships = {}
    for gid, uid in con.execute("SELECT group_id, user_id FROM group_members"):
        memberships.setdefault(uid, set()).add(gid)
    items = con.execute("SELECT c.*, g.name AS group_name FROM catalog_items c "
                        "LEFT JOIN groups g ON g.id = c.fulfillment_group_id "
                        "ORDER BY c.name").fetchall()
    return render(request, con, user, "admin.html", tab=tab, users=users, groups=groups,
                  memberships=memberships, items=items)


@app.post("/admin/users")
async def admin_save_user(request: Request, user=Depends(admin_user), con=Depends(get_con)):
    form = await request.form()
    uid = form.get("id")
    role = form.get("role") if form.get("role") in ("requester", "agent", "admin") else "requester"
    manager = int(form["manager_id"]) if form.get("manager_id") else None
    groups = [int(g) for g in form.getlist("groups")]
    if uid:
        uid = int(uid)
        con.execute("UPDATE users SET name = ?, email = ?, role = ?, manager_id = ?, active = ? "
                    "WHERE id = ?", (form["name"].strip(), form.get("email"), role, manager,
                                     1 if form.get("active") else 0, uid))
        if form.get("password"):
            con.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                        (hash_password(form["password"]), uid))
    else:
        username = form["username"].strip().lower()
        if con.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            flash(request, f"Username {username} is taken.", "error")
            return done(con, "/admin?tab=users")
        uid = con.execute(
            "INSERT INTO users (username, name, email, password_hash, role, manager_id) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (username, form["name"].strip(), form.get("email"),
             hash_password(form.get("password") or "password"), role, manager)).fetchone()[0]
    con.execute("DELETE FROM group_members WHERE user_id = ?", (uid,))
    con.executemany("INSERT INTO group_members VALUES (?, ?)", [(g, uid) for g in groups])
    flash(request, "User saved.")
    return done(con, "/admin?tab=users")


@app.post("/admin/groups")
async def admin_save_group(request: Request, user=Depends(admin_user), con=Depends(get_con)):
    form = await request.form()
    manager = int(form["manager_id"]) if form.get("manager_id") else None
    if form.get("id"):
        con.execute("UPDATE groups SET name = ?, description = ?, manager_id = ? WHERE id = ?",
                    (form["name"].strip(), form.get("description"), manager, int(form["id"])))
    else:
        con.execute("INSERT INTO groups (name, description, manager_id) VALUES (?, ?, ?)",
                    (form["name"].strip(), form.get("description"), manager))
    flash(request, "Group saved.")
    return done(con, "/admin?tab=groups")


@app.post("/admin/catalog")
async def admin_save_item(request: Request, user=Depends(admin_user), con=Depends(get_con)):
    form = await request.form()
    try:
        variables = json.loads(form.get("variables") or "[]")
        assert isinstance(variables, list)
    except (ValueError, AssertionError):
        flash(request, "Variables must be a JSON list.", "error")
        return done(con, "/admin?tab=catalog")
    values = (form["name"].strip(), form["category"].strip(), form.get("description"),
              form.get("icon") or "📦",
              int(form["fulfillment_group_id"]) if form.get("fulfillment_group_id") else None,
              1 if form.get("requires_approval") else 0, json.dumps(variables),
              1 if form.get("active") else 0)
    if form.get("id"):
        con.execute("UPDATE catalog_items SET name = ?, category = ?, description = ?, icon = ?, "
                    "fulfillment_group_id = ?, requires_approval = ?, variables = ?, active = ? "
                    "WHERE id = ?", (*values, int(form["id"])))
    else:
        con.execute("INSERT INTO catalog_items (name, category, description, icon, "
                    "fulfillment_group_id, requires_approval, variables, active) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", values)
    flash(request, "Catalog item saved.")
    return done(con, "/admin?tab=catalog")
