"""Operations Assistant: intake form, human review screen, records and audit log.

The AI work itself is done by an n8n workflow that calls a local Ollama model.
This app never sends email or messages. Stakeholder updates stay drafts until a
person approves them, and approval only changes their status in the database.
"""
import hashlib
import json
import os
import re
import secrets
from datetime import date, datetime, timezone

import httpx
from fastapi import BackgroundTasks, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db

N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "http://127.0.0.1:5678/webhook/project-intake")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
APP_CALLBACK_URL = os.environ.get("APP_CALLBACK_URL", "http://127.0.0.1:8000")
PROCESSING_TIMEOUT_S = int(os.environ.get("PROCESSING_TIMEOUT_S", "1800"))

PRIORITIES = ["Low", "Medium", "High", "Critical"]
ITEM_KINDS = ["objective", "requirement", "task", "dependency", "risk", "missing_info"]
KIND_LABELS = {
    "objective": "Project objective", "requirement": "Key requirements", "task": "Tasks",
    "dependency": "Dependencies", "risk": "Potential risks", "missing_info": "Missing information",
}
HERE = os.path.dirname(__file__)

app = FastAPI(title="Operations Assistant", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
templates.env.globals.update(KIND_LABELS=KIND_LABELS, ITEM_KINDS=ITEM_KINDS, PRIORITIES=PRIORITIES)
db.init()


# ---------- helpers ----------

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (s or "").lower())).strip()


def evidence_found(evidence: str, source_text: str) -> bool:
    """A 'stated' claim only counts if its quote really appears in the request."""
    e = _norm(evidence)
    return len(e) >= 8 and e in _norm(source_text)


def request_source_text(r) -> str:
    return " \n ".join(str(r[k] or "") for k in
                       ("project_name", "department", "description", "priority", "requested_date",
                        "stakeholders", "business_objective"))


def validate_request(f: dict) -> dict:
    errors = {}
    if not 3 <= len(f["project_name"]) <= 120:
        errors["project_name"] = "Enter a project name between 3 and 120 characters."
    if not 2 <= len(f["department"]) <= 80:
        errors["department"] = "Enter the business department."
    if not 20 <= len(f["description"]) <= 5000:
        errors["description"] = "Describe the request in 20 to 5,000 characters."
    if f["priority"] not in PRIORITIES:
        errors["priority"] = "Choose a priority from the list."
    if f["requested_date"]:
        try:
            d = date.fromisoformat(f["requested_date"])
            if d < date.today():
                errors["requested_date"] = "The requested completion date is in the past."
        except ValueError:
            errors["requested_date"] = "Enter the date as YYYY-MM-DD."
    if len(f["stakeholders"]) > 500:
        errors["stakeholders"] = "Keep stakeholders under 500 characters."
    if len(f["business_objective"]) > 1000:
        errors["business_objective"] = "Keep the business objective under 1,000 characters."
    if not 2 <= len(f["submitted_by"]) <= 80:
        errors["submitted_by"] = "Enter your name."
    return errors


def system_checks(f: dict) -> list[str]:
    """Deterministic checks. These do not depend on the AI model."""
    out = []
    if not f["requested_date"]:
        out.append("No requested completion date was supplied.")
    else:
        days = (date.fromisoformat(f["requested_date"]) - date.today()).days
        if days < 14:
            out.append(f"The requested completion date is only {days} day(s) away. Confirm it is realistic.")
    if not f["stakeholders"]:
        out.append("No stakeholders were listed.")
    if not f["business_objective"]:
        out.append("No business objective was supplied.")
    return out


def get_request(con, rid: int):
    r = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    if not r:
        raise HTTPException(404, "Request not found")
    return r


def mark_timeouts(con):
    rows = con.execute("SELECT id, processing_started_at FROM requests WHERE status='processing'").fetchall()
    for r in rows:
        started = datetime.fromisoformat(r["processing_started_at"])
        if (datetime.now(timezone.utc) - started).total_seconds() > PROCESSING_TIMEOUT_S:
            con.execute("UPDATE requests SET status='failed', error=? WHERE id=?",
                        ("The AI workflow did not answer in time. Check that n8n and Ollama are running, then retry.", r["id"]))
            db.audit(con, "system", "ai_run_timed_out", r["id"])


