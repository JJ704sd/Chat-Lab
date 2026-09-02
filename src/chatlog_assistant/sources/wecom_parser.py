from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone, timedelta
import hashlib
import json
import re
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .wecom_protobuf import parse_wecom_content, decode_forwarded_content, parse_protobuf_fields, pb_get
from .wecom_subject import WecomSubjectClassifier, SubjectClassification


CN_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True, slots=True)
class WecomUnifiedRecord:
    source: str
    account_id: str
    source_database: str
    message_id: str
    server_id: str
    conversation_id: str
    conversation_name: str
    sender_id: str
    sender_name: str
    sender_corp_id: str | None
    sender_corp_name: str | None
    subject_bucket: str
    subject_basis: str
    message_type: str
    sent_at: datetime | None
    text: str
    reply_to_message_id: str | None
    parse_status: str
    source_reference: str
    parent_id: str | None = None
    root_message_id: str | None = None
    nesting_depth: int = 0
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        val = f"{self.source}\0{self.account_id}\0{self.source_database}\0{self.message_id}".encode("utf-8")
        return hashlib.sha256(val).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "account_id": self.account_id,
            "source_database": self.source_database,
            "message_id": self.message_id,
            "server_id": self.server_id,
            "conversation_id": self.conversation_id,
            "conversation_name": self.conversation_name,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "sender_corp_id": self.sender_corp_id,
            "sender_corp_name": self.sender_corp_name,
            "subject_bucket": self.subject_bucket,
            "subject_basis": self.subject_basis,
            "message_type": self.message_type,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "text": self.text,
            "reply_to_message_id": self.reply_to_message_id,
            "parse_status": self.parse_status,
            "source_reference": self.source_reference,
            "parent_id": self.parent_id,
            "root_message_id": self.root_message_id,
            "nesting_depth": self.nesting_depth,
            "provenance": self.provenance,
        }


def _safe_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="ignore").strip()
    return str(val).strip()


