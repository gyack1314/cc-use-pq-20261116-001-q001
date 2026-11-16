"""管理 SQLite 事务、审计日志与持久化状态。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .contracts import DomainError

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS grids (
    grid_id TEXT PRIMARY KEY, district TEXT NOT NULL, active INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS route_rules (
    category TEXT PRIMARY KEY, department TEXT NOT NULL, priority INTEGER NOT NULL,
    response_hours INTEGER NOT NULL, resolution_hours INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS batches (
    batch_key TEXT PRIMARY KEY, digest TEXT NOT NULL, submitted_at TEXT NOT NULL,
    imported_count INTEGER NOT NULL, sequence_start INTEGER NOT NULL,
    sequence_end INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    case_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, external_id TEXT NOT NULL,
    grid_id TEXT NOT NULL REFERENCES grids(grid_id), district TEXT NOT NULL,
    category TEXT NOT NULL, summary TEXT NOT NULL, resident_hash TEXT NOT NULL,
    priority INTEGER NOT NULL, accepted_at TEXT NOT NULL,
    department TEXT NOT NULL, initial_department TEXT NOT NULL, route_revision INTEGER NOT NULL,
    response_due TEXT NOT NULL, resolution_due TEXT NOT NULL,
    status TEXT NOT NULL, resolution TEXT,
    first_response_at TEXT, last_event_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1, created_sequence INTEGER NOT NULL,
    correction_count INTEGER NOT NULL DEFAULT 0,
    corrected_at TEXT, dispute_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS cases_grid_status ON cases(grid_id,status);
CREATE INDEX IF NOT EXISTS cases_category_status ON cases(category,status);
CREATE INDEX IF NOT EXISTS cases_accepted ON cases(accepted_at);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES cases(case_id),
    event_key TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL, department TEXT, note TEXT, resolution TEXT,
    case_version INTEGER NOT NULL, event_sequence INTEGER NOT NULL UNIQUE,
    digest TEXT NOT NULL, UNIQUE(case_id,event_key), UNIQUE(case_id,case_version)
);
CREATE INDEX IF NOT EXISTS events_case_time ON events(case_id,occurred_at,event_sequence);
CREATE TABLE IF NOT EXISTS journal (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, operation TEXT NOT NULL,
    entity TEXT NOT NULL, entity_id TEXT NOT NULL, payload_digest TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    report_key TEXT PRIMARY KEY, request_digest TEXT NOT NULL,
    as_of TEXT NOT NULL, watermark INTEGER NOT NULL,
    group_by TEXT NOT NULL, district TEXT, category TEXT,
    body_json TEXT NOT NULL, created_at TEXT NOT NULL,
    view TEXT NOT NULL DEFAULT 'as_known',
    needs_review INTEGER NOT NULL DEFAULT 0, review_marked_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS policy_changes (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
    item_key TEXT NOT NULL, revision INTEGER NOT NULL,
    old_json TEXT, new_json TEXT NOT NULL, sequence INTEGER NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS case_corrections (
    correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(case_id),
    correction_key TEXT NOT NULL,
    expected_version INTEGER NOT NULL,
    case_version INTEGER NOT NULL,
    submitted_at TEXT NOT NULL, effective_at TEXT NOT NULL,
    source TEXT NOT NULL,
    old_json TEXT NOT NULL, new_json TEXT NOT NULL,
    route_revision INTEGER NOT NULL,
    response_due TEXT NOT NULL, resolution_due TEXT NOT NULL,
    sequence INTEGER NOT NULL UNIQUE, digest TEXT NOT NULL,
    UNIQUE(case_id,correction_key)
);
CREATE INDEX IF NOT EXISTS corrections_case ON case_corrections(case_id,effective_at);
CREATE TABLE IF NOT EXISTS case_disputes (
    dispute_id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(case_id),
    correction_id INTEGER NOT NULL REFERENCES case_corrections(correction_id),
    event_id INTEGER NOT NULL REFERENCES events(event_id),
    reason TEXT NOT NULL, detail TEXT NOT NULL,
    created_sequence INTEGER NOT NULL,
    UNIQUE(case_id,correction_id,event_id,reason)
);
CREATE INDEX IF NOT EXISTS disputes_case ON case_disputes(case_id);
CREATE TABLE IF NOT EXISTS report_reviews (
    report_key TEXT NOT NULL REFERENCES reports(report_key),
    case_id INTEGER NOT NULL REFERENCES cases(case_id),
    correction_id INTEGER NOT NULL REFERENCES case_corrections(correction_id),
    marked_sequence INTEGER NOT NULL,
    PRIMARY KEY (report_key, case_id, correction_id)
);
"""

def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()

def row(value: sqlite3.Row | None) -> dict | None:
    return dict(value) if value is not None else None

