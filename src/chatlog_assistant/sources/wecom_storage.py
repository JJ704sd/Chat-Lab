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
                        source_reference, ingested_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, source_database, message_id) DO UPDATE SET
                        server_id=excluded.server_id,
                        conversation_id=excluded.conversation_id,
                        conversation_name=excluded.conversation_name,
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
                        source_reference=excluded.source_reference
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
                        r.sent_at.isoformat(),
                        r.text,
                        r.reply_to_message_id,
                        r.parse_status,
                        r.source_reference,
                        now,
                    ),
                )
                count += 1
        return count

    def rebuild_analysis(
        self,
        semantic_analyzer: Any | None = None,
    ) -> dict[str, Any]:
        """Runs issue identification, response association, and status evaluation.
        Supports both rule-based engine and MiniMax-M3 LLM semantic analyzer.
        """
        with self.connect() as conn:
            conn.execute("DELETE FROM responses")
            conn.execute("DELETE FROM issues")

            messages = conn.execute(
                "SELECT * FROM messages ORDER BY conversation_id, sent_at, id"
            ).fetchall()

            # Group messages by conversation
            by_conversation: dict[str, list[sqlite3.Row]] = {}
            for row in messages:
                by_conversation.setdefault(row["conversation_id"], []).append(row)

            # Optional Semantic Analysis pre-pass
            semantic_issues: dict[str, tuple[list[Any], str]] = {}
            semantic_responses: dict[str, Any] = {}

            if semantic_analyzer and getattr(semantic_analyzer, "enabled", False):
                # 1. Collect candidate issue messages
                issue_candidates = []
                for row in messages:
                    t = row["text"]
                    if t and row["parse_status"] != "unparsed_media" and len(t.strip()) >= 2:
                        issue_candidates.append((row["id"], t))
                if issue_candidates:
                    semantic_issues = semantic_analyzer.classify_issues_batch(issue_candidates)

                # 2. Collect candidate response messages
                resp_candidates = []
                for row in messages:
                    t = row["text"]
                    if t and row["parse_status"] != "unparsed_media":
                        resp_candidates.append((row["id"], t))
                if resp_candidates:
                    semantic_responses = semantic_analyzer.assess_responses_batch(resp_candidates)

            total_issues = 0
            for conv_id, conv_msgs in by_conversation.items():
                issue_rows: list[tuple[int, sqlite3.Row, list[Any], Any]] = []

                # Step 1: Identify issues in the conversation
                for row in conv_msgs:
                    text = row["text"]
                    if not text or row["parse_status"] == "unparsed_media":
                        continue

                    rule_classifications = classify_logistics_issue(text)
                    clues = extract_business_clues(text)

                    # Merge with semantic LLM if available
                    final_classifications = rule_classifications
                    issue_summary = text[:140]

                    if row["id"] in semantic_issues:
                        llm_matches, llm_sum = semantic_issues[row["id"]]
                        if llm_matches:
                            final_classifications = llm_matches
                            if llm_sum:
                                issue_summary = llm_sum
                        elif not llm_matches and rule_classifications:
                            # If LLM says not an issue, only keep if rule had strong specific logistics keyword
                            high_conf_rules = [c for c in rule_classifications if c.category != "其他待分类问题" and c.confidence >= 0.93]
                            final_classifications = high_conf_rules

                    for c in final_classifications:
                        cursor = conn.execute(
                            """
                            INSERT INTO issues (
                                message_id, category, confidence, evidence_json, clues_json, summary, status
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                row["id"],
                                c.category,
                                c.confidence,
                                json.dumps(c.evidence, ensure_ascii=False),
                                json.dumps(clues.as_dict(), ensure_ascii=False),
                                issue_summary,
                                "unreplied",
                            ),
                        )
                        issue_id = cursor.lastrowid
                        issue_rows.append((issue_id, row, c.evidence, clues))
                        total_issues += 1

                # Step 2: Match responses for each issue
                for issue_id, issue_msg, _evidence, issue_clues in issue_rows:
                    issue_time = datetime.fromisoformat(issue_msg["sent_at"])
                    issue_sender = issue_msg["sender_id"]

                    best_status = "unreplied"
                    status_reason = "暂无回复消息"

                    for candidate in conv_msgs:
                        cand_time = datetime.fromisoformat(candidate["sent_at"])
                        if cand_time <= issue_time:
                            continue

                        latency = int((cand_time - issue_time).total_seconds())
                        # Context window: 4 hours
                        if latency > 4 * 3600:
                            break

                        # Same sender replying themselves
                        if candidate["sender_id"] == issue_sender:
                            continue

                        # Assess candidate response (LLM or rule)
                        cand_text = candidate["text"]
                        if candidate["id"] in semantic_responses:
                            assessment = semantic_responses[candidate["id"]]
                        else:
                            assessment = assess_logistics_response(cand_text)

                        # Determine association basis
                        assoc_basis = "conversation_context"
                        if candidate["reply_to_message_id"] == issue_msg["message_id"]:
                            assoc_basis = "explicit_reply_reference"
                        elif issue_clues.waybill_no and issue_clues.waybill_no in cand_text:
                            assoc_basis = "waybill_clue_match"

                        conn.execute(
                            """
                            INSERT OR IGNORE INTO responses (
                                issue_id, response_message_id, response_kind, is_solution,
                                solution_text, status_contribution, confidence, latency_seconds, association_basis
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                issue_id,
                                candidate["id"],
                                assessment.kind,
                                int(assessment.is_solution),
                                assessment.solution_text,
                                assessment.status_contribution,
                                assessment.confidence,
                                latency,
                                assoc_basis,
                            ),
                        )

                        # Update issue status
                        if assessment.is_solution:
                            best_status = "solved"
                            sol_text = assessment.solution_text or cand_text
                            status_reason = f"由 {candidate['sender_name']} 给出方案: {sol_text[:80]}"
                            break
                        elif assessment.status_contribution == "in_progress" and best_status in ("unreplied", "acknowledged"):
                            best_status = "in_progress"
                            status_reason = f"由 {candidate['sender_name']} 跟进中: {cand_text[:80]}"
                        elif assessment.status_contribution == "acknowledged" and best_status == "unreplied":
                            best_status = "acknowledged"
                            status_reason = f"由 {candidate['sender_name']} 确认收到: {cand_text[:80]}"

                    conn.execute(
                        "UPDATE issues SET status=?, status_reason=? WHERE id=?",
                        (best_status, status_reason, issue_id),
                    )

        return {"messages": len(messages), "issues": total_issues}

    def get_summary(
        self,
        account_id: str | None = None,
        subject: str | None = None,
        category: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        clauses = []
        params: list[Any] = []
        if account_id:
            clauses.append("m.account_id = ?")
            params.append(account_id)
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
            # Stats by status
            totals = conn.execute(
                f"""
                SELECT
                    COUNT(DISTINCT i.id) AS issue_count,
                    COUNT(DISTINCT i.category) AS category_count,
                    COUNT(DISTINCT CASE WHEN i.status != 'unreplied' THEN i.id END) AS replied_count,
                    COUNT(DISTINCT CASE WHEN i.status = 'acknowledged' THEN i.id END) AS ack_only_count,
                    COUNT(DISTINCT CASE WHEN i.status = 'solved' THEN i.id END) AS solved_count,
                    COUNT(DISTINCT CASE WHEN i.status IN ('unreplied', 'in_progress', 'open') THEN i.id END) AS pending_count
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
            "issue_count": totals["issue_count"],
            "category_count": totals["category_count"],
            "replied_count": totals["replied_count"],
            "ack_only_count": totals["ack_only_count"],
            "solved_count": totals["solved_count"],
            "pending_count": totals["pending_count"],
            "categories": [dict(r) for r in cat_rows],
            "subjects": [dict(r) for r in subj_rows],
        }

    def list_issues(
        self,
        account_id: str | None = None,
        subject: str | None = None,
        category: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if account_id:
            clauses.append("m.account_id = ?")
            params.append(account_id)
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
        params.append(max(1, min(limit, 1000)))

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
                    r.response_kind,
                    r.is_solution,
                    r.solution_text,
                    r.latency_seconds,
                    r.association_basis,
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
                    ORDER BY rr.is_solution DESC, rr.latency_seconds ASC
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
