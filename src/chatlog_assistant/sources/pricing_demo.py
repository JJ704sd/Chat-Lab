"""Local presentation price book: both acquisition lanes require human review.

This deliberately uses a separate demo database and a named demo reviewer. It
never calls the legacy rule-based automatic adoption path or writes business DBs.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from .wecom_airfreight import AirfreightOperationError
from .wecom_presentation import PresentationService, _canonical
from .presentation_samples import curate_materials


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidates(
 id TEXT NOT NULL, run_id TEXT NOT NULL, lane TEXT NOT NULL, revision TEXT NOT NULL,
 payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 reviewed_payload TEXT, reviewed_at TEXT, reason TEXT, version INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(run_id,id));
CREATE TABLE IF NOT EXISTS prices(
 run_id TEXT NOT NULL, price_key TEXT NOT NULL, candidate_id TEXT NOT NULL,
 payload TEXT NOT NULL, version INTEGER NOT NULL, source_order TEXT NOT NULL,
 PRIMARY KEY(run_id,price_key));
CREATE TABLE IF NOT EXISTS decisions(
 id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
 action TEXT NOT NULL, actor TEXT NOT NULL, occurred_at TEXT NOT NULL,
 reason TEXT NOT NULL, before_json TEXT, after_json TEXT NOT NULL);
"""
REVIEWER = "价格审核负责人（演示）"
EDITABLE = {"origin", "destination", "airline", "weight_break", "amount", "currency",
            "unit", "conditions", "validity"}


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()[:24]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pdf_candidates(card: dict) -> list[dict]:
    result = []
    for row in card["rows"]:
        for tier, value in row["breaks"].items():
            if value is None:
                continue  # An empty price is an inquiry requirement, never zero.
            result.append({
                "id": "pdf-" + _digest([card["source_sha256"], row["destination"], tier]),
                "lane": "pdf", "supplier": card["supplier"], "origin": "",
                "destination": row["destination"], "airline": card["airline"],
                "weight_break": tier, "amount": value, "currency": card["currency"], "unit": "KG",
                "conditions": "；".join(filter(None, ["基础运价，附加费另计", card.get("volume_share_raw"), row["notes"]])),
                "valid_from": card["valid_from"], "validity": card["validity"],
                "scope": "供应商价表 · 按原件条款适用", "scope_key": "rate-card",
                "source_order": card["valid_from"],
                "issues": ["确认起运交仓条件", "核对分泡、附加费与补充条款"],
                "evidence": {"kind": "pdf", "sha256": card["source_sha256"],
                    "file_name": card["file_name"], "page": row["page"], "row": row["row"],
                    "bbox": row["bbox"], "raw_cells": row["raw_cells"], "minimum": row["minimum"],
                    "tier": tier, "value": value, "fees": card["fees"], "warnings": card["warnings"]},
            })
    return result