class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript(SCHEMA)
            self._migrate(db)

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(cases)")}
        for name, ddl in (
            ("correction_count", "ALTER TABLE cases ADD COLUMN correction_count INTEGER NOT NULL DEFAULT 0"),
            ("corrected_at", "ALTER TABLE cases ADD COLUMN corrected_at TEXT"),
            ("dispute_count", "ALTER TABLE cases ADD COLUMN dispute_count INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                db.execute(ddl)
        report_columns = {row["name"] for row in db.execute("PRAGMA table_info(reports)")}
        if "needs_review" not in report_columns:
            db.execute("ALTER TABLE reports ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0")
        if "review_marked_sequence" not in report_columns:
            db.execute("ALTER TABLE reports ADD COLUMN review_marked_sequence INTEGER")
        if "view" not in report_columns:
            db.execute("ALTER TABLE reports ADD COLUMN view TEXT NOT NULL DEFAULT 'as_known'")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        if self.path != ":memory:":
            db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except Exception:
                db.rollback()
                raise
            else:
                db.commit()

    @staticmethod
    def journal(db: sqlite3.Connection, operation: str, entity: str, entity_id: str,
                payload: object, occurred_at: str) -> int:
        cursor = db.execute(
            "INSERT INTO journal(operation,entity,entity_id,payload_digest,occurred_at) VALUES(?,?,?,?,?)",
            (operation, entity, entity_id, digest(payload), occurred_at),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def watermark(db: sqlite3.Connection) -> int:
        return int(db.execute("SELECT COALESCE(MAX(sequence),0) FROM journal").fetchone()[0])

    @staticmethod
    def grid(db: sqlite3.Connection, grid_id: str) -> dict | None:
        return row(db.execute("SELECT * FROM grids WHERE grid_id=?", (grid_id,)).fetchone())

    @staticmethod
    def rule(db: sqlite3.Connection, category: str) -> dict | None:
        return row(db.execute("SELECT * FROM route_rules WHERE category=?", (category,)).fetchone())

    @staticmethod
    def case(db: sqlite3.Connection, case_id: int) -> dict:
        result = row(db.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone())
        if result is None:
            raise DomainError("CASE_NOT_FOUND", "Case does not exist", 404)
        return result

    @staticmethod
    def case_by_external(db: sqlite3.Connection, source: str, external_id: str) -> dict | None:
        return row(db.execute("SELECT * FROM cases WHERE source=? AND external_id=?",
                              (source, external_id)).fetchone())

    @staticmethod
    def batch(db: sqlite3.Connection, batch_key: str) -> dict | None:
        return row(db.execute("SELECT * FROM batches WHERE batch_key=?", (batch_key,)).fetchone())

    @staticmethod
    def report(db: sqlite3.Connection, report_key: str) -> dict | None:
        return row(db.execute("SELECT * FROM reports WHERE report_key=?", (report_key,)).fetchone())

    @staticmethod
    def event_by_key(db: sqlite3.Connection, case_id: int, event_key: str) -> dict | None:
        return row(db.execute("SELECT * FROM events WHERE case_id=? AND event_key=?",
                              (case_id, event_key)).fetchone())

    @staticmethod
    def events(db: sqlite3.Connection, case_id: int, watermark: int | None = None) -> list[dict]:
        if watermark is None:
            sql, params = "SELECT * FROM events WHERE case_id=? ORDER BY case_version", (case_id,)
        else:
            sql = "SELECT * FROM events WHERE case_id=? AND event_sequence<=? ORDER BY case_version"
            params = (case_id, watermark)
        return [dict(item) for item in db.execute(sql, params)]

    @staticmethod
    def changes(db: sqlite3.Connection, after: int = 0, limit: int = 100) -> list[dict]:
        if not 0 <= after or not 1 <= limit <= 500:
            raise DomainError("BAD_CURSOR", "Invalid journal cursor or limit")
        return [dict(item) for item in db.execute(
            "SELECT * FROM journal WHERE sequence>? ORDER BY sequence LIMIT ?", (after, limit))]

    @staticmethod
    def policy_history(db: sqlite3.Connection, kind: str, item_key: str) -> list[dict]:
        return [dict(item) for item in db.execute(
            "SELECT * FROM policy_changes WHERE kind=? AND item_key=? ORDER BY revision",
            (kind, item_key))]

    @staticmethod
    def case_list(db: sqlite3.Connection, *, district: str | None = None,
                  category: str | None = None, status: str | None = None,
                  after: int = 0, limit: int = 50) -> list[dict]:
        if not 1 <= limit <= 200 or after < 0:
            raise DomainError("BAD_PAGE", "Invalid pagination")
        clauses = ["case_id > ?"]
        params: list[object] = [after]
        for field, value in (("district", district), ("category", category), ("status", status)):
            if value is not None:
                clauses.append(field + " = ?")
                params.append(value)
        params.append(limit)
        sql = "SELECT * FROM cases WHERE " + " AND ".join(clauses) + " ORDER BY case_id LIMIT ?"
        return [dict(item) for item in db.execute(sql, params)]

    @staticmethod
    def require_report(db: sqlite3.Connection, report_key: str) -> dict:
        report = Store.report(db, report_key)
        if report is None:
            raise DomainError("REPORT_NOT_FOUND", "Report does not exist", 404)
        return report

    @staticmethod
    def save_policy_change(db: sqlite3.Connection, kind: str, item_key: str,
                           revision: int, old: dict | None, new: dict, sequence: int) -> None:
        db.execute("INSERT INTO policy_changes(kind,item_key,revision,old_json,new_json,sequence) "
                   "VALUES(?,?,?,?,?,?)",
                   (kind, item_key, revision, canonical(old) if old else None,
                    canonical(new), sequence))

    @staticmethod
    def table_counts(db: sqlite3.Connection) -> dict:
        return {name: int(db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0])
                for name in ("grids", "route_rules", "batches", "cases", "events", "reports",
                             "case_corrections", "case_disputes")}

    @staticmethod
    def journal_span(db: sqlite3.Connection, start: int, end: int) -> list[dict]:
        if start < 0 or end < start:
            raise DomainError("BAD_SPAN", "Invalid journal span")
        return [dict(item) for item in db.execute(
            "SELECT * FROM journal WHERE sequence BETWEEN ? AND ? ORDER BY sequence",
            (start, end))]

    @staticmethod
    def batch_cases(db: sqlite3.Connection, batch_key: str) -> list[dict]:
        batch = Store.batch(db, batch_key)
        if batch is None:
            raise DomainError("BATCH_NOT_FOUND", "Batch does not exist", 404)
        return [dict(item) for item in db.execute(
            "SELECT case_id,source,external_id,grid_id,category,accepted_at,created_sequence "
            "FROM cases WHERE created_sequence BETWEEN ? AND ? ORDER BY created_sequence",
            (batch["sequence_start"], batch["sequence_end"]))]

    @staticmethod
    def correction_by_key(db: sqlite3.Connection, case_id: int,
                          correction_key: str) -> dict | None:
        return row(db.execute(
            "SELECT * FROM case_corrections WHERE case_id=? AND correction_key=?",
            (case_id, correction_key)).fetchone())

    @staticmethod
    def corrections(db: sqlite3.Connection, case_id: int,
                    watermark: int | None = None) -> list[dict]:
        if watermark is None:
            sql = "SELECT * FROM case_corrections WHERE case_id=? ORDER BY correction_id"
            params: tuple = (case_id,)
        else:
            sql = ("SELECT * FROM case_corrections WHERE case_id=? AND sequence<=? "
                   "ORDER BY correction_id")
            params = (case_id, watermark)
        return [dict(item) for item in db.execute(sql, params)]
    @staticmethod
    def latest_correction(db: sqlite3.Connection, case_id: int,
                          watermark: int | None = None) -> dict | None:
        items = Store.corrections(db, case_id, watermark)
        return items[-1] if items else None

    @staticmethod
    def disputes(db: sqlite3.Connection, case_id: int,
                 watermark: int | None = None) -> list[dict]:
        if watermark is None:
            sql = "SELECT * FROM case_disputes WHERE case_id=? ORDER BY dispute_id"
            params: tuple = (case_id,)
        else:
            sql = ("SELECT * FROM case_disputes WHERE case_id=? AND created_sequence<=? "
                   "ORDER BY dispute_id")
            params = (case_id, watermark)
        return [dict(item) for item in db.execute(sql, params)]

    @staticmethod
    def add_dispute(db: sqlite3.Connection, case_id: int, correction_id: int,
                    event_id: int, reason: str, detail: str, sequence: int) -> None:
        db.execute(
            "INSERT OR IGNORE INTO case_disputes(case_id,correction_id,event_id,reason,"
            "detail,created_sequence) VALUES(?,?,?,?,?,?)",
            (case_id, correction_id, event_id, reason, detail, sequence))

    @staticmethod
    def review_rows(db: sqlite3.Connection, report_key: str | None = None) -> list[dict]:
        if report_key is None:
            sql = "SELECT * FROM report_reviews ORDER BY report_key"
            params: tuple = ()
        else:
            sql = "SELECT * FROM report_reviews WHERE report_key=? ORDER BY correction_id"
            params = (report_key,)
        return [dict(item) for item in db.execute(sql, params)]

    @staticmethod
    def mark_report_review(db: sqlite3.Connection, report_key: str, case_id: int,
                           correction_id: int, sequence: int) -> None:
        db.execute(
            "INSERT OR IGNORE INTO report_reviews(report_key,case_id,correction_id,"
            "marked_sequence) VALUES(?,?,?,?)",
            (report_key, case_id, correction_id, sequence))
        db.execute(
            "UPDATE reports SET needs_review=1, review_marked_sequence=? WHERE report_key=?",
            (sequence, report_key))

    @staticmethod
    def correction_count(db: sqlite3.Connection) -> int:
        return int(db.execute("SELECT COUNT(*) FROM case_corrections").fetchone()[0])