def refresh_status(con, rid: int):
    pend_items = con.execute("SELECT COUNT(*) FROM items WHERE request_id=? AND status='pending'", (rid,)).fetchone()[0]
    pend_docs = con.execute("SELECT COUNT(*) FROM documents WHERE request_id=? AND status='draft'", (rid,)).fetchone()[0]
    r = get_request(con, rid)
    if r["status"] in ("needs_review", "reviewed"):
        new = "reviewed" if pend_items + pend_docs == 0 else "needs_review"
        if new != r["status"]:
            con.execute("UPDATE requests SET status=? WHERE id=?", (new, rid))
            if new == "reviewed":
                db.audit(con, "system", "review_completed", rid)


def trigger_workflow(rid: int):
    """Hand the request to n8n. Runs after the HTTP response has been sent."""
    token = secrets.token_urlsafe(24)
    with db.connect() as con:
        r = get_request(con, rid)
        con.execute("UPDATE requests SET status='processing', error=NULL, model=?, callback_token=?, processing_started_at=? WHERE id=?",
                    (OLLAMA_MODEL, hashlib.sha256(token.encode()).hexdigest(), db.now(), rid))
        db.audit(con, "system", "ai_run_started", rid, detail=f"model={OLLAMA_MODEL}")
        payload = {
            "request_id": rid, "callback_token": token, "callback_url": APP_CALLBACK_URL,
            "ollama_url": OLLAMA_URL, "model": OLLAMA_MODEL, "today": date.today().isoformat(),
            "request": {k: r[k] or "" for k in ("project_name", "department", "description", "priority",
                                               "requested_date", "stakeholders", "business_objective")},
        }
    try:
        resp = httpx.post(N8N_WEBHOOK_URL, json=payload, timeout=20)
        resp.raise_for_status()
    except Exception as e:  # n8n down, workflow inactive, etc.
        with db.connect() as con:
            con.execute("UPDATE requests SET status='failed', error=? WHERE id=?",
                        (f"Could not reach the n8n workflow ({type(e).__name__}). Start n8n and retry.", rid))
            db.audit(con, "system", "ai_run_failed", rid, detail=str(e)[:300])


def check_callback(con, rid: int, token: str | None):
    r = get_request(con, rid)
    if not token or not r["callback_token"] or \
            not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), r["callback_token"]):
        raise HTTPException(403, "Invalid callback token")
    if r["status"] != "processing":
        raise HTTPException(409, "Request is not waiting for an AI result")
    return r


def render_summary(s: dict) -> str:
    def bullets(xs):
        return "\n".join(f"- {x}" for x in xs if str(x).strip()) or "- None identified"
    return (f"EXECUTIVE SUMMARY\n{s.get('executive_summary', '').strip()}\n\n"
            f"OBJECTIVES\n{bullets(s.get('objectives', []))}\n\n"
            f"KEY MILESTONES\n{bullets(s.get('key_milestones', []))}\n\n"
            f"OUTSTANDING ACTIONS\n{bullets(s.get('outstanding_actions', []))}\n\n"
            f"POTENTIAL BLOCKERS\n{bullets(s.get('potential_blockers', []))}\n\n"
            f"STAKEHOLDER RESPONSIBILITIES\n{bullets(s.get('stakeholder_responsibilities', []))}")


def render_update(u: dict) -> str:
    def bullets(xs):
        return "\n".join(f"- {x}" for x in xs if str(x).strip()) or "- None yet"
    return (f"Subject: {u.get('subject', '').strip()}\n\n"
            f"PROJECT PROGRESS\n{u.get('progress', '').strip()}\n\n"
            f"COMPLETED ACTIVITIES\n{bullets(u.get('completed_activities', []))}\n\n"
            f"OUTSTANDING TASKS\n{bullets(u.get('outstanding_tasks', []))}\n\n"
            f"UPCOMING MILESTONES\n{bullets(u.get('upcoming_milestones', []))}\n\n"
            f"RISKS AND REQUIRED DECISIONS\n{bullets(u.get('risks_and_decisions', []))}")


