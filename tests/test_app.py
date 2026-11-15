"""Tests for the review app. The n8n workflow is replaced by a stub so these run without n8n or Ollama."""
import os, sqlite3, tempfile
from datetime import date, timedelta

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
import pytest
from fastapi.testclient import TestClient
from app import db, main

TOKENS = {}


def fake_trigger(rid):
    """Stands in for n8n: marks the request as processing and remembers a callback token."""
    import hashlib
    with db.connect() as con:
        con.execute("UPDATE requests SET status='processing', model='stub', callback_token=?, processing_started_at=? WHERE id=?",
                    (hashlib.sha256(b"tok").hexdigest(), db.now(), rid))
    TOKENS[rid] = "tok"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(main, "trigger_workflow", fake_trigger)
    return TestClient(main.app, follow_redirects=False)


FUTURE = (date.today() + timedelta(days=90)).isoformat()
VALID = dict(project_name="CRM migration", department="Sales", priority="High", requested_date=FUTURE,
             description="Move the sales team to the new CRM. Finance needs the invoicing export to keep working.",
             stakeholders="Anna Keller", business_objective="", submitted_by="Tester")
RESULT = {"extraction": {
    "objective": {"text": "Move sales to the new CRM", "source": "stated", "evidence": "Move the sales team to the new CRM"},
    "requirements": [{"text": "Keep invoicing export", "source": "stated", "evidence": "Finance needs the invoicing export to keep working"}],
    "tasks": [{"text": "Plan the migration", "source": "stated", "evidence": "This quote is not in the request at all",
               "suggested_owner": "Anna Keller", "due_date": None}],
    "dependencies": [], "risks": [{"text": "Data loss", "source": "stated", "evidence": "Move the sales team to the new CRM"}],
    "missing_information": [{"text": "No budget", "source": "suggested", "evidence": ""}]},
    "summary": {"executive_summary": "Summary.", "objectives": ["A"], "key_milestones": [], "outstanding_actions": [],
                "potential_blockers": [], "stakeholder_responsibilities": []},
    "update": {"subject": "Update", "progress": "Not started.", "completed_activities": [], "outstanding_tasks": ["Plan"],
               "upcoming_milestones": [], "risks_and_decisions": ["Budget"]}}


def submit(client, **over):
    r = client.post("/requests", data={**VALID, **over})
    return r


def new_request(client, **over):
    r = submit(client, **over)
    assert r.status_code == 303
    return int(r.headers["location"].rsplit("/", 1)[1])


def deliver(client, rid, result=RESULT, token="tok"):
    return client.post("/api/ai-result", json={"request_id": rid, "result": result}, headers={"X-Callback-Token": token})


def rows(sql, *a):
    con = sqlite3.connect(os.environ["DB_PATH"]); con.row_factory = sqlite3.Row
    out = con.execute(sql, a).fetchall(); con.close(); return out


# ---- intake ----
def test_valid_request_is_stored_and_audited(client):
    rid = new_request(client)
    assert rows("SELECT status FROM requests WHERE id=?", rid)[0]["status"] == "processing"
    assert rows("SELECT action FROM audit_log WHERE request_id=?", rid)[0]["action"] == "request_submitted"


@pytest.mark.parametrize("over,field", [
    ({"project_name": ""}, "project_name"), ({"department": ""}, "department"),
    ({"description": "too short"}, "description"), ({"priority": "Urgent!!"}, "priority"),
    ({"requested_date": "31/03/2027"}, "requested_date"), ({"requested_date": "2020-01-01"}, "requested_date"),
    ({"submitted_by": ""}, "submitted_by"), ({"description": "x" * 5001}, "description")])
def test_invalid_input_is_rejected(client, over, field):
    before = len(rows("SELECT id FROM requests"))
    r = submit(client, **over)
    assert r.status_code == 422 and f'id="err-{field}"' in r.text
    assert len(rows("SELECT id FROM requests")) == before


