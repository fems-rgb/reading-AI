"""SQLite 持久化层。

只用标准库 sqlite3，单文件库，方便园内备份/迁移/一键删除。
每次调用开一个短连接（WAL 模式），避免多协程共享连接的问题。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import get_settings
from .utils import now

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS children (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    class_name TEXT NOT NULL DEFAULT '小一班',
    age_months INTEGER NOT NULL,
    languages TEXT NOT NULL DEFAULT '["zh"]',
    guardian_name TEXT DEFAULT '',
    guardian_consent INTEGER NOT NULL DEFAULT 0,
    consent_at TEXT,
    parent_token TEXT UNIQUE,
    notes TEXT DEFAULT '',
    deleted_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'zh',
    theme TEXT DEFAULT '',
    age_min_months INTEGER NOT NULL DEFAULT 36,
    age_max_months INTEGER NOT NULL DEFAULT 72,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS book_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL,
    image_path TEXT DEFAULT '',
    raw_text TEXT DEFAULT '',
    faithful_description TEXT DEFAULT '',
    read_aloud_script TEXT DEFAULT '',
    keywords TEXT DEFAULT '[]',
    characters TEXT DEFAULT '[]',
    objects TEXT DEFAULT '[]',
    scene TEXT DEFAULT '',
    relations TEXT DEFAULT '[]',
    mood TEXT DEFAULT '',
    flags TEXT DEFAULT '[]',
    ocr_confidence REAL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    updated_at TEXT NOT NULL,
    UNIQUE (book_id, page_no)
);

CREATE TABLE IF NOT EXISTS question_bank (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_page_id INTEGER NOT NULL REFERENCES book_pages(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    qtype TEXT NOT NULL DEFAULT 'observe',
    age_min_months INTEGER NOT NULL DEFAULT 36,
    age_max_months INTEGER NOT NULL DEFAULT 72,
    source TEXT NOT NULL DEFAULT 'teacher',
    version INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    child_id INTEGER NOT NULL REFERENCES children(id),
    book_id INTEGER NOT NULL REFERENCES books(id),
    device_id TEXT DEFAULT '',
    state TEXT NOT NULL DEFAULT 'IDLE',
    status TEXT NOT NULL DEFAULT 'active',
    token TEXT NOT NULL,
    last_page_no INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL,
    book_page_id INTEGER,
    question TEXT NOT NULL DEFAULT '',
    qtype TEXT NOT NULL DEFAULT 'observe',
    source TEXT NOT NULL DEFAULT 'agent',
    answer_text TEXT DEFAULT '',
    asr_confidence REAL DEFAULT 0,
    understood TEXT DEFAULT '',
    action TEXT DEFAULT '',
    reply TEXT DEFAULT '',
    checker TEXT DEFAULT '{}',
    latency_ms REAL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS advice (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id INTEGER NOT NULL REFERENCES children(id),
    session_id TEXT NOT NULL,
    text TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    evidence TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    edited_text TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS weekly_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id INTEGER NOT NULL REFERENCES children(id),
    week_start TEXT NOT NULL,
    week_end TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS selfchecks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    checks TEXT NOT NULL DEFAULT '[]',
    actions TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    page_no INTEGER NOT NULL DEFAULT 0,
    stage TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    latency_ms REAL NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    body TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE (name, version)
);

CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, page_no);
CREATE INDEX IF NOT EXISTS idx_metrics_created ON metrics(created_at);
CREATE INDEX IF NOT EXISTS idx_advice_child ON advice(child_id, status);
CREATE INDEX IF NOT EXISTS idx_selfchecks_created ON selfchecks(created_at);
"""