def records_from_message_tree(
    items: list[dict[str, Any]], *, account_id: str, source_database: str,
    conversation_id: str, conversation_name: str, source_reference: str,
    parent: WecomUnifiedRecord | None = None, provenance: dict[str, Any] | None = None,
    strict: bool = True, identity_lookup: dict[str, dict[str, Any]] | None = None,
    classifier: WecomSubjectClassifier | None = None,
) -> tuple[list[WecomUnifiedRecord], list[str]]:
    """Flatten explicit message trees without inheriting authors, time or body.

    Conversation identity belongs to each containing card; original group labels
    remain in provenance. Structural-only legacy JSONL containers have no fake
    timestamp or database row, and are reported as gaps.
    """
    classifier = classifier or WecomSubjectClassifier()
    identity_lookup = identity_lookup or {}
    records, gaps = [], []
    def invalid(message):
        if strict:
            raise ValueError(message)
        gaps.append(message)
    stack = [(item, parent, conversation_id, source_database, source_reference + f"/{index}",
              parent.nesting_depth + 1 if parent else 0,
              (parent.root_message_id or parent.id) if parent else None)
             for index, item in reversed(list(enumerate(items)))]
    while stack:
        item, ancestor, scope, database, reference, depth, root_id = stack.pop()
        if not isinstance(item, dict):
            invalid(f"{reference}: 消息必须是对象")
            continue
        key = item.get("source_message_id") or item.get("message_id") or item.get("msg_id")
        if key is None:
            invalid(f"{reference}: 缺少原消息 ID，未将该节点解释为消息")
            continue
        key = str(key)
        children = item.get("forwarded_messages")
        if children is not None and not isinstance(children, list):
            invalid(f"{reference}: forwarded_messages 必须是消息列表")
            continue
        known_identity = identity_lookup.get(str(item.get("sender_id") or ""), {})
        name = item.get("sender_display") or item.get("sender_name") or known_identity.get("name")
        timestamp = item.get("sent_at")
        record = None
        error = None
        sent_at = None
        body = item.get("content", item.get("text"))
        if body is None and children is not None:
            body = "[群聊的聊天记录]"
        if children is not None and (not name or not timestamp):
            gaps.append(f"{reference}: 容器 {key} 缺少发送者或时间，仅保留结构路径")
        else:
            if not name or not timestamp:
                error = "缺少原发送者或原发送时间"
            else:
                try:
                    sent_at = datetime.fromisoformat(str(timestamp))
                    if sent_at.tzinfo is None:
                        error = "sent_at 必须带时区"
                except (ValueError, TypeError):
                    error = "原发送时间格式无效"
            if not isinstance(body, str):
                error = "正文必须是字符串"
            if error:
                invalid(f"{reference}: {error}")
        if sent_at is not None and not error and name:
            sender_id = str(item.get("sender_id") or name)
            identity = classifier.classify(sender_id=sender_id, sender_display=str(name),
                sender_corp_id=item.get("sender_corp_id") or known_identity.get("corp_id"),
                sender_corp_name=item.get("sender_corp_name") or known_identity.get("corp_name"))
            kind = item.get("content_type", "text")
            is_card = children is not None or kind in (4, 40, 49, "4", "40", "49", "合并转发记录") or body.startswith("[群聊的聊天记录]")
            text_kind = kind in (0, 2, "text", "quote", "文本", "引用回复")
            status = "expanded_forward" if children else "unexpanded_forward" if is_card else "parsed" if text_kind else "unparsed_media"
            info = dict(provenance or {"origin": "normalized_unverified"})
            if info.get("origin") == "database" and item.get("_native_provenance"):
                info.update(item["_native_provenance"])
                status = info.get("body_parse_status", status)
            info.update({"original_conversation_name": item.get("conversation_name"),
                         "original_conversation_id": item.get("conversation_id"),
                         "original_sender_id_present": bool(item.get("sender_id")),
                         "identity_lookup_used": bool(known_identity)})
            if item.get("quoted_text"):
                info["quoted_text"] = str(item["quoted_text"])
            record = WecomUnifiedRecord(
                source="wecom-local", account_id=account_id, source_database=database,
                message_id=key, server_id=str(item.get("server_id") or ""),
                conversation_id=scope, conversation_name=conversation_name,
                sender_id=sender_id, sender_name=str(name), sender_corp_id=identity.corp_id,
                sender_corp_name=identity.corp_name, subject_bucket=identity.subject_bucket,
                subject_basis=identity.basis, message_type="合并转发记录" if is_card else "文本" if text_kind else str(kind),
                sent_at=sent_at.astimezone(CN_TZ), text=body,
                reply_to_message_id=str(item["reply_to_message_id"]) if item.get("reply_to_message_id") else None,
                parse_status=status, source_reference=reference,
                parent_id=ancestor.id if ancestor else None, root_message_id=root_id,
                nesting_depth=depth, provenance=info,
            )
            records.append(record)
        if children is not None:
            child_scope = f"{scope}:forwarded:{key}"
            child_database = source_database + ":forwarded:" + hashlib.sha256(child_scope.encode()).hexdigest()
            for index, child in reversed(list(enumerate(children))):
                stack.append((child, record, child_scope, child_database,
                              f"{reference}/forwarded:{key}/{index}", depth + 1,
                              root_id or (record.id if record else None)))
    for index, record in enumerate(records):
        affected = [gap for gap in gaps if gap.startswith(record.source_reference + "/") or gap.startswith(record.source_reference + ":")]
        if affected and record.message_type == "合并转发记录":
            records[index] = replace(record, parse_status="partial_forward", provenance={**record.provenance, "gaps": affected})
    records.sort(key=lambda r: (r.sent_at, r.source_reference))
    return records, gaps


