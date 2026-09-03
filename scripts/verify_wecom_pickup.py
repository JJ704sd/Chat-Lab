"""Build and verify a synthetic pickup-fleet acceptance fixture.

The fixture deliberately uses the same parser, storage, analysis rebuild and
HTTP handler as the local dashboard.  It never opens or modifies the real
database.  ``--verify`` runs the HTTP checks and writes the selected-row xlsx
sample under ``outputs/wecom-pickup-acceptance``; ``--serve`` keeps the same
temporary fixture available for browser acceptance.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
from typing import Any, Mapping
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zipfile import ZipFile
import xml.etree.ElementTree as ET

# Make direct ``python scripts/verify_wecom_pickup.py`` work from the repo.
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from chatlog_assistant.sources.wecom_parser import records_from_message_tree
from chatlog_assistant.sources.wecom_storage import WecomLocalStorage
from chatlog_assistant.sources.wecom_web import WecomDashboardHandler
from chatlog_assistant.sources.wecom_pricing import PriceMaintenance


CN_TZ = timezone(timedelta(hours=8))
SCOPE = {
    "account_id": "pickup-fixture",
    "source_database": "synthetic-pickup-db",
    "conversation_id": "pickup-room",
    "conversation_name": "揽收车队合成群",
}
PAGINATION_SCOPE = {
    "account_id": "pickup-fixture",
    "source_database": "synthetic-pickup-db",
    "conversation_id": "pagination-room",
    "conversation_name": "分页验证群（揽收车队）",
}
FOREIGN_SCOPE = {
    "account_id": "other-account",
    "source_database": "other-db",
    "conversation_id": "other-room",
    "conversation_name": "其他范围群",
}
OUTPUT_DIR = REPO_ROOT / "outputs" / "wecom-pickup-acceptance"
CORE_HEADERS = ["起点", "终点", "重量", "体积", "包装数量", "时效", "价格"]


@dataclass
class Fixture:
    tempdir: tempfile.TemporaryDirectory[str]
    storage: WecomLocalStorage
    manifest: dict[str, Any]

    @property
    def db_path(self) -> Path:
        return Path(self.storage.path)

    def close(self) -> None:
        self.tempdir.cleanup()


def _messages(scope: Mapping[str, str]) -> list[dict[str, Any]]:
    """Messages cover both role presentation and evidence navigation."""
    base = datetime(2026, 9, 3, 9, 0, tzinfo=CN_TZ)
    messages = [
        {"source_message_id": "ask-1", "sender_id": "zhang", "sender_display": "张调度",
         "sender_corp_id": "zhongji", "sender_corp_name": "中技物流",
         "sent_at": base.isoformat(),
         "content": "从上海到深圳，1000kg，2cbm，10箱，请报价"},
        {"source_message_id": "quote-a", "sender_id": "li-a", "sender_display": "李经理",
         "sender_corp_id": "fleet-a", "sender_corp_name": "嘉航车队",
         "sent_at": (base + timedelta(minutes=1)).isoformat(), "reply_to_message_id": "ask-1",
         "content": "深圳：620 CNY 总价 隔日达；深圳：700 CNY 总价 当天达"},
        {"source_message_id": "quote-b", "sender_id": "wang-b", "sender_display": "王师傅",
         "sender_corp_id": "fleet-b", "sender_corp_name": "迅达车队",
         "sent_at": (base + timedelta(minutes=2)).isoformat(), "reply_to_message_id": "ask-1",
         "content": "深圳：680 CNY 总价 次日达"},
        {"source_message_id": "quote-missing", "sender_id": "li-a", "sender_display": "李经理",
         "sender_corp_id": "fleet-a", "sender_corp_name": "嘉航车队",
         "sent_at": (base + timedelta(minutes=3)).isoformat(), "reply_to_message_id": "ask-1",
         "content": "深圳：650 CNY 总价，时效待确认"},
        {"source_message_id": "unknown-1", "sender_id": "guest", "sender_display": "访客",
         "sender_corp_id": "unknown-corp", "sender_corp_name": "未知企业",
         "sent_at": (base + timedelta(minutes=4)).isoformat(),
         "content": "有人知道这票怎么安排吗？"},
        {"source_message_id": "quoted-1", "sender_id": "zhang", "sender_display": "张调度",
         "sender_corp_id": "zhongji", "sender_corp_name": "中技物流",
         "sent_at": (base + timedelta(minutes=5)).isoformat(), "reply_to_message_id": "quote-a",
         "quoted_text": "深圳：620 CNY 总价 隔日达", "content": "深圳：620 CNY 总价 隔日达\n请确认可否安排"},
        {"source_message_id": "forward-1", "sender_id": "guest", "sender_display": "访客",
         "sender_corp_id": "unknown-corp", "sender_corp_name": "未知企业",
         "sent_at": (base + timedelta(minutes=6)).isoformat(),
         "content": "[群聊的聊天记录]", "forwarded_messages": [
             {"source_message_id": "forwarded-original", "sender_id": "zhang",
              "sender_display": "张调度", "sender_corp_id": "zhongji",
              "sender_corp_name": "中技物流",
              "sent_at": (base + timedelta(minutes=6, seconds=1)).isoformat(),
              "content": "原始作者确认按约定揽收"},
         ]},
        {"source_message_id": "quote-repair", "sender_id": "li-a", "sender_display": "李经理",
         "sender_corp_id": "fleet-a", "sender_corp_name": "嘉航车队",
         "sent_at": (base + timedelta(minutes=7)).isoformat(), "reply_to_message_id": "ask-1",
         "content": "深圳：640 总价 次日达"},
        {"source_message_id": "ask-2", "sender_id": "zhang", "sender_display": "张调度",
         "sender_corp_id": "zhongji", "sender_corp_name": "中技物流",
         "sent_at": (base + timedelta(minutes=7, seconds=10)).isoformat(),
         "content": "从上海到深圳，请报价"},
        {"source_message_id": "quote-missing-numeric", "sender_id": "li-a", "sender_display": "李经理",
         "sender_corp_id": "fleet-a", "sender_corp_name": "嘉航车队",
         "sent_at": (base + timedelta(minutes=7, seconds=20)).isoformat(), "reply_to_message_id": "ask-2",
         "content": "深圳：655 CNY 总价 隔日达"},
    ]
    # Enough ordinary adjacent messages make the evidence context expansion
    # visible in the browser (the initial window is 20, not the whole room).
    for index in range(60):
        messages.append({"source_message_id": f"context-{index:02d}", "sender_id": "context-user",
            "sender_display": "群成员", "sender_corp_id": "unknown-corp",
            "sender_corp_name": "未知企业", "sent_at": (base + timedelta(minutes=8 + index)).isoformat(),
            "content": f"收到，继续跟进揽收上下文 {index + 1:02d}"})
    return messages


def _field_sources(message_id: str, *, missing: tuple[str, ...] = ()) -> dict[str, dict[str, str]]:
    values = {
        "origin": "上海", "destination": "深圳", "weight": "1000kg", "volume": "2cbm",
        "package_count": "10箱", "service_time": "隔日达", "price": "620",
        "quote_company": "嘉航车队", "currency": "CNY", "pricing_method": "总价",
    }
    return {field: {"message_id": message_id, "excerpt": value}
            for field, value in values.items() if field not in missing}


def _quote_payload(*, company_id: str, company_name: str, amount: str, service_time: str,
                   quote_message_id: str, quote_time: str, scope: Mapping[str, str],
                   missing: tuple[str, ...] = (), pricing_method: str = "总价") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "origin": "上海", "destination": "深圳", "weight": "1000kg", "volume": "2cbm",
        "package_count": "10", "package_type": "箱", "service_time": service_time,
        "price": amount, "quote_company_id": company_id, "quote_company_name": company_name,
        "quoted_by_id": "li-a" if company_id == "fleet-a" else "wang-b",
        "quoted_by_name": "李经理" if company_id == "fleet-a" else "王师傅",
        "inquiry_company_id": "zhongji", "inquiry_company_name": "中技物流",
        "inquired_by_id": "zhang", "inquired_by_name": "张调度",
        "currency": "CNY", "pricing_method": pricing_method, "quote_time": quote_time,
        "source_message_id": quote_message_id, "source_reference": f"synthetic/{quote_message_id}",
        "company_status": "known", "field_sources": _field_sources(quote_message_id, missing=missing),
        "source_scope": dict(scope),
    }
    for key in missing:
        # Keep the raw storage value empty; no zero or guessed replacement.
        payload[key] = None
    return payload


def _finish_generated_candidates(storage: WecomLocalStorage) -> dict[str, Any]:
    """Review generated candidates whose explicit text supplies a missing method."""
    service = storage.price_maintenance()
    candidates = service.list_candidates(account_id=SCOPE["account_id"], source_database=SCOPE["source_database"],
                                         conversation_id=SCOPE["conversation_id"], limit=100)
    reviewed: list[dict[str, Any]] = []
    for item in candidates:
        missing = set(item.get("missing_fields") or [])
        correction: dict[str, Any] = {}
        if "currency" in missing:
            correction["currency"] = "CNY"
        if "pricing_method" in missing:
            correction["pricing_method"] = "total"
        # Leave rows without cargo/service facts visible as 待完善.  The
        # other missing values are explicit in their quote text and this
        # correction goes through the same human review transaction used by UI.
        if not correction or "service_time" in missing or any(field in missing for field in ("weight", "volume", "package_count")):
            continue
        reviewed.append(service.review_candidate(
            item["candidate_id"], action="correct_and_approve", actor_id="reviewer",
            actor_name="合成审核员", reason="合成验收：从报价原文补录币种与计价方式后通过",
            correction=correction, account_id=SCOPE["account_id"],
            source_database=SCOPE["source_database"], conversation_id=SCOPE["conversation_id"],
            idempotency_key=f"repair-review-{item['candidate_id']}"))
    return {"generated_candidates": candidates, "reviewed": reviewed}


def _build_pagination_prices(storage: WecomLocalStorage, count: int = 520) -> dict[str, Any]:
    """Create 520 effective rows in an isolated conversation for paging/export."""
    service = storage.price_maintenance()
    base = datetime(2026, 9, 3, 8, 0, tzinfo=CN_TZ)
    messages = [{
        "source_message_id": f"pagination-quote-{index:04d}", "sender_id": f"page-supplier-{index:04d}",
        "sender_display": f"分页供应商{index:04d}", "sender_corp_id": f"page-fleet-{index:04d}",
        "sender_corp_name": f"分页车队{index:04d}",
        "sent_at": (base + timedelta(seconds=index)).isoformat(),
        "content": f"分页验证：上海到深圳，1000kg，2cbm，10箱，分页车队{index:04d}报价 {800 + index} CNY 总价 隔日达",
    } for index in range(count)]
    records, gaps = records_from_message_tree(messages, account_id=PAGINATION_SCOPE["account_id"],
        source_database=PAGINATION_SCOPE["source_database"], conversation_id=PAGINATION_SCOPE["conversation_id"],
        conversation_name=PAGINATION_SCOPE["conversation_name"], source_reference="synthetic/pagination", strict=True)
    storage.upsert_messages(records)
    result_ids: list[str] = []
    for index in range(count):
        message_id = f"pagination-quote-{index:04d}"
        company_name = f"分页车队{index:04d}"
        amount = str(800 + index)
        payload = _quote_payload(company_id=f"page-fleet-{index:04d}", company_name=company_name,
            amount=amount, service_time="隔日达", quote_message_id=message_id,
            quote_time="2026-09-03T08:00:00+08:00", scope=PAGINATION_SCOPE)
        payload["quoted_by_id"] = f"page-supplier-{index:04d}"
        payload["quoted_by_name"] = f"分页供应商{index:04d}"
        payload["field_sources"] = {field: {"message_id": message_id, "excerpt": excerpt} for field, excerpt in {
            "origin": "上海", "destination": "深圳", "weight": "1000kg", "volume": "2cbm",
            "package_count": "10箱", "service_time": "隔日达", "price": amount,
            "quote_company": company_name, "currency": "CNY", "pricing_method": "总价"}.items()}
        # Unique company IDs make each row a distinct maintained match key.
        result = service.submit_candidate(payload, source_kind="chat", source_scope=PAGINATION_SCOPE,
            idempotency_key=f"pagination-{index:04d}", actor_type="system", actor_id="system", actor_name="系统执行")
        result_ids.append(result.get("record_id") or result.get("candidate_id") or "")
    return {"count": count, "record_ids": result_ids, "gaps": gaps}


def _build_fixture() -> Fixture:
    tempdir = tempfile.TemporaryDirectory(prefix="wecom-pickup-acceptance-")
    storage = WecomLocalStorage(Path(tempdir.name) / "synthetic-analysis.db")
    storage.initialize()
    storage.price_maintenance().configure_responsibility("reviewer", "合成审核员")

    records, gaps = records_from_message_tree(_messages(SCOPE), account_id=SCOPE["account_id"],
        source_database=SCOPE["source_database"], conversation_id=SCOPE["conversation_id"],
        conversation_name=SCOPE["conversation_name"], source_reference="synthetic/pickup", strict=True)
    storage.upsert_messages(records)
    storage.confirm_business_role(account_id=SCOPE["account_id"], source_database=SCOPE["source_database"],
        conversation_id=SCOPE["conversation_id"], company_id="fleet-a", company_name="嘉航车队",
        business_role="supplier", basis="合成审核确认的车队业务角色", actor_id="reviewer", actor_name="合成审核员")
    storage.confirm_business_role(account_id=SCOPE["account_id"], source_database=SCOPE["source_database"],
        conversation_id=SCOPE["conversation_id"], company_id="fleet-b", company_name="迅达车队",
        business_role="supplier", basis="合成审核确认的车队业务角色", actor_id="reviewer", actor_name="合成审核员")

    # This is intentionally a real rebuild. It creates the rule-linked quote
    # candidates first; explicit candidates below fill the deterministic
    # currency/pricing-method and multi-option acceptance cases.
    rebuild = storage.rebuild_analysis(account_id=SCOPE["account_id"], conversation_id=SCOPE["conversation_id"])
    prices = _finish_generated_candidates(storage)
    pagination = _build_pagination_prices(storage)

    # One record in another account/source proves HTTP range isolation.
    foreign_records, foreign_gaps = records_from_message_tree([{
        "source_message_id": "foreign-quote", "sender_id": "foreign", "sender_display": "外部报价员",
        "sender_corp_id": "foreign-fleet", "sender_corp_name": "外部车队",
        "sent_at": "2026-09-03T07:00:00+08:00", "content": "外部范围报价",
    }], account_id=FOREIGN_SCOPE["account_id"], source_database=FOREIGN_SCOPE["source_database"],
        conversation_id=FOREIGN_SCOPE["conversation_id"], conversation_name=FOREIGN_SCOPE["conversation_name"],
        source_reference="synthetic/foreign", strict=True)
    storage.upsert_messages(foreign_records)
    foreign_payload = _quote_payload(company_id="foreign-fleet", company_name="外部车队", amount="999",
        service_time="隔日达", quote_message_id="foreign-quote", quote_time="2026-09-03T07:00:00+08:00", scope=FOREIGN_SCOPE)
    foreign_price = storage.price_maintenance().submit_candidate(foreign_payload, source_kind="chat",
        source_scope=FOREIGN_SCOPE, idempotency_key="foreign-price", actor_type="system", actor_id="system", actor_name="系统执行")

    manifest = {
        "business_direction": "揽收车队群聊分析", "scope": SCOPE,
        "pagination_scope": PAGINATION_SCOPE, "foreign_scope": FOREIGN_SCOPE,
        "db_path": str(storage.path), "message_count": len(records), "parse_gaps": gaps,
        "rebuild": rebuild,
        "prices": {
            "generated_candidates": [{k: item.get(k) for k in (
                "candidate_id", "record_id", "applied", "review_status", "adoption_status", "display_status", "missing_fields")}
                for item in prices["generated_candidates"]],
            "reviewed": [{k: item.get(k) for k in (
                "candidate_id", "record_id", "applied", "review_status", "adoption_status")}
                for item in prices["reviewed"]],
        },
        "pagination": {"count": pagination["count"], "gaps": pagination["gaps"]},
        "foreign_price": {k: foreign_price.get(k) for k in ("record_id", "candidate_id", "applied")},
        "generated_at": datetime.now(CN_TZ).isoformat(),
    }
    return Fixture(tempdir=tempdir, storage=storage, manifest=manifest)


def _static_bytes() -> bytes:
    path = SRC_ROOT / "chatlog_assistant" / "static" / "wecom.html"
    return path.read_bytes()


def start_http(fixture: Fixture, port: int = 0):
    """Start the real dashboard handler on loopback, returning server/thread."""
    from http.server import ThreadingHTTPServer
    handler = type("AcceptanceWecomHandler", (WecomDashboardHandler,), {
        "storage": fixture.storage, "dashboard_bytes": _static_bytes(),
        "csrf_token": "synthetic-acceptance-csrf",
    })
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, name="wecom-acceptance-http", daemon=True)
    thread.start()
    return server, thread


def _get(base_url: str, path: str, params: Mapping[str, Any] | None = None) -> tuple[int, bytes, Mapping[str, str]]:
    url = base_url + path
    if params:
        url += "?" + urlencode({key: value for key, value in params.items() if value is not None})
    try:
        with urlopen(Request(url, headers={"Connection": "close"}), timeout=15) as response:
            return response.status, response.read(), response.headers
    except Exception as exc:
        if hasattr(exc, "code") and hasattr(exc, "read"):
            body = exc.read()
            if hasattr(exc, "close"):
                exc.close()
            return int(exc.code), body, getattr(exc, "headers", {})
        raise


def _json_get(base_url: str, path: str, params: Mapping[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    status, body, _ = _get(base_url, path, params)
    return status, json.loads(body.decode("utf-8"))


def _post_json(base_url: str, path: str, value: Mapping[str, Any], *, csrf: str,
               actor_id: str = "reviewer", actor_name: str = "合成审核员") -> tuple[int, dict[str, Any]]:
    body = json.dumps(value, ensure_ascii=False).encode("utf-8")
    request = Request(base_url + path, data=body, method="POST", headers={
        "Content-Type": "application/json", "Content-Length": str(len(body)),
        "Origin": base_url, "X-CSRF-Token": csrf,
        # The web client transports these byte-string headers as percent
        # encoded Unicode; the server must decode before checking identity.
        "X-Operator-Id": quote(actor_id, safe=""),
        "X-Operator-Name": quote(actor_name, safe=""),
        "Connection": "close",
    })
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        if hasattr(exc, "code") and hasattr(exc, "read"):
            body = exc.read()
            if hasattr(exc, "close"):
                exc.close()
            return int(exc.code), json.loads(body.decode("utf-8"))
        raise


def _xlsx_rows(data: bytes) -> list[list[str]]:
    """Independent OOXML reader: no openpyxl or application is needed."""
    with ZipFile(io.BytesIO(data)) as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows: list[list[str]] = []
    for row in root.findall(".//x:sheetData/x:row", ns):
        cells: dict[int, str] = {}
        for cell in row.findall("x:c", ns):
            ref = cell.attrib.get("r", "A1")
            letters = re.match(r"([A-Z]+)", ref)
            if not letters:
                continue
            col = 0
            for letter in letters.group(1):
                col = col * 26 + ord(letter) - 64
            text_node = cell.find(".//x:t", ns)
            value = text_node.text if text_node is not None and text_node.text is not None else ""
            numeric = cell.find("x:v", ns)
            if not value and numeric is not None and numeric.text is not None:
                value = numeric.text
            cells[col] = value
        rows.append([cells.get(index, "") for index in range(1, max(cells, default=0) + 1)])
    return rows


def run_http_verification(fixture: Fixture, *, base_url: str | None = None) -> dict[str, Any]:
    own_server = None
    if base_url is None:
        own_server, _ = start_http(fixture)
        base_url = f"http://127.0.0.1:{own_server.server_address[1]}"
    scope_params = dict(SCOPE)
    results: dict[str, Any] = {"base_url": base_url, "checks": {}}
    try:
        status, details = _json_get(base_url, "/api/wecom/prices", {**scope_params, "view": "details", "limit": 100})
        items = details.get("items", [])
        results["checks"]["details_view"] = {
            "status": status, "items": len(items), "has_core_fields": bool(items and all(
                field in (items[0].get("payload") or {}) for field in
                ("origin_normalized", "destination_normalized", "weight_value", "volume_value",
                 "package_count", "service_time_raw", "price_raw"))),
            "display_statuses": sorted({item.get("display_status") for item in items}),
        }
        assert status == 200 and len(items) >= 5, details
        assert {"有效价格", "待完善"}.issubset({item.get("display_status") for item in items})

        current_status, current = _json_get(base_url, "/api/wecom/prices", {**scope_params, "view": "current", "limit": 100})
        pending_status, pending = _json_get(base_url, "/api/wecom/prices", {**scope_params, "view": "pending", "limit": 100})
        results["checks"]["view_consistency"] = {"current_status": current_status, "current": current.get("total"),
            "pending_status": pending_status, "pending": pending.get("total"), "details": details.get("total")}
        assert current_status == pending_status == 200
        assert current.get("total", 0) >= 4 and pending.get("total", 0) >= 1

        # Exercise the exact browser write-header representation, including a
        # percent-encoded Chinese reviewer name.  This candidate is intentionally
        # a harmless pending manual row in the synthetic database.
        csrf_status, csrf_payload = _json_get(base_url, "/api/wecom/csrf")
        operator_payload = _quote_payload(company_id="fleet-a", company_name="嘉航车队", amount="611",
            service_time="隔日达", quote_message_id="quote-a", quote_time="2026-09-03T09:01:00+08:00", scope=SCOPE)
        operator_payload["idempotency_key"] = "http-encoded-operator"
        operator_payload["source_scope"] = dict(SCOPE)
        operator_status, operator_result = _post_json(base_url, "/api/wecom/price-candidates",
            operator_payload, csrf=csrf_payload.get("csrf_token", ""))
        results["checks"]["encoded_operator_header"] = {
            "csrf_status": csrf_status, "status": operator_status,
            "created_by_name": operator_result.get("created_by_name"),
            "review_status": operator_result.get("review_status"),
        }
        assert csrf_status == 200 and operator_status == 201
        assert operator_result.get("created_by_name") == "合成审核员"

        first = next(item for item in items if item.get("record_id"))
        detail_status, detail = _json_get(base_url, "/api/wecom/price-detail", {**scope_params, "record_id": first["record_id"]})
        export_status, export_data, export_headers = _get(base_url, "/api/wecom/price-export", {**scope_params, "record_id": first["record_id"]})
        rows = _xlsx_rows(export_data)
        results["checks"]["single_export"] = {"status": export_status, "content_type": export_headers.get("Content-Type"),
            "headers": rows[0] if rows else [], "data_rows": max(0, len(rows) - 1), "bytes": len(export_data)}
        assert detail_status == export_status == 200 and rows[0] == CORE_HEADERS and len(rows) == 2
        sample_path = OUTPUT_DIR / "pickup_quote_single.xlsx"
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        sample_path.write_bytes(export_data)

        incomplete = next(item for item in items if item.get("display_status") == "待完善")
        incomplete_status, incomplete_data, _ = _get(base_url, "/api/wecom/price-export", {
            **scope_params, "candidate_id": incomplete["candidate_id"]})
        incomplete_rows = _xlsx_rows(incomplete_data)
        incomplete_path = OUTPUT_DIR / "pickup_quote_incomplete.xlsx"
        incomplete_path.write_bytes(incomplete_data)
        results["checks"]["incomplete_export"] = {
            "status": incomplete_status, "headers": incomplete_rows[0] if incomplete_rows else [],
            "data_rows": max(0, len(incomplete_rows) - 1),
            "blank_core_cells": [index for index, value in enumerate(incomplete_rows[1], 1) if value == ""]
                                if len(incomplete_rows) > 1 else [],
            "display_status": incomplete.get("display_status"),
        }
        assert incomplete_status == 200 and incomplete_rows[0] == CORE_HEADERS and len(incomplete_rows) == 2
        assert any(incomplete_rows[1][index] == "" for index in (2, 3, 4))

        evidence_status, evidence = _json_get(base_url, "/api/wecom/messages/quote-a/evidence", {**scope_params, "context_limit": 20})
        forwarded_status, forwarded = _json_get(base_url, "/api/wecom/messages/forward-1/forwarded", scope_params)
        results["checks"]["evidence"] = {"status": evidence_status, "messages": len(evidence.get("messages", [])),
            "supplier_role": evidence.get("message", {}).get("business_role"),
            "forwarded_status": forwarded_status, "forwarded_messages": len(forwarded.get("messages", [])),
            "original_sender": (forwarded.get("messages") or [{}])[0].get("sender_name")}
        assert evidence_status == forwarded_status == 200
        assert evidence.get("message", {}).get("business_role") == "supplier"
        assert (forwarded.get("messages") or [{}])[0].get("sender_name") == "张调度"

        # The isolated pagination group must export all 520 rows, regardless of
        # the UI page size (the handler loops through all cursors).
        page_status, page_one = _json_get(base_url, "/api/wecom/prices", {**PAGINATION_SCOPE, "view": "current", "limit": 500})
        page_two_status, page_two = _json_get(base_url, "/api/wecom/prices", {**PAGINATION_SCOPE, "view": "current", "limit": 500, "cursor": page_one.get("next_cursor")})
        all_export_status, all_export_data, _ = _get(base_url, "/api/wecom/prices-export", {**PAGINATION_SCOPE, "view": "current"})
        all_rows = _xlsx_rows(all_export_data)
        results["checks"]["cross_page_export"] = {"first_page": page_one.get("total"), "first_page_items": len(page_one.get("items", [])),
            "second_page_status": page_two_status, "second_page_items": len(page_two.get("items", [])),
            "export_status": all_export_status, "export_data_rows": max(0, len(all_rows) - 1)}
        assert page_status == page_two_status == all_export_status == 200
        assert len(page_one.get("items", [])) == 500 and len(page_two.get("items", [])) == 20 and len(all_rows) == 521

        foreign_status, foreign_view = _json_get(base_url, "/api/wecom/prices", {**SCOPE, "view": "details", "limit": 100})
        results["checks"]["source_isolation"] = {"status": foreign_status, "contains_foreign": any(
            (item.get("payload") or {}).get("quote_company_id") == "foreign-fleet" for item in foreign_view.get("items", []))}
        assert foreign_status == 200 and not results["checks"]["source_isolation"]["contains_foreign"]
        wrong_detail_status, wrong_detail = _json_get(base_url, "/api/wecom/price-detail", {
            "account_id": "other-account", "source_database": SCOPE["source_database"],
            "conversation_id": SCOPE["conversation_id"], "record_id": first["record_id"]})
        wrong_export_status, _, _ = _get(base_url, "/api/wecom/price-export", {
            "account_id": "other-account", "source_database": SCOPE["source_database"],
            "conversation_id": SCOPE["conversation_id"], "record_id": first["record_id"]})
        results["checks"]["detail_scope_isolation"] = {
            "detail_status": wrong_detail_status, "export_status": wrong_export_status,
            "error_code": wrong_detail.get("error_code"),
        }
        assert wrong_detail_status == wrong_export_status == 404
    finally:
        if own_server is not None:
            own_server.shutdown()
            own_server.server_close()
    results["sample_xlsx"] = str(OUTPUT_DIR / "pickup_quote_single.xlsx")
    results["incomplete_sample_xlsx"] = str(OUTPUT_DIR / "pickup_quote_incomplete.xlsx")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="run loopback HTTP and independent XLSX checks")
    parser.add_argument("--serve", action="store_true", help="serve the temporary fixture for browser acceptance")
    parser.add_argument("--port", type=int, default=8879)
    args = parser.parse_args(argv)
    if not args.verify and not args.serve:
        parser.error("请指定 --verify 或 --serve")
    fixture = _build_fixture()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = OUTPUT_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(fixture.manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.verify:
        result = run_http_verification(fixture)
        manifest_path.write_text(json.dumps({**fixture.manifest, "verification": result}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.serve:
        server, thread = start_http(fixture, args.port)
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_address[1]}",
                          "manifest": str(manifest_path), "db_path": str(fixture.db_path)}, ensure_ascii=False), flush=True)
        try:
            # start_http owns the single serve_forever loop in its daemon
            # thread; the main thread only keeps the temporary DB alive.
            thread.join()
        except KeyboardInterrupt:
            server.shutdown()
        finally:
            server.server_close()
    fixture.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