def store_ai_result(con, r, result: dict):
    rid = r["id"]
    src = request_source_text(r)
    ex = result.get("extraction") or {}

    downgraded = 0

    def add(kind, entry, force_suggestion=False):
        nonlocal downgraded
        if isinstance(entry, str):
            entry = {"text": entry}
        text = str(entry.get("text") or "").strip()
        if not text:
            return
        evidence = str(entry.get("evidence") or "").strip()
        claimed = not force_suggestion and entry.get("source") == "stated"
        stated = claimed and evidence_found(evidence, src)
        downgraded += int(claimed and not stated)
        owner = (entry.get("suggested_owner") or None) if kind == "task" else None
        due = (entry.get("due_date") or None) if kind == "task" else None
        con.execute(
            "INSERT INTO items (request_id, kind, provenance, evidence, ai_text, ai_owner, ai_due_date, text, owner, due_date, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?, 'pending')",
            (rid, kind, "stated" if stated else "ai_suggestion", evidence if stated else None,
             text, owner, due, text, owner, due))

    if ex.get("objective"):
        add("objective", ex["objective"])
    for e in ex.get("requirements") or []:
        add("requirement", e)
    for e in ex.get("tasks") or []:
        add("task", e)
    for e in ex.get("dependencies") or []:
        add("dependency", e)
    for e in ex.get("risks") or []:
        add("risk", e, force_suggestion=True)      # a risk is always a judgement, never a stated fact
    for e in ex.get("missing_information") or []:
        add("missing_info", e, force_suggestion=True)

    con.execute("INSERT INTO documents (request_id, kind, ai_content, content, status) VALUES (?,?,?,?, 'draft')",
                (rid, "summary", render_summary(result.get("summary") or {}), render_summary(result.get("summary") or {})))
    con.execute("INSERT INTO documents (request_id, kind, ai_content, content, status) VALUES (?,?,?,?, 'draft')",
                (rid, "stakeholder_update", render_update(result.get("update") or {}), render_update(result.get("update") or {})))
    return downgraded


# ---------- pages ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with db.connect() as con:
        mark_timeouts(con)
        rows = con.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM items i WHERE i.request_id=r.id AND i.status='pending') AS pending"
            " FROM requests r ORDER BY r.id DESC").fetchall()
    return templates.TemplateResponse(request, "index.html", {"rows": rows})


@app.get("/new", response_class=HTMLResponse)
def new_form(request: Request):
    return templates.TemplateResponse(request, "new.html", {"f": {"priority": "Medium"}, "errors": {}})


@app.post("/requests")
def create_request(request: Request, background: BackgroundTasks,
                   project_name: str = Form(""), department: str = Form(""), description: str = Form(""),
                   priority: str = Form(""), requested_date: str = Form(""), stakeholders: str = Form(""),
                   business_objective: str = Form(""), submitted_by: str = Form("")):
    f = {k: v.strip() for k, v in dict(
        project_name=project_name, department=department, description=description, priority=priority,
        requested_date=requested_date, stakeholders=stakeholders, business_objective=business_objective,
        submitted_by=submitted_by).items()}
    errors = validate_request(f)
    if errors:
        return templates.TemplateResponse(request, "new.html", {"f": f, "errors": errors}, status_code=422)
    with db.connect() as con:
        cur = con.execute(
            "INSERT INTO requests (created_at, project_name, department, description, priority, requested_date,"
            " stakeholders, business_objective, submitted_by, status) VALUES (?,?,?,?,?,?,?,?,?, 'submitted')",
            (db.now(), f["project_name"], f["department"], f["description"], f["priority"],
             f["requested_date"] or None, f["stakeholders"] or None, f["business_objective"] or None, f["submitted_by"]))
        rid = cur.lastrowid
        db.audit(con, f["submitted_by"], "request_submitted", rid, detail=f["project_name"])
        for msg in system_checks(f):
            con.execute("INSERT INTO items (request_id, kind, provenance, ai_text, text, status)"
                        " VALUES (?, 'missing_info', 'system_check', ?, ?, 'pending')", (rid, msg, msg))
    background.add_task(trigger_workflow, rid)
    return RedirectResponse(f"/requests/{rid}", status_code=303)


@app.get("/requests/{rid}", response_class=HTMLResponse)
def request_page(request: Request, rid: int):
    with db.connect() as con:
        mark_timeouts(con)
        r = get_request(con, rid)
        items = con.execute("SELECT * FROM items WHERE request_id=? ORDER BY id", (rid,)).fetchall()
        docs = con.execute("SELECT * FROM documents WHERE request_id=? ORDER BY id", (rid,)).fetchall()
    grouped = {k: [i for i in items if i["kind"] == k] for k in ITEM_KINDS}
    return templates.TemplateResponse(request, "request.html", {"r": r, "grouped": grouped, "docs": docs,
                                                               "reviewer": request.cookies.get("reviewer", "")})