DEFAULT_PROMPTS: tuple[tuple[str, str], ...] = (
    (
        "A3_fuse",
        "你是绘本页面转述助手。只使用给定的 OCR 文字与画面事实，不得新增人物、物品、情节。"
        "输出 JSON：raw_text（OCR 原文，禁止改写）、faithful_description（3-6 岁可懂，每句≤15 字）、"
        "read_aloud_script（只能重组前两层）、speculative_flags（推测内容及原因）、keywords。",
    ),
    (
        "A4_question",
        "你是幼儿园绘本共读老师。基于当前页事实提 1 个问题：句子≤{max_len}字、不用抽象词、"
        "不重复最近 3 个问题、不提及画面里没有的东西、每 3 题最多 1 道封闭题。"
        "输出 JSON：question、type、expected、hint。",
    ),
    (
        "A7_understand",
        "判断孩子是否理解了刚才的问题，不要打分。输出 JSON：understood(yes/partial/no)、"
        "action(encourage/confirm/give_answer/answer_why/icon_choice)、reply（≤30 字，温柔）。",
    ),
    (
        "A10_advice",
        "基于本次问答记录给老师 3-5 条可执行建议。每条必须给出 evidence（对应问答记录 id），"
        "不得排名、比较幼儿、贴能力标签。输出 JSON 数组：text、category、evidence。",
    ),
    (
        "A11_selfcheck",
        "你是内容质检员。检查摘要/问题是否只包含画面事实、是否适龄、是否重复。"
        "输出 JSON：verdict(pass/rewrite/block)、score、checks[]、actions[]。",
    ),
)


