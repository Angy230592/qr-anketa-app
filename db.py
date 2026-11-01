"""SQLite storage for questions, phone login and survey answers."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = Path(__file__).resolve().parent / "data" / "anketa.db"


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db(admin_username: str, admin_password: str) -> None:
    conn = connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sort_order INTEGER NOT NULL,
            qtype TEXT NOT NULL,
            prompt TEXT NOT NULL,
            options_json TEXT NOT NULL DEFAULT '[]',
            correct_json TEXT NOT NULL DEFAULT '[]',
            scored INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            duration_seconds INTEGER NOT NULL,
            score_percent REAL,
            scored_correct INTEGER NOT NULL DEFAULT 0,
            scored_total INTEGER NOT NULL DEFAULT 0,
            ip TEXT
        );
        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id INTEGER NOT NULL,
            question_id INTEGER,
            prompt TEXT NOT NULL,
            qtype TEXT NOT NULL,
            answer_text TEXT NOT NULL,
            is_correct INTEGER,
            FOREIGN KEY(submission_id) REFERENCES submissions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS otp_codes (
            phone TEXT PRIMARY KEY,
            code_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            sent_at TEXT NOT NULL,
            send_count INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    _ensure_column(conn, "submissions", "phone", "TEXT")
    conn.execute("DROP INDEX IF EXISTS submissions_phone_unique")
    row = conn.execute("SELECT value FROM settings WHERE key='admin_username'").fetchone()
    if not row:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('admin_username', ?), ('admin_password_hash', ?)",
            (admin_username, generate_password_hash(admin_password)),
        )
    conn.commit()
    conn.close()


def get_setting(key: str, default: str = "") -> str:
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def replace_questions(questions: list[dict[str, Any]]) -> None:
    conn = connect()
    conn.execute("DELETE FROM questions")
    for q in questions:
        conn.execute(
            """
            INSERT INTO questions(sort_order, qtype, prompt, options_json, correct_json, scored)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                q["sort_order"],
                q["qtype"],
                q["prompt"],
                json.dumps(q["options"], ensure_ascii=False),
                json.dumps(q["correct"], ensure_ascii=False),
                1 if q["scored"] else 0,
            ),
        )
    conn.commit()
    conn.close()


def list_questions() -> list[dict[str, Any]]:
    conn = connect()
    rows = conn.execute("SELECT * FROM questions ORDER BY sort_order, id").fetchall()
    conn.close()
    result = []
    for row in rows:
        item = dict(row)
        item["options"] = json.loads(item["options_json"] or "[]")
        item["correct"] = json.loads(item["correct_json"] or "[]")
        item["scored"] = bool(item["scored"])
        result.append(item)
    return result


def verify_admin(username: str, password: str) -> bool:
    conn = connect()
    user = conn.execute("SELECT value FROM settings WHERE key='admin_username'").fetchone()
    pwd = conn.execute("SELECT value FROM settings WHERE key='admin_password_hash'").fetchone()
    conn.close()
    if not user or not pwd:
        return False
    return user["value"] == username and check_password_hash(pwd["value"], password)


def phone_submitted(phone: str) -> bool:
    conn = connect()
    row = conn.execute("SELECT id FROM submissions WHERE phone=?", (phone,)).fetchone()
    conn.close()
    return row is not None