@app.post("/requests/{rid}/retry")
def retry(rid: int, background: BackgroundTasks):
    with db.connect() as con:
        r = get_request(con, rid)
        if r["status"] != "failed":
            raise HTTPException(409, "Only failed requests can be retried")
        db.audit(con, "reviewer", "ai_run_retried", rid)
    background.add_task(trigger_workflow, rid)
    return RedirectResponse(f"/requests/{rid}", status_code=303)


@app.post("/items/{iid}/review")
def review_item(iid: int, action: str = Form(...), reviewer: str = Form(""), text: str = Form(""),
                owner: str = Form(""), due_date: str = Form("")):
    reviewer = reviewer.strip()
    if len(reviewer) < 2:
        raise HTTPException(422, "Enter the reviewer's name before approving or rejecting.")
    if action not in ("accept", "reject", "save_edit"):
        raise HTTPException(422, "Unknown action")
    with db.connect() as con:
        it = con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
        if not it:
            raise HTTPException(404, "Item not found")
        if action == "reject":
            con.execute("UPDATE items SET status='rejected', reviewed_by=?, reviewed_at=? WHERE id=?", (reviewer, db.now(), iid))
            db.audit(con, reviewer, "item_rejected", it["request_id"], "item", iid, it["text"][:200])
        else:
            text = text.strip() or it["text"]
            owner = owner.strip() or None
            due_date = due_date.strip() or None
            if due_date:
                try:
                    date.fromisoformat(due_date)
                except ValueError:
                    raise HTTPException(422, "Enter the due date as YYYY-MM-DD.")
            changed = (text != it["ai_text"]) or (owner != it["ai_owner"]) or (due_date != it["ai_due_date"])
            status = "edited" if changed else "accepted"
            con.execute("UPDATE items SET status=?, text=?, owner=?, due_date=?, reviewed_by=?, reviewed_at=? WHERE id=?",
                        (status, text, owner, due_date, reviewer, db.now(), iid))
            detail = json.dumps({"from": {"text": it["ai_text"], "owner": it["ai_owner"], "due": it["ai_due_date"]},
                                 "to": {"text": text, "owner": owner, "due": due_date}}) if changed else it["text"][:200]
            db.audit(con, reviewer, "item_edited_and_accepted" if changed else "item_accepted",
                     it["request_id"], "item", iid, detail)
        refresh_status(con, it["request_id"])
        rid = it["request_id"]
    resp = RedirectResponse(f"/requests/{rid}#item-{iid}", status_code=303)
    resp.set_cookie("reviewer", reviewer, httponly=True, samesite="strict")
    return resp


@app.post("/documents/{did}/review")
def review_document(did: int, action: str = Form(...), reviewer: str = Form(""), content: str = Form("")):
    reviewer = reviewer.strip()
    if len(reviewer) < 2:
        raise HTTPException(422, "Enter the reviewer's name before approving or rejecting.")
    if action not in ("approve", "reject"):
        raise HTTPException(422, "Unknown action")
    with db.connect() as con:
        d = con.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
        if not d:
            raise HTTPException(404, "Document not found")
        if action == "approve":
            content = content.strip() or d["content"]
            edited = int(content != d["ai_content"].strip())
            con.execute("UPDATE documents SET status='approved', content=?, edited=?, reviewed_by=?, reviewed_at=? WHERE id=?",
                        (content, edited, reviewer, db.now(), did))
            db.audit(con, reviewer, f"{d['kind']}_approved" + ("_with_edits" if edited else ""),
                     d["request_id"], "document", did, "Approved as a record. Nothing was sent.")
        else:
            con.execute("UPDATE documents SET status='rejected', reviewed_by=?, reviewed_at=? WHERE id=?", (reviewer, db.now(), did))
            db.audit(con, reviewer, f"{d['kind']}_rejected", d["request_id"], "document", did)
        refresh_status(con, d["request_id"])
        rid = d["request_id"]
    resp = RedirectResponse(f"/requests/{rid}#doc-{did}", status_code=303)
    resp.set_cookie("reviewer", reviewer, httponly=True, samesite="strict")
    return resp


