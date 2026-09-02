from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Sequence

from .wecom_classifier import (
    classify_logistics_issue,
    assess_logistics_response,
    extract_business_clues,
    LOGISTICS_CATEGORIES,
)
from .wecom_parser import WecomUnifiedRecord


LOCAL_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    account_id TEXT NOT NULL,
    source_database TEXT NOT NULL,
    message_id TEXT NOT NULL,
    server_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    conversation_name TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    sender_corp_id TEXT,
    sender_corp_name TEXT,
    subject_bucket TEXT NOT NULL CHECK (subject_bucket IN ('zhongji', 'other', 'unknown')),
    subject_basis TEXT NOT NULL,
    message_type TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    text TEXT NOT NULL,
    reply_to_message_id TEXT,
    parse_status TEXT NOT NULL,
    source_reference TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    UNIQUE(account_id, source_database, message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_conv_time
ON messages(conversation_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_messages_subject ON messages(subject_bucket);
CREATE INDEX IF NOT EXISTS idx_messages_account ON messages(account_id);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL,
    clues_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unreplied' CHECK (status IN ('unreplied', 'acknowledged', 'solved', 'in_progress', 'open', 'needs_review')),
    status_reason TEXT NOT NULL DEFAULT '',
    UNIQUE(message_id, category)
);

CREATE INDEX IF NOT EXISTS idx_issues_category ON issues(category);
CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);
CREATE INDEX IF NOT EXISTS idx_issues_msg_id ON issues(message_id);

CREATE TABLE IF NOT EXISTS responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    response_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    response_kind TEXT NOT NULL,
    is_solution INTEGER NOT NULL CHECK (is_solution IN (0, 1)),
    solution_text TEXT,
    status_contribution TEXT NOT NULL,
    confidence REAL NOT NULL,
    latency_seconds INTEGER NOT NULL,
    association_basis TEXT NOT NULL,
    UNIQUE(issue_id, response_message_id)
);

CREATE INDEX IF NOT EXISTS idx_responses_issue_perf
ON responses(issue_id, is_solution DESC, confidence DESC, latency_seconds ASC);
CREATE INDEX IF NOT EXISTS idx_responses_msg_id ON responses(response_message_id);