def _connect() -> sqlite3.Connection:
    settings = get_settings()
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # WAL 下 NORMAL 不会损坏库，最多丢最后一个事务；换来每次 commit 不再强制 fsync。
    # 一页处理有 7~8 次埋点写入，这一项占了实测端到端延迟的一大半。
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def q(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def q1(sql: str, params: Iterable[Any] = ()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def x(sql: str, params: Iterable[Any] = ()) -> int:
    with tx() as conn:
        cur = conn.execute(sql, tuple(params))
        return int(cur.lastrowid or 0)


def init_db() -> None:
    with tx() as conn:
        conn.executescript(SCHEMA)
        for name, body in DEFAULT_PROMPTS:
            exists = conn.execute(
                "SELECT 1 FROM prompts WHERE name=? AND version=1", (name,)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO prompts(name, version, body, active, created_at) VALUES (?,1,?,1,?)",
                    (name, body, now()),
                )


def get_prompt(name: str) -> dict | None:
    return q1(
        "SELECT * FROM prompts WHERE name=? AND active=1 ORDER BY version DESC LIMIT 1",
        (name,),
    )


def audit(actor: str, action: str, detail: dict | None = None) -> None:
    x(
        "INSERT INTO audit_log(actor, action, detail, created_at) VALUES (?,?,?,?)",
        (actor, action, json.dumps(detail or {}, ensure_ascii=False), now()),
    )


# ---------------- 幼儿 / 授权 ----------------


def list_children(include_deleted: bool = False) -> list[dict]:
    suffix = "" if include_deleted else "WHERE deleted_at IS NULL"
    return q(f"SELECT * FROM children {suffix} ORDER BY created_at DESC")


def get_child(child_id: int) -> dict | None:
    return q1("SELECT * FROM children WHERE id=? AND deleted_at IS NULL", (child_id,))


def get_child_by_parent_token(token: str) -> dict | None:
    return q1(
        "SELECT * FROM children WHERE parent_token=? AND deleted_at IS NULL", (token,)
    )


def create_child(
    name: str,
    age_months: int,
    class_name: str,
    languages: list[str],
    guardian_name: str = "",
    guardian_consent: bool = False,
    parent_token: str | None = None,
) -> int:
    return x(
        "INSERT INTO children(name, class_name, age_months, languages, guardian_name,"
        " guardian_consent, consent_at, parent_token, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            name,
            class_name,
            age_months,
            json.dumps(languages, ensure_ascii=False),
            guardian_name,
            1 if guardian_consent else 0,
            now() if guardian_consent else None,
            parent_token,
            now(),
        ),
    )


def set_consent(child_id: int, consented: bool, guardian_name: str | None = None) -> None:
    if guardian_name is None:
        x(
            "UPDATE children SET guardian_consent=?, consent_at=? WHERE id=?",
            (1 if consented else 0, now() if consented else None, child_id),
        )
    else:
        x(
            "UPDATE children SET guardian_consent=?, consent_at=?, guardian_name=? WHERE id=?",
            (1 if consented else 0, now() if consented else None, guardian_name, child_id),
        )


def delete_child_everything(child_id: int) -> dict:
    """一键删除某幼儿全部记录（规范 §6）。删除动作本身留审计，但不留任何内容。"""
    sessions = q("SELECT id FROM sessions WHERE child_id=?", (child_id,))
    with tx() as conn:
        conn.execute("DELETE FROM turns WHERE session_id IN (SELECT id FROM sessions WHERE child_id=?)", (child_id,))
        conn.execute("DELETE FROM sessions WHERE child_id=?", (child_id,))
        conn.execute("DELETE FROM advice WHERE child_id=?", (child_id,))
        conn.execute("DELETE FROM weekly_reports WHERE child_id=?", (child_id,))
        conn.execute("DELETE FROM children WHERE id=?", (child_id,))
    audit("admin", "delete_child_everything", {"child_id": child_id, "sessions": len(sessions)})
    return {"deleted_sessions": len(sessions)}


# ---------------- 绘本库 ----------------


def list_books() -> list[dict]:
    return q(
        "SELECT b.*, (SELECT COUNT(*) FROM book_pages p WHERE p.book_id=b.id) AS page_count"
        " FROM books b ORDER BY b.id DESC"
    )


def get_book(book_id: int) -> dict | None:
    return q1("SELECT * FROM books WHERE id=?", (book_id,))


def create_book(title: str, language: str, theme: str, age_min: int, age_max: int) -> int:
    return x(
        "INSERT INTO books(title, language, theme, age_min_months, age_max_months, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (title, language, theme, age_min, age_max, now()),
    )


def list_pages(book_id: int) -> list[dict]:
    rows = q("SELECT * FROM book_pages WHERE book_id=? ORDER BY page_no", (book_id,))
    return [_decode_page(row) for row in rows]


def get_page(book_id: int, page_no: int) -> dict | None:
    row = q1("SELECT * FROM book_pages WHERE book_id=? AND page_no=?", (book_id, page_no))
    return _decode_page(row) if row else None


def get_page_by_id(page_id: int) -> dict | None:
    row = q1("SELECT * FROM book_pages WHERE id=?", (page_id,))
    return _decode_page(row) if row else None


def _decode_page(row: dict) -> dict:
    for field in ("keywords", "characters", "objects", "relations", "flags"):
        try:
            row[field] = json.loads(row.get(field) or "[]")
        except (TypeError, json.JSONDecodeError):
            row[field] = []
    return row


def upsert_page(book_id: int, page_no: int, **fields: Any) -> int:
    payload = {
        "image_path": "",
        "raw_text": "",
        "faithful_description": "",
        "read_aloud_script": "",
        "keywords": [],
        "characters": [],
        "objects": [],
        "scene": "",
        "relations": [],
        "mood": "",
        "flags": [],
        "ocr_confidence": 0.0,
        "status": "pending",
    }
    payload.update(fields)
    for key in ("keywords", "characters", "objects", "relations", "flags"):
        payload[key] = json.dumps(payload[key], ensure_ascii=False)
    existing = q1("SELECT id FROM book_pages WHERE book_id=? AND page_no=?", (book_id, page_no))
    if existing:
        assignments = ", ".join(f"{key}=?" for key in payload)
        x(
            f"UPDATE book_pages SET {assignments}, updated_at=? WHERE id=?",
            (*payload.values(), now(), existing["id"]),
        )
        return int(existing["id"])
    columns = ", ".join(payload)
    marks = ", ".join("?" for _ in payload)
    return x(
        f"INSERT INTO book_pages(book_id, page_no, {columns}, updated_at) VALUES (?,?,{marks},?)",
        (book_id, page_no, *payload.values(), now()),
    )


# ---------------- 题库 ----------------


def list_bank(book_id: int | None = None, book_page_id: int | None = None) -> list[dict]:
    if book_page_id:
        return q(
            "SELECT * FROM question_bank WHERE book_page_id=? AND active=1 ORDER BY id",
            (book_page_id,),
        )
    return q(
        "SELECT qb.*, p.page_no FROM question_bank qb"
        " JOIN book_pages p ON p.id=qb.book_page_id"
        " WHERE p.book_id=? AND qb.active=1 ORDER BY p.page_no, qb.id",
        (book_id,),
    )


def add_bank_item(
    book_page_id: int,
    text: str,
    qtype: str,
    age_min: int = 36,
    age_max: int = 72,
    source: str = "teacher",
) -> int:
    return x(
        "INSERT INTO question_bank(book_page_id, text, qtype, age_min_months, age_max_months,"
        " source, version, active, created_at) VALUES (?,?,?,?,?,?,1,1,?)",
        (book_page_id, text, qtype, age_min, age_max, source, now()),
    )


def deactivate_bank_item(item_id: int) -> None:
    x("UPDATE question_bank SET active=0, version=version+1 WHERE id=?", (item_id,))


# ---------------- 会话 / 问答 ----------------


def create_session(session_id: str, child_id: int, book_id: int, token: str, device_id: str = "") -> None:
    x(
        "INSERT INTO sessions(id, child_id, book_id, device_id, state, status, token, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (session_id, child_id, book_id, device_id, "IDLE", "active", token, now()),
    )


def get_session(session_id: str) -> dict | None:
    return q1("SELECT * FROM sessions WHERE id=?", (session_id,))


def update_session(session_id: str, **fields: Any) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{key}=?" for key in fields)
    x(f"UPDATE sessions SET {assignments} WHERE id=?", (*fields.values(), session_id))


def end_session(session_id: str) -> None:
    x(
        "UPDATE sessions SET status='ended', state='IDLE', ended_at=? WHERE id=?",
        (now(), session_id),
    )


def add_turn(
    session_id: str,
    page_no: int,
    book_page_id: int | None,
    question: str,
    qtype: str,
    checker: dict,
    source: str = "agent",
) -> int:
    return x(
        "INSERT INTO turns(session_id, page_no, book_page_id, question, qtype, source,"
        " checker, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            session_id,
            page_no,
            book_page_id,
            question,
            qtype,
            source,
            json.dumps(checker, ensure_ascii=False),
            now(),
        ),
    )