def chat_candidates(snapshot: dict) -> list[dict]:
    result = []
    source = snapshot["source"]
    if not source:
        return result
    for inquiry in snapshot["inquiries"]:
        for reply in inquiry["price_responses"]:
            body = reply.get("reply_body", reply["body"])
            segments = list(re.finditer(r"(?<![A-Z])(?:TK|SQ|CZ|HU|YG|KJ|O3)(?![A-Z])", body, re.I))
            for i, match in enumerate(segments):
                segment = body[match.end():segments[i + 1].start() if i + 1 < len(segments) else len(body)]
                if re.search(r"没做|不接|无价|不收", segment):
                    continue
                tier = re.search(r"\+\s*(45|1000|100|300|500)(?!\d)", segment)
                # Remove weight and density numbers before locating the rate.
                residual = re.sub(r"\+\s*(?:45|1000|100|300|500)(?!\d)", " ", segment)
                residual = re.sub(r"1\s*:\s*(?:1\s*:\s*)?\d+(?:\.\d+)?", " ", residual)
                value = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)", residual)
                complex_price = len(re.findall(r"1\s*:\s*\d+", segment)) > 1 or len(inquiry["destinations"]) > 1
                # Several remaining numbers may be malformed density, surcharges,
                # dates or alternate rates. Do not silently choose the first one.
                if len(re.findall(r"(?<![\d.])\d+(?:\.\d+)?", residual)) > 1:
                    complex_price = True
                if re.search(r"\d\s*\+\s*\d", residual):
                    complex_price = True
                currency = next((c for c in ("HKD", "CNY", "USD", "EUR") if re.search(r"\b" + c + r"\b", body, re.I)), "")
                origin = next((c for c in ("深圳", "广州", "鄂州", "嘉兴", "香港") if c in body[:match.end()]), "")
                issues = ["核对回复与询价的对应关系", "确认币种、单位和适用期限"]
                if complex_price:
                    issues.append("原回复包含多个价格条件，请人工拆分或明确当前价格")
                result.append({
                    "id": "chat-" + _digest([source["message_date"], inquiry["id"], reply, i]),
                    "lane": "chat", "supplier": reply["sender"], "origin": origin,
                    "destination": inquiry["destination"], "airline": match[0].upper(),
                    "weight_break": "+" + tier[1] if tier else "N" if "N价" in segment else "",
                    "amount": value[1] if value and not complex_price else "", "currency": currency,
                    "unit": reply.get("unit") or "", "conditions": body,
                    "valid_from": source["message_date"], "validity": "",
                    "scope": f"仅本票询价 · {inquiry['gross_kg']} kg / {inquiry['volume_cbm']} CBM",
                    "scope_key": _digest([source.get("group_alias"), inquiry["sender"], inquiry["original"]]),
                    "source_order": source["message_date"] + f"/{reply['capture_order']:08d}",
                    "issues": issues, "inquiry_id": inquiry["id"],
                    "evidence": {"kind": "chat", "date": source["message_date"],
                        "source_mode": source["source_mode"], "group": source.get("group_alias", "航线沟通群"),
                        "association": reply["association_label"], "reply_order": reply["capture_order"],
                        "messages": [m for m in source["messages"] if m["capture_order"] in
                            set(inquiry["message_orders"] + [reply["capture_order"]])]},
                })
    return result


