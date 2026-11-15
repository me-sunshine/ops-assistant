"""SQLite storage for the Operations Assistant review app.

Design rules enforced here:
- AI output is stored as *pending* items. Only a human review changes that.
- The audit log is append-only: triggers reject UPDATE and DELETE.
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "data/ops_assistant.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  project_name TEXT NOT NULL,
  department TEXT NOT NULL,
  description TEXT NOT NULL,
  priority TEXT NOT NULL,
  requested_date TEXT,
  stakeholders TEXT,
  business_objective TEXT,
  submitted_by TEXT NOT NULL,
  status TEXT NOT NULL,            -- submitted | processing | needs_review | reviewed | failed
  error TEXT,
  model TEXT,
  callback_token TEXT,
  processing_started_at TEXT,
  processing_seconds REAL,
  downgraded_claims INTEGER      -- 'stated' claims whose quote was not found in the request
);
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER NOT NULL REFERENCES requests(id),
  kind TEXT NOT NULL,              -- objective | requirement | task | dependency | risk | missing_info
  provenance TEXT NOT NULL,        -- stated | ai_suggestion | system_check
  evidence TEXT,                   -- quote from the request, for "stated" items
  ai_text TEXT NOT NULL,           -- what was originally proposed (never edited)
  ai_owner TEXT,
  ai_due_date TEXT,
  text TEXT NOT NULL,              -- current text (may be edited by the reviewer)
  owner TEXT,
  due_date TEXT,
  status TEXT NOT NULL,            -- pending | accepted | edited | rejected
  reviewed_by TEXT,
  reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER NOT NULL REFERENCES requests(id),
  kind TEXT NOT NULL,              -- summary | stakeholder_update
  ai_content TEXT NOT NULL,
  content TEXT NOT NULL,
  status TEXT NOT NULL,            -- draft | approved | rejected
  edited INTEGER NOT NULL DEFAULT 0,
  reviewed_by TEXT,
  reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  request_id INTEGER,
  entity TEXT,
  entity_id INTEGER,
  detail TEXT
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with connect() as con:
        con.executescript(SCHEMA)


def audit(con, actor, action, request_id=None, entity=None, entity_id=None, detail=None):
    con.execute(
        "INSERT INTO audit_log (at, actor, action, request_id, entity, entity_id, detail) VALUES (?,?,?,?,?,?,?)",
        (now(), actor, action, request_id, entity, entity_id, detail),
    )