def update_turn(turn_id: int, **fields: Any) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{key}=?" for key in fields)
    x(f"UPDATE turns SET {assignments} WHERE id=?", (*fields.values(), turn_id))


def list_turns(session_id: str) -> list[dict]:
    rows = q("SELECT * FROM turns WHERE session_id=? ORDER BY id", (session_id,))
    for row in rows:
        try:
            row["checker"] = json.loads(row.get("checker") or "{}")
        except json.JSONDecodeError:
            row["checker"] = {}
    return rows


# ---------------- 教学建议 ----------------


def add_advice(child_id: int, session_id: str, text: str, category: str, evidence: list[int]) -> int:
    return x(
        "INSERT INTO advice(child_id, session_id, text, category, evidence, status, created_at)"
        " VALUES (?,?,?,?,?,'pending',?)",
        (child_id, session_id, text, category, json.dumps(evidence), now()),
    )


def list_advice(status: str | None = None, child_id: int | None = None) -> list[dict]:
    clauses, params = [], []
    if status:
        clauses.append("a.status=?")
        params.append(status)
    if child_id:
        clauses.append("a.child_id=?")
        params.append(child_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = q(
        f"SELECT a.*, c.name AS child_name FROM advice a JOIN children c ON c.id=a.child_id"
        f" {where} ORDER BY a.id DESC LIMIT 200",
        params,
    )
    for row in rows:
        try:
            row["evidence"] = json.loads(row.get("evidence") or "[]")
        except json.JSONDecodeError:
            row["evidence"] = []
    return rows


def decide_advice(advice_id: int, decision: str, edited_text: str = "") -> None:
    x(
        "UPDATE advice SET status=?, edited_text=?, decided_at=? WHERE id=?",
        (decision, edited_text, now(), advice_id),
    )


# ---------------- 家长周报 ----------------


def add_report(child_id: int, week_start: str, week_end: str, payload: dict, expires_at: str) -> int:
    return x(
        "INSERT INTO weekly_reports(child_id, week_start, week_end, payload, status, created_at, expires_at)"
        " VALUES (?,?,?,?,'draft',?,?)",
        (child_id, week_start, week_end, json.dumps(payload, ensure_ascii=False), now(), expires_at),
    )


def latest_report(child_id: int) -> dict | None:
    row = q1("SELECT * FROM weekly_reports WHERE child_id=? ORDER BY id DESC LIMIT 1", (child_id,))
    if row:
        try:
            row["payload"] = json.loads(row["payload"])
        except json.JSONDecodeError:
            row["payload"] = {}
    return row


def get_report(report_id: int) -> dict | None:
    row = q1("SELECT * FROM weekly_reports WHERE id=?", (report_id,))
    if row:
        try:
            row["payload"] = json.loads(row["payload"])
        except json.JSONDecodeError:
            row["payload"] = {}
    return row


def publish_report(report_id: int) -> None:
    x("UPDATE weekly_reports SET status='published' WHERE id=?", (report_id,))


# ---------------- 自检 / 埋点 ----------------


def add_selfcheck(scope: str, target: str, verdict: str, score: float, checks: list, actions: list) -> int:
    return x(
        "INSERT INTO selfchecks(scope, target, verdict, score, checks, actions, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (
            scope,
            target,
            verdict,
            score,
            json.dumps(checks, ensure_ascii=False),
            json.dumps(actions, ensure_ascii=False),
            now(),
        ),
    )