class PricingDemo:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / "pricing-demo.sqlite"
        self.sources = PresentationService(self.root)

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        if not self._run(conn):
            conn.execute("INSERT INTO runs VALUES(?,?)", (uuid.uuid4().hex, _now()))
        return conn

    @staticmethod
    def _run(conn: sqlite3.Connection) -> str | None:
        row = conn.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def snapshot(self) -> dict:
        materials = curate_materials(self.root, self.sources.snapshot())
        result = {"materials": materials, "run_id": None, "candidates": [], "prices": [], "decisions": [],
                  "reviewer": REVIEWER, "demo_only": True}
        if not self.path.is_file():
            return result
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            run = self._run(conn)
            result["run_id"] = run
            for row in conn.execute("SELECT * FROM candidates WHERE run_id=? ORDER BY rowid", (run,)):
                item = json.loads(row["reviewed_payload"] or row["payload"])
                item.update(status=row["status"], reviewed_at=row["reviewed_at"], reason=row["reason"],
                            version=row["version"], revision=row["revision"],
                            stale=row["revision"] != materials["revision"])
                if (item['lane'] != 'chat' or 'curation' not in materials
                        or item['id'] in materials['curation']['candidate_ids']):
                    result["candidates"].append(item)
            for row in conn.execute("SELECT * FROM prices WHERE run_id=? ORDER BY rowid DESC", (run,)):
                result["prices"].append({**json.loads(row["payload"]), "version": row["version"],
                                         "price_key": row["price_key"]})
            result["decisions"] = [dict(row) for row in conn.execute(
                "SELECT candidate_id,action,actor,occurred_at,reason,before_json,after_json FROM decisions WHERE run_id=? ORDER BY id DESC", (run,))]
        return result

    def prepare(self, lane: str, revision: str) -> dict:
        materials = curate_materials(self.root, self.sources.snapshot())
        if revision != materials["revision"]:
            raise AirfreightOperationError("source_changed", "资料已更新，请刷新后重新整理", http_status=409)
        if lane not in {"pdf", "chat"}:
            raise AirfreightOperationError("invalid_lane", "请选择 PDF 或群聊链路")
        if lane == "pdf" and not materials["rate_card"] or lane == "chat" and not materials["source"]:
            raise AirfreightOperationError("missing_source", "请先导入本链路的真实资料")
        candidates = pdf_candidates(materials["rate_card"]) if lane == "pdf" else chat_candidates(materials)
        if lane == 'chat' and 'curation' in materials:
            candidates = [c for c in candidates if c['id'] in materials['curation']['candidate_ids']]
        with closing(self._connect()) as conn, conn:
            run = self._run(conn)
            for item in candidates:
                conn.execute("INSERT OR IGNORE INTO candidates(id,run_id,lane,revision,payload) VALUES(?,?,?,?,?)",
                             (item["id"], run, lane, revision, _canonical(item).decode()))
                # Re-importing unchanged evidence can refresh the source binding,
                # but never undoes a review or overwrites the edited payload.
                conn.execute("UPDATE candidates SET revision=? WHERE run_id=? AND id=?", (revision, run, item["id"]))
        return self.snapshot()

    @staticmethod
    def _validate(item: dict) -> None:
        labels = {"origin": "起运交仓地", "destination": "目的港", "airline": "航司", "weight_break": "重量档",
                  "currency": "币种", "unit": "计价单位", "conditions": "适用条件", "validity": "适用期限"}
        missing = [label for key, label in labels.items() if not str(item.get(key) or "").strip()]
        if missing:
            raise AirfreightOperationError("review_incomplete", "请确认：" + "、".join(missing))
        if re.fullmatch(r"\d+", item['validity'].strip()):
            raise AirfreightOperationError('validity_unclear', '适用期限不能只填写数字，请填写日期或明确的适用说明')
        try:
            value = Decimal(str(item.get("amount")))
            if not value.is_finite() or value <= 0:
                raise InvalidOperation
            item["amount"] = str(value.normalize()) if value < 1 else format(value, "f")
        except InvalidOperation:
            raise AirfreightOperationError("invalid_price", "请填写大于零的明确价格")
        if not re.fullmatch(r"[A-Z]{3}", item["destination"]):
            raise AirfreightOperationError("destination_unclear", "请明确一个目的港，不能将多目的港合并生效")
        if item["currency"] not in {"HKD", "CNY", "USD", "EUR"} or item["unit"] not in {"KG", "票"}:
            raise AirfreightOperationError("pricing_basis_unclear", "请选择明确的币种和计价单位")

    @staticmethod
    def _price_key(item: dict) -> str:
        # Chat quotes retain the cargo/inquiry scope; never promote a spot quote
        # to an unrestricted route tariff. Density/share terms distinguish offers.
        terms = re.findall(r"1\s*:\s*[\d:]+|分半|1/3 volume share", item["conditions"])
        return _digest([item[k] for k in ("lane", "supplier", "origin", "destination", "airline",
                                         "weight_break", "currency", "unit", "scope_key")] + [terms])

    def review(self, value: dict) -> dict:
        ids = value.get("candidate_ids")
        if not isinstance(ids, list) or not ids or len(ids) > 250 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            raise AirfreightOperationError("selection_required", "请明确选择要审核的价格")
        if value.get("action") not in {"approve", "reject"}:
            raise AirfreightOperationError("invalid_action", "请选择通过或驳回")
        # Clicking the review action is the human decision. Keep its audit trail
        # without requiring a separate attestation checkbox or a written note.
        reason = str(value.get("reason") or "").strip()
        if len(reason) > 1000:
            raise AirfreightOperationError("reason_too_long", "审核说明不能超过 1000 字")
        reason = reason or ("人工点击审核通过" if value["action"] == "approve" else "人工点击驳回")
        changes = value.get("corrections") or {}
        if not isinstance(changes, dict) or set(changes) - EDITABLE:
            raise AirfreightOperationError("invalid_correction", "审核只能修改业务字段")
        if any(not isinstance(v, str) or len(v) > 2000 for v in changes.values()):
            raise AirfreightOperationError("invalid_correction", "请使用不超过 2000 字的字段内容")
        demo_fields = value.get("demo_prefill_fields", [])
        if (not isinstance(demo_fields, list) or any(not isinstance(k, str) for k in demo_fields)
                or set(demo_fields) - set(changes)):
            raise AirfreightOperationError("invalid_prefill_fields", "演示预填标记只能对应本次填写的业务字段")
        materials = curate_materials(self.root, self.sources.snapshot())
        with closing(self._connect()) as conn, conn:
            run = self._run(conn)
            if value.get("run_id") != run or value.get("revision") != materials["revision"]:
                raise AirfreightOperationError("review_changed", "演示或资料已更新，请刷新后审核", http_status=409)
            rows = [conn.execute("SELECT * FROM candidates WHERE run_id=? AND id=?", (run, i)).fetchone() for i in ids]
            if any(row is None for row in rows):
                raise AirfreightOperationError("candidate_missing", "所选价格不存在", http_status=404)
            if 'curation' in materials and any(row['lane'] == 'chat' and
                    row['id'] not in materials['curation']['candidate_ids'] for row in rows):
                raise AirfreightOperationError('case_not_selected', '此报价未纳入精选演示，请刷新后选择已核对案例', http_status=409)
            if len(rows) > 1 and any(row["lane"] != "pdf" for row in rows):
                raise AirfreightOperationError("chat_review_one", "群聊价格必须逐条人工审核")
            if len(rows) > 1 and set(changes) - {"origin", "validity"}:
                raise AirfreightOperationError("batch_correction", "批量审核仅支持统一确认交仓地和适用期限；其他字段请逐条修改")
            # Each request is one transaction: validation/conflict failure cannot
            # partially publish a selected batch. A reviewed row cannot be replayed.
            for row in rows:
                if row["status"] != "pending" or row["revision"] != materials["revision"]:
                    raise AirfreightOperationError("already_reviewed", "价格已处理或资料已变化，请刷新后核对", http_status=409)
                original = json.loads(row["payload"])
                item = {**original, **{k: v.strip() for k, v in changes.items()}}
                item["demo_prefill_fields"] = sorted(set(demo_fields))
                before = None
                version = 0
                if value["action"] == "approve":
                    self._validate(item)
                    key = self._price_key(item)
                    current = conn.execute("SELECT * FROM prices WHERE run_id=? AND price_key=?", (run, key)).fetchone()
                    if current and item["source_order"] < current["source_order"]:
                        raise AirfreightOperationError("older_quote", "已有更新报价，不能用旧报价覆盖", http_status=409)
                    before = current["payload"] if current else None
                    version = current["version"] + 1 if current else 1
                    item.update(reviewed_by=REVIEWER, reviewed_at=_now(), review_reason=reason,
                                manual_fields=list(changes), status="approved")
                    conn.execute("INSERT OR REPLACE INTO prices VALUES(?,?,?,?,?,?)",
                                 (run, key, item["id"], _canonical(item).decode(), version, item["source_order"]))
                status = "approved" if value["action"] == "approve" else "rejected"
                conn.execute("UPDATE candidates SET status=?,reviewed_payload=?,reviewed_at=?,reason=?,version=? WHERE run_id=? AND id=?",
                             (status, _canonical(item).decode(), _now(), reason, version, run, item["id"]))
                conn.execute("INSERT INTO decisions(run_id,candidate_id,action,actor,occurred_at,reason,before_json,after_json) VALUES(?,?,?,?,?,?,?,?)",
                             (run, item["id"], value["action"], REVIEWER, _now(), reason, before, _canonical(item).decode()))
        return self.snapshot()

    def new_run(self) -> dict:
        """Archive rather than erase the previous presentation's review history."""
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT INTO runs VALUES(?,?)", (uuid.uuid4().hex, _now()))
        return self.snapshot()