def test_html_in_input_is_escaped(client):
    rid = new_request(client, project_name="<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in client.get(f"/requests/{rid}").text


def test_missing_optional_fields_raise_system_checks(client):
    rid = new_request(client, requested_date="", stakeholders="", business_objective="")
    checks = rows("SELECT text, status FROM items WHERE request_id=? AND provenance='system_check'", rid)
    assert len(checks) == 3 and all(c["status"] == "pending" for c in checks)


def test_tight_deadline_is_flagged(client):
    rid = new_request(client, requested_date=(date.today() + timedelta(days=3)).isoformat())
    assert any("realistic" in c["text"] for c in rows("SELECT text FROM items WHERE request_id=?", rid))


# ---- AI result handling ----
def test_ai_output_is_pending_and_absent_from_record(client):
    rid = new_request(client); assert deliver(client, rid).status_code == 200
    ai = rows("SELECT status FROM items WHERE request_id=? AND provenance!='system_check'", rid)
    assert ai and all(i["status"] == "pending" for i in ai)
    assert all(d["status"] == "draft" for d in rows("SELECT status FROM documents WHERE request_id=?", rid))
    assert "Nothing has been confirmed yet" in client.get(f"/requests/{rid}/record").text


def test_stated_claim_without_real_quote_is_downgraded(client):
    rid = new_request(client); deliver(client, rid)
    task = rows("SELECT provenance FROM items WHERE request_id=? AND kind='task'", rid)[0]
    req = rows("SELECT provenance, evidence FROM items WHERE request_id=? AND kind='requirement'", rid)[0]
    assert task["provenance"] == "ai_suggestion"
    assert req["provenance"] == "stated" and req["evidence"]


def test_risks_are_never_recorded_as_stated(client):
    rid = new_request(client); deliver(client, rid)
    assert rows("SELECT provenance FROM items WHERE request_id=? AND kind='risk'", rid)[0]["provenance"] == "ai_suggestion"


def test_callback_requires_the_right_token(client):
    rid = new_request(client)
    assert deliver(client, rid, token="wrong").status_code == 403
    assert client.post("/api/ai-result", json={"request_id": rid, "result": RESULT}).status_code == 403
    assert rows("SELECT COUNT(*) c FROM documents WHERE request_id=?", rid)[0]["c"] == 0


def test_callback_cannot_be_replayed(client):
    rid = new_request(client); deliver(client, rid)
    assert deliver(client, rid).status_code in (403, 409)


def test_malformed_ai_output_fails_cleanly(client):
    rid = new_request(client)
    deliver(client, rid, result={"extraction": {"tasks": "not a list"}, "summary": "x", "update": 3})
    assert rows("SELECT status FROM requests WHERE id=?", rid)[0]["status"] == "failed"
    assert client.get(f"/requests/{rid}").status_code == 200


def test_ai_error_callback_marks_failed_and_retry_works(client):
    rid = new_request(client)
    client.post("/api/ai-error", json={"request_id": rid, "error": "model not found"}, headers={"X-Callback-Token": "tok"})
    assert rows("SELECT status FROM requests WHERE id=?", rid)[0]["status"] == "failed"
    assert client.post(f"/requests/{rid}/retry").status_code == 303
    assert rows("SELECT status FROM requests WHERE id=?", rid)[0]["status"] == "processing"


def test_unreachable_n8n_marks_request_failed(monkeypatch):
    monkeypatch.setattr(main, "N8N_WEBHOOK_URL", "http://127.0.0.1:9/webhook/none")
    c = TestClient(main.app, follow_redirects=False)
    rid = int(c.post("/requests", data=VALID).headers["location"].rsplit("/", 1)[1])
    r = rows("SELECT status, error FROM requests WHERE id=?", rid)[0]
    assert r["status"] == "failed" and "n8n" in r["error"]


# ---- human approval ----
def test_accept_edit_reject_and_record(client):
    rid = new_request(client); deliver(client, rid)
    items = {i["kind"]: i["id"] for i in rows("SELECT id, kind FROM items WHERE request_id=? AND provenance!='system_check'", rid)}
    client.post(f"/items/{items['requirement']}/review", data={"action": "accept", "reviewer": "Rev", "text": "Keep invoicing export"})
    client.post(f"/items/{items['task']}/review", data={"action": "accept", "reviewer": "Rev", "text": "Plan the migration",
                                                       "owner": "Ben Roth", "due_date": FUTURE})
    client.post(f"/items/{items['risk']}/review", data={"action": "reject", "reviewer": "Rev"})
    st = {i["kind"]: i for i in rows("SELECT * FROM items WHERE request_id=? AND provenance!='system_check'", rid)}
    assert st["requirement"]["status"] == "accepted" and st["risk"]["status"] == "rejected"
    assert st["task"]["status"] == "edited" and st["task"]["owner"] == "Ben Roth" and st["task"]["ai_owner"] == "Anna Keller"
    rec = client.get(f"/requests/{rid}/record").text
    assert "Ben Roth" in rec and "Data loss" not in rec and "No budget" not in rec


def test_review_needs_a_reviewer_name(client):
    rid = new_request(client); deliver(client, rid)
    iid = rows("SELECT id FROM items WHERE request_id=?", rid)[0]["id"]
    assert client.post(f"/items/{iid}/review", data={"action": "accept", "reviewer": ""}).status_code == 422
    assert rows("SELECT status FROM items WHERE id=?", iid)[0]["status"] == "pending"


def test_bad_due_date_is_rejected(client):
    rid = new_request(client); deliver(client, rid)
    iid = rows("SELECT id FROM items WHERE request_id=? AND kind='task'", rid)[0]["id"]
    assert client.post(f"/items/{iid}/review", data={"action": "accept", "reviewer": "Rev", "due_date": "soon"}).status_code == 422


def test_stakeholder_update_stays_draft_until_approved(client):
    rid = new_request(client); deliver(client, rid)
    did = rows("SELECT id FROM documents WHERE request_id=? AND kind='stakeholder_update'", rid)[0]["id"]
    assert "Stakeholder update (approved" not in client.get(f"/requests/{rid}/record").text
    client.post(f"/documents/{did}/review", data={"action": "approve", "reviewer": "Rev", "content": "Edited text"})
    d = rows("SELECT * FROM documents WHERE id=?", did)[0]
    assert d["status"] == "approved" and d["edited"] == 1 and d["content"] == "Edited text" and "Not started" in d["ai_content"]


def test_request_becomes_reviewed_when_nothing_is_pending(client):
    rid = new_request(client); deliver(client, rid)
    for i in rows("SELECT id FROM items WHERE request_id=?", rid):
        client.post(f"/items/{i['id']}/review", data={"action": "reject", "reviewer": "Rev"})
    for d in rows("SELECT id FROM documents WHERE request_id=?", rid):
        client.post(f"/documents/{d['id']}/review", data={"action": "reject", "reviewer": "Rev"})
    assert rows("SELECT status FROM requests WHERE id=?", rid)[0]["status"] == "reviewed"


# ---- audit trail ----
def test_every_decision_is_audited(client):
    rid = new_request(client); deliver(client, rid)
    iid = rows("SELECT id FROM items WHERE request_id=? AND kind='task'", rid)[0]["id"]
    client.post(f"/items/{iid}/review", data={"action": "accept", "reviewer": "Rev", "owner": "Ben Roth"})
    actions = [a["action"] for a in rows("SELECT action FROM audit_log WHERE request_id=? ORDER BY id", rid)]
    assert actions[0] == "request_submitted" and "ai_run_completed" in actions and "item_edited_and_accepted" in actions
    assert "Ben Roth" in client.get(f"/audit?request_id={rid}").text


def test_audit_log_cannot_be_changed_or_deleted(client):
    new_request(client)
    con = sqlite3.connect(os.environ["DB_PATH"])
    with pytest.raises(sqlite3.Error):
        con.execute("UPDATE audit_log SET actor='x'")
    with pytest.raises(sqlite3.Error):
        con.execute("DELETE FROM audit_log")
    con.close()


def test_no_endpoint_can_send_messages():
    """The app has no outbound channel except the n8n trigger."""
    src = open(main.__file__).read()
    assert "smtplib" not in src and "sendmail" not in src.lower()
    assert src.count("httpx.post(") == 1


def test_unknown_request_returns_404(client):
    assert client.get("/requests/99999").status_code == 404