@app.get("/requests/{rid}/record", response_class=HTMLResponse)
def record_page(request: Request, rid: int):
    """The confirmed project record: only what a human accepted."""
    with db.connect() as con:
        r = get_request(con, rid)
        items = con.execute("SELECT * FROM items WHERE request_id=? AND status IN ('accepted','edited') ORDER BY id", (rid,)).fetchall()
        docs = con.execute("SELECT * FROM documents WHERE request_id=? AND status='approved' ORDER BY id", (rid,)).fetchall()
        counts = con.execute("SELECT status, COUNT(*) c FROM items WHERE request_id=? GROUP BY status", (rid,)).fetchall()
    grouped = {k: [i for i in items if i["kind"] == k] for k in ITEM_KINDS}
    return templates.TemplateResponse(request, "record.html", {"r": r, "grouped": grouped, "docs": docs,
                                                              "counts": {c["status"]: c["c"] for c in counts}})


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, request_id: int | None = None):
    with db.connect() as con:
        if request_id:
            rows = con.execute("SELECT * FROM audit_log WHERE request_id=? ORDER BY id DESC", (request_id,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 500").fetchall()
    return templates.TemplateResponse(request, "audit.html", {"rows": rows, "request_id": request_id})


# ---------- read-only JSON, used by the evaluation script ----------

@app.get("/api/requests/{rid}")
def api_request(rid: int):
    with db.connect() as con:
        mark_timeouts(con)
        r = dict(get_request(con, rid))
        r.pop("callback_token", None)
        r["items"] = [dict(i) for i in con.execute("SELECT * FROM items WHERE request_id=? ORDER BY id", (rid,))]
        r["documents"] = [dict(d) for d in con.execute("SELECT * FROM documents WHERE request_id=? ORDER BY id", (rid,))]
    return r


@app.get("/api/review-stats")
def api_review_stats():
    """Human correction rate: how often reviewers changed or rejected what the AI proposed."""
    with db.connect() as con:
        items = {r["status"]: r["c"] for r in con.execute(
            "SELECT status, COUNT(*) c FROM items WHERE provenance!='system_check' GROUP BY status")}
        docs = {(r["status"], r["edited"]): r["c"] for r in con.execute(
            "SELECT status, edited, COUNT(*) c FROM documents GROUP BY status, edited")}
    reviewed = sum(items.get(k, 0) for k in ("accepted", "edited", "rejected"))
    return {"items": items, "items_reviewed": reviewed,
            "item_correction_rate": round((items.get("edited", 0) + items.get("rejected", 0)) / reviewed, 3) if reviewed else None,
            "documents": {f"{k[0]}{'_edited' if k[1] else ''}": v for k, v in docs.items()}}


# ---------- callbacks from the n8n workflow ----------

@app.post("/api/ai-result")
async def ai_result(request: Request, x_callback_token: str | None = Header(None)):
    try:
        body = await request.json()
        rid = int(body["request_id"])
    except Exception:
        raise HTTPException(422, "Body must be JSON with a request_id")
    with db.connect() as con:
        r = check_callback(con, rid, x_callback_token)
        secs = (datetime.now(timezone.utc) - datetime.fromisoformat(r["processing_started_at"])).total_seconds()
        try:
            downgraded = store_ai_result(con, r, body.get("result") or {})
        except Exception as e:
            con.rollback()
            con.execute("UPDATE requests SET status='failed', error=?, callback_token=NULL WHERE id=?",
                        ("The AI returned output that could not be read. Retry the request.", rid))
            db.audit(con, "system", "ai_run_failed", rid, detail=f"unreadable output: {e}"[:300])
            return {"ok": False}
        n = con.execute("SELECT COUNT(*) FROM items WHERE request_id=? AND provenance!='system_check'", (rid,)).fetchone()[0]
        con.execute("UPDATE requests SET status='needs_review', processing_seconds=?, downgraded_claims=?, callback_token=NULL WHERE id=?",
                    (round(secs, 1), downgraded, rid))
        db.audit(con, "ai-workflow", "ai_run_completed", rid,
                 detail=f"{n} items proposed in {secs:.0f}s, all pending human review; "
                        f"{downgraded} 'stated' claim(s) downgraded because the quote was not in the request")
    return {"ok": True}


@app.post("/api/ai-error")
async def ai_error(request: Request, x_callback_token: str | None = Header(None)):
    try:
        body = await request.json()
        rid = int(body["request_id"])
    except Exception:
        raise HTTPException(422, "Body must be JSON with a request_id")
    with db.connect() as con:
        check_callback(con, rid, x_callback_token)
        con.execute("UPDATE requests SET status='failed', error=?, callback_token=NULL WHERE id=?",
                    ("The AI step failed. Check that Ollama is running and the model is downloaded, then retry.", rid))
        db.audit(con, "ai-workflow", "ai_run_failed", rid, detail=str(body.get("error", ""))[:300])
    return {"ok": True}
