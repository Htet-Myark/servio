# Servio

A self-hosted IT service management app: service desk, self-service portal, approvals and
knowledge base. Python (FastAPI + Jinja2) with Postgres (Supabase), no JS framework.

## Run it

Servio stores its data in Postgres (Supabase), in a `servio` schema that Supabase's public
Data API doesn't expose.

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # then set DATABASE_URL to your Supabase Session pooler string
.venv\Scripts\python -m uvicorn app.main:app --port 8700 --reload
```

Open http://127.0.0.1:8700. The first start creates the tables and loads demo data.
`python -m app.db reset --yes` wipes everything and reloads the demo data.
Change the demo passwords if anyone else can reach the app.

| Login | Password | Role |
|-------|----------|------|
| `admin` | `admin` | Admin, CAB approver |
| `alex` | `password` | Agent: Service Desk, Hardware, CAB |
| `priya` | `password` | Agent: Network, Database |
| `sam` | `password` | Agent: Application Support |
| `jamie`, `rin` | `password` | Employees (manager: Morgan) |
| `morgan` | `password` | Employee and manager, approves requests |

Tests: `.venv\Scripts\python -m pytest -q`. They run in a separate `servio_test` schema
(wiped on every test) on `TEST_DATABASE_URL`, or on `DATABASE_URL` if that isn't set.

## What's in it

**Agent workspace** (`/dashboard`)
- Dashboard: my work, unassigned work in my groups, critical incidents, SLA breaches, charts
- List views for incidents, requests, changes and problems, with filters, sorting and paging
- Record form with an activity stream: customer-visible comments, internal work notes, and an
  automatic audit trail of field changes
- Global search; typing a number (e.g. `INC0001004`) jumps to that record

**Incidents**
- Priority = impact × urgency (1 Critical … 5 Planning), previewed live on the form
- Resolution SLA by priority (P1 4h, P2 8h, P3 24h, P4 3d, P5 5d), with on-track, at-risk and breached states
- Category routes to an assignment group automatically
- Assigning moves New to In Progress
- Resolving requires a resolution code and notes
- A caller reply on a resolved incident reopens it; the caller can also confirm and close

**Requests and service catalog**
- Catalog items have their own form fields (configured as JSON in Admin)
- Items that need approval go to the requester's manager; once approved they route to the
  fulfillment group and the SLA starts

**Changes**
- Normal, Standard (pre-approved) and Emergency
- Moving a change to *Authorize* asks the Change Advisory Board for approval; it can't be
  scheduled until approved

**Problems**
- Link incidents to a problem; resolving the problem resolves all linked open incidents

**Self-service portal** (`/portal`): report an issue, order from the catalog, search
knowledge, follow your tickets and reply to them.

**Admin** (`/admin`): users, roles, managers, group membership, groups, catalog items.

## Layout

```
app/
  config.py    record types, states, priorities, SLAs, form and list layouts
  services.py  business rules: numbering, SLA, transitions, approvals, audit
  db.py        schema and demo seed
  main.py      routes
  templates/   workspace + portal pages
tests/         end-to-end workflow tests
```

To add a field, add a column in `db.py` (and `services.COLUMNS`), then add it to `FIELDS` in
`config.py`. The form renders it automatically.

## Not production-ready yet

No CSRF tokens (the session cookie is SameSite=Lax), no email notifications, and the SLA clock
doesn't pause while a ticket is on hold.
