from __future__ import annotations

from contextlib import contextmanager
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping, Sequence

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
    company_status TEXT NOT NULL DEFAULT 'unknown' CHECK (company_status IN ('known', 'unknown', 'conflict')),
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

-- Explicit, scoped business-role confirmations; never inferred from message text.
CREATE TABLE IF NOT EXISTS business_role_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL, source_database TEXT NOT NULL, conversation_id TEXT NOT NULL,
    company_id TEXT, company_name TEXT NOT NULL,
    business_role TEXT NOT NULL CHECK (business_role IN ('supplier','unknown')),
    basis TEXT NOT NULL, confirmed_by TEXT NOT NULL, confirmed_name TEXT NOT NULL,
    confirmed_at TEXT NOT NULL
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
            from .wecom_pricing import PRICE_SCHEMA
            conn.executescript(PRICE_SCHEMA)
            # Additive migration: old readers/writers and old rows stay valid.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
            for name, definition in (("parent_id", "TEXT"), ("root_message_id", "TEXT"),
                                     ("nesting_depth", "INTEGER NOT NULL DEFAULT 0"),
                                     ("provenance_json", "TEXT NOT NULL DEFAULT '{}'"),
                                     ("company_status", "TEXT NOT NULL DEFAULT 'unknown'")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_company ON messages(sender_corp_id, sender_corp_name)")
            price_preview_columns = {row[1] for row in conn.execute("PRAGMA table_info(price_import_previews)")}
            for name, definition in (("confirmed_request_id", "TEXT"), ("confirmation_result_json", "TEXT")):
                if name not in price_preview_columns:
                    conn.execute(f"ALTER TABLE price_import_previews ADD COLUMN {name} {definition}")

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
                        company_status, message_type, sent_at, text, reply_to_message_id, parse_status,
                        source_reference, ingested_at, parent_id, root_message_id, nesting_depth, provenance_json
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
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
                        company_status=excluded.company_status,
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
                        ("known" if r.sender_corp_name and r.company_status == "unknown" else r.company_status),
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
        price_sync = {"candidates": 0, "applied": 0}
        try:
            from .wecom_pricing import PriceMaintenance
            price_sync = PriceMaintenance(self).sync_chat_candidates(
                account_id=account_id, conversation_id=conversation_id,
                conversation_name=conversation_name,
            )
        except Exception as exc:
            # Price maintenance is an additive boundary.  A malformed quote
            # must not make the legacy analysis rebuild fail or delete data.
            price_sync = {"candidates": 0, "applied": 0, "error": type(exc).__name__}
        return {"messages": len(messages), "issues": total_issues,
                "semantic_enabled": bool(semantic_analyzer and semantic_analyzer.enabled),
                "semantic_calls": getattr(semantic_analyzer, "calls", 0),
                "semantic_errors": getattr(semantic_analyzer, "errors", 0),
                "price_maintenance": price_sync}

    def get_summary(
        self,
        account_id: str | None = None,
        source_database: str | None = None,
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
        if source_database:
            clauses.append("(m.source_database=? OR instr(m.source_database, ? || ':forwarded:')=1)")
            params.extend([source_database, source_database])
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
            if source_database:
                coverage_clauses.append("(source_database=? OR instr(source_database, ? || ':forwarded:')=1)")
                coverage_params.extend([source_database, source_database])
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

    def _apply_company_overrides(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply confirmed display-layer corrections without changing messages."""
        if not rows:
            return rows
        from .wecom_subject import sender_label
        with self.connect() as conn:
            corrections = [dict(row) for row in conn.execute(
                "SELECT * FROM company_corrections WHERE status='applied'"
            )]
            aliases = [dict(row) for row in conn.execute(
                "SELECT * FROM company_aliases"
            )]
        for row in rows:
            for correction in corrections:
                if row.get("account_id") != correction.get("account_id"):
                    continue
                correction_source = correction.get("source_database") or ""
                row_source = row.get("source_database") or ""
                if row_source != correction_source and not row_source.startswith(correction_source + ":forwarded:"):
                    continue
                if correction.get("conversation_id"):
                    corrected_scope = correction["conversation_id"]
                    row_scope = row.get("conversation_id") or ""
                    if row_scope != corrected_scope and not row_scope.startswith(corrected_scope + ":forwarded:"):
                        continue
                message_ids = set(json.loads(correction.get("message_ids_json") or "[]"))
                sender_ids = set(json.loads(correction.get("sender_ids_json") or "[]"))
                if message_ids and row.get("id") not in message_ids and row.get("message_id") not in message_ids:
                    if not sender_ids or row.get("sender_id") not in sender_ids:
                        continue
                elif not message_ids and sender_ids and row.get("sender_id") not in sender_ids:
                    continue
                if correction.get("original_corp_id") and row.get("sender_corp_id") not in (None, correction["original_corp_id"]):
                    continue
                if correction.get("original_corp_name") and row.get("sender_corp_name") not in (None, correction["original_corp_name"]):
                    continue
                row["sender_corp_id"] = correction["normalized_company_id"]
                row["sender_corp_name"] = correction["normalized_company_name"]
                row["company_status"] = "known"
                row["company_correction_id"] = correction["correction_id"]
                row["company_correction_basis"] = correction["basis"]
                row["sender_label"] = sender_label(row.get("sender_name"), row.get("sender_corp_name"), company_status="known")
                break
            else:
                # A confirmed shorthand mapping is also a display-layer
                # overlay.  The raw contact/message columns remain intact.
                for alias in aliases:
                    if row.get("account_id") != alias.get("account_id"):
                        continue
                    alias_source = alias.get("source_database") or ""
                    row_source = row.get("source_database") or ""
                    if row_source != alias_source and not row_source.startswith(alias_source + ":forwarded:"):
                        continue
                    if row.get("sender_corp_name") != alias.get("alias_name"):
                        continue
                    row["sender_corp_id"] = alias["normalized_company_id"]
                    row["sender_corp_name"] = alias["normalized_company_name"]
                    row["company_status"] = "known"
                    row["company_alias_id"] = alias["alias_id"]
                    row["company_alias_basis"] = alias["basis"]
                    row["sender_label"] = sender_label(row.get("sender_name"), row.get("sender_corp_name"), company_status="known")
                    break
        return rows

    @staticmethod
    def _message_view(row: Mapping[str, Any], *, include_text: bool = False) -> dict[str, Any]:
        from .wecom_report import display_safe_text
        from .wecom_subject import sender_label
        result = dict(row)
        result["provenance"] = json.loads(result.pop("provenance_json", "{}") or "{}")
        result["company_status"] = result.get("company_status") or ("known" if result.get("sender_corp_name") else "unknown")
        result["sender_label"] = result.get("sender_label") or sender_label(
            result.get("sender_name"), result.get("sender_corp_name"), company_status=result["company_status"]
        )
        raw_text = str(result.pop("text", "") or "")
        result["text_preview"] = display_safe_text(raw_text)[:160]
        if include_text:
            result["text"] = display_safe_text(raw_text)
        return result

    def list_messages(
        self, *, account_id: str | None = None, source_database: str | None = None,
        conversation_id: str | None = None, conversation_name: str | None = None,
        author_id: str | None = None, company: str | None = None, keyword: str | None = None,
        message_type: str | None = None, start_at: str | None = None, end_at: str | None = None,
        cursor: str | None = None, limit: int = 100, include_text: bool = False,
    ) -> dict[str, Any]:
        """Search all imported messages independently of opened report rounds."""
        offset = 0
        if cursor:
            try:
                import base64
                offset = int(base64.urlsafe_b64decode(cursor.encode()).decode())
            except Exception as exc:
                raise ValueError("游标无效") from exc
        limit = max(1, min(int(limit), 500))
        base_clauses, base_params = [], []
        if account_id:
            base_clauses.append("m.account_id=?")
            base_params.append(account_id)
        if source_database:
            base_clauses.append("(m.source_database=? OR instr(m.source_database, ? || ':forwarded:')=1)")
            base_params.extend([source_database, source_database])
        if conversation_id:
            base_clauses.append("(m.conversation_id=? OR instr(m.conversation_id, ? || ':forwarded:')=1)")
            base_params.extend([conversation_id, conversation_id])
        if conversation_name:
            base_clauses.append("instr(lower(m.conversation_name),lower(?))>0")
            base_params.append(conversation_name)
        base_where = "WHERE " + " AND ".join(base_clauses) if base_clauses else ""
        match_clauses = list(base_clauses)
        params = list(base_params)
        if author_id:
            match_clauses.append("m.sender_id=?"); params.append(author_id)
        # A confirmed company correction is a display-layer overlay, so a
        # company search cannot be restricted to the raw columns alone.
        # Leave this predicate for the bounded in-memory filtering path below.
        if keyword:
            match_clauses.append("(m.text LIKE ? OR m.sender_name LIKE ? OR m.message_id LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"])
        if message_type:
            match_clauses.append("m.message_type=?"); params.append(message_type)
        if start_at:
            match_clauses.append("m.sent_at>=?"); params.append(start_at)
        if end_at:
            match_clauses.append("m.sent_at<=?"); params.append(end_at)
        match_where = "WHERE " + " AND ".join(match_clauses) if match_clauses else ""
        with self.connect() as conn:
            range_total = int(conn.execute(f"SELECT COUNT(*) FROM messages m {base_where}", base_params).fetchone()[0])
            if company:
                candidate_rows = [dict(row) for row in conn.execute(
                    f"SELECT m.* FROM messages m {match_where} ORDER BY CASE WHEN m.sent_at='' THEN 1 ELSE 0 END,m.sent_at,m.source_reference,m.id",
                    params,
                )]
            else:
                matched_total = int(conn.execute(f"SELECT COUNT(*) FROM messages m {match_where}", params).fetchone()[0])
                candidate_rows = [dict(row) for row in conn.execute(
                    f"SELECT m.* FROM messages m {match_where} ORDER BY CASE WHEN m.sent_at='' THEN 1 ELSE 0 END,m.sent_at,m.source_reference,m.id LIMIT ? OFFSET ?",
                    (*params, limit, offset),
                )]
        candidate_rows = self._apply_company_overrides(candidate_rows)
        if company:
            wanted = company.casefold().strip()
            def company_matches(row: Mapping[str, Any]) -> bool:
                status = row.get("company_status") or ("known" if row.get("sender_corp_name") else "unknown")
                name = str(row.get("sender_corp_name") or "").casefold()
                corp_id = str(row.get("sender_corp_id") or "").casefold()
                if wanted in {"公司待确认", "conflict"}:
                    return status == "conflict"
                if wanted in {"公司未知", "unknown"}:
                    return status == "unknown" and not name
                return wanted in name or wanted == corp_id
            matched_rows = [row for row in candidate_rows if company_matches(row)]
            matched_total = len(matched_rows)
            candidate_rows = matched_rows[offset:offset + limit]
        rows = candidate_rows
        items = [self._message_view(row, include_text=include_text) for row in rows]
        import base64
        next_cursor = base64.urlsafe_b64encode(str(offset + len(items)).encode()).decode() if offset + len(items) < matched_total else None
        return {"items": items, "total": matched_total, "range_total": range_total, "matched_total": matched_total,
                "loaded_count": len(items), "next_cursor": next_cursor, "limit": limit,
                "range": {"account_id": account_id, "source_database": source_database, "conversation_id": conversation_id,
                          "local_imported_scope": True, "cloud_history_complete": False}}

    def get_message_evidence(
        self, message_id: str, *, account_id: str | None = None, source_database: str | None = None,
        conversation_id: str | None = None, conversation_name: str | None = None,
        context_limit: int = 20,
    ) -> dict[str, Any] | None:
        """Return one message with quoted/forwarded ancestors and explicit gaps."""
        scope_where, scope_params = self._report_scope_sql(account_id=account_id, conversation_id=conversation_id, conversation_name=conversation_name)
        clauses = ["(m.id=? OR m.message_id=?)"]
        params = [message_id, message_id]
        if source_database:
            clauses.append("(m.source_database=? OR instr(m.source_database, ? || ':forwarded:')=1)")
            params.extend([source_database, source_database])
        if scope_where:
            clauses.append(scope_where.removeprefix("WHERE "))
            params.extend(scope_params)
        with self.connect() as conn:
            target_rows = [dict(row) for row in conn.execute("SELECT m.* FROM messages m WHERE " + " AND ".join(clauses), params)]
            if not target_rows:
                return None
            if not any(row["id"] == message_id for row in target_rows) and len(target_rows) > 1:
                # An external source message ID may repeat across accounts or
                # source databases; never choose an arbitrary matching row.
                return None
            target = next((row for row in target_rows if row["id"] == message_id), target_rows[0])
            known: dict[str, dict[str, Any]] = {target["id"]: target}
            known_refs = {target["id"], target.get("message_id")}
            ancestors: list[dict[str, Any]] = []
            missing: list[str] = []
            missing_refs: set[str] = set()
            pending = [target.get("parent_id")] if target.get("parent_id") else []
            if target.get("reply_to_message_id"):
                pending.append(target["reply_to_message_id"])
            while pending:
                ref = pending.pop(0)
                if not ref or ref in known or ref in known_refs:
                    continue
                parent_rows = [dict(candidate) for candidate in conn.execute(
                    "SELECT * FROM messages WHERE account_id=? AND (id=? OR message_id=? OR server_id=?)",
                    (target["account_id"], ref, ref, ref),
                )]
                parent_rows = [candidate for candidate in parent_rows if self._ancestor_in_context(candidate, target)]
                exact_parent = next((candidate for candidate in parent_rows if candidate['id'] == ref), None)
                parent = exact_parent or (parent_rows[0] if len(parent_rows) == 1 else None)
                if parent is None:
                    if str(ref) not in missing_refs:
                        missing.append(str(ref)); missing_refs.add(str(ref))
                    continue
                if parent["id"] in known or parent.get("message_id") in known_refs:
                    continue
                known[parent["id"]] = parent
                known_refs.add(parent.get("message_id"))
                ancestors.append(parent)
                if parent.get("parent_id"):
                    pending.append(parent["parent_id"])
            # Adjacent messages always stay in the exact original conversation,
            # including a forwarded conversation's boundary.
            context_limit = max(1, min(int(context_limit), 200))
            exact = (target['account_id'], target['source_database'], target['conversation_id'])
            before = [dict(row) for row in conn.execute(
                "SELECT * FROM messages WHERE account_id=? AND source_database=? AND conversation_id=? "
                "AND (sent_at < ? OR (sent_at=? AND id<?)) ORDER BY sent_at DESC,id DESC LIMIT ?",
                (*exact, target['sent_at'], target['sent_at'], target['id'], context_limit + 1))]
            after = [dict(row) for row in conn.execute(
                "SELECT * FROM messages WHERE account_id=? AND source_database=? AND conversation_id=? "
                "AND (sent_at > ? OR (sent_at=? AND id>?)) ORDER BY sent_at,id LIMIT ?",
                (*exact, target['sent_at'], target['sent_at'], target['id'], context_limit + 1))]
            # Include the inquiry and its associated replies even if outside the
            # initial time window; do not merge a different cargo conversation.
            related = [dict(row) for row in conn.execute(
                "SELECT DISTINCT m.* FROM messages m WHERE m.account_id=? AND m.source_database=? AND m.conversation_id=? "
                "AND (m.id IN (SELECT i.message_id FROM issues i JOIN responses r ON r.issue_id=i.id WHERE r.response_message_id=?) "
                "OR m.id IN (SELECT r.response_message_id FROM responses r JOIN issues i ON i.id=r.issue_id WHERE i.message_id=?))",
                (*exact, target['id'], target['id']))]
            context = {row['id']: row for row in [*before[:context_limit], target, *after[:context_limit], *related,
                       *[row for row in ancestors if (row['account_id'], row['source_database'], row['conversation_id']) == exact]]}
        views = self._chat_message_views([*context.values(), *ancestors])
        by_id = {row['id']: row for row in views}
        target_view = by_id[target['id']]
        ancestor_views = [by_id[row['id']] for row in ancestors]
        context_views = sorted([by_id[key] for key in context], key=lambda row: (row.get('sent_at') or '', row['id']))
        return {"message": target_view, "ancestors": ancestor_views, "missing_ids": missing,
                "messages": context_views, "conversation_name": target.get('conversation_name'),
                "has_more_before": len(before) > context_limit, "has_more_after": len(after) > context_limit,
                "context_limit": context_limit,
                "gaps": [{"id": value, "message": "父级消息未导入、未解析或当前范围不可访问"} for value in missing],
                "path": [target_view.get("id")] + [row.get("id") for row in ancestor_views],
                "ancestors_count": len(ancestor_views)}

    def confirm_business_role(self, *, account_id: str, source_database: str, conversation_id: str,
                              company_id: str | None, company_name: str, business_role: str, basis: str,
                              actor_id: str | None, actor_name: str | None = None) -> dict[str, Any]:
        from .wecom_pricing import PriceOperationError
        if business_role not in {'supplier', 'unknown'} or not all((account_id, source_database, conversation_id, company_name, basis.strip())):
            raise PriceOperationError('invalid_business_role', '身份确认需要完整范围、公司、角色和核验依据')
        _, reviewer_id, reviewer_name = self.price_maintenance()._require_human(actor_id, actor_name)
        with self.connect() as conn:
            conn.execute("INSERT INTO business_role_confirmations "
                         "(account_id,source_database,conversation_id,company_id,company_name,business_role,basis,confirmed_by,confirmed_name,confirmed_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?)", (account_id, source_database, conversation_id, company_id, company_name,
                         business_role, basis, reviewer_id, reviewer_name, datetime.now(timezone.utc).isoformat()))
        return {'business_role': business_role, 'confirmed': True}

    def _chat_message_views(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from .wecom_report import display_safe_value
        from .wecom_subject import WecomSubjectClassifier
        from .wecom_analysis import split_quoted_reply
        rows = self._apply_company_overrides(rows)
        result = []
        with self.connect() as conn:
            for row in rows:
                view = self._message_view(row, include_text=True)
                role = 'unknown'
                company_known = row.get('company_status') == 'known'
                # Legacy rows kept their author company and reporting identity
                # when company_status was added with an 'unknown' default.
                # Recover only corroborated Zhongji identity in this read view;
                # never rewrite rows, override conflicts or infer suppliers.
                if (row.get('company_status') == 'unknown'
                        and row.get('subject_bucket') == 'zhongji'
                        and row.get('sender_corp_name')):
                    legacy_identity = WecomSubjectClassifier().classify(
                        sender_display=row.get('sender_name'),
                        sender_corp_id=row.get('sender_corp_id'),
                        sender_corp_name=row.get('sender_corp_name'))
                    company_known = legacy_identity.subject_bucket == 'zhongji' and legacy_identity.company_status == 'known'
                if company_known:
                    identity = WecomSubjectClassifier().classify(sender_corp_id=row.get('sender_corp_id'), sender_corp_name=row.get('sender_corp_name'))
                    if identity.subject_bucket == 'zhongji':
                        role = 'zhongji'
                    else:
                        confirmations = conn.execute("SELECT * FROM business_role_confirmations WHERE account_id=? AND company_name=? ORDER BY id DESC",
                                                     (row['account_id'], row.get('sender_corp_name'))).fetchall()
                        for confirmation in confirmations:
                            if (row['source_database'] == confirmation['source_database'] or row['source_database'].startswith(confirmation['source_database'] + ':forwarded:')) and \
                               (row['conversation_id'] == confirmation['conversation_id'] or row['conversation_id'].startswith(confirmation['conversation_id'] + ':forwarded:')) and \
                               (not confirmation['company_id'] or row.get('sender_corp_id') == confirmation['company_id']):
                                role = confirmation['business_role']
                                break
                view['business_role'] = role
                view['role_label'] = {'zhongji': '中技方', 'supplier': '供应商', 'unknown': '身份待确认'}[role]
                view['forwarded_count'] = conn.execute("SELECT COUNT(*) FROM messages WHERE account_id=? AND parent_id=?", (row['account_id'], row['id'])).fetchone()[0]
                quoted, body = split_quoted_reply(view.get('text') or '')
                if quoted:
                    view['provenance'].setdefault('quoted_text', quoted)
                    view['text'] = body
                # Resolve a display quote only with a unique exact original in
                # this conversation, never from an @ mention or guessed author.
                quote = view['provenance'].get('quoted_text')
                reply_ref = view.get('reply_to_message_id')
                if reply_ref:
                    originals = conn.execute("SELECT id FROM messages WHERE account_id=? AND source_database=? AND conversation_id=? "
                        "AND (id=? OR message_id=? OR server_id=?)",
                        (row['account_id'], row['source_database'], row['conversation_id'], reply_ref, reply_ref, reply_ref)).fetchall()
                    if len(originals) == 1:
                        view['reply_to_message_id'] = originals[0]['id']
                if quote and not view.get('reply_to_message_id'):
                    originals = conn.execute("SELECT id FROM messages WHERE account_id=? AND source_database=? AND conversation_id=? AND text=? AND id<>?",
                        (row['account_id'], row['source_database'], row['conversation_id'], quote, row['id'])).fetchall()
                    if len(originals) == 1:
                        view['reply_to_message_id'] = originals[0]['id']
                result.append(display_safe_value(view))
        return result

    def get_forwarded_messages(self, message_id: str, **scope: Any) -> dict[str, Any] | None:
        evidence = self.get_message_evidence(message_id, context_limit=1, **scope)
        if evidence is None:
            return None
        target = evidence['message']
        with self.connect() as conn:
            children = [dict(row) for row in conn.execute("SELECT * FROM messages WHERE account_id=? AND parent_id=? ORDER BY sent_at,id",
                                                         (target['account_id'], target['id']))]
        children = [row for row in children if self._ancestor_in_context(target, row)]
        return {'messages': self._chat_message_views(children), 'message': target,
                'conversation_name': target['conversation_name'], 'forwarded': True,
                'gaps': [] if children else [{'message': '聊天记录未解析或尚未导入'}]}

    def list_companies(self, *, account_id: str | None = None, source_database: str | None = None,
                       conversation_id: str | None = None) -> dict[str, Any]:
        clauses, params = [], []
        if account_id:
            clauses.append("account_id=?")
            params.append(account_id)
        if source_database:
            clauses.append("(source_database=? OR instr(source_database, ? || ':forwarded:')=1)")
            params.extend([source_database, source_database])
        if conversation_id:
            clauses.append("(conversation_id=? OR instr(conversation_id, ? || ':forwarded:')=1)")
            params.extend([conversation_id, conversation_id])
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            rows = [dict(row) for row in conn.execute(f"SELECT * FROM messages {where}", params)]
        rows = self._apply_company_overrides(rows)
        groups: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            company_status = row.get("company_status") or ("known" if row.get("sender_corp_name") else "unknown")
            key = (row.get("sender_corp_id"), row.get("sender_corp_name"), company_status)
            item = groups.setdefault(key, {"company_id": key[0], "company_name": key[1], "company_status": key[2],
                                           "subject_buckets": set(), "message_count": 0, "author_ids": set(), "author_names": set()})
            item["message_count"] += 1
            if row.get("subject_bucket"):
                item["subject_buckets"].add(row["subject_bucket"])
            item["author_ids"].add(row.get("sender_id")); item["author_names"].add(row.get("sender_name"))
        items = []
        for item in groups.values():
            item["author_ids"] = sorted(value for value in item["author_ids"] if value)
            item["author_names"] = sorted(value for value in item["author_names"] if value)
            item["subject_buckets"] = sorted(value for value in item["subject_buckets"] if value)
            item["subject_bucket"] = item["subject_buckets"][0] if len(item["subject_buckets"]) == 1 else "mixed"
            item["display_name"] = item["company_name"] or ("公司待确认" if item["company_status"] == "conflict" else "公司未知")
            items.append(item)
        items.sort(key=lambda item: (-item["message_count"], str(item["display_name"])))
        return {"items": items, "total": len(items), "range": {"account_id": account_id, "source_database": source_database, "conversation_id": conversation_id}}

    def list_issues(
        self,
        account_id: str | None = None,
        source_database: str | None = None,
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
        if source_database:
            clauses.append("(m.source_database=? OR instr(m.source_database, ? || ':forwarded:')=1)")
            params.extend([source_database, source_database])
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
                    m.company_status AS question_company_status,
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
                    rm.company_status AS responder_company_status,
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
        rows = [dict(row) for row in rows]
        # Issue rows use question_/responder_ aliases rather than the message
        # table's sender_ names.  Apply the same confirmed display overlay to
        # both participants without touching the stored message columns.
        message_keys = {item.get("message_pk") for item in rows} | {item.get("response_message_pk") for item in rows}
        message_keys.discard(None)
        overlay_by_id = {}
        if message_keys:
            with self.connect() as conn:
                placeholders = self._in_clause(message_keys)
                source_rows = [dict(row) for row in conn.execute(
                    f"SELECT * FROM messages WHERE id IN ({placeholders})", list(message_keys)
                )]
            overlay_by_id = {row["id"]: row for row in self._apply_company_overrides(source_rows)}
        from .wecom_subject import sender_label
        for r in rows:
            item = dict(r)
            question = overlay_by_id.get(item.get("message_pk"))
            responder = overlay_by_id.get(item.get("response_message_pk"))
            if question:
                item["question_corp_id"] = question.get("sender_corp_id")
                item["question_corp_name"] = question.get("sender_corp_name")
                item["question_company_status"] = question.get("company_status")
            if responder:
                item["responder_corp_id"] = responder.get("sender_corp_id")
                item["responder_corp_name"] = responder.get("sender_corp_name")
                item["responder_company_status"] = responder.get("company_status")
            item["evidence"] = json.loads(item.pop("evidence_json", "[]"))
            item["clues"] = json.loads(item.pop("clues_json", "{}"))
            item["provenance"] = json.loads(item.pop("provenance_json", "{}"))
            item["question_company_status"] = item.get("question_company_status") or ("known" if item.get("question_corp_name") else "unknown")
            item["question_sender_label"] = sender_label(item.get("question_sender_name"), item.get("question_corp_name"), company_status=item["question_company_status"])
            item["responder_company_status"] = item.get("responder_company_status") or ("known" if item.get("responder_corp_name") else "unknown")
            item["responder_sender_label"] = sender_label(item.get("responder_name"), item.get("responder_corp_name"), company_status=item["responder_company_status"])
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

    @staticmethod
    def _report_scope_sql(*, account_id=None, source_database=None, conversation_id=None, conversation_name=None,
                          alias="m") -> tuple[str, list[Any]]:
        prefix = f"{alias}." if alias else ""
        clauses, params = [], []
        if account_id:
            clauses.append(f"{prefix}account_id=?")
            params.append(account_id)
        if source_database:
            clauses.append(f"({prefix}source_database=? OR instr({prefix}source_database, ? || ':forwarded:')=1)")
            params.extend([source_database, source_database])
        if conversation_id:
            clauses.append(f"({prefix}conversation_id=? OR instr({prefix}conversation_id, ? || ':forwarded:')=1)")
            params.extend([conversation_id, conversation_id])
        if conversation_name:
            clauses.append(f"instr(lower({prefix}conversation_name), lower(?))>0")
            params.append(conversation_name)
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", params

    @staticmethod
    def _ancestor_in_context(row: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
        """Keep parent evidence inside the target account/source/conversation tree."""
        if row.get("account_id") != context.get("account_id"):
            return False
        context_source = str(context.get("source_database") or "")
        row_source = str(row.get("source_database") or "")
        if row_source != context_source and not context_source.startswith(row_source + ":forwarded:"):
            return False
        context_conversation = str(context.get("conversation_id") or "")
        row_conversation = str(row.get("conversation_id") or "")
        if row_conversation != context_conversation and not context_conversation.startswith(row_conversation + ":forwarded:"):
            return False
        context_name = str(context.get("conversation_name") or "")
        return not context_name or row.get("conversation_name") == context_name

    @staticmethod
    def _in_clause(values) -> str:
        return ",".join("?" for _ in values)

    @staticmethod
    def _index_event(event: dict[str, Any]) -> dict[str, Any]:
        """Keep the report index useful without sending message/reply bodies."""
        return {
            "round": event["round"],
            "message_id": event["message_id"],
            "source_message_id": event["source_message_id"],
            "sent_at": event["sent_at"],
            "sender_name": event["sender_name"],
            "sender_label": event.get("sender_label"),
            "sender_corp_id": event.get("sender_corp_id"),
            "sender_corp_name": event.get("sender_corp_name"),
            "company_status": event.get("company_status", "unknown"),
            "subject_bucket": event["subject_bucket"],
            "subject_basis": event["subject_basis"],
            "text_preview": event["text"][:120],
            "parent_id": event["parent_id"],
            "nesting_depth": event["nesting_depth"],
            "clues": {
                key: event["clues"].get(key)
                for key in ("origin", "destination", "route_items", "destination_items")
                if event["clues"].get(key) is not None
            },
            "route_coverage": event["route_coverage"],
            "status": event["status"],
            "needs_review": event["needs_review"],
            "category_statuses": event["category_statuses"],
            "first_response_seconds": event["first_response_seconds"],
            "ack_seconds": event["ack_seconds"],
            "solution_seconds": event["solution_seconds"],
            "final_solution_seconds": event["final_solution_seconds"],
            "first_ack_at": event["first_ack_at"],
            "response_count": len(event["responses"]),
            "has_parent_evidence": bool(event["parent_id"]),
        }

    def _get_report_index(self, *, account_id=None, source_database=None, conversation_id=None, conversation_name=None,
                          subject=None, category=None, status=None) -> dict[str, Any]:
        """Build the dashboard index from scoped rows and only related evidence rows."""
        from .wecom_report import build_report

        scope_where, scope_params = self._report_scope_sql(
            account_id=account_id, source_database=source_database, conversation_id=conversation_id,
            conversation_name=conversation_name,
        )
        with self.connect() as conn:
            # Metadata is enough for coverage and provenance filtering. The
            # full message body is fetched only for candidate rounds/replies.
            scope_rows = [dict(row) for row in conn.execute(
                f"""
                SELECT m.id, m.conversation_id, m.conversation_name, m.subject_bucket, m.message_type,
                       m.sent_at, m.parse_status, m.nesting_depth, m.provenance_json
                FROM messages m {scope_where}
                """, scope_params,
            )]
            verified_names = {
                row["conversation_name"].casefold()
                for row in scope_rows
                if json.loads(row.get("provenance_json") or "{}").get("origin") == "database"
            }
            valid_rows = [
                row for row in scope_rows
                if not (
                    json.loads(row.get("provenance_json") or "{}").get("origin") != "database"
                    and row["conversation_name"].casefold() in verified_names
                )
            ]
            valid_ids = {row["id"] for row in valid_rows}
            metadata_by_id = {row["id"]: row for row in valid_rows}

            issue_pairs = [dict(row) for row in conn.execute(
                f"""
                SELECT i.message_id, i.category
                FROM issues i JOIN messages m ON m.id=i.message_id
                {scope_where}
                """, scope_params,
            )]
            available_categories = sorted({
                row["category"] for row in issue_pairs
                if row["message_id"] in valid_ids
                and (not subject or metadata_by_id[row["message_id"]]["subject_bucket"] == subject)
            })
            candidate_ids = {
                row["message_id"] for row in issue_pairs
                if row["message_id"] in valid_ids
                and (not subject or metadata_by_id[row["message_id"]]["subject_bucket"] == subject)
                and (not category or row["category"] == category)
            }

            issue_rows: list[dict[str, Any]] = []
            if candidate_ids:
                placeholders = self._in_clause(candidate_ids)
                issue_rows = [dict(row) for row in conn.execute(
                    f"SELECT * FROM issues WHERE message_id IN ({placeholders})", list(candidate_ids)
                )]

            # build_report deliberately chooses the same primary issue rows as
            # the complete report. Fetch replies only for those rows.
            by_question = defaultdict(list)
            for issue in issue_rows:
                by_question[issue["message_id"]].append(issue)
            primary_issue_ids = set()
            for issues_for_question in by_question.values():
                primary = ([issue for issue in issues_for_question if issue["category"] == category]
                           if category else
                           [issue for issue in issues_for_question if issue["category"] == "询价报价"]
                           or issues_for_question)
                primary_issue_ids.update(issue["id"] for issue in primary)

            response_rows: list[dict[str, Any]] = []
            if primary_issue_ids:
                placeholders = self._in_clause(primary_issue_ids)
                response_rows = [dict(row) for row in conn.execute(
                    f"SELECT * FROM responses WHERE issue_id IN ({placeholders})", list(primary_issue_ids)
                )]
            response_message_ids = {row["response_message_id"] for row in response_rows} & valid_ids
            needed_ids = candidate_ids | response_message_ids
            message_rows: list[dict[str, Any]] = []
            if needed_ids:
                placeholders = self._in_clause(needed_ids)
                message_rows = [dict(row) for row in conn.execute(
                    f"SELECT * FROM messages WHERE id IN ({placeholders})", list(needed_ids)
                )]

        message_rows = self._apply_company_overrides(message_rows)
        report = build_report(message_rows, issue_rows, response_rows,
                              subject=subject, category=category, status=status)
        valid_timestamps = [row["sent_at"] or None for row in valid_rows]
        valid_timestamps = [value for value in valid_timestamps if value]
        valid_origins = [json.loads(row.get("provenance_json") or "{}") for row in valid_rows]
        scope_groups = defaultdict(int)
        for row in valid_rows:
            if row["nesting_depth"] > 0:
                scope_groups[row["conversation_id"]] += 1
        scope_options = [{"conversation_id": conversation_id, "message_count": count}
                         for conversation_id, count in sorted(scope_groups.items(),
                                                              key=lambda item: (-item[1], item[0]))]
        coverage = {
            "message_count": len(valid_rows),
            "conversation_message_count": len(valid_rows),
            "filtered_message_count": report["coverage"]["filtered_message_count"],
            "first_message_at": min(valid_timestamps) if valid_timestamps else None,
            "last_message_at": max(valid_timestamps) if valid_timestamps else None,
            "missing_timestamp_count": sum(not row["sent_at"] for row in valid_rows),
            "forward_count": sum(row["message_type"] == "合并转发记录" for row in valid_rows),
            "unexpanded_forward_count": sum(row["parse_status"] in ("unexpanded_forward", "partial_forward")
                                             for row in valid_rows),
            "unparsed_media_count": sum(row["parse_status"] == "unparsed_media" for row in valid_rows),
            "nested_message_count": sum(row["nesting_depth"] > 0 for row in valid_rows),
            "max_nesting_depth": max((row["nesting_depth"] for row in valid_rows), default=0),
            "database_backed_count": sum(item.get("origin") == "database" for item in valid_origins),
            "unverified_source_count": sum(item.get("origin") != "database" for item in valid_origins),
            "excluded_unverified_source_count": len(scope_rows) - len(valid_rows),
            "display_redacted_count": None,
            "available_forwards_expanded": all(
                row["parse_status"] == "expanded_forward"
                for row in valid_rows if row["message_type"] == "合并转发记录"
            ),
            "full_history_verified": False,
            "note": (
                "真实数据库与转发载荷已按本地快照核验；云端完整历史、未解析媒体内容仍未证明。已解决表示有报价或处置结论。"
                if verified_names else
                "本地可用材料范围；原库与全部转发层完整性尚未证明。手工或规范化导入不能证明真实来源。"
            ),
        }
        index_events = [self._index_event(event) for event in report["events"]]
        report.update({
            "view": "index",
            "coverage": coverage,
            "available_categories": available_categories,
            "scope_options": scope_options,
            "events": index_events,
            "messages": [],
            "ancestors": [],
            "detail_endpoint": "/api/wecom/report-detail",
        })
        return report

    def get_report_detail(self, *, message_id, account_id=None, source_database=None, conversation_id=None,
                          conversation_name=None, subject=None, category=None, status=None) -> dict[str, Any] | None:
        """Return one complete business round and its parent evidence on demand."""
        from .wecom_report import build_report

        scope_where, scope_params = self._report_scope_sql(
            account_id=account_id, source_database=source_database, conversation_id=conversation_id,
            conversation_name=conversation_name,
        )
        with self.connect() as conn:
            target_rows = [dict(row) for row in conn.execute(
                f"""
                SELECT m.* FROM messages m {scope_where}
                AND (m.id=? OR m.message_id=?)
                """ if scope_where else
                """
                SELECT m.* FROM messages m
                WHERE (m.id=? OR m.message_id=?)
                """,
                [*scope_params, message_id, message_id],
            )]
            if not target_rows:
                return None
            scope_meta = [dict(row) for row in conn.execute(
                f"SELECT m.id, m.conversation_name, m.provenance_json FROM messages m {scope_where}",
                scope_params,
            )]
            verified_names = {
                row["conversation_name"].casefold()
                for row in scope_meta
                if json.loads(row.get("provenance_json") or "{}").get("origin") == "database"
            }
            valid_scope_ids = {
                row["id"] for row in scope_meta
                if not (
                    json.loads(row.get("provenance_json") or "{}").get("origin") != "database"
                    and row["conversation_name"].casefold() in verified_names
                )
            }
            target = next((row for row in target_rows if row["id"] == message_id), target_rows[0])
            if target["id"] not in valid_scope_ids:
                return None
            issue_rows = [dict(row) for row in conn.execute(
                "SELECT * FROM issues WHERE message_id=?", (target["id"],)
            )]
            if not issue_rows:
                return None
            issue_ids = [row["id"] for row in issue_rows]
            placeholders = self._in_clause(set(issue_ids))
            response_rows = [dict(row) for row in conn.execute(
                f"SELECT * FROM responses WHERE issue_id IN ({placeholders})", issue_ids
            )]
            related_ids = ({target["id"]} | {row["response_message_id"] for row in response_rows}) & valid_scope_ids
            placeholders = self._in_clause(related_ids)
            message_rows = [dict(row) for row in conn.execute(
                f"SELECT * FROM messages WHERE id IN ({placeholders})", list(related_ids)
            )]

            known = {row["id"] for row in message_rows}
            pending = {row["parent_id"] for row in message_rows if row["parent_id"]} - known
            ancestors = []
            missing_ancestor_ids = []
            while pending:
                placeholders = self._in_clause(pending)
                parents = [dict(row) for row in conn.execute(
                    f"SELECT * FROM messages WHERE id IN ({placeholders})", list(pending)
                )]
                found = {row["id"] for row in parents}
                for ref in pending - found:
                    if ref not in missing_ancestor_ids:
                        missing_ancestor_ids.append(ref)
                next_pending = set()
                for parent in parents:
                    if parent["id"] not in valid_scope_ids or not self._ancestor_in_context(parent, target):
                        if parent["id"] not in missing_ancestor_ids:
                            missing_ancestor_ids.append(parent["id"])
                        continue
                    if parent["id"] in known:
                        continue
                    known.add(parent["id"])
                    ancestors.append(parent)
                    if parent.get("parent_id") and parent["parent_id"] not in known:
                        next_pending.add(parent["parent_id"])
                pending = next_pending

        message_rows = self._apply_company_overrides(message_rows)
        ancestors = self._apply_company_overrides(ancestors)
        report = build_report(message_rows, issue_rows, response_rows,
                              subject=subject, category=category, status=status)
        events = [event for event in report["events"] if event["message_id"] == target["id"]]
        if not events:
            return None
        ancestor_report = build_report(ancestors, [], []) if ancestors else {"messages": []}
        return {
            "event": events[0],
            "messages": report["messages"],
            "ancestors": ancestor_report["messages"],
            "missing_ids": missing_ancestor_ids,
            "gaps": [{"id": value, "message": "父级消息未导入、未解析或当前范围不可访问"}
                     for value in missing_ancestor_ids],
        }

    def get_report(self, *, account_id=None, source_database=None, conversation_id=None, conversation_name=None,
                   subject=None, category=None, status=None, view=None) -> dict[str, Any]:
        if view == "index":
            return self._get_report_index(
                account_id=account_id, source_database=source_database, conversation_id=conversation_id,
                conversation_name=conversation_name, subject=subject,
                category=category, status=status,
            )
        from .wecom_report import build_report
        clauses, params = [], []
        if account_id:
            clauses.append("m.account_id=?")
            params.append(account_id)
        if source_database:
            clauses.append("(m.source_database=? OR instr(m.source_database, ? || ':forwarded:')=1)")
            params.extend([source_database, source_database])
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
            missing_ancestor_ids = []
            while pending:
                parents = [dict(r) for r in conn.execute(
                    'SELECT * FROM messages WHERE id IN ('+','.join('?' for _ in pending)+')',list(pending))]
                found = {row["id"] for row in parents}
                for ref in pending - found:
                    if ref not in missing_ancestor_ids:
                        missing_ancestor_ids.append(ref)
                next_pending = set()
                contexts = messages
                for parent in parents:
                    if not any(self._ancestor_in_context(parent, context) for context in contexts):
                        if parent["id"] not in missing_ancestor_ids:
                            missing_ancestor_ids.append(parent["id"])
                        continue
                    if parent["id"] in known:
                        continue
                    known.add(parent["id"])
                    ancestors.append(parent)
                    if parent.get("parent_id") and parent["parent_id"] not in known:
                        next_pending.add(parent["parent_id"])
                pending = next_pending
        messages = self._apply_company_overrides(messages)
        ancestors = self._apply_company_overrides(ancestors)
        report = build_report(messages, issues, responses, subject=subject, category=category, status=status)
        report['ancestors'] = build_report(ancestors, [], [])['messages'] if ancestors else []
        report['ancestor_gaps'] = [{"id": value, "message": "父级消息未导入、未解析或当前范围不可访问"}
                                   for value in missing_ancestor_ids]
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

    def price_maintenance(self, **kwargs):
        from .wecom_pricing import PriceMaintenance
        self.initialize()
        return PriceMaintenance(self, **kwargs)

    def list_prices(self, **kwargs):
        return self.price_maintenance().list_prices(**kwargs)

    def submit_price_candidate(self, payload, **kwargs):
        return self.price_maintenance().submit_candidate(payload, **kwargs)

    def review_price_candidate(self, candidate_id, **kwargs):
        return self.price_maintenance().review_candidate(candidate_id, **kwargs)

    def deactivate_price(self, record_id, **kwargs):
        return self.price_maintenance().deactivate(record_id, **kwargs)