def _mark_retakes(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    phones = sorted({(row.get("phone") or "").strip() for row in rows if (row.get("phone") or "").strip()})
    attempt_by_id: dict[int, int] = {}
    if phones:
        placeholders = ",".join("?" * len(phones))
        history = conn.execute(
            f"SELECT id, phone FROM submissions WHERE phone IN ({placeholders}) ORDER BY submitted_at, id",
            phones,
        ).fetchall()
        seen: dict[str, int] = {}
        for item in history:
            n = seen.get(item["phone"], 0) + 1
            seen[item["phone"]] = n
            attempt_by_id[item["id"]] = n
    for row in rows:
        n = attempt_by_id.get(row["id"], 1)
        row["attempt_no"] = n
        row["is_retake"] = n > 1
    return rows


def _hash_code(phone: str, code: str) -> str:
    return hashlib.sha256(f"{phone}:{code}".encode("utf-8")).hexdigest()


def create_otp(phone: str) -> tuple[str, bool]:
    """Create a 6-digit code. Returns (code, reused_recent)."""
    now = datetime.now()
    conn = connect()
    row = conn.execute("SELECT * FROM otp_codes WHERE phone=?", (phone,)).fetchone()
    if row:
        sent_at = datetime.fromisoformat(row["sent_at"])
        if now - sent_at < timedelta(seconds=45):
            conn.close()
            return "", True
        if row["send_count"] >= 8 and now - sent_at < timedelta(hours=1):
            conn.close()
            raise ValueError("Слишком много попыток. Подождите час и попробуйте снова.")
        send_count = row["send_count"] + 1 if now - sent_at < timedelta(hours=1) else 1
    else:
        send_count = 1
    code = f"{secrets.randbelow(1_000_000):06d}"
    conn.execute(
        """
        INSERT INTO otp_codes(phone, code_hash, expires_at, attempts, sent_at, send_count)
        VALUES (?, ?, ?, 0, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            code_hash=excluded.code_hash,
            expires_at=excluded.expires_at,
            attempts=0,
            sent_at=excluded.sent_at,
            send_count=excluded.send_count
        """,
        (
            phone,
            _hash_code(phone, code),
            (now + timedelta(minutes=5)).isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds"),
            send_count,
        ),
    )
    conn.commit()
    conn.close()
    return code, False


def verify_otp(phone: str, code: str) -> bool:
    digits = "".join(ch for ch in code if ch.isdigit())
    conn = connect()
    row = conn.execute("SELECT * FROM otp_codes WHERE phone=?", (phone,)).fetchone()
    if not row:
        conn.close()
        return False
    if datetime.fromisoformat(row["expires_at"]) < datetime.now():
        conn.close()
        return False
    if row["attempts"] >= 5:
        conn.close()
        raise ValueError("Код введён неверно слишком много раз. Запросите новый.")
    ok = row["code_hash"] == _hash_code(phone, digits)
    if ok:
        conn.execute("DELETE FROM otp_codes WHERE phone=?", (phone,))
        conn.commit()
        conn.close()
        return True
    conn.execute("UPDATE otp_codes SET attempts=attempts+1 WHERE phone=?", (phone,))
    conn.commit()
    conn.close()
    return False


def save_submission(
    started_at: datetime,
    submitted_at: datetime,
    duration_seconds: int,
    score_percent: float | None,
    scored_correct: int,
    scored_total: int,
    ip: str | None,
    answers: list[dict[str, Any]],
    phone: str | None = None,
) -> int:
    conn = connect()
    cur = conn.execute(
        """
        INSERT INTO submissions(started_at, submitted_at, duration_seconds, score_percent,
                                scored_correct, scored_total, ip, phone)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            started_at.isoformat(timespec="seconds"),
            submitted_at.isoformat(timespec="seconds"),
            duration_seconds,
            score_percent,
            scored_correct,
            scored_total,
            ip,
            phone,
        ),
    )
    sub_id = cur.lastrowid
    for ans in answers:
        conn.execute(
            """
            INSERT INTO answers(submission_id, question_id, prompt, qtype, answer_text, is_correct)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                sub_id,
                ans.get("question_id"),
                ans["prompt"],
                ans["qtype"],
                ans["answer_text"],
                ans.get("is_correct"),
            ),
        )
    conn.commit()
    conn.close()
    return sub_id


def list_submissions(date_from: str | None = None, date_to: str | None = None) -> list[dict[str, Any]]:
    conn = connect()
    query = "SELECT * FROM submissions WHERE 1=1"
    params: list[Any] = []
    if date_from:
        query += " AND submitted_at >= ?"
        params.append(date_from)
    if date_to:
        query += " AND submitted_at <= ?"
        params.append(date_to + " 23:59:59" if len(date_to) == 10 else date_to)
    query += " ORDER BY submitted_at DESC"
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    _mark_retakes(conn, rows)
    for row in rows:
        row["answers"] = [
            dict(a)
            for a in conn.execute(
                "SELECT * FROM answers WHERE submission_id=? ORDER BY id", (row["id"],)
            ).fetchall()
        ]
    conn.close()
    return rows


def stats(date_from: str | None = None, date_to: str | None = None) -> dict[str, Any]:
    items = list_submissions(date_from, date_to)
    if not items:
        return {"count": 0, "avg_score": None, "avg_duration": None}
    scores = [s["score_percent"] for s in items if s["score_percent"] is not None]
    durations = [s["duration_seconds"] for s in items]
    return {
        "count": len(items),
        "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
        "avg_duration": int(sum(durations) / len(durations)) if durations else None,
    }


def clear_responses() -> int:
    conn = connect()
    count = conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    conn.execute("DELETE FROM answers")
    conn.execute("DELETE FROM submissions")
    conn.execute("DELETE FROM otp_codes")
    conn.commit()
    conn.close()
    return count
