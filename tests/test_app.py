import os
import re

import pytest
from fastapi.testclient import TestClient

from app import db, services
from app.main import app


TEST_SCHEMA = "servio_test"


@pytest.fixture
def client(monkeypatch):
    # Tests wipe their tables, so they run in a separate schema and never touch `servio`.
    # TEST_DATABASE_URL can point them at another database; default is DATABASE_URL.
    url = os.environ.get("TEST_DATABASE_URL") or db.DATABASE_URL
    if not url:
        pytest.skip("Set DATABASE_URL or TEST_DATABASE_URL to run the tests")
    db.close_pool()
    monkeypatch.setattr(db, "DATABASE_URL", url)
    monkeypatch.setattr(db, "PG_SCHEMA", TEST_SCHEMA)
    with db.transaction() as con:
        for table in db.TABLES:
            con.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    with TestClient(app) as c:
        yield c
    db.close_pool()


def login(client, username, password="password"):
    client.cookies.clear()
    r = client.post("/login", data={"username": username, "password": password},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def record(number):
    con = db.connect()
    try:
        return services.get_record_by_number(con, number)
    finally:
        con.close()


def number_from(response):
    return re.search(r"/(?:r|portal/ticket)/([A-Z]{3}\d{7})", str(response.url)).group(1)


def test_login_required_and_bad_password(client):
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = client.post("/login", data={"username": "alex", "password": "nope"})
    assert "Wrong username or password" in r.text


def test_requester_portal_issue_is_routed_and_prioritized(client):
    login(client, "jamie")
    assert client.get("/dashboard", follow_redirects=False).headers["location"] == "/portal"
    r = client.post("/portal/issue", data={"short_description": "Monitor flickers",
                                           "category": "Hardware", "how_bad": "blocked"})
    rec = record(number_from(r))
    assert rec["state"] == "New"
    assert rec["urgency"] == 1 and rec["priority"] == 3  # impact 3 x urgency 1
    assert rec["sla_due"]
    con = db.connect()
    assert con.execute("SELECT name FROM groups WHERE id = ?",
                       (rec["assignment_group_id"],)).fetchone()[0] == "Hardware"
    con.close()


def test_resolve_requires_resolution_and_caller_reply_reopens(client):
    login(client, "alex")
    num = number_from(client.post("/new/incident", data={
        "short_description": "Outlook crashes", "category": "Software", "impact": "2",
        "urgency": "2", "caller_id": "6"}))  # jamie
    assert record(num)["priority"] == 3

    r = client.post(f"/r/{num}", data={"state": "Resolved"})
    assert "Resolution code and resolution notes are required" in r.text
    assert record(num)["state"] == "New"

    client.post(f"/r/{num}", data={"state": "Resolved", "resolution_code": "Solved (Workaround)",
                                   "resolution_notes": "Cleared the cache",
                                   "journal_body": "Should be fixed now", "journal_kind": "comment"})
    assert record(num)["state"] == "Resolved"
    assert record(num)["resolved_at"]

    login(client, "jamie")
    page = client.get(f"/portal/ticket/{num}").text
    assert "Should be fixed now" in page and "We think this is fixed" in page
    client.post(f"/portal/ticket/{num}/comment", data={"body": "Still crashing"})
    assert record(num)["state"] == "In Progress"
    assert record(num)["resolved_at"] is None


def test_work_notes_hidden_from_requester(client):
    login(client, "alex")
    client.post("/r/INC0001001", data={"journal_body": "secret internal note",
                                       "journal_kind": "work_note"})
    login(client, "jamie")
    assert "secret internal note" not in client.get("/portal/ticket/INC0001001").text


def test_catalog_request_manager_approval(client):
    login(client, "jamie")
    r = client.post("/portal/catalog/2", data={"application": "Figma", "details": "Design work"})
    num = number_from(r)
    assert record(num)["state"] == "Pending Approval"
    assert record(num)["approval"] == "requested"

    con = db.connect()
    appr = con.execute("SELECT a.id, u.username FROM approvals a JOIN users u "
                       "ON u.id = a.approver_id WHERE a.record_id = ?",
                       (record(num)["id"],)).fetchone()
    con.close()
    assert appr["username"] == "morgan"

    client.post(f"/approvals/{appr['id']}", data={"decision": "approve"})  # jamie can't
    assert record(num)["approval"] == "requested"

    login(client, "morgan")
    assert num in client.get("/approvals").text
    client.post(f"/approvals/{appr['id']}", data={"decision": "approve", "comments": "OK"})
    rec = record(num)
    assert rec["state"] == "Open" and rec["approval"] == "approved"
    assert rec["assignment_group_id"] and rec["sla_due"]


def test_catalog_item_without_approval_goes_straight_to_fulfillment(client):
    login(client, "rin")
    num = number_from(client.post("/portal/catalog/3", data={"system": "Email"}))
    assert record(num)["state"] == "Open"
    assert record(num)["approval"] == "not requested"


def test_change_needs_cab_approval(client):
    login(client, "priya")
    num = number_from(client.post("/new/change", data={
        "short_description": "Upgrade core switch", "change_type": "Normal", "risk": "High",
        "impact": "1", "urgency": "2"}))
    r = client.post(f"/r/{num}", data={"state": "Scheduled"})
    assert "must be approved" in r.text

    client.post(f"/r/{num}", data={"state": "Authorize"})
    assert record(num)["approval"] == "requested"

    login(client, "admin", "admin")
    con = db.connect()
    appr_id = con.execute("SELECT a.id FROM approvals a JOIN users u ON u.id = a.approver_id "
                          "WHERE a.record_id = ? AND u.username = 'admin'",
                          (record(num)["id"],)).fetchone()[0]
    con.close()
    client.post(f"/approvals/{appr_id}", data={"decision": "approve"})
    assert record(num)["state"] == "Scheduled"


def test_resolving_problem_resolves_linked_incidents(client):
    login(client, "priya")
    prb = record("PRB0001001")
    linked = ["INC0001006", "INC0001009"]
    assert all(record(n)["problem_id"] == prb["id"] for n in linked)
    client.post("/r/PRB0001001", data={"state": "Resolved", "root_cause": "Bug in v5.2"})
    for n in linked:
        rec = record(n)
        assert rec["state"] == "Resolved"
        assert "PRB0001001" in rec["resolution_notes"]


def test_requester_cannot_see_other_peoples_tickets(client):
    login(client, "rin")
    assert client.get("/portal/ticket/INC0001001").status_code == 404  # jamie's
    assert client.get("/r/INC0001001", follow_redirects=False).status_code == 303


def test_pages_render(client):
    login(client, "admin", "admin")
    pages = ["/dashboard", "/list/incident", "/list/request", "/list/change", "/list/problem",
             "/list/incident?assigned=me&sort=priority&dir=asc", "/r/INC0001001",
             "/r/CHG0001001", "/r/PRB0001001", "/r/REQ0001001", "/new/incident", "/new/change",
             "/new/problem", "/approvals", "/kb", "/kb/KB0001001", "/kb/new", "/search?q=vpn",
             "/admin", "/admin?tab=groups", "/admin?tab=catalog", "/portal", "/portal/catalog",
             "/portal/catalog/1", "/portal/tickets?show=all", "/portal/kb?q=printer",
             "/portal/kb/KB0001002", "/portal/issue"]
    for p in pages:
        r = client.get(p)
        assert r.status_code == 200, (p, r.status_code, r.text[:500])
    assert client.get("/search?q=inc0001001", follow_redirects=False).headers["location"] \
        == "/r/INC0001001"
