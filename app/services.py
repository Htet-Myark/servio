"""Business rules: numbering, priority, SLAs, state transitions, approvals, audit trail."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .config import (CAB_GROUP, CATEGORY_GROUPS, FIELDS, IMPACT, PRIORITY, REQUEST_SLA_HOURS,
                     SLA_HOURS, TYPES, URGENCY, priority_for)
from .db import iso, now

INT_FIELDS = {"impact", "urgency", "priority", "caller_id", "assigned_to_id",
              "assignment_group_id", "problem_id", "catalog_item_id"}
DATETIME_FIELDS = {"planned_start", "planned_end"}
EDITABLE = {f["name"] for fields in FIELDS.values() for f in fields
            if f["widget"] not in ("readonly", "priority")}
COLUMNS = ["type", "number", "short_description", "description", "state", "impact", "urgency",
           "priority", "category", "caller_id", "opened_by_id", "assignment_group_id",
           "assigned_to_id", "problem_id", "catalog_item_id", "variables", "approval",
           "change_type", "risk", "planned_start", "planned_end", "implementation_plan",
           "backout_plan", "workaround", "root_cause", "resolution_code", "resolution_notes",
           "sla_due", "opened_at", "updated_at", "resolved_at", "closed_at"]


class ValidationError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:  # from a <input type=datetime-local>: the user's local time
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def clean(data: dict) -> dict:
    """Normalize raw form values: '' -> None, ints for id/number fields, UTC datetimes."""
    out = {}
    for key, value in data.items():
        if isinstance(value, str):
            value = value.strip() or None
        if key in INT_FIELDS and value is not None:
            value = int(value)
        if key in DATETIME_FIELDS and value is not None:
            value = iso(parse_dt(value) if isinstance(value, str) else value)
        out[key] = value
    return out


# ---------------------------------------------------------------- lookups

def get_record(con, record_id: int) -> dict | None:
    row = con.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    return dict(row) if row else None


def get_record_by_number(con, number: str) -> dict | None:
    row = con.execute("SELECT * FROM records WHERE number = ?", (number.upper(),)).fetchone()
    return dict(row) if row else None


def next_number(con, prefix: str) -> str:
    con.execute("INSERT INTO counters VALUES (?, 1000) ON CONFLICT (prefix) DO NOTHING",
                (prefix,))
    con.execute("UPDATE counters SET value = value + 1 WHERE prefix = ?", (prefix,))
    value = con.execute("SELECT value FROM counters WHERE prefix = ?", (prefix,)).fetchone()[0]
    return f"{prefix}{value:07d}"


def group_id(con, name: str) -> int | None:
    row = con.execute("SELECT id FROM groups WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


def is_closed(rec: dict) -> bool:
    return rec["state"] in TYPES[rec["type"]]["closed"]


def display_value(con, field: str, value) -> str:
    """Human-readable value for the audit trail."""
    if value is None:
        return "(empty)"
    if field in ("caller_id", "assigned_to_id"):
        row = con.execute("SELECT name FROM users WHERE id = ?", (value,)).fetchone()
        return row[0] if row else str(value)
    if field == "assignment_group_id":
        row = con.execute("SELECT name FROM groups WHERE id = ?", (value,)).fetchone()
        return row[0] if row else str(value)
    if field == "problem_id":
        row = con.execute("SELECT number FROM records WHERE id = ?", (value,)).fetchone()
        return row[0] if row else str(value)
    labels = {"impact": IMPACT, "urgency": URGENCY, "priority": PRIORITY}.get(field)
    if labels:
        return labels.get(value, str(value))
    text = str(value)
    return text if len(text) <= 80 else text[:77] + "..."


# ---------------------------------------------------------------- journal

def add_journal(con, record_id: int, author_id: int | None, kind: str, body: str,
                at: datetime | None = None) -> None:
    con.execute(
        "INSERT INTO journal (record_id, author_id, kind, body, created_at) VALUES (?, ?, ?, ?, ?)",
        (record_id, author_id, kind, body, iso(at or now())),
    )
    con.execute("UPDATE records SET updated_at = ? WHERE id = ?", (iso(at or now()), record_id))


def _set(con, record_id: int, fields: dict) -> None:
    fields = {**fields, "updated_at": iso(now())}
    assignments = ", ".join(f"{k} = ?" for k in fields)
    con.execute(f"UPDATE records SET {assignments} WHERE id = ?", (*fields.values(), record_id))


# ---------------------------------------------------------------- SLA

def sla_due_for(rec: dict, start: datetime) -> str | None:
    if rec["type"] == "incident" and rec.get("priority"):
        return iso(start + timedelta(hours=SLA_HOURS[rec["priority"]]))
    if rec["type"] == "request":
        return iso(start + timedelta(hours=REQUEST_SLA_HOURS))
    return None


def sla_info(rec: dict) -> dict | None:
    """Resolution SLA status: ok / at_risk / breached while open, met / missed once done."""
    due = parse_dt(rec.get("sla_due"))
    if not due or rec["state"] in ("Canceled", "Rejected", "Closed Incomplete"):
        return None
    start = parse_dt(rec["opened_at"])
    end = parse_dt(rec.get("resolved_at") or rec.get("closed_at"))
    if end:
        return {"status": "met", "label": "Met"} if end <= due else \
               {"status": "missed", "label": "Missed"}
    remaining = due - now()
    total = max((due - start).total_seconds(), 1)
    elapsed_pct = min(100, max(0, round(100 * (1 - remaining.total_seconds() / total))))
    if remaining.total_seconds() < 0:
        return {"status": "breached", "label": f"Breached {_human(-remaining)} ago", "pct": 100}
    status = "at_risk" if elapsed_pct >= 75 else "ok"
    return {"status": status, "label": f"{_human(remaining)} left", "pct": elapsed_pct}


def _human(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


# ---------------------------------------------------------------- create / update

def create_record(con, rtype: str, data: dict, actor_id: int,
                  opened_at: datetime | None = None) -> dict:
    cfg = TYPES[rtype]
    data = clean(data)
    t = opened_at or now()
    rec = {col: None for col in COLUMNS}
    rec.update({k: v for k, v in data.items() if k in COLUMNS})
    rec.update(type=rtype, number=next_number(con, cfg["prefix"]), opened_by_id=actor_id,
               opened_at=iso(t), updated_at=iso(t), approval="not requested")
    rec["state"] = rec["state"] or cfg["states"][0]
    rec["caller_id"] = rec["caller_id"] or actor_id
    if not rec["short_description"]:
        raise ValidationError(["Short description is required."])
    if rec["impact"] and rec["urgency"]:
        rec["priority"] = priority_for(rec["impact"], rec["urgency"])
    if rtype == "incident" and not rec["assignment_group_id"] and rec["category"]:
        rec["assignment_group_id"] = group_id(con, CATEGORY_GROUPS.get(rec["category"], ""))
    if rtype == "change" and rec["change_type"] == "Standard":
        rec["approval"] = "approved"  # standard changes are pre-approved
    if rtype == "incident" or (rtype == "request" and rec["state"] == "Open"):
        rec["sla_due"] = sla_due_for(rec, t)

    cols = ", ".join(COLUMNS)
    marks = ", ".join("?" for _ in COLUMNS)
    rid = con.execute(f"INSERT INTO records ({cols}) VALUES ({marks}) RETURNING id",
                      [rec[c] for c in COLUMNS]).fetchone()[0]
    add_journal(con, rid, actor_id, "system", f"{cfg['label']} created", at=t)
    return get_record(con, rid)


def update_record(con, rec: dict, changes: dict, actor_id: int) -> dict:
    """Apply field changes with validation, derived fields, audit trail and side effects."""
    changes = {k: v for k, v in clean(changes).items() if k in EDITABLE}
    new = {**rec, **changes}
    rtype, old_state, state = rec["type"], rec["state"], new["state"]

    if new.get("impact") and new.get("urgency"):
        new["priority"] = priority_for(new["impact"], new["urgency"])
    if rtype == "incident" and new["assigned_to_id"] and not rec["assigned_to_id"] \
            and state == "New":
        new["state"] = state = "In Progress"
    if state not in TYPES[rtype]["states"]:
        raise ValidationError([f"Unknown state '{state}'."])

    errors = []
    if not new["short_description"]:
        errors.append("Short description is required.")
    if state != old_state:
        if rtype == "incident" and state in ("Resolved", "Closed") \
                and not (new["resolution_code"] and new["resolution_notes"]):
            errors.append("Resolution code and resolution notes are required to resolve.")
        if rtype == "change" and state in ("Scheduled", "Implement", "Review", "Closed") \
                and new["approval"] != "approved" and new["change_type"] != "Standard":
            errors.append("This change must be approved before it can be scheduled.")
        if rtype == "request" and rec["approval"] == "requested" \
                and state not in ("Pending Approval", "Closed Incomplete"):
            errors.append("This request is still waiting for approval.")
    if errors:
        raise ValidationError(errors)

    t = now()
    if new["priority"] != rec["priority"] and rtype == "incident":
        new["sla_due"] = sla_due_for(new, parse_dt(rec["opened_at"]))
    if new["change_type"] == "Standard" and rtype == "change":
        new["approval"] = "approved"
    if state != old_state:
        closed_states = TYPES[rtype]["closed"]
        if state == "Resolved":
            new["resolved_at"] = iso(t)
        elif state in closed_states:
            new["closed_at"] = iso(t)
            new["resolved_at"] = new["resolved_at"] or iso(t)
        else:  # reopened
            new["resolved_at"] = new["closed_at"] = None

    diff = {k: new[k] for k in COLUMNS if new[k] != rec[k]}
    if not diff:
        return rec
    labels = {f["name"]: f["label"] for f in FIELDS[rtype]}
    audit = [f"{labels[k]}: {display_value(con, k, rec[k])} → {display_value(con, k, new[k])}"
             for k in diff if k in labels and k not in ("description", "implementation_plan",
                                                        "backout_plan", "resolution_notes")]
    _set(con, rec["id"], diff)
    if audit:
        add_journal(con, rec["id"], actor_id, "system", "\n".join(audit), at=t)

    updated = get_record(con, rec["id"])
    if state != old_state:
        _after_state_change(con, updated, actor_id)
    return get_record(con, rec["id"])


def _after_state_change(con, rec: dict, actor_id: int) -> None:
    if rec["type"] == "change" and rec["state"] == "Authorize" \
            and rec["approval"] not in ("approved", "requested"):
        cab = group_id(con, CAB_GROUP)
        approvers = [r[0] for r in con.execute(
            "SELECT user_id FROM group_members WHERE group_id = ?", (cab,))]
        request_approvals(con, rec, approvers or [_any_admin(con)], actor_id)

    if rec["type"] == "problem" and rec["state"] in ("Resolved", "Closed"):
        linked = con.execute(
            "SELECT * FROM records WHERE problem_id = ? AND type = 'incident'", (rec["id"],))
        for inc in map(dict, linked.fetchall()):
            if not is_closed(inc):
                update_record(con, inc, {
                    "state": "Resolved",
                    "resolution_code": "Solved (Permanently)",
                    "resolution_notes": f"Resolved by {rec['number']}. "
                                        f"{rec['root_cause'] or rec['workaround'] or ''}".strip(),
                }, actor_id)


def _any_admin(con) -> int:
    return con.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id").fetchone()[0]


# ---------------------------------------------------------------- approvals

def request_approvals(con, rec: dict, approver_ids: list[int], actor_id: int) -> None:
    t = iso(now())
    for uid in approver_ids:
        con.execute("INSERT INTO approvals (record_id, approver_id, created_at) VALUES (?, ?, ?)",
                    (rec["id"], uid, t))
    _set(con, rec["id"], {"approval": "requested"})
    names = ", ".join(display_value(con, "caller_id", uid) for uid in approver_ids)
    add_journal(con, rec["id"], actor_id, "system", f"Approval requested from {names}")


def decide_approval(con, approval_id: int, approve: bool, actor: dict,
                    comments: str | None = None) -> dict:
    appr = con.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if not appr or appr["state"] != "requested":
        raise ValidationError(["This approval is no longer pending."])
    if appr["approver_id"] != actor["id"] and actor["role"] != "admin":
        raise ValidationError(["You are not the approver."])

    t = iso(now())
    decision = "approved" if approve else "rejected"
    con.execute("UPDATE approvals SET state = ?, comments = ?, decided_at = ? WHERE id = ?",
                (decision, comments, t, approval_id))
    # First decision wins; the other approvers are released.
    con.execute("UPDATE approvals SET state = 'no longer required', decided_at = ? "
                "WHERE record_id = ? AND state = 'requested'", (t, appr["record_id"]))

    rec = get_record(con, appr["record_id"])
    if rec["type"] == "request":
        if approve:
            item = con.execute("SELECT fulfillment_group_id FROM catalog_items WHERE id = ?",
                               (rec["catalog_item_id"],)).fetchone()
            _set(con, rec["id"], {"approval": "approved", "state": "Open",
                                  "assignment_group_id": item[0] if item else None,
                                  "sla_due": sla_due_for(rec, now())})
        else:
            _set(con, rec["id"], {"approval": "rejected", "state": "Rejected", "closed_at": t})
    elif rec["type"] == "change":
        _set(con, rec["id"], {"approval": decision, "state": "Scheduled" if approve else "New"})

    note = f"{decision.capitalize()} by {actor['name']}"
    add_journal(con, rec["id"], actor["id"], "system", note + (f": {comments}" if comments else ""))
    return get_record(con, rec["id"])


# ---------------------------------------------------------------- catalog

def order_catalog_item(con, item_id: int, values: dict, requester_id: int,
                       opened_at: datetime | None = None) -> dict:
    item = con.execute("SELECT * FROM catalog_items WHERE id = ? AND active = 1",
                       (item_id,)).fetchone()
    if not item:
        raise ValidationError(["That catalog item is not available."])
    variables = json.loads(item["variables"])
    missing = [v["label"] for v in variables if v.get("required") and not values.get(v["name"])]
    if missing:
        raise ValidationError([f"{label} is required." for label in missing])

    lines = [f"{v['label']}: {values.get(v['name']) or '-'}" for v in variables]
    needs_approval = bool(item["requires_approval"])
    rec = create_record(con, "request", {
        "short_description": item["name"],
        "description": "\n".join(lines),
        "caller_id": requester_id,
        "catalog_item_id": item["id"],
        "variables": json.dumps({v["name"]: values.get(v["name"]) for v in variables}),
        "state": "Pending Approval" if needs_approval else "Open",
        "assignment_group_id": None if needs_approval else item["fulfillment_group_id"],
    }, actor_id=requester_id, opened_at=opened_at)

    if needs_approval:
        manager = con.execute("SELECT manager_id FROM users WHERE id = ?",
                              (requester_id,)).fetchone()[0]
        request_approvals(con, rec, [manager or _any_admin(con)], requester_id)
    return get_record(con, rec["id"])


# ---------------------------------------------------------------- knowledge

def create_kb_article(con, title: str, category: str, body: str, author_id: int) -> str:
    number = next_number(con, "KB")
    t = iso(now())
    con.execute("INSERT INTO kb_articles (number, title, category, body, author_id, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (number, title, category, body, author_id, t, t))
    return number
