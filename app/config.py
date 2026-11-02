"""Domain configuration: record types, states, priorities, SLAs, form layouts."""

TYPES = {
    "incident": {
        "label": "Incident",
        "plural": "Incidents",
        "prefix": "INC",
        "states": ["New", "In Progress", "On Hold", "Resolved", "Closed", "Canceled"],
        "closed": ["Resolved", "Closed", "Canceled"],
    },
    "request": {
        "label": "Request",
        "plural": "Requests",
        "prefix": "REQ",
        "states": ["Pending Approval", "Open", "Work in Progress", "Closed Complete",
                   "Closed Incomplete", "Rejected"],
        "closed": ["Closed Complete", "Closed Incomplete", "Rejected"],
    },
    "change": {
        "label": "Change",
        "plural": "Changes",
        "prefix": "CHG",
        "states": ["New", "Assess", "Authorize", "Scheduled", "Implement", "Review",
                   "Closed", "Canceled"],
        "closed": ["Closed", "Canceled"],
    },
    "problem": {
        "label": "Problem",
        "plural": "Problems",
        "prefix": "PRB",
        "states": ["New", "Assess", "Root Cause Analysis", "Fix in Progress", "Resolved", "Closed"],
        "closed": ["Resolved", "Closed"],
    },
}

IMPACT = {1: "1 - High", 2: "2 - Medium", 3: "3 - Low"}
URGENCY = {1: "1 - High", 2: "2 - Medium", 3: "3 - Low"}
PRIORITY = {1: "1 - Critical", 2: "2 - High", 3: "3 - Moderate", 4: "4 - Low", 5: "5 - Planning"}

# Resolution targets in hours, by priority.
SLA_HOURS = {1: 4, 2: 8, 3: 24, 4: 72, 5: 120}
REQUEST_SLA_HOURS = 72

# Incident category -> default assignment group.
CATEGORY_GROUPS = {
    "Inquiry / Help": "Service Desk",
    "Software": "Application Support",
    "Hardware": "Hardware",
    "Network": "Network",
    "Database": "Database",
}

CHANGE_TYPES = ["Normal", "Standard", "Emergency"]
RISKS = ["High", "Moderate", "Low"]
CAB_GROUP = "Change Advisory Board"
RESOLUTION_CODES = [
    "Solved (Permanently)",
    "Solved (Workaround)",
    "Not Solved (Not Reproducible)",
    "Not Solved (Too Costly)",
    "Closed by Caller",
    "Duplicate",
]
KB_CATEGORIES = ["Getting Started", "Accounts & Access", "Email", "Network & VPN", "Hardware",
                 "Software", "Policies"]


def priority_for(impact: int, urgency: int) -> int:
    """Impact x urgency matrix: (1,1)->1 Critical ... (3,3)->5 Planning."""
    return impact + urgency - 1


def F(name, label, widget="text", choices=None, wide=False):
    return {"name": name, "label": label, "widget": widget, "choices": choices, "wide": wide}


# Record form layouts. `choices` names a list the view supplies (see main.form_choices).
FIELDS = {
    "incident": [
        F("number", "Number", "readonly"),
        F("caller_id", "Caller", "select", "users"),
        F("category", "Category", "select", "categories"),
        F("state", "State", "select", "states"),
        F("impact", "Impact", "select", "impact"),
        F("urgency", "Urgency", "select", "urgency"),
        F("priority", "Priority", "priority"),
        F("assignment_group_id", "Assignment group", "select", "groups"),
        F("assigned_to_id", "Assigned to", "select", "agents"),
        F("problem_id", "Problem", "select", "problems"),
        F("short_description", "Short description", wide=True),
        F("description", "Description", "textarea", wide=True),
        F("resolution_code", "Resolution code", "select", "resolution"),
        F("resolution_notes", "Resolution notes", "textarea", wide=True),
    ],
    "request": [
        F("number", "Number", "readonly"),
        F("caller_id", "Requested for", "select", "users"),
        F("catalog_item", "Item", "readonly"),
        F("state", "State", "select", "states"),
        F("approval", "Approval", "readonly"),
        F("assignment_group_id", "Assignment group", "select", "groups"),
        F("assigned_to_id", "Assigned to", "select", "agents"),
        F("short_description", "Short description", wide=True),
        F("description", "Description", "textarea", wide=True),
    ],
    "change": [
        F("number", "Number", "readonly"),
        F("caller_id", "Requested by", "select", "users"),
        F("change_type", "Type", "select", "change_types"),
        F("state", "State", "select", "states"),
        F("risk", "Risk", "select", "risks"),
        F("approval", "Approval", "readonly"),
        F("impact", "Impact", "select", "impact"),
        F("urgency", "Urgency", "select", "urgency"),
        F("assignment_group_id", "Assignment group", "select", "groups"),
        F("assigned_to_id", "Assigned to", "select", "agents"),
        F("planned_start", "Planned start", "datetime"),
        F("planned_end", "Planned end", "datetime"),
        F("short_description", "Short description", wide=True),
        F("description", "Description", "textarea", wide=True),
        F("implementation_plan", "Implementation plan", "textarea", wide=True),
        F("backout_plan", "Backout plan", "textarea", wide=True),
    ],
    "problem": [
        F("number", "Number", "readonly"),
        F("category", "Category", "select", "categories"),
        F("state", "State", "select", "states"),
        F("priority", "Priority", "priority"),
        F("impact", "Impact", "select", "impact"),
        F("urgency", "Urgency", "select", "urgency"),
        F("assignment_group_id", "Assignment group", "select", "groups"),
        F("assigned_to_id", "Assigned to", "select", "agents"),
        F("short_description", "Short description", wide=True),
        F("description", "Description", "textarea", wide=True),
        F("workaround", "Workaround", "textarea", wide=True),
        F("root_cause", "Root cause", "textarea", wide=True),
    ],
}

# List view columns: (key, label). Keys are columns of the enriched list query.
COLUMNS = {
    "incident": [("number", "Number"), ("short_description", "Short description"),
                 ("caller_name", "Caller"), ("priority", "Priority"), ("state", "State"),
                 ("group_name", "Assignment group"), ("assignee_name", "Assigned to"),
                 ("sla", "SLA"), ("updated_at", "Updated")],
    "request": [("number", "Number"), ("item_name", "Item"), ("caller_name", "Requested for"),
                ("state", "State"), ("approval", "Approval"), ("group_name", "Assignment group"),
                ("assignee_name", "Assigned to"), ("sla", "SLA"), ("updated_at", "Updated")],
    "change": [("number", "Number"), ("short_description", "Short description"),
               ("change_type", "Type"), ("risk", "Risk"), ("state", "State"),
               ("approval", "Approval"), ("planned_start", "Planned start"),
               ("assignee_name", "Assigned to")],
    "problem": [("number", "Number"), ("short_description", "Short description"),
                ("priority", "Priority"), ("state", "State"), ("incident_count", "Incidents"),
                ("group_name", "Assignment group"), ("assignee_name", "Assigned to"),
                ("updated_at", "Updated")],
}