CREATE TABLE IF NOT EXISTS import_cursors (
    account_id TEXT NOT NULL,
    source_database TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    message_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, source_database)
);
"""


class WecomLocalStorage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(LOCAL_SCHEMA)
            # Additive migration: old readers/writers and old rows stay valid.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
            for name, definition in (("parent_id", "TEXT"), ("root_message_id", "TEXT"),
                                     ("nesting_depth", "INTEGER NOT NULL DEFAULT 0"),
                                     ("provenance_json", "TEXT NOT NULL DEFAULT '{}'")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_id)")

    def upsert_messages(self, records: Sequence[WecomUnifiedRecord]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        with self.connect() as conn:
            for r in records:
                conn.execute(
                    """
                    INSERT INTO messages (
                        id, source, account_id, source_database, message_id, server_id,
                        conversation_id, conversation_name, sender_id, sender_name,
                        sender_corp_id, sender_corp_name, subject_bucket, subject_basis,
                        message_type, sent_at, text, reply_to_message_id, parse_status,
                        source_reference, ingested_at, parent_id, root_message_id, nesting_depth, provenance_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, source_database, message_id) DO UPDATE SET
                        server_id=excluded.server_id,
                        conversation_id=excluded.conversation_id,
                        conversation_name=excluded.conversation_name,
                        sender_id=excluded.sender_id,
                        sender_name=excluded.sender_name,
                        sender_corp_id=excluded.sender_corp_id,
                        sender_corp_name=excluded.sender_corp_name,
                        subject_bucket=excluded.subject_bucket,
                        subject_basis=excluded.subject_basis,
                        message_type=excluded.message_type,
                        sent_at=excluded.sent_at,
                        text=excluded.text,
                        reply_to_message_id=excluded.reply_to_message_id,
                        parse_status=excluded.parse_status,
                        source_reference=excluded.source_reference,
                        parent_id=excluded.parent_id,
                        root_message_id=excluded.root_message_id,
                        nesting_depth=excluded.nesting_depth,
                        provenance_json=excluded.provenance_json
                    """,
                    (
                        r.id,
                        r.source,
                        r.account_id,
                        r.source_database,
                        r.message_id,
                        r.server_id,
                        r.conversation_id,
                        r.conversation_name,
                        r.sender_id,
                        r.sender_name,
                        r.sender_corp_id,
                        r.sender_corp_name,
                        r.subject_bucket,
                        r.subject_basis,
                        r.message_type,
                        r.sent_at.isoformat() if r.sent_at else "",
                        r.text,
                        r.reply_to_message_id,
                        r.parse_status,
                        r.source_reference,
                        now,
                        r.parent_id,
                        r.root_message_id,
                        r.nesting_depth,
                        json.dumps(r.provenance, ensure_ascii=False),
                    ),
                )
                count += 1
        return count

    def rebuild_analysis(
        self, semantic_analyzer: Any | None = None, *,
        account_id: str | None = None, conversation_id: str | None = None,
        conversation_name: str | None = None,
    ) -> dict[str, Any]:
        from .wecom_analysis import analyze_conversation, response_for_category

        clauses, params = [], []
        for name, value in (("account_id", account_id), ("conversation_id", conversation_id), ("conversation_name", conversation_name)):
            if value:
                if name == "conversation_id":
                    clauses.append("(conversation_id=? OR instr(conversation_id, ? || ':forwarded:')=1)")
                    params.extend([value, value])
                else:
                    clauses.append("instr(lower(conversation_name), lower(?))>0" if name == "conversation_name" else f"{name} = ?")
                    params.append(value)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            messages = [dict(row) for row in conn.execute(
                f"SELECT * FROM messages {where} ORDER BY account_id, conversation_id, sent_at, id", params
            )]
        by_conversation = {}
        for row in messages:
            by_conversation.setdefault((row["account_id"], row["conversation_id"]), []).append(row)
        # Remote calls finish before the atomic replacement; errors keep the rule fallback.
        analyses = {}
        for rows in by_conversation.values():
            analyses.update(analyze_conversation(rows, semantic_analyzer))
        total_issues = 0
        with self.connect() as conn:
            conn.executemany("DELETE FROM issues WHERE message_id=?", [(row["id"],) for row in messages])
            for row in messages:
                analysis = analyses.get(row["id"])
                if not analysis:
                    continue
                clues = dict(analysis["clues"])
                clues["response_entities"] = {response["row"]["id"]: response["clues"] for response in analysis["responses"]}
                clues.update({key: analysis[key] for key in ("analysis_source", "urgency_level", "risk_evaluation", "needs_review")})
                for classification in analysis["classes"]:
                    status, reason = "unreplied", "暂无可关联的有效回复"
                    cursor = conn.execute(
                        "INSERT INTO issues (message_id,category,confidence,evidence_json,clues_json,summary,status) VALUES (?,?,?,?,?,?,?)",
                        (row["id"], classification.category, classification.confidence,
                         json.dumps(classification.evidence, ensure_ascii=False), json.dumps(clues, ensure_ascii=False),
                         analysis["summary"], status),
                    )
                    issue_id = cursor.lastrowid
                    total_issues += 1
                    for response in analysis["responses"]:
                        assessment = response_for_category(response, classification.category)
                        candidate = response["row"]
                        conn.execute(
                            "INSERT INTO responses (issue_id,response_message_id,response_kind,is_solution,solution_text,status_contribution,confidence,latency_seconds,association_basis) VALUES (?,?,?,?,?,?,?,?,?)",
                            (issue_id, candidate["id"], assessment.kind, int(assessment.is_solution), assessment.solution_text,
                             assessment.status_contribution, assessment.confidence, response["latency"], response["basis"]),
                        )
                        if assessment.is_solution:
                            status, reason = "solved", f"由 {candidate['sender_name']} 给出相关方案"
                        elif status != "solved" and assessment.status_contribution == "in_progress":
                            status, reason = "in_progress", "已有跟进进展，尚无具体方案"
                        elif status == "unreplied" and assessment.status_contribution == "acknowledged":
                            status, reason = "acknowledged", "仅确认收到，尚无具体方案"
                    if analysis["needs_review"] and status != "solved":
                        reason += "；存在无法唯一关联的回复，需人工核对"
                    conn.execute("UPDATE issues SET status=?,status_reason=? WHERE id=?", (status, reason, issue_id))
        return {"messages": len(messages), "issues": total_issues,
                "semantic_enabled": bool(semantic_analyzer and semantic_analyzer.enabled),
                "semantic_calls": getattr(semantic_analyzer, "calls", 0),
                "semantic_errors": getattr(semantic_analyzer, "errors", 0)}

    def get_summary(
        self,
        account_id: str | None = None,
        subject: str | None = None,
        category: str | None = None,
        status: str | None = None,
        conversation_id: str | None = None,
        conversation_name: str | None = None,
    ) -> dict[str, Any]:
        clauses = []
        params: list[Any] = []
        if account_id:
            clauses.append("m.account_id = ?")
            params.append(account_id)
        if conversation_id:
            clauses.append("(m.conversation_id=? OR instr(m.conversation_id, ? || ':forwarded:')=1)")
            params.extend([conversation_id, conversation_id])
        if conversation_name:
            clauses.append("instr(lower(m.conversation_name), lower(?))>0")
            params.append(conversation_name)
        if subject in ("zhongji", "other", "unknown"):
            clauses.append("m.subject_bucket = ?")
            params.append(subject)
        if category:
            clauses.append("i.category = ?")
            params.append(category)
        if status:
            clauses.append("i.status = ?")
            params.append(status)

        where = "WHERE " + " AND ".join(clauses) if clauses else ""

        with self.connect() as conn:
            coverage_clauses, coverage_params = [], []
            if account_id:
                coverage_clauses.append("account_id = ?")
                coverage_params.append(account_id)
            if conversation_id:
                coverage_clauses.append("(conversation_id=? OR instr(conversation_id, ? || ':forwarded:')=1)")
                coverage_params.extend([conversation_id, conversation_id])
            if conversation_name:
                coverage_clauses.append("instr(lower(conversation_name), lower(?))>0")
                coverage_params.append(conversation_name)
            coverage_where = "WHERE " + " AND ".join(coverage_clauses) if coverage_clauses else ""
            coverage = conn.execute(
                f"SELECT COUNT(*) message_count, MIN(NULLIF(sent_at,'')) first_message_at, MAX(NULLIF(sent_at,'')) last_message_at, "
                f"SUM(parse_status NOT IN ('parsed','expanded_forward')) unparsed_count FROM messages {coverage_where}", coverage_params,
            ).fetchone()
            # Stats by status
            totals = conn.execute(
                f"""
                SELECT
                    COUNT(DISTINCT i.id) AS issue_count,
                    COUNT(DISTINCT m.id) AS question_count,
                    COUNT(DISTINCT CASE WHEN i.status = 'unreplied' THEN i.id END) AS unreplied_count,
                    COUNT(DISTINCT i.category) AS category_count,
                    COUNT(DISTINCT CASE WHEN i.status != 'unreplied' THEN i.id END) AS replied_count,
                    COUNT(DISTINCT CASE WHEN i.status = 'acknowledged' THEN i.id END) AS ack_only_count,
                    COUNT(DISTINCT CASE WHEN i.status = 'solved' THEN i.id END) AS solved_count,
                    COUNT(DISTINCT CASE WHEN i.status != 'solved' THEN i.id END) AS pending_count
                FROM issues i
                JOIN messages m ON m.id = i.message_id
                {where}
                """,
                params,
            ).fetchone()

            # Stats by category
            cat_rows = conn.execute(
                f"""
                SELECT i.category, COUNT(*) AS count
                FROM issues i
                JOIN messages m ON m.id = i.message_id
                {where}
                GROUP BY i.category ORDER BY count DESC, i.category
                """,
                params,
            ).fetchall()

            # Stats by subject
            subj_rows = conn.execute(
                f"""
                SELECT m.subject_bucket, COUNT(DISTINCT i.id) AS count
                FROM issues i
                JOIN messages m ON m.id = i.message_id
                {where}
                GROUP BY m.subject_bucket ORDER BY count DESC
                """,
                params,
            ).fetchall()

        return {
            "coverage": dict(coverage),
            "issue_count": totals["issue_count"],
            "question_count": totals["question_count"],
            "unreplied_count": totals["unreplied_count"],
            "category_count": totals["category_count"],
            "replied_count": totals["replied_count"],
            "ack_only_count": totals["ack_only_count"],
            "solved_count": totals["solved_count"],
            "pending_count": totals["pending_count"],
            "categories": [dict(r) for r in cat_rows],
            "subjects": [dict(r) for r in subj_rows],
        }

    def list_conversations(self, account_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            where = "WHERE account_id = ?" if account_id else ""
            params = [account_id] if account_id else []
            rows = conn.execute(
                f"""
                SELECT conversation_id, conversation_name, COUNT(*) AS message_count,
                       MIN(sent_at) AS first_sent_at, MAX(sent_at) AS last_sent_at
                FROM messages
                {where}
                GROUP BY conversation_id, conversation_name
                ORDER BY message_count DESC
                """,
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def list_issues(
        self,
        account_id: str | None = None,
        subject: str | None = None,
        category: str | None = None,
        status: str | None = None,
        limit: int = 200,
        conversation_id: str | None = None,
        conversation_name: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if account_id:
            clauses.append("m.account_id = ?")
            params.append(account_id)
        if conversation_id:
            clauses.append("(m.conversation_id=? OR instr(m.conversation_id, ? || ':forwarded:')=1)")
            params.extend([conversation_id, conversation_id])
        if conversation_name:
            clauses.append("instr(lower(m.conversation_name), lower(?))>0")
            params.append(conversation_name)
        if subject in ("zhongji", "other", "unknown"):
            clauses.append("m.subject_bucket = ?")
            params.append(subject)
        if category:
            clauses.append("i.category = ?")
            params.append(category)
        if status:
            clauses.append("i.status = ?")
            params.append(status)

        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(limit, 10000)))

        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    i.id AS issue_id,
                    i.category,
                    i.confidence AS issue_confidence,
                    i.summary AS issue_summary,
                    i.status AS issue_status,
                    i.status_reason,
                    i.evidence_json,
                    i.clues_json,
                    m.id AS message_pk,
                    m.account_id,
                    m.conversation_id,
                    m.conversation_name,
                    m.sent_at AS question_sent_at,
                    m.sender_id AS question_sender_id,
                    m.sender_name AS question_sender_name,
                    m.sender_corp_id AS question_corp_id,
                    m.sender_corp_name AS question_corp_name,
                    m.subject_bucket AS question_subject_bucket,
                    m.subject_basis AS question_subject_basis,
                    m.text AS question_raw_text,
                    m.source_reference,
                    m.parent_id,
                    m.root_message_id,
                    m.nesting_depth,
                    m.provenance_json,
                    r.response_kind,
                    r.is_solution,
                    r.solution_text,
                    r.latency_seconds,
                    r.association_basis,
                    (SELECT MIN(rr.latency_seconds) FROM responses rr WHERE rr.issue_id=i.id) AS first_response_seconds,
                    (SELECT MIN(rr.latency_seconds) FROM responses rr WHERE rr.issue_id=i.id AND rr.response_kind='即时响应') AS first_ack_seconds,
                    (SELECT MIN(rr.latency_seconds) FROM responses rr WHERE rr.issue_id=i.id AND rr.is_solution=1) AS solution_seconds,
                    (SELECT MAX(rr.latency_seconds) FROM responses rr WHERE rr.issue_id=i.id AND rr.is_solution=1) AS final_solution_seconds,
                    rm.id AS response_message_pk,
                    rm.sender_name AS responder_name,
                    rm.sender_corp_name AS responder_corp_name,
                    rm.subject_bucket AS responder_subject_bucket,
                    rm.text AS responder_raw_text,
                    rm.sent_at AS response_sent_at
                FROM issues i
                JOIN messages m ON m.id = i.message_id
                LEFT JOIN responses r ON r.id = (
                    SELECT rr.id FROM responses rr
                    WHERE rr.issue_id = i.id
                    ORDER BY rr.is_solution DESC, (rr.status_contribution='in_progress') DESC, rr.latency_seconds ASC
                    LIMIT 1
                )
                LEFT JOIN messages rm ON rm.id = r.response_message_id
                {where}
                ORDER BY m.sent_at DESC, i.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

        result = []
        for r in rows:
            item = dict(r)
            item["evidence"] = json.loads(item.pop("evidence_json", "[]"))
            item["clues"] = json.loads(item.pop("clues_json", "{}"))
            item["provenance"] = json.loads(item.pop("provenance_json", "{}"))
            from .wecom_analysis import split_quoted_reply
            saved_response_clues = item["clues"].pop("response_entities", {}).get(item.get("response_message_pk"))
            item["response_clues"] = saved_response_clues or extract_business_clues(split_quoted_reply(item.get("responder_raw_text") or "")[1]).as_dict()
            result.append(item)
        return result

    def get_cursor(self, account_id: str, source_database: str) -> tuple[int, str]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT sequence, message_id FROM import_cursors WHERE account_id=? AND source_database=?",
                (account_id, source_database),
            ).fetchone()
            if row:
                return int(row["sequence"]), str(row["message_id"])
        return 0, ""

    def get_report(self, *, account_id=None, conversation_id=None, conversation_name=None,
                   subject=None, category=None, status=None) -> dict[str, Any]:
        from .wecom_report import build_report
        clauses, params = [], []
        if account_id:
            clauses.append("m.account_id=?")
            params.append(account_id)
        if conversation_id:
            clauses.append("(m.conversation_id=? OR instr(m.conversation_id, ? || ':forwarded:')=1)")
            params.extend([conversation_id, conversation_id])
        if conversation_name:
            clauses.append("instr(lower(m.conversation_name), lower(?))>0")
            params.append(conversation_name)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            messages = [dict(r) for r in conn.execute(f"SELECT m.* FROM messages m {where} ORDER BY m.sent_at,m.source_reference", params)]
            issues = [dict(r) for r in conn.execute(f"SELECT i.* FROM issues i JOIN messages m ON m.id=i.message_id {where}", params)]
            responses = [dict(r) for r in conn.execute(f"SELECT r.* FROM responses r JOIN issues i ON i.id=r.issue_id JOIN messages m ON m.id=i.message_id {where}", params)]
            known = {m['id'] for m in messages}
            pending = {m['parent_id'] for m in messages if m['parent_id']} - known
            ancestors = []
            while pending:
                parents = [dict(r) for r in conn.execute(
                    'SELECT * FROM messages WHERE id IN ('+','.join('?' for _ in pending)+')',list(pending))]
                known.update(pending)
                ancestors.extend(parents)
                pending = {m['parent_id'] for m in parents if m['parent_id']} - known
        report = build_report(messages, issues, responses, subject=subject, category=category, status=status)
        report['ancestors'] = build_report(ancestors, [], [])['messages'] if ancestors else []
        return report

    def set_cursor(self, account_id: str, source_database: str, sequence: int, message_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO import_cursors (account_id, source_database, sequence, message_id, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_id, source_database) DO UPDATE SET
                    sequence=excluded.sequence,
                    message_id=excluded.message_id,
                    updated_at=excluded.updated_at
                """,
                (account_id, source_database, sequence, message_id, now),
            )