def list_selfchecks(scope: str | None = None, limit: int = 50) -> list[dict]:
    if scope:
        rows = q("SELECT * FROM selfchecks WHERE scope=? ORDER BY id DESC LIMIT ?", (scope, limit))
    else:
        rows = q("SELECT * FROM selfchecks ORDER BY id DESC LIMIT ?", (limit,))
    for row in rows:
        for field in ("checks", "actions"):
            try:
                row[field] = json.loads(row.get(field) or "[]")
            except json.JSONDecodeError:
                row[field] = []
    return rows


def selfcheck_stats(since: str) -> dict:
    row = q1(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN verdict='pass' THEN 1 ELSE 0 END) AS passed,"
        " SUM(CASE WHEN verdict='rewrite' THEN 1 ELSE 0 END) AS rewritten,"
        " SUM(CASE WHEN verdict='block' THEN 1 ELSE 0 END) AS blocked,"
        " AVG(score) AS avg_score"
        " FROM selfchecks WHERE scope='content' AND created_at>=?",
        (since,),
    ) or {}
    row["total"] = row.get("total") or 0
    row["passed"] = row.get("passed") or 0
    row["rewritten"] = row.get("rewritten") or 0
    row["blocked"] = row.get("blocked") or 0
    row["avg_score"] = round(row.get("avg_score") or 0, 1)
    return row


METRIC_COLUMNS = (
    "trace_id",
    "session_id",
    "page_no",
    "stage",
    "provider",
    "model",
    "latency_ms",
    "ok",
    "tokens_in",
    "tokens_out",
    "error",
)

_METRIC_DEFAULTS: dict[str, Any] = {
    "trace_id": "",
    "session_id": "",
    "page_no": 0,
    "stage": "",
    "provider": "",
    "model": "",
    "latency_ms": 0.0,
    "ok": 1,
    "tokens_in": 0,
    "tokens_out": 0,
    "error": "",
}


def add_metric(**fields: Any) -> None:
    add_metrics([fields])


def add_metrics(rows: Iterable[dict]) -> int:
    """批量写埋点：一次连接、一次提交。

    一次页面处理要记 7~8 条，逐条写等于反复 connect/commit/close，
    实测占掉了端到端延迟的一大半（见 README §性能）。
    """
    stamp = now()
    prepared: list[tuple] = []
    for fields in rows:
        item = dict(_METRIC_DEFAULTS)
        item.update(fields)
        prepared.append(tuple(item[column] for column in METRIC_COLUMNS) + (stamp,))
    if not prepared:
        return 0
    with tx() as conn:
        conn.executemany(
            "INSERT INTO metrics(trace_id, session_id, page_no, stage, provider, model, latency_ms,"
            " ok, tokens_in, tokens_out, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            prepared,
        )
    return len(prepared)


def metrics_summary(since: str) -> dict:
    row = q1(
        "SELECT COUNT(*) AS calls, AVG(latency_ms) AS avg_latency,"
        " SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS failures,"
        " SUM(tokens_in+tokens_out) AS tokens FROM metrics WHERE created_at>=?",
        (since,),
    ) or {}
    row["calls"] = row.get("calls") or 0
    row["avg_latency"] = round(row.get("avg_latency") or 0)
    row["failures"] = row.get("failures") or 0
    row["tokens"] = row.get("tokens") or 0
    row["failure_rate"] = round((row["failures"] / row["calls"] * 100), 1) if row["calls"] else 0.0
    return row


def slowest_pages(since: str, limit: int = 10) -> list[dict]:
    return q(
        "SELECT session_id, page_no, stage, MAX(latency_ms) AS latency_ms, COUNT(*) AS calls"
        " FROM metrics WHERE created_at>=? GROUP BY session_id, page_no, stage"
        " ORDER BY latency_ms DESC LIMIT ?",
        (since, limit),
    )


def data_dir_size() -> int:
    root: Path = get_settings().data_dir
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    return total