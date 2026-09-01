from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from .models import Classification, Message, ResponseAssessment, SubjectMatch


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    sender_display TEXT NOT NULL,
    sender_subject TEXT,
    subject_bucket TEXT NOT NULL CHECK (subject_bucket IN ('zhongji', 'other')),
    direction TEXT NOT NULL,
    content TEXT NOT NULL,
    content_type TEXT NOT NULL,
    raw_ref TEXT,
    ingested_at TEXT NOT NULL,
    UNIQUE(source, source_message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_time
ON messages(conversation_id, sent_at);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    UNIQUE(message_id, category)
);

CREATE INDEX IF NOT EXISTS idx_issues_category ON issues(category);

CREATE TABLE IF NOT EXISTS responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    response_kind TEXT NOT NULL,
    is_solution INTEGER NOT NULL CHECK (is_solution IN (0, 1)),
    confidence REAL NOT NULL,
    latency_seconds INTEGER NOT NULL,
    UNIQUE(issue_id, message_id)
);

CREATE TABLE IF NOT EXISTS source_events (
    source_key TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    modified_ns INTEGER NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_cursors (
    source_key TEXT PRIMARY KEY,
    cursor_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class Storage:
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
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def upsert_message(self, message: Message, subject: SubjectMatch) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO messages (
                    id, source, source_message_id, conversation_id, sent_at,
                    sender_display, sender_subject, subject_bucket, direction,
                    content, content_type, raw_ref, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    conversation_id=excluded.conversation_id,
                    sent_at=excluded.sent_at,
                    sender_display=excluded.sender_display,
                    sender_subject=excluded.sender_subject,
                    subject_bucket=excluded.subject_bucket,
                    direction=excluded.direction,
                    content=excluded.content,
                    content_type=excluded.content_type,
                    raw_ref=excluded.raw_ref
                """,
                (
                    message.id,
                    message.source,
                    message.source_message_id,
                    message.conversation_id,
                    message.sent_at.isoformat(),
                    message.sender_display,
                    subject.raw_subject,
                    subject.bucket,
                    message.direction,
                    message.content,
                    message.content_type,
                    message.raw_ref,
                    now,
                ),
            )

    def rebuild_analysis(
        self,
        *,
        semantic: bool | None = None,
        analyzer: object | None = None,
    ) -> dict[str, Any]:
        from .classifier import assess_response, classify_issue, needs_semantic, summarize_problem
        from .semantic import SemanticAnalyzer, SemanticConfigError, SemanticSettings, merge_assessment, merge_classifications

        if analyzer is None:
            analyzer = SemanticAnalyzer()
        if semantic is True and not analyzer.enabled:
            raise SemanticConfigError("启用语义识别需要设置 MINIMAX_API_KEY")
        if semantic is False:
            analyzer = SemanticAnalyzer(settings=SemanticSettings(api_key=""))

        with self.connect() as connection:
            connection.execute("DELETE FROM responses")
            connection.execute("DELETE FROM issues")
            messages = connection.execute(
                "SELECT * FROM messages ORDER BY conversation_id, sent_at, id"
            ).fetchall()

            keyword_hits: dict[str, list] = {}
            semantic_candidates: list[tuple[str, str]] = []
            for row in messages:
                hits = classify_issue(row["content"])
                keyword_hits[row["id"]] = hits
                if analyzer.enabled and needs_semantic(row["content"], hits):
                    semantic_candidates.append((row["id"], row["content"]))
            semantic_hits = analyzer.classify_many(semantic_candidates)

            by_conversation: dict[str, list[sqlite3.Row]] = {}
            for row in messages:
                by_conversation.setdefault(row["conversation_id"], []).append(row)

            issue_count = 0
            pending_assess: dict[str, str] = {}
            for conversation in by_conversation.values():
                issue_rows: list[tuple[int, sqlite3.Row]] = []
                issue_message_ids: set[str] = set()
                for row in conversation:
                    classifications = merge_classifications(
                        row["content"],
                        keyword_hits.get(row["id"], []),
                        semantic_hits.get(row["id"]),
                    )
                    for classification in classifications:
                        issue_message_ids.add(row["id"])
                        cursor = connection.execute(
                            """
                            INSERT INTO issues (
                                message_id, category, confidence, evidence_json, summary
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                row["id"],
                                classification.category,
                                classification.confidence,
                                json.dumps(classification.evidence, ensure_ascii=False),
                                summarize_problem(row["content"]),
                            ),
                        )
                        issue_rows.append((cursor.lastrowid, row))
                        issue_count += 1

                for issue_id, issue_message in issue_rows:
                    issue_time = datetime.fromisoformat(issue_message["sent_at"])
                    for candidate in conversation:
                        candidate_time = datetime.fromisoformat(candidate["sent_at"])
                        if candidate_time <= issue_time:
                            continue
                        latency = int((candidate_time - issue_time).total_seconds())
                        if latency > 2 * 60 * 60:
                            break
                        if candidate["sender_display"] == issue_message["sender_display"]:
                            if candidate["id"] in issue_message_ids:
                                break
                            continue
                        if candidate["id"] in issue_message_ids:
                            continue
                        assessment = assess_response(candidate["content"])
                        if analyzer.enabled and assessment.kind == "一般回复":
                            pending_assess[candidate["id"]] = candidate["content"]
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO responses (
                                issue_id, message_id, response_kind, is_solution,
                                confidence, latency_seconds
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                issue_id,
                                candidate["id"],
                                assessment.kind,
                                int(assessment.is_solution),
                                assessment.confidence,
                                latency,
                            ),
                        )
                        if assessment.is_solution:
                            break

            semantic_assess = analyzer.assess_many(list(pending_assess.items()))
            for message_id, assessment in semantic_assess.items():
                merged = merge_assessment(assess_response(pending_assess[message_id]), assessment)
                connection.execute(
                    """
                    UPDATE responses
                    SET response_kind=?, is_solution=?, confidence=?
                    WHERE message_id=? AND response_kind='一般回复'
                    """,
                    (merged.kind, int(merged.is_solution), merged.confidence, message_id),
                )

        return {
            "messages": len(messages),
            "issues": issue_count,
            "semantic_enabled": analyzer.enabled,
            "semantic_texts": len(semantic_candidates),
            "semantic_calls": analyzer.calls,
            "model": analyzer.settings.model if analyzer.enabled else None,
        }

    def summary(self, subject: str | None = None) -> dict[str, Any]:
        where = "WHERE m.subject_bucket = ?" if subject in {"zhongji", "other"} else ""
        params: tuple[Any, ...] = (subject,) if where else ()
        with self.connect() as connection:
            totals = connection.execute(
                f"""
                SELECT
                    COUNT(DISTINCT i.id) AS issue_count,
                    COUNT(DISTINCT i.category) AS category_count,
                    COUNT(DISTINCT r.issue_id) AS replied_count,
                    COUNT(DISTINCT CASE WHEN r.is_solution=1 THEN r.issue_id END) AS solved_count
                FROM issues i
                JOIN messages m ON m.id=i.message_id
                LEFT JOIN responses r ON r.id = (
                    SELECT rr.id FROM responses rr
                    WHERE rr.issue_id=i.id
                    ORDER BY rr.is_solution DESC, rr.latency_seconds ASC
                    LIMIT 1
                )
                {where}
                """,
                params,
            ).fetchone()
            categories = connection.execute(
                f"""
                SELECT i.category, COUNT(*) AS count
                FROM issues i JOIN messages m ON m.id=i.message_id
                {where}
                GROUP BY i.category ORDER BY count DESC, i.category
                """,
                params,
            ).fetchall()
        return {
            "issue_count": totals["issue_count"],
            "category_count": totals["category_count"],
            "replied_count": totals["replied_count"],
            "solved_count": totals["solved_count"],
            "categories": [dict(row) for row in categories],
        }

    def list_issues(
        self,
        subject: str | None = None,
        category: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if subject in {"zhongji", "other"}:
            clauses.append("m.subject_bucket = ?")
            params.append(subject)
        if category:
            clauses.append("i.category = ?")
            params.append(category)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(limit, 1000)))

        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    i.id, i.category, i.confidence, i.summary, i.status,
                    m.sent_at, m.sender_display, m.sender_subject,
                    m.subject_bucket, m.content,
                    rm.content AS response_text,
                    r.response_kind, r.is_solution, r.latency_seconds
                FROM issues i
                JOIN messages m ON m.id=i.message_id
                LEFT JOIN responses r ON r.id = (
                    SELECT rr.id FROM responses rr
                    WHERE rr.issue_id=i.id
                    ORDER BY rr.is_solution DESC, rr.latency_seconds ASC
                    LIMIT 1
                )
                LEFT JOIN messages rm ON rm.id=r.message_id
                {where}
                ORDER BY m.sent_at DESC, i.id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_source_event(self, source_key: str, path: str, size: int, modified_ns: int) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT size, modified_ns FROM source_events WHERE source_key=?",
                (source_key,),
            ).fetchone()
            changed = existing is None or existing["size"] != size or existing["modified_ns"] != modified_ns
            connection.execute(
                """
                INSERT INTO source_events(source_key, path, size, modified_ns, observed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    path=excluded.path,
                    size=excluded.size,
                    modified_ns=excluded.modified_ns,
                    observed_at=excluded.observed_at
                """,
                (source_key, path, size, modified_ns, now),
            )
        return changed

    def get_cursor(self, source_key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cursor_json FROM import_cursors WHERE source_key=?",
                (source_key,),
            ).fetchone()
        return str(row["cursor_json"]) if row else None

    def set_cursor(self, source_key: str, cursor_json: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO import_cursors(source_key, cursor_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    cursor_json=excluded.cursor_json,
                    updated_at=excluded.updated_at
                """,
                (source_key, cursor_json, now),
            )