class WecomLocalParser:
    """Parses decrypted message.db, user.db, and session.db into normalized WecomUnifiedRecord objects."""

    def __init__(
        self,
        subject_classifier: WecomSubjectClassifier | None = None,
    ) -> None:
        self.classifier = subject_classifier or WecomSubjectClassifier()

    def parse_databases(
        self,
        account_id: str,
        message_conn: sqlite3.Connection,
        user_conn: sqlite3.Connection | None = None,
        session_conn: sqlite3.Connection | None = None,
        company_conn: sqlite3.Connection | None = None,
        *,
        since_sequence: int = 0,
        since_message_id: str = "",
    ) -> tuple[list[WecomUnifiedRecord], int, str]:
        user_map: dict[str, dict[str, Any]] = {}
        if user_conn:
            user_map = self._load_users(user_conn, company_conn)

        conv_map: dict[str, str] = {}
        if session_conn:
            conv_map = self._load_conversations(session_conn)

        records: list[WecomUnifiedRecord] = []
        seen_ids: set[str] = set()
        max_seq = since_sequence
        max_id = since_message_id

        # Discover message tables
        message_tables = self._find_message_tables(message_conn)
        for tbl in message_tables:
            tbl_records, new_max_seq, new_max_id = self._read_table_messages(
                account_id=account_id,
                message_conn=message_conn,
                table_name=tbl,
                user_map=user_map,
                conv_map=conv_map,
                since_sequence=since_sequence,
                since_message_id=since_message_id,
                seen_ids=seen_ids,
            )
            records.extend(tbl_records)
            if new_max_seq > max_seq or (new_max_seq == max_seq and new_max_id > max_id):
                max_seq = new_max_seq
                max_id = new_max_id

        records.sort(key=lambda r: (r.sent_at or datetime.min.replace(tzinfo=CN_TZ), r.message_id))
        return records, max_seq, max_id

    def _find_message_tables(self, conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = [r[0] for r in rows]
        # Include both message_table and message_small_table
        found = []
        for wanted in ("message_table", "message_small_table", "message"):
            if wanted in names:
                found.append(wanted)
        for n in names:
            if n.startswith("message_") and n not in found and not n.endswith("_index"):
                found.append(n)
        return found

    def _load_users(self, conn: sqlite3.Connection, company_conn: sqlite3.Connection | None = None) -> dict[str, dict[str, Any]]:
        # Load corp_id -> corp_name mapping if company_conn available
        corp_id_names: dict[str, str] = {}
        if company_conn:
            try:
                for tbl in ("company_table", "company", "corp", "external_company_table_v2"):
                    if company_conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone():
                        cols = {r[1] for r in company_conn.execute(f"PRAGMA table_info({tbl})")}
                        id_col = next((c for c in ("id", "corp_id", "corpid", "corpany_id") if c in cols), None)
                        name_col = next((c for c in ("name", "corp_name", "company_name") if c in cols), None)
                        if id_col and name_col:
                            for r in company_conn.execute(f"SELECT {id_col}, {name_col} FROM {tbl}").fetchall():
                                cid = _safe_str(r[0])
                                cname = _safe_str(r[1])
                                if cid and cname:
                                    corp_id_names[cid] = cname
                if company_conn.execute("SELECT 1 FROM sqlite_master WHERE name='self_corp_list_table'").fetchone():
                    for cid, blob in company_conn.execute("SELECT corpany_id, self_corp_info FROM self_corp_list_table"):
                        fields = parse_protobuf_fields(blob)
                        if pb_get(fields, 1, 0) == cid:
                            name = _safe_str(pb_get(fields, 3, 2))
                            if name:
                                corp_id_names[str(cid)] = name
            except Exception:
                pass

        users: dict[str, dict[str, Any]] = {}
        # 1. user_table
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(user_table)")}
            id_col = next((c for c in ("id", "user_id", "uin") if c in cols), None)
            if id_col:
                select_cols = [id_col]
                for c in ("name", "english_name", "account", "corp_name", "corp_id", "remark", "external_corp_name"):
                    if c in cols:
                        select_cols.append(c)
                sql = f"SELECT {', '.join(select_cols)} FROM user_table"
                for row in conn.execute(sql).fetchall():
                    uid = str(row[0])
                    data = dict(zip(select_cols, row))
                    name = _safe_str(data.get("name") or data.get("english_name") or data.get("account") or uid)
                    corp_id = _safe_str(data.get("corp_id")) if "corp_id" in data else None
                    corp_name = _safe_str(data.get("corp_name") or data.get("external_corp_name")) or None
                    if not corp_name and corp_id and corp_id in corp_id_names:
                        corp_name = corp_id_names[corp_id]
                    users[uid] = {
                        "name": name,
                        "corp_name": corp_name,
                        "corp_id": corp_id,
                    }
        except Exception:
            pass

        # 2. wechat_contactV1
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(wechat_contactV1)")}
            id_col = next((c for c in ("id", "wxid", "user_id") if c in cols), None)
            name_col = next((c for c in ("name", "nickname", "remark") if c in cols), None)
            if id_col and name_col:
                for row in conn.execute(f"SELECT {id_col}, {name_col} FROM wechat_contactV1").fetchall():
                    wxid = _safe_str(row[0])
                    name = _safe_str(row[1])
                    if wxid and name:
                        users[f"wx_{wxid}"] = {
                            "name": name,
                            "corp_name": None,
                            "corp_id": None,
                        }
                        users[wxid] = users[f"wx_{wxid}"]
        except Exception:
            pass
        return users

    def _load_conversations(self, conn: sqlite3.Connection) -> dict[str, str]:
        convs: dict[str, str] = {}
        try:
            rows = conn.execute("SELECT id, name FROM conversation_table").fetchall()
            for r in rows:
                cid = _safe_str(r[0])
                cname = _safe_str(r[1])
                if cid:
                    convs[cid] = cname or cid
        except Exception:
            pass
        return convs

    def _read_table_messages(
        self,
        account_id: str,
        message_conn: sqlite3.Connection,
        table_name: str,
        user_map: dict[str, dict[str, Any]],
        conv_map: dict[str, str],
        since_sequence: int,
        since_message_id: str,
        seen_ids: set[str],
    ) -> tuple[list[WecomUnifiedRecord], int, str]:
        records: list[WecomUnifiedRecord] = []
        cols = {r[1] for r in message_conn.execute(f"PRAGMA table_info({table_name})")}

        id_col = next((c for c in ("message_id", "id") if c in cols), None)
        server_col = "server_id" if "server_id" in cols else id_col
        sender_col = next((c for c in ("sender_id", "sender") if c in cols), None)
        conv_col = next((c for c in ("conversation_id", "conversation") if c in cols), None)
        type_col = next((c for c in ("content_type", "type") if c in cols), None)
        time_col = next((c for c in ("send_time", "create_time", "time") if c in cols), None)
        content_col = next((c for c in ("content", "message") if c in cols), None)
        extra_col = "extra_content" if "extra_content" in cols else None

        if not (id_col and conv_col and time_col and content_col):
            return [], since_sequence, since_message_id

        select_cols = [id_col, server_col, sender_col or "''", conv_col, type_col or "0", time_col, content_col]
        if extra_col:
            select_cols.append(extra_col)

        sql = f"SELECT {', '.join(select_cols)} FROM {table_name} ORDER BY {time_col}, {id_col}"
        max_seq = since_sequence
        max_id = since_message_id

        for row in message_conn.execute(sql):
            msg_id = str(row[0])
            if msg_id in seen_ids:
                continue

            srv_id = str(row[1]) if row[1] is not None else ""
            sender_id = str(row[2]) if row[2] is not None else ""
            conv_id = _safe_str(row[3])
            content_type = int(row[4] or 0)
            raw_time = int(row[5] or 0)
            raw_content = row[6]

            # Sequence check
            if raw_time < since_sequence or (raw_time == since_sequence and msg_id <= since_message_id):
                continue

            seen_ids.add(msg_id)
            if raw_time > max_seq or (raw_time == max_seq and msg_id > max_id):
                max_seq = raw_time
                max_id = msg_id

            # Parse content
            display_text, message_type_name, parse_status = parse_wecom_content(content_type, raw_content)

            # Resolve conversation name
            conv_name = conv_map.get(conv_id, "")
            if not conv_name:
                if conv_id.startswith("R:"):
                    conv_name = f"群聊_{conv_id[2:]}"
                elif conv_id.startswith("S:"):
                    parts = conv_id[2:].split("_")
                    names = [user_map.get(p, {}).get("name", p) for p in parts if p in user_map]
                    conv_name = "/".join(names) if names else conv_id
                else:
                    conv_name = conv_id

            # Resolve sender info & subject classification
            user_info = user_map.get(sender_id, {})
            sender_name = user_info.get("name", sender_id or "未知")
            sender_corp_name = user_info.get("corp_name")
            sender_corp_id = user_info.get("corp_id")

            subj_match = self.classifier.classify(
                sender_id=sender_id,
                sender_display=sender_name,
                sender_corp_id=sender_corp_id,
                sender_corp_name=sender_corp_name,
            )

            # Time parsing Asia/Shanghai
            if raw_time > 10**12:
                sent_at = datetime.fromtimestamp(raw_time / 1000, CN_TZ)
            elif raw_time > 0:
                sent_at = datetime.fromtimestamp(raw_time, CN_TZ)
            else:
                sent_at = None
                parse_status = "invalid_timestamp"

            record = WecomUnifiedRecord(
                    source="wecom-local",
                    account_id=account_id,
                    source_database=f"{table_name}.db",
                    message_id=msg_id,
                    server_id=srv_id,
                    conversation_id=conv_id,
                    conversation_name=conv_name,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    sender_corp_id=subj_match.corp_id,
                    sender_corp_name=subj_match.corp_name,
                    subject_bucket=subj_match.subject_bucket,
                    subject_basis=subj_match.basis,
                    message_type=message_type_name,
                    sent_at=sent_at,
                    text=display_text,
                    reply_to_message_id=None,
                    parse_status=parse_status,
                    source_reference=f"{table_name}:{msg_id}",
                    provenance={"origin": "database", "raw_send_time": raw_time, "content_sha256": hashlib.sha256(
                        raw_content if isinstance(raw_content, bytes) else str(raw_content or '').encode()).hexdigest()},
                )
            if message_type_name == "合并转发记录":
                forward = decode_forwarded_content(raw_content, row[7] if extra_col else None)
                children = []
                gaps = list(forward.gaps)
                # Preserve every valid sibling even if another node is damaged.
                for index, item in enumerate(forward.messages):
                    try:
                        decoded, missing = records_from_message_tree([item], account_id=account_id,
                            source_database=f"{table_name}.db:forwarded:{record.id}",
                            conversation_id=f"{conv_id}:forwarded:{msg_id}", conversation_name=conv_name,
                            source_reference=f"{record.source_reference}/forwarded:{msg_id}/{index}",
                            parent=record, provenance=record.provenance, strict=False,
                            identity_lookup=user_map, classifier=self.classifier)
                        children.extend(decoded)
                        gaps.extend(missing)
                    except (ValueError, TypeError) as exc:
                        gaps.append(str(exc))
                info = {**record.provenance, "payload_paths": forward.payload_paths, "gaps": gaps}
                # Native quotes carry author/time/conversation independently of
                # the visible @mention. Resolve only an unambiguous exact body.
                keyed = {}
                def quote_body(text):
                    # Native quoted metadata omits leading mention segments.
                    # Author/time/conversation still have to match exactly.
                    return ''.join(re.sub(r'^(?:@[^\s]+\s+)+', '', text).split())
                for child in children:
                    identity = child.provenance
                    key = (child.sender_id, identity.get("native_send_time"), identity.get("native_conversation_id"))
                    keyed.setdefault(key, []).append(child)
                for index, child in enumerate(children):
                    quote = child.provenance.get("quoted_identity")
                    if quote:
                        matches = keyed.get((quote["sender_id"], quote["send_time"], quote["conversation_id"]), [])
                        matches = [q for q in matches if quote_body(q.provenance.get("unquoted_text", q.text)) == quote_body(quote["text"])]
                        if len(matches) == 1:
                            children[index] = replace(child, reply_to_message_id=matches[0].id)
                info["decoded_child_count"] = len(children)
                status = "invalid_timestamp" if sent_at is None else "partial_forward" if gaps else "expanded_forward" if children else "unexpanded_forward"
                record = replace(record, parse_status=status, provenance=info)
                records.extend(children)
            records.append(record)

        return records, max_seq, max_id
