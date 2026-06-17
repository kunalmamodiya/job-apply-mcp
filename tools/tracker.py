"""
SQLite-backed application tracker.

Schema:
    applications (
        id            INTEGER PRIMARY KEY,
        job_title     TEXT,
        company       TEXT,
        platform      TEXT,
        job_url       TEXT UNIQUE,
        status        TEXT DEFAULT 'applied',   -- applied | viewed | responded | rejected | failed
        applied_at    TEXT,                      -- ISO-8601
        confirmation  TEXT,
        cover_note    TEXT,
        match_score   REAL
    )
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import DB_PATH, ensure_dirs

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS applications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_title     TEXT NOT NULL,
    company       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    job_url       TEXT UNIQUE NOT NULL,
    status        TEXT NOT NULL DEFAULT 'applied',
    applied_at    TEXT NOT NULL,
    confirmation  TEXT,
    cover_note    TEXT,
    match_score   REAL
);
"""


def _connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_TABLE)
    conn.commit()
    return conn


def record_application(
    job_title: str,
    company: str,
    platform: str,
    job_url: str,
    status: str = "applied",
    confirmation: str | None = None,
    cover_note: str | None = None,
    match_score: float | None = None,
) -> int:
    """Insert a new application record. Returns the row id."""
    conn = _connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO applications
                (job_title, company, platform, job_url, status, applied_at, confirmation, cover_note, match_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_title,
                company,
                platform,
                job_url,
                status,
                datetime.now(timezone.utc).isoformat(),
                confirmation,
                cover_note,
                match_score,
            ),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]
    finally:
        conn.close()


def is_already_applied(job_url: str) -> bool:
    """Check if we've already applied to this URL."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM applications WHERE job_url = ?", (job_url,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def update_status(job_url: str, status: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE applications SET status = ? WHERE job_url = ?",
            (status, job_url),
        )
        conn.commit()
    finally:
        conn.close()


def get_applications(days: int = 7) -> list[dict[str, Any]]:
    """Return applications from the last *days* days."""
    conn = _connect()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = conn.execute(
            """
            SELECT id, job_title, company, platform, job_url,
                   status, applied_at, confirmation, match_score
            FROM applications
            WHERE applied_at >= ?
            ORDER BY applied_at DESC
            """,
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_application_summary(days: int = 7) -> dict[str, Any]:
    """
    Return applications grouped by platform and status.
    {
        "total": int,
        "by_platform": { "linkedin": { "applied": [...], ... }, ... },
        "by_status":   { "applied": int, "viewed": int, ... }
    }
    """
    apps = get_applications(days)
    by_platform: dict[str, dict[str, list[dict]]] = {}
    by_status: dict[str, int] = {}

    for app in apps:
        plat = app["platform"]
        status = app["status"]
        by_platform.setdefault(plat, {}).setdefault(status, []).append(app)
        by_status[status] = by_status.get(status, 0) + 1

    return {
        "total": len(apps),
        "days": days,
        "by_platform": by_platform,
        "by_status": by_status,
    }
