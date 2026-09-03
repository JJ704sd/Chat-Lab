"""Local-first price maintenance, review, audit and safe XLSX exchange.

The module deliberately owns the write path for maintained prices.  Chat
analysis, the web UI and workbook import all create candidates here; only the
transactional apply step can change the current-price row.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import base64
import json
from pathlib import Path
import re
import secrets
from typing import Any, Callable, Iterable, Mapping, Protocol
import unicodedata
import uuid
import zipfile
from xml.etree import ElementTree as ET


PRICE_RULE_VERSION = "price-rules-1"
PRICE_TEMPLATE_VERSION = "1.0"
DEFAULT_RESPONSIBILITY_ROLE = "价格审核负责人（运营／报价管理岗）"
MAX_XLSX_BYTES = 10 * 1024 * 1024
MAX_XLSX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_XLSX_ROWS = 10_000
MAX_XLSX_ENTRIES = 200

PRICE_CORE_COLUMNS = ("起点", "终点", "重量", "体积", "包装数量", "时效", "价格")
PRICE_COLUMNS = PRICE_CORE_COLUMNS + (
    "报价公司", "报价公司ID", "报价方姓名", "询价公司", "询价公司ID", "询价人姓名",
    "重量单位", "体积单位", "包装类型", "币种", "计价方式", "报价时间",
    "接收时间", "有效期", "状态", "采用状态", "判断方式", "责任岗位", "审核人",
    "来源类型", "来源范围", "来源消息ID", "字段来源", "record_id", "base_version", "模板版本",
    "显示状态",
)
SYSTEM_COLUMNS = {"状态", "采用状态", "判断方式", "责任岗位", "审核人", "record_id", "base_version", "模板版本", "显示状态"}


PRICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL
);
INSERT OR IGNORE INTO price_settings(setting_key, setting_value)
VALUES ('responsibility_role', '价格审核负责人（运营／报价管理岗）');
INSERT OR IGNORE INTO price_settings(setting_key, setting_value)
VALUES ('price_review_llm_enabled', '0');
INSERT OR IGNORE INTO price_settings(setting_key, setting_value)
VALUES ('price_review_llm_model', '');
INSERT OR IGNORE INTO price_settings(setting_key, setting_value)
VALUES ('price_review_llm_model_version', '');

CREATE TABLE IF NOT EXISTS price_candidates (
    candidate_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    source_database TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    conversation_name TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL CHECK (source_kind IN ('chat', 'manual', 'excel', 'system')),
    source_scope_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    field_sources_json TEXT NOT NULL,
    match_key_json TEXT,
    match_key_hash TEXT,
    business_time TEXT,
    received_at TEXT,
    maintained_at TEXT NOT NULL,
    base_version INTEGER NOT NULL DEFAULT 0,
    request_id TEXT NOT NULL,
    review_status TEXT NOT NULL CHECK (review_status IN ('pending', 'needs_review', 'approved', 'rejected', 'conflict')),
    adoption_status TEXT NOT NULL CHECK (adoption_status IN ('not_applied', 'applied', 'not_applied_old', 'conflict', 'deactivated')),
    validation_json TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    model_version TEXT,
    model_result_json TEXT,
    status_reason TEXT NOT NULL DEFAULT '',
    created_by_type TEXT NOT NULL CHECK (created_by_type IN ('system', 'human')),
    created_by_id TEXT,
    created_by_name TEXT,
    UNIQUE(account_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_price_candidates_scope
ON price_candidates(account_id, source_database, conversation_id, review_status);
CREATE INDEX IF NOT EXISTS idx_price_candidates_key
ON price_candidates(account_id, source_database, match_key_hash);

CREATE TABLE IF NOT EXISTS price_current (
    record_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    source_database TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    conversation_name TEXT NOT NULL DEFAULT '',
    match_key_hash TEXT NOT NULL,
    match_key_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    business_time TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    state TEXT NOT NULL CHECK (state IN ('current', 'deactivated')),
    candidate_id TEXT NOT NULL,
    maintained_at TEXT NOT NULL,
    responsibility_role TEXT NOT NULL,
    last_actor_type TEXT NOT NULL CHECK (last_actor_type IN ('system', 'human')),
    last_actor_id TEXT,
    last_actor_name TEXT,
    UNIQUE(account_id, source_database, match_key_hash)
);
CREATE INDEX IF NOT EXISTS idx_price_current_scope
ON price_current(account_id, source_database, conversation_id, state);
CREATE INDEX IF NOT EXISTS idx_price_current_company
ON price_current(account_id, json_extract(payload_json, '$.quote_company_id'));

CREATE TABLE IF NOT EXISTS price_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT,
    candidate_id TEXT,
    account_id TEXT NOT NULL,
    source_database TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('system', 'human')),
    actor_id TEXT,
    actor_name TEXT,
    responsibility_role TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL,
    input_version INTEGER,
    evidence_json TEXT NOT NULL,
    rule_version TEXT,
    model_version TEXT,
    decision_method TEXT NOT NULL,
    reason TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    applied INTEGER NOT NULL CHECK (applied IN (0, 1)),
    request_id TEXT,
    UNIQUE(request_id, action)
);
CREATE INDEX IF NOT EXISTS idx_price_audit_candidate ON price_audit(candidate_id, occurred_at_utc);
CREATE INDEX IF NOT EXISTS idx_price_audit_record ON price_audit(record_id, occurred_at_utc);

CREATE TABLE IF NOT EXISTS price_import_previews (
    preview_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    source_scope_json TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    template_version TEXT NOT NULL,
    rows_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'confirmed', 'expired', 'rejected')),
    request_id TEXT NOT NULL,
    UNIQUE(account_id, request_id)
);

CREATE TABLE IF NOT EXISTS company_corrections (
    correction_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    source_database TEXT NOT NULL,
    conversation_id TEXT,
    message_ids_json TEXT NOT NULL,
    sender_ids_json TEXT NOT NULL,
    original_corp_id TEXT,
    original_corp_name TEXT,
    normalized_company_id TEXT NOT NULL,
    normalized_company_name TEXT NOT NULL,
    basis TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('applied', 'needs_review', 'rejected')),
    conflict_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_company_corrections_scope
ON company_corrections(account_id, source_database, conversation_id, status);

CREATE TABLE IF NOT EXISTS company_aliases (
    alias_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    source_database TEXT NOT NULL,
    alias_name TEXT NOT NULL,
    normalized_company_id TEXT NOT NULL,
    normalized_company_name TEXT NOT NULL,
    basis TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, source_database, alias_name)
);
"""


class PriceOperationError(Exception):
    """An expected, caller-readable price operation failure."""

    def __init__(self, error_code: str, message: str, *, http_status: int = 422,
                 field_errors: list[dict[str, Any]] | None = None,
                 details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.http_status = http_status
        self.field_errors = field_errors or []
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        result = {"error_code": self.error_code, "message": self.message}
        if self.field_errors:
            result["field_errors"] = self.field_errors
        result.update(self.details)
        return result


@dataclass(frozen=True, slots=True)
class PriceReviewSettings:
    enabled: bool = False
    model: str | None = None
    model_version: str | None = None
    adapter: "PriceReviewAdapter | None" = None


class PriceReviewAdapter(Protocol):
    def review(self, payload: dict[str, Any], evidence: dict[str, Any]) -> Mapping[str, Any]:
        """Return a suggestion only; never write the database."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(UTC).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if result.tzinfo is None:
        return None
    return result.astimezone(UTC)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", _text(value))).strip()


def _norm_key_text(value: Any) -> str:
    return _norm_text(value).casefold()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return default


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _canonical_unit(value: Any, kind: str) -> str | None:
    unit = _norm_key_text(value)
    if not unit:
        return None
    aliases = {
        "weight": {"kg": "kg", "kgs": "kg", "公斤": "kg", "千克": "kg", "g": "g", "克": "g", "吨": "t", "t": "t", "tons": "t"},
        "volume": {"cbm": "cbm", "m3": "cbm", "m³": "cbm", "立方": "cbm", "方": "cbm", "cm3": "cm3", "l": "l", "升": "l"},
        "package": {"箱": "箱", "ctn": "箱", "ctns": "箱", "carton": "箱", "托": "托", "plt": "托", "plts": "托", "pallet": "托", "件": "件", "pcs": "件", "piece": "件", "包": "包", "桶": "桶"},
    }
    mapping = aliases.get(kind, {})
    # Weight and volume units are a closed set for deterministic matching.
    # An unfamiliar unit must remain a reviewable error instead of becoming a
    # new, silently comparable key.  Packaging labels are intentionally open
    # so an explicit business type (for example "桶") is preserved.
    return mapping.get(unit) if kind in {"weight", "volume"} else mapping.get(unit, unit)


def _measure(value: Any, unit: Any, kind: str) -> tuple[str | None, str | None, str | None, str | None]:
    """Return canonical value, unit, raw value and an optional error code."""
    raw = _text(value)
    parsed_value = value
    parsed_unit = unit
    if raw and _decimal(value) is None:
        pattern = r"^\s*([+-]?(?:\d+(?:[.,]\d+)?|\.\d+))\s*([^\d\s]+)\s*$"
        match = re.match(pattern, raw, re.I)
        if match:
            parsed_value, parsed_unit = match.group(1), parsed_unit or match.group(2)
    number = _decimal(parsed_value)
    if raw.casefold() in {"未知", "unknown", "不详", "待确认"}:
        return None, None, raw, "unknown"
    if raw.casefold() in {"不适用", "n/a", "na"}:
        return "not_applicable", "not_applicable", raw, None
    if number is None:
        return None, _canonical_unit(parsed_unit, kind), raw or None, "invalid_number"
    canonical_unit = _canonical_unit(parsed_unit, kind)
    if not canonical_unit:
        return _decimal_text(number), None, raw or _decimal_text(number), "unknown_unit" if _text(parsed_unit) else "missing_unit"
    # Only deterministic conversions are applied to matching.  Original text
    # remains in the payload for display and evidence review.
    if kind == "weight" and canonical_unit == "g":
        number = number / Decimal(1000)
        canonical_unit = "kg"
    elif kind == "weight" and canonical_unit == "t":
        number = number * Decimal(1000)
        canonical_unit = "kg"
    elif kind == "volume" and canonical_unit == "l":
        number = number / Decimal(1000)
        canonical_unit = "cbm"
    elif kind == "volume" and canonical_unit == "cm3":
        number = number / Decimal(1_000_000)
        canonical_unit = "cbm"
    return _decimal_text(number), canonical_unit, raw or _decimal_text(number), None


def _company_id(company_id: Any, company_name: Any) -> tuple[str | None, str | None, str | None]:
    cid = _norm_text(company_id)
    name = _norm_text(company_name)
    forbidden = {"unknown", "zhongji", "other", "中技方", "其他企业", "未知主体", "公司未知", "公司待确认"}
    if cid.casefold() in forbidden or name.casefold() in forbidden:
        return None, None, "ambiguous_company"
    if not cid and name:
        cid = "name:" + re.sub(r"\s+", "", name).casefold()
    if cid and not name:
        # A stable identifier is useful provenance, but it is not a real
        # company name.  Do not manufacture a display name from an opaque ID
        # or let that placeholder pass the automatic-maintenance gate.
        return cid, None, "missing_company_name"
    return cid or None, name or None, None


def _field_value(data: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and data[name] not in (None, ""):
            return data[name]
    return None


def _scope_contains(parent: Mapping[str, Any], child: Mapping[str, Any]) -> bool:
    """Return whether a nested source scope remains inside its import scope."""
    if _text(parent.get("account_id")) != _text(child.get("account_id")):
        return False
    for key in ("source_database", "conversation_id"):
        parent_value = _text(parent.get(key))
        child_value = _text(child.get(key))
        if not parent_value or not child_value:
            return False
        if child_value != parent_value and not child_value.startswith(parent_value + ":forwarded:"):
            return False
    parent_name = _text(parent.get("conversation_name"))
    child_name = _text(child.get("conversation_name"))
    return not parent_name or not child_name or parent_name == child_name


def normalize_price_payload(raw: Mapping[str, Any], source_scope: Mapping[str, Any], *, source_kind: str) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Normalize values without filling an unknown with zero or a wildcard."""
    data = dict(raw)
    scope = dict(source_scope)
    result: dict[str, Any] = {
        "source_scope": scope,
        "origin_raw": _text(_field_value(data, "origin_raw", "起点原文", "origin", "起点")) or None,
        "origin_normalized": _text(_field_value(data, "origin_normalized", "起点规范", "origin", "起点")) or None,
        "origin_kind": _text(_field_value(data, "origin_kind", "起点类别")) or None,
        "destination_raw": _text(_field_value(data, "destination_raw", "终点原文", "destination", "终点")) or None,
        "destination_normalized": _text(_field_value(data, "destination_normalized", "终点规范", "destination", "终点")) or None,
        "destination_kind": _text(_field_value(data, "destination_kind", "终点类别")) or None,
        "service_time_raw": _text(_field_value(data, "service_time_raw", "service_time", "delivery_time", "时效")) or None,
        "service_time_id": _text(_field_value(data, "service_time_id", "service_plan_id")) or None,
        "price_raw": _text(_field_value(data, "price_raw", "price_amount", "price", "价格")) or None,
        "currency": _norm_text(_field_value(data, "currency", "币种")) or None,
        "pricing_method": _norm_text(_field_value(data, "pricing_method", "计价方式", "price_unit", "unit")) or None,
        "quote_company_id": _norm_text(_field_value(data, "quote_company_id", "报价公司ID")) or None,
        "quote_company_name": _norm_text(_field_value(data, "quote_company_name", "报价公司", "quote_company")) or None,
        "quoted_by_id": _norm_text(_field_value(data, "quoted_by_id", "报价方ID")) or None,
        "quoted_by_name": _norm_text(_field_value(data, "quoted_by_name", "报价方姓名")) or None,
        "inquiry_company_id": _norm_text(_field_value(data, "inquiry_company_id", "询价公司ID")) or None,
        "inquiry_company_name": _norm_text(_field_value(data, "inquiry_company_name", "询价公司")) or None,
        "inquired_by_id": _norm_text(_field_value(data, "inquired_by_id", "询价人姓名", "询价方ID")) or None,
        "inquired_by_name": _norm_text(_field_value(data, "inquired_by_name", "询价人姓名")) or None,
        "source_message_id": _text(_field_value(data, "source_message_id", "来源消息ID", "message_id")) or None,
        "source_reference": _text(_field_value(data, "source_reference", "来源引用")) or None,
        "business_time": _iso(_parse_time(_field_value(data, "business_time", "quote_time", "quoted_at", "报价时间"))),
        "received_at": _iso(_parse_time(_field_value(data, "received_at", "接收时间"))),
        "effective_until": _iso(_parse_time(_field_value(data, "effective_until", "valid_until", "有效期"))),
        "conditions": data.get("conditions") or data.get("条件") or {},
        "route_ambiguous": bool(data.get("route_ambiguous")),
        "company_status": _text(data.get("company_status")) or "known",
        "validity_note": _text(data.get("validity_note", data.get("有效期说明"))) or "有效期未注明",
        "original_payload": {key: value for key, value in data.items() if key not in {"auto_approve", "source_verified", "operator_id", "审核状态", "审核人"}},
    }
    result["origin"] = result["origin_normalized"]
    result["destination"] = result["destination_normalized"]

    result["weight_value"], result["weight_unit"], result["weight_raw"], weight_error = _measure(
        _field_value(data, "weight_value", "重量值", "weight", "重量"),
        _field_value(data, "weight_unit", "重量单位"), "weight")
    result["volume_value"], result["volume_unit"], result["volume_raw"], volume_error = _measure(
        _field_value(data, "volume_value", "体积值", "volume", "体积"),
        _field_value(data, "volume_unit", "体积单位"), "volume")
    result["package_count"], result["package_type"], result["package_raw"], package_error = _measure(
        _field_value(data, "package_count", "包装数量", "packages"),
        _field_value(data, "package_type", "包装类型"), "package")
    result["package_count"] = result["package_count"]

    quote_id, quote_name, company_error = _company_id(result["quote_company_id"], result["quote_company_name"])
    result["quote_company_id"], result["quote_company_name"] = quote_id, quote_name
    if result["company_status"] != "known":
        company_error = company_error or "ambiguous_company"
    inquiry_id, inquiry_name, _ = _company_id(result["inquiry_company_id"], result["inquiry_company_name"])
    result["inquiry_company_id"], result["inquiry_company_name"] = inquiry_id, inquiry_name

    field_sources = data.get("field_sources") or data.get("字段来源") or {}
    if not isinstance(field_sources, Mapping):
        field_sources = {}
    result["field_sources"] = {str(key): value for key, value in field_sources.items()}
    errors: list[dict[str, Any]] = []
    for field, error in (("weight", weight_error), ("volume", volume_error), ("package_count", package_error), ("quote_company", company_error)):
        if error and error not in {"unknown", "missing_unit"}:
            errors.append({"field": field, "error_code": error, "message": {
                "invalid_number": "数值格式无效", "unknown_unit": "单位不受支持",
                "ambiguous_company": "报价公司不能使用分类桶或未知主体",
                "missing_company_name": "报价公司缺少可展示的真实公司名称",
            }.get(error, "字段格式无效")})
    return result, result["field_sources"], errors


def build_match_key(payload: Mapping[str, Any], scope: Mapping[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    required = {
        "account_id": scope.get("account_id"), "source_database": scope.get("source_database"),
        "conversation_id": scope.get("conversation_id"), "quote_company_id": payload.get("quote_company_id"),
        "origin": payload.get("origin_normalized"), "destination": payload.get("destination_normalized"),
        "weight_value": payload.get("weight_value"), "weight_unit": payload.get("weight_unit"),
        "volume_value": payload.get("volume_value"), "volume_unit": payload.get("volume_unit"),
        "package_count": payload.get("package_count"), "package_type": payload.get("package_type"),
        "service_time": payload.get("service_time_id") or payload.get("service_time_raw"),
        "currency": payload.get("currency"), "pricing_method": payload.get("pricing_method"),
    }
    for field, value in required.items():
        if value in (None, "") or value == "unknown":
            errors.append({"field": field, "error_code": "missing_key_field", "message": "匹配条件缺失，不能自动覆盖现行价格"})
    if payload.get("route_ambiguous"):
        errors.append({"field": "route", "error_code": "ambiguous_route", "message": "路线无法唯一归属，需人工拆分"})
    if payload.get("company_status") != "known":
        errors.append({"field": "quote_company", "error_code": "company_conflict" if payload.get("company_status") == "conflict" else "ambiguous_company", "message": "报价公司身份冲突，需人工确认"})
    if errors:
        return None, errors
    currency = _norm_key_text(payload["currency"])
    if currency in {"元", "块", "人民币元"}:
        errors.append({"field": "currency", "error_code": "ambiguous_currency", "message": "币种不明确，不能默认人民币"})
    amount = _decimal(payload.get("price_raw"))
    if amount is None or amount < 0:
        errors.append({"field": "price", "error_code": "invalid_price", "message": "价格必须是有效的非负十进制数"})
    if not _parse_time(payload.get("business_time")):
        errors.append({"field": "business_time", "error_code": "missing_business_time", "message": "缺少带时区的可信报价业务时间"})
    if payload.get("weight_unit") in (None, "") or payload.get("volume_unit") in (None, ""):
        # Keep the explicit missing-unit reason separate from an unknown value.
        pass
    if errors:
        return None, errors
    key = {
        "account_id": _norm_key_text(scope["account_id"]),
        "source_database": _norm_key_text(scope["source_database"]),
        "conversation_id": _norm_key_text(scope["conversation_id"]),
        "quote_company_id": _norm_key_text(payload["quote_company_id"]),
        "origin": {"value": _norm_key_text(payload["origin_normalized"]), "kind": _norm_key_text(payload.get("origin_kind"))},
        "destination": {"value": _norm_key_text(payload["destination_normalized"]), "kind": _norm_key_text(payload.get("destination_kind"))},
        "weight": {"value": payload["weight_value"], "unit": _norm_key_text(payload["weight_unit"])},
        "volume": {"value": payload["volume_value"], "unit": _norm_key_text(payload["volume_unit"])},
        "package": {"count": payload["package_count"], "type": _norm_key_text(payload["package_type"])},
        "service_time": _norm_key_text(payload.get("service_time_id") or payload.get("service_time_raw")),
        "currency": currency,
        "pricing_method": _norm_key_text(payload["pricing_method"]),
        "conditions": payload.get("conditions") or {},
    }
    return key, []


def _safe_excel_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def _xml_cell(ref: str, value: Any, style: int = 0) -> str:
    attrs = f' r="{ref}"'
    if style:
        attrs += f' s="{style}"'
    if value is None:
        return f"<c{attrs}/>"
    text = _safe_excel_text(value)
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) and not text.startswith("'"):
        return f'<c{attrs} t="n"><v>{text}</v></c>'
    return f'<c{attrs} t="inlineStr"><is><t xml:space="preserve">{_escape_xml(text)}</t></is></c>'


def _escape_xml(value: Any) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def build_price_workbook(items: Iterable[Mapping[str, Any]] = (), *, template: bool = False,
                        scope: Mapping[str, Any] | None = None, view: str = "current") -> bytes:
    """Create a macro/formula/link-free OOXML workbook using only stdlib."""
    # Price exports are a user-facing evidence surface.  Keep the same local
    # credential/phone redaction boundary as report and JSON responses while
    # leaving the raw source values in the analysis database.
    from .wecom_report import display_safe_value

    rows = [] if template else list(items)
    header = list(PRICE_COLUMNS)
    sheet_rows: list[list[Any]] = [header]
    for item in rows:
        payload = dict(item.get("payload") or item)
        source_scope = payload.get("source_scope") or scope or {}
        values = [
            payload.get("origin_raw") or payload.get("origin_normalized") or payload.get("origin"),
            payload.get("destination_raw") or payload.get("destination_normalized") or payload.get("destination"),
            payload.get("weight_raw") or payload.get("weight_value"),
            payload.get("volume_raw") or payload.get("volume_value"),
            payload.get("package_raw") or payload.get("package_count"),
            payload.get("service_time_raw") or payload.get("service_time_id"),
            payload.get("price_raw") or payload.get("price_amount") or payload.get("price"),
            payload.get("quote_company_name") or payload.get("quote_company"), payload.get("quote_company_id"),
            payload.get("quoted_by_name"), payload.get("inquiry_company_name"), payload.get("inquiry_company_id"),
            payload.get("inquired_by_name"), payload.get("weight_unit"), payload.get("volume_unit"), payload.get("package_type"),
            payload.get("currency"), payload.get("pricing_method"), payload.get("business_time"), payload.get("received_at"),
            payload.get("effective_until") or payload.get("validity_note") or "有效期未注明",
            item.get("review_status", item.get("status", "current")), item.get("adoption_status", item.get("state", "current")),
            item.get("decision_method", item.get("judgement_method", "")), item.get("responsibility_role", DEFAULT_RESPONSIBILITY_ROLE),
            item.get("reviewer_name") or item.get("last_actor_name"), payload.get("source_kind", item.get("source_kind")),
            _json(source_scope), payload.get("source_message_id"), _json(payload.get("field_sources") or {}),
            item.get("record_id"), item.get("version", item.get("base_version")), PRICE_TEMPLATE_VERSION,
            item.get("display_status") or ("有效价格" if item.get("record_id") else "待审核"),
        ]
        sheet_rows.append([display_safe_value(value) for value in values])
    if template:
        sheet_rows.append(["" for _ in header])

    def sheet_xml(rows_for_sheet: list[list[Any]], *, filter_sheet: bool = False) -> str:
        max_col = max((len(row) for row in rows_for_sheet), default=1)
        max_row = max(1, len(rows_for_sheet))
        body = []
        for row_index, row in enumerate(rows_for_sheet, 1):
            cells = []
            for col_index, value in enumerate(row, 1):
                style = 1 if row_index == 1 else 2 if col_index in {3, 4, 5, 7, 36} else 0
                cells.append(_xml_cell(f"{_column_name(col_index)}{row_index}", value, style))
            body.append(f'<row r="{row_index}">' + "".join(cells) + "</row>")
        widths = "".join(f'<col min="{i}" max="{i}" width="{max(12, min(32, len(str(header[i-1])) + 8))}" customWidth="1"/>' for i in range(1, max_col + 1))
        auto = f'<autoFilter ref="A1:{_column_name(max_col)}{max_row}"/>' if filter_sheet else ""
        return (f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f'<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/><selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView></sheetViews>'
                f'<cols>{widths}</cols><sheetData>{"".join(body)}</sheetData>{auto}</worksheet>')

    notes = [
        ["填写说明"],
        ["模板版本", PRICE_TEMPLATE_VERSION],
        ["主表语义", "价格维护是网页主数据的导入/导出视图；导入后必须经过服务端候选、校验和审核。"],
        ["七个核心列", "起点、终点、重量、体积、包装数量、时效、价格。缺失值保持未知，不填零，不把未知当通配符。"],
        ["匹配规则", "报价公司、来源范围、路线、明确规格、时效、币种和计价方式共同隔离；价格金额不属于匹配键。"],
        ["系统列", "状态、采用状态、判断方式、责任岗位、审核人、record_id、base_version、模板版本由服务端决定，修改这些列不会直接生效。"],
        ["安全", "不执行公式、宏、外链或工作簿中的文字指令；导出文本会按文字类型防公式注入。"],
    ]

    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="价格维护" sheetId="1" r:id="rId1"/><sheet name="填写说明" sheetId="2" r:id="rId2"/></sheets></workbook>')
    workbook_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>')
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Aptos"/></font><font><b/><sz val="11"/><name val="Aptos"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" applyFont="1"/><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellXfs></styleSheet>')
    package = BytesIO()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in {
            "[Content_Types].xml": content_types, "_rels/.rels": root_rels,
            "xl/workbook.xml": workbook, "xl/_rels/workbook.xml.rels": workbook_rels,
            "xl/styles.xml": styles, "xl/worksheets/sheet1.xml": sheet_xml(sheet_rows, filter_sheet=True),
            "xl/worksheets/sheet2.xml": sheet_xml(notes),
        }.items():
            archive.writestr(name, value.encode("utf-8"))
    return package.getvalue()


def build_single_price_workbook(item: Mapping[str, Any]) -> bytes:
    """Build a safe, read-only presentation workbook for one price row.

    This intentionally is not an import template: the first sheet contains
    exactly the seven user-facing columns and one selected row.  Provenance,
    status and units live on the small notes sheet so a display download
    cannot accidentally be sent through the maintenance import flow.
    """
    payload = dict(item.get("payload") or item)
    from .wecom_report import display_safe_value

    def number(value: Any) -> Decimal | None:
        parsed = _decimal(value)
        return parsed if parsed is not None else None

    def first_number(*values: Any) -> Decimal | None:
        for value in values:
            parsed = number(value)
            if parsed is not None:
                return parsed
        return None

    values: list[Any] = [
        payload.get("origin_raw") or payload.get("origin_normalized") or payload.get("origin"),
        payload.get("destination_raw") or payload.get("destination_normalized") or payload.get("destination"),
        first_number(payload.get("weight_value"), payload.get("weight_raw")),
        first_number(payload.get("volume_value"), payload.get("volume_raw")),
        first_number(payload.get("package_count"), payload.get("package_raw")),
        payload.get("service_time_raw") or payload.get("service_time_id") or payload.get("service_time"),
        first_number(payload.get("price_raw"), payload.get("price_amount"), payload.get("price")),
    ]

    company = payload.get("quote_company_name") or payload.get("quote_company") or "待补充"
    review_status = item.get("review_status", item.get("status", "current"))
    display_status = item.get("display_status") or ("有效价格" if item.get("record_id") else "待审核")
    incomplete = display_status == "待完善"
    weight_unit = _text(payload.get("weight_unit"))
    volume_unit = _text(payload.get("volume_unit"))
    package_type = _text(payload.get("package_type"))
    currency = _text(payload.get("currency"))
    pricing_method = _text(payload.get("pricing_method"))
    notes = [
        ["揽收车队群聊分析·报价明细"],
        ["用途", "展示文件，仅供查看，不可直接回传价格维护导入"],
        ["报价公司", company],
        ["状态", display_status],
        ["原始审核状态", review_status],
        ["重量单位", payload.get("weight_unit") or "待补充"],
        ["体积单位", payload.get("volume_unit") or "待补充"],
        ["包装类型", payload.get("package_type") or "待补充"],
        ["币种", payload.get("currency") or "待补充"],
        ["计价方式", payload.get("pricing_method") or "待补充"],
        ["业务方向", "揽收车队"],
    ]

    def sheet_xml(rows_for_sheet: list[list[Any]], *, header: bool = False) -> str:
        max_col = max((len(row) for row in rows_for_sheet), default=1)
        body: list[str] = []
        for row_index, row in enumerate(rows_for_sheet, 1):
            cells: list[str] = []
            for col_index, value in enumerate(row, 1):
                if header and row_index == 1:
                    style = 1
                elif header and col_index in {3, 4, 5, 7}:
                    style = {3: 2, 4: 3, 5: 4, 7: 5}[col_index]
                elif not header and row_index == 4:
                    style = 6
                else:
                    style = 0
                cells.append(_xml_cell(f"{_column_name(col_index)}{row_index}",
                                       display_safe_value(value) if isinstance(value, (str, dict, list)) else value,
                                       style))
            height = 36 if header and row_index == 1 else 30 if header else 32
            body.append(f'<row r="{row_index}" ht="{height}" customHeight="1">' + "".join(cells) + "</row>")
        widths = "".join(
            f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>'
            for i, width in enumerate(([16, 16, 12, 12, 12, 16, 14] if header else [22, 44]), 1)
        )
        return (f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f'<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
                f'<cols>{widths}</cols><sheetData>{"".join(body)}</sheetData></worksheet>')

    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="揽收车队价格明细" sheetId="1" r:id="rId1"/><sheet name="说明" sheetId="2" r:id="rId2"/></sheets></workbook>')
    workbook_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<numFmts count="4"><numFmt numFmtId="165" formatCode="{_escape_xml("0.##" + (chr(34) + " " + weight_unit + chr(34) if weight_unit else ""))}"/><numFmt numFmtId="166" formatCode="{_escape_xml("0.##" + (chr(34) + " " + volume_unit + chr(34) if volume_unit else ""))}"/><numFmt numFmtId="167" formatCode="{_escape_xml("0.##" + (chr(34) + " " + package_type + chr(34) if package_type else ""))}"/><numFmt numFmtId="168" formatCode="{_escape_xml("0.##" + (chr(34) + " " + (currency + " " if currency else "") + (pricing_method or "价格") + chr(34)))}"/></numFmts>'
        '<fonts count="3"><font><sz val="11"/><name val="Aptos"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Aptos"/></font><font><b/><sz val="11"/><name val="Aptos"/></font></fonts>'
        '<fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF2F75B5"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFFFF2CC"/><bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border><left style="thin"/><right style="thin"/><top style="thin"/><bottom style="thin"/><diagonal/></border></borders>'
        '<cellXfs count="7"><xf numFmtId="0" fontId="0" fillId="0" borderId="1"><alignment wrapText="1" vertical="center"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="1" applyFont="1" applyFill="1"><alignment wrapText="1" vertical="center"/></xf><xf numFmtId="165" fontId="0" fillId="0" borderId="1" applyNumberFormat="1"><alignment wrapText="1" vertical="center"/></xf><xf numFmtId="166" fontId="0" fillId="0" borderId="1" applyNumberFormat="1"><alignment wrapText="1" vertical="center"/></xf><xf numFmtId="167" fontId="0" fillId="0" borderId="1" applyNumberFormat="1"><alignment wrapText="1" vertical="center"/></xf><xf numFmtId="168" fontId="0" fillId="0" borderId="1" applyNumberFormat="1"><alignment wrapText="1" vertical="center"/></xf><xf numFmtId="0" fontId="2" fillId="3" borderId="1" applyFont="1" applyFill="1"><alignment wrapText="1" vertical="center"/></xf></cellXfs></styleSheet>')
    package = BytesIO()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in {
            "[Content_Types].xml": content_types, "_rels/.rels": root_rels,
            "xl/workbook.xml": workbook, "xl/_rels/workbook.xml.rels": workbook_rels,
            "xl/styles.xml": styles, "xl/worksheets/sheet1.xml": sheet_xml([list(PRICE_CORE_COLUMNS), values], header=True),
            "xl/worksheets/sheet2.xml": sheet_xml(notes),
        }.items():
            archive.writestr(name, value.encode("utf-8"))
    return package.getvalue()


def _ns(tag: str) -> str:
    return "{" + "http://schemas.openxmlformats.org/spreadsheetml/2006/main" + "}" + tag


def _cell_value(cell: ET.Element, shared: list[str]) -> Any:
    if cell.find(_ns("f")) is not None:
        raise PriceOperationError("workbook_formula_rejected", "工作簿包含公式，已拒绝执行", http_status=422)
    kind = cell.attrib.get("t")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(f".//{_ns('t')}"))
    value = cell.find(_ns("v"))
    raw = "" if value is None else value.text or ""
    if kind == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return raw == "1"
    return raw


def parse_price_workbook(data: bytes, *, max_bytes: int = MAX_XLSX_BYTES) -> dict[str, Any]:
    if not isinstance(data, (bytes, bytearray)) or len(data) > max_bytes:
        raise PriceOperationError("file_too_large", "Excel 文件超过大小限制", http_status=413)
    if not zipfile.is_zipfile(BytesIO(data)):
        raise PriceOperationError("invalid_xlsx", "只接受真实 .xlsx 文件，不接受重命名的 CSV 或其他格式")
    archive = None
    try:
        archive = zipfile.ZipFile(BytesIO(data))
        infos = archive.infolist()
        if len(infos) > MAX_XLSX_ENTRIES or sum(info.file_size for info in infos) > MAX_XLSX_UNCOMPRESSED_BYTES:
            raise PriceOperationError("file_too_large", "Excel 解压后大小或文件项数量超过限制", http_status=413)
        names = {info.filename for info in infos}
        if any(name.lower().endswith(("vbaProject.bin", ".xlsm")) for name in names) or any("vba" in name.lower() for name in names):
            raise PriceOperationError("workbook_macro_rejected", "不接受包含宏的工作簿")
        if any(name.startswith("xl/externalLinks/") for name in names):
            raise PriceOperationError("workbook_external_link_rejected", "不接受包含外部链接的工作簿")
        for name in names:
            if name.endswith(".rels") and b"targetmode=\"external\"" in archive.read(name).lower():
                raise PriceOperationError("workbook_external_link_rejected", "不跟随工作簿外部链接")
        # Do not use a byte substring such as ``b"<f"`` here: a perfectly
        # valid worksheet can contain ``<filterColumn>`` for an Excel filter.
        # Parse worksheet XML and reject only actual formula elements.
        for name in names:
            if not name.startswith("xl/worksheets/"):
                continue
            try:
                worksheet_root = ET.fromstring(archive.read(name))
            except ET.ParseError as exc:
                raise PriceOperationError("invalid_xlsx", "工作表 XML 结构无效") from exc
            if worksheet_root.find(f".//{_ns('f')}") is not None:
                raise PriceOperationError("workbook_formula_rejected", "工作簿包含公式，已拒绝执行")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.text or "" for node in item.findall(f".//{_ns('t')}")) for item in root.findall(_ns("si"))]
        workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
        rel_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rels = {rel.attrib.get("Id"): rel.attrib.get("Target", "") for rel in rel_root}
        sheets: dict[str, str] = {}
        for sheet in workbook_root.findall(f".//{_ns('sheet')}"):
            target = rels.get(sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"), "")
            target = target.lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            sheets[sheet.attrib.get("name", "")] = target
        if "价格维护" not in sheets or "填写说明" not in sheets:
            raise PriceOperationError("template_missing_sheet", "工作簿必须包含“价格维护”和“填写说明”工作表")
        rows: list[dict[str, Any]] = []
        sheet_root = ET.fromstring(archive.read(sheets["价格维护"]))
        if sheet_root.find(_ns("mergeCells")) is not None:
            raise PriceOperationError("merged_cells_rejected", "价格维护表不允许用合并单元格继承条件")
        sheet_data = sheet_root.find(_ns("sheetData"))
        if sheet_data is None:
            raise PriceOperationError("template_missing_header", "价格维护表缺少表头")
        cols = sheet_root.find(_ns("cols"))
        if cols is not None and any(column.attrib.get("hidden") == "1" for column in cols.findall(_ns("col"))):
            raise PriceOperationError("hidden_columns_rejected", "不接受包含隐藏列的工作簿")
        raw_rows: list[list[Any]] = []
        for row in sheet_data.findall(_ns("row")):
            if row.attrib.get("hidden") == "1":
                raise PriceOperationError("hidden_rows_rejected", "不接受包含隐藏数据行的工作簿")
            values: dict[int, Any] = {}
            for cell in row.findall(_ns("c")):
                match = re.match(r"([A-Z]+)", cell.attrib.get("r", ""))
                if not match:
                    continue
                col = 0
                for char in match.group(1):
                    col = col * 26 + ord(char) - 64
                values[col] = _cell_value(cell, shared)
            raw_rows.append([values.get(index, "") for index in range(1, max(values.keys(), default=0) + 1)])
            if len(raw_rows) > MAX_XLSX_ROWS + 1:
                raise PriceOperationError("too_many_rows", "Excel 行数超过限制", http_status=413)
        if not raw_rows:
            raise PriceOperationError("template_missing_header", "价格维护表缺少单行表头")
        headers = [str(value).strip() for value in raw_rows[0]]
        if headers[:7] != list(PRICE_CORE_COLUMNS) or len(set(headers)) != len(headers):
            raise PriceOperationError("template_invalid_header", "价格维护表前七列必须依次为起点、终点、重量、体积、包装数量、时效、价格，且表头不可重复")
        missing = [column for column in ("报价公司", "币种", "计价方式", "报价时间", "来源范围", "record_id", "base_version", "模板版本") if column not in headers]
        if missing:
            raise PriceOperationError("template_missing_columns", "工作簿缺少必要列", field_errors=[{"field": column, "message": "缺少必要列"} for column in missing])
        for index, values in enumerate(raw_rows[1:], 2):
            if not any(_text(value) for value in values):
                continue
            row = {headers[col]: values[col] if col < len(values) else "" for col in range(len(headers))}
            row["_row_number"] = index
            rows.append(row)
        notes_root = ET.fromstring(archive.read(sheets["填写说明"]))
        return {"headers": headers, "rows": rows, "notes_present": notes_root.find(_ns("sheetData")) is not None}
    except ET.ParseError as exc:
        raise PriceOperationError("invalid_xlsx", "Excel XML 结构无效") from exc
    finally:
        if archive is not None:
            try:
                archive.close()
            except Exception:
                pass


class PriceMaintenance:
    def __init__(self, storage: Any, *, settings: PriceReviewSettings | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.storage = storage
        self.clock = clock or _now
        self.settings = settings or PriceReviewSettings()

    def _now_iso(self) -> str:
        return _iso(self.clock()) or datetime.now(UTC).isoformat()

    def configure_responsibility(self, reviewer_id: str | None, reviewer_name: str | None,
                                 *, role: str = DEFAULT_RESPONSIBILITY_ROLE) -> dict[str, Any]:
        with self.storage.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO price_settings(setting_key,setting_value) VALUES (?,?)", ("responsibility_role", role or DEFAULT_RESPONSIBILITY_ROLE))
            if reviewer_id and reviewer_name:
                conn.execute("INSERT OR REPLACE INTO price_settings(setting_key,setting_value) VALUES (?,?)", ("reviewer_id", _text(reviewer_id)))
                conn.execute("INSERT OR REPLACE INTO price_settings(setting_key,setting_value) VALUES (?,?)", ("reviewer_name", _text(reviewer_name)))
            else:
                conn.execute("DELETE FROM price_settings WHERE setting_key IN ('reviewer_id','reviewer_name')")
        return self.get_settings()

    def get_settings(self) -> dict[str, Any]:
        with self.storage.connect() as conn:
            values = {row["setting_key"]: row["setting_value"] for row in conn.execute("SELECT setting_key,setting_value FROM price_settings")}
        enabled = values.get("price_review_llm_enabled", "0") == "1"
        return {"responsibility_role": values.get("responsibility_role", DEFAULT_RESPONSIBILITY_ROLE),
                "reviewer_id": values.get("reviewer_id"), "reviewer_name": values.get("reviewer_name"),
                "price_review_llm_enabled": enabled,
                "model": values.get("price_review_llm_model") or self.settings.model,
                "model_version": values.get("price_review_llm_model_version") or self.settings.model_version}

    def configure_llm(self, enabled: bool, *, model: str | None = None, model_version: str | None = None) -> dict[str, Any]:
        # Explicit configuration only.  A pre-existing semantic-analysis key
        # never enables this setting.
        model = model if model is not None else self.settings.model
        model_version = model_version if model_version is not None else self.settings.model_version
        self.settings = PriceReviewSettings(enabled=bool(enabled), model=model, model_version=model_version, adapter=self.settings.adapter)
        with self.storage.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO price_settings(setting_key,setting_value) VALUES (?,?)", ("price_review_llm_enabled", "1" if enabled else "0"))
            conn.execute("INSERT OR REPLACE INTO price_settings(setting_key,setting_value) VALUES (?,?)", ("price_review_llm_model", model or ""))
            conn.execute("INSERT OR REPLACE INTO price_settings(setting_key,setting_value) VALUES (?,?)", ("price_review_llm_model_version", model_version or ""))
        return self.get_settings()

    def _role_and_reviewer(self) -> tuple[str, str | None, str | None]:
        settings = self.get_settings()
        return settings["responsibility_role"], settings.get("reviewer_id"), settings.get("reviewer_name")

    def _require_human(self, actor_id: str | None, actor_name: str | None) -> tuple[str, str, str]:
        role, configured_id, configured_name = self._role_and_reviewer()
        if not configured_id or not configured_name:
            raise PriceOperationError("reviewer_not_configured", "尚未配置价格审核负责人，不能执行人工确认、修改生效或停用", http_status=403)
        if not actor_id or _text(actor_id) != configured_id:
            raise PriceOperationError("operator_not_configured", "操作者不是已配置的实际审核身份", http_status=403)
        if not actor_name or _text(actor_name) != configured_name:
            raise PriceOperationError("operator_not_configured", "操作者显示名与已配置身份不一致", http_status=403)
        return role, configured_id, configured_name

    def _scope(self, raw: Mapping[str, Any] | None, payload: Mapping[str, Any], source_kind: str) -> dict[str, Any]:
        scope = dict(raw or payload.get("source_scope") or {})
        for key in ("account_id", "source_database", "conversation_id", "conversation_name"):
            if key not in scope and key in payload:
                scope[key] = payload[key]
        scope["account_id"] = _text(scope.get("account_id"))
        scope["source_database"] = _text(scope.get("source_database"))
        scope["conversation_id"] = _text(scope.get("conversation_id"))
        scope["conversation_name"] = _text(scope.get("conversation_name"))
        return scope

    def _allowed_evidence(self, payload: Mapping[str, Any], field_sources: Mapping[str, Any]) -> set[str]:
        allowed = set()
        if payload.get("source_message_id"):
            allowed.add(str(payload["source_message_id"]))
        for item in field_sources.values():
            values = item if isinstance(item, list) else [item]
            for value in values:
                if not isinstance(value, Mapping):
                    continue
                for key in ("message_id", "source_message_id", "id"):
                    if value.get(key):
                        allowed.add(str(value[key]))
        return allowed

    @staticmethod
    def _evidence_refs(value: Any) -> list[str]:
        values = value if isinstance(value, list) else [value]
        refs: list[str] = []
        for item in values:
            if isinstance(item, Mapping):
                for key in ("message_id", "source_message_id", "id"):
                    if item.get(key):
                        refs.append(str(item[key]))
            elif item not in (None, ""):
                refs.append(str(item))
        return list(dict.fromkeys(refs))

    def _evidence_errors(self, payload: Mapping[str, Any], field_sources: Mapping[str, Any],
                         scope: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Verify chat citations against the bounded local message scope.

        A source ID supplied by a parser or caller is only a claim until it
        resolves to an imported message in the same account, conversation and
        source tree.  This check stays local and runs before an optional model
        call, so a fabricated citation cannot become an automatic price.
        """
        required_fields = ("origin", "destination", "weight", "volume", "package_count",
                           "service_time", "price", "quote_company")
        global_refs = self._evidence_refs(payload.get("source_message_id"))
        refs_by_field: dict[str, list[str]] = {}
        for field in required_fields:
            refs = self._evidence_refs(field_sources.get(field))
            refs_by_field[field] = refs or global_refs
        for field, value in field_sources.items():
            if field not in refs_by_field:
                refs_by_field[field] = self._evidence_refs(value)
        refs = sorted({ref for values in refs_by_field.values() for ref in values})
        if not refs:
            return []
        account_id = _text(scope.get("account_id"))
        source_database = _text(scope.get("source_database"))
        conversation_id = _text(scope.get("conversation_id"))
        if not account_id or not source_database or not conversation_id:
            return [{"field": "source_scope", "error_code": "scope_required",
                     "message": "聊天证据缺少可验证的账号／来源库／会话范围"}]
        found: set[str] = set()
        with self.storage.connect() as conn:
            for ref in refs:
                row = conn.execute(
                    """SELECT 1 FROM messages
                       WHERE account_id=?
                         AND (source_database=? OR instr(source_database, ? || ':forwarded:')=1)
                         AND (conversation_id=? OR instr(conversation_id, ? || ':forwarded:')=1)
                         AND (id=? OR message_id=?)
                       LIMIT 1""",
                    (account_id, source_database, source_database, conversation_id, conversation_id, ref, ref),
                ).fetchone()
                if row:
                    found.add(ref)
        errors: list[dict[str, Any]] = []
        for field, field_refs in refs_by_field.items():
            for ref in field_refs:
                if ref not in found:
                    errors.append({"field": field, "error_code": "evidence_not_found",
                                   "message": f"证据消息 {ref} 不在当前账号／会话范围或尚未导入"})
        return errors

    @staticmethod
    def _redact_model_value(value: Any) -> Any:
        """Apply the existing local export redaction before an adapter call."""
        from .wecom_exporter import mask_sensitive_text
        from .wecom_report import display_safe_text
        if isinstance(value, str):
            return display_safe_text(mask_sensitive_text(value))
        if isinstance(value, dict):
            return {key: PriceMaintenance._redact_model_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [PriceMaintenance._redact_model_value(item) for item in value]
        return value

    def _model_review(self, payload: dict[str, Any], field_sources: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        configured = self.get_settings()
        enabled = self.settings.enabled or bool(configured.get("price_review_llm_enabled"))
        if not enabled:
            return None, []
        if self.settings.adapter is None:
            return {"status": "failed", "reason": "未配置价格审核模型适配器"}, [{"field": "model", "error_code": "model_unavailable", "message": "价格审核模型不可用，已转人工"}]
        evidence = {"allowed_message_ids": sorted(self._allowed_evidence(payload, field_sources)), "fields": field_sources}
        try:
            result = dict(self.settings.adapter.review(
                self._redact_model_value(dict(payload)), self._redact_model_value(evidence)
            ))
        except Exception as exc:
            return {"status": "failed", "reason": type(exc).__name__}, [{"field": "model", "error_code": "model_failed", "message": "价格审核模型调用失败，已转人工"}]
        refs = result.get("evidence_ids") or result.get("citations") or []
        if not isinstance(refs, list) or any(str(ref) not in evidence["allowed_message_ids"] for ref in refs):
            return {"status": "invalid", "reason": "引用越界", "raw_status": result.get("status")}, [{"field": "model", "error_code": "model_evidence_out_of_range", "message": "模型引用不在允许证据范围，已转人工"}]
        status = _text(result.get("status") or result.get("decision")).casefold()
        if status not in {"approve", "approved", "通过", "needs_review", "review", "人工", "reject", "rejected"}:
            return {"status": "invalid", "reason": "返回结构无效"}, [{"field": "model", "error_code": "model_invalid_output", "message": "价格审核模型返回结构无效，已转人工"}]
        result["status"] = "approve" if status in {"approve", "approved", "通过"} else "needs_review" if status in {"needs_review", "review", "人工"} else "reject"
        result["model_version"] = configured.get("model_version") or self.settings.model_version
        return self._redact_model_value(result), []

    def _find_current(self, conn: Any, scope: Mapping[str, Any], match_hash: str | None) -> Any:
        if not match_hash:
            return None
        return conn.execute("SELECT * FROM price_current WHERE account_id=? AND source_database=? AND match_key_hash=?",
                            (scope.get("account_id"), scope.get("source_database"), match_hash)).fetchone()

    def _candidate_result(self, row: Any, *, idempotent: bool = False) -> dict[str, Any]:
        if row is None:
            return {}
        payload = _load_json(row["payload_json"], {})
        validation = _load_json(row["validation_json"], {})
        missing_fields = self._missing_fields(payload, validation)
        return {"candidate_id": row["candidate_id"], "account_id": row["account_id"], "source_scope": _load_json(row["source_scope_json"], {}),
                "payload": payload, "field_sources": _load_json(row["field_sources_json"], {}), "review_status": row["review_status"],
                "adoption_status": row["adoption_status"], "base_version": row["base_version"], "business_time": row["business_time"],
                "status_reason": row["status_reason"], "validation": validation, "missing_fields": missing_fields,
                "display_status": self._display_status(row["review_status"], row["adoption_status"], missing_fields),
                "rule_version": row["rule_version"], "model_version": row["model_version"], "model_result": _load_json(row["model_result_json"], None),
                "idempotent": idempotent, "created_by_type": row["created_by_type"], "created_by_id": row["created_by_id"],
                "created_by_name": row["created_by_name"]}

    @staticmethod
    def _missing_fields(payload: Mapping[str, Any], validation: Mapping[str, Any] | None = None) -> list[str]:
        """Return absent maintenance fields without manufacturing values."""
        aliases = {
            "origin": ("origin_normalized", "origin"), "destination": ("destination_normalized", "destination"),
            "weight": ("weight_value",), "volume": ("volume_value",), "package_count": ("package_count",),
            "service_time": ("service_time_id", "service_time_raw"), "price": ("price_raw", "price_amount", "price"),
            "quote_company": ("quote_company_id",), "currency": ("currency",), "pricing_method": ("pricing_method",),
        }
        missing: list[str] = []
        for field, names in aliases.items():
            if all(payload.get(name) in (None, "", "unknown") for name in names):
                missing.append(field)
        for error in (validation or {}).get("errors", []):
            if error.get("error_code") == "missing_key_field" and error.get("field") and error["field"] not in missing:
                missing.append(error["field"])
        if payload.get("route_ambiguous") and "route" not in missing:
            missing.append("route")
        return missing

    @staticmethod
    def _display_status(review_status: str, adoption_status: str, missing_fields: Iterable[str]) -> str:
        if adoption_status == "applied" or review_status == "approved" and adoption_status == "applied":
            return "有效价格"
        if review_status == "rejected":
            return "已拒绝"
        if list(missing_fields):
            return "待完善"
        return "待审核"

    def _current_result(self, row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        result = {"record_id": row["record_id"], "account_id": row["account_id"], "source_scope": {
                    "account_id": row["account_id"], "source_database": row["source_database"], "conversation_id": row["conversation_id"], "conversation_name": row["conversation_name"]},
                "payload": _load_json(row["payload_json"], {}), "match_key": _load_json(row["match_key_json"], {}),
                "match_key_hash": row["match_key_hash"],
                "version": row["version"], "state": row["state"], "candidate_id": row["candidate_id"],
                "maintained_at": row["maintained_at"], "business_time": row["business_time"],
                "responsibility_role": row["responsibility_role"], "last_actor_type": row["last_actor_type"],
                "last_actor_id": row["last_actor_id"], "last_actor_name": row["last_actor_name"]}
        result["display_status"] = "有效价格" if row["state"] == "current" else "已停用"
        result["missing_fields"] = []
        keys = set(row.keys()) if hasattr(row, "keys") else set()
        for source_key, result_key in (("candidate_review_status", "review_status"),
                                       ("candidate_adoption_status", "adoption_status"),
                                       ("candidate_created_by_type", "created_by_type"),
                                       ("candidate_created_by_name", "created_by_name"),
                                       ("candidate_validation_json", "validation"),
                                       ("candidate_field_sources_json", "field_sources")):
            if source_key in keys:
                value = row[source_key]
                result[result_key] = _load_json(value, {}) if source_key.endswith("_json") else value
        return result

    def _insert_audit(self, conn: Any, *, candidate: Any, current: Any, action: str,
                      actor_type: str, actor_id: str | None, actor_name: str | None,
                      role: str, reason: str, before: Any, after: Any, applied: bool,
                      request_id: str | None, decision_method: str) -> None:
        try:
            conn.execute("""INSERT INTO price_audit(
                record_id,candidate_id,account_id,source_database,conversation_id,action,
                actor_type,actor_id,actor_name,responsibility_role,occurred_at_utc,input_version,
                evidence_json,rule_version,model_version,decision_method,reason,before_json,after_json,applied,request_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                current["record_id"] if current else None, candidate["candidate_id"] if candidate else None,
                candidate["account_id"], candidate["source_database"], candidate["conversation_id"], action,
                actor_type, actor_id, actor_name, role, self._now_iso(), candidate["base_version"] if candidate else None,
                candidate["field_sources_json"] if candidate else "{}", candidate["rule_version"] if candidate else PRICE_RULE_VERSION,
                candidate["model_version"] if candidate else None, decision_method, reason,
                _json(_load_json(before["payload_json"], {})) if before else None,
                _json(_load_json(after["payload_json"], {})) if after else None, int(applied), request_id,
            ))
        except Exception as exc:
            if "UNIQUE constraint" not in str(exc):
                raise

    def _apply_candidate_tx(self, conn: Any, candidate: Any, payload: dict[str, Any], match_key: dict[str, Any],
                            match_hash: str, *, actor_type: str, actor_id: str | None, actor_name: str | None,
                            role: str, action: str, request_id: str | None) -> tuple[Any, bool, str]:
        scope = {"account_id": candidate["account_id"], "source_database": candidate["source_database"], "conversation_id": candidate["conversation_id"], "conversation_name": candidate["conversation_name"]}
        current = self._find_current(conn, scope, match_hash)
        expected = int(candidate["base_version"])
        if current and int(current["version"]) != expected:
            raise PriceOperationError("version_conflict", "现行价格已被其他操作修改，请刷新后重试", http_status=409,
                                       details={"record_id": current["record_id"], "current_version": current["version"], "base_version": expected})
        if not current and expected != 0:
            raise PriceOperationError("version_conflict", "基础版本不存在或已变化，请刷新后重试", http_status=409,
                                       details={"current_version": 0, "base_version": expected})
        business = _parse_time(payload.get("business_time"))
        current_business = _parse_time(current["business_time"]) if current else None
        if current_business and business and business <= current_business:
            reason = "报价业务时间较旧，保留当前现行值" if business < current_business else "报价业务时间相同且现行值已存在，未静默覆盖"
            self._insert_audit(
                conn, candidate=candidate, current=current, action="not_applied_old",
                actor_type=actor_type, actor_id=actor_id, actor_name=actor_name,
                role=role, reason=reason, before=current, after=current,
                applied=False, request_id=request_id, decision_method="business_time_check",
            )
            return current, False, reason
        now = self._now_iso()
        before = current
        record_id = current["record_id"] if current else "price_" + uuid.uuid4().hex
        version = int(current["version"]) + 1 if current else 1
        conn.execute("""INSERT INTO price_current(
            record_id,account_id,source_database,conversation_id,conversation_name,match_key_hash,match_key_json,
            payload_json,business_time,version,state,candidate_id,maintained_at,responsibility_role,last_actor_type,last_actor_id,last_actor_name
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id,source_database,match_key_hash) DO UPDATE SET
            record_id=excluded.record_id,conversation_id=excluded.conversation_id,conversation_name=excluded.conversation_name,
            match_key_json=excluded.match_key_json,payload_json=excluded.payload_json,business_time=excluded.business_time,
            version=excluded.version,state=excluded.state,candidate_id=excluded.candidate_id,maintained_at=excluded.maintained_at,
            responsibility_role=excluded.responsibility_role,last_actor_type=excluded.last_actor_type,
            last_actor_id=excluded.last_actor_id,last_actor_name=excluded.last_actor_name""", (
            record_id, candidate["account_id"], candidate["source_database"], candidate["conversation_id"], candidate["conversation_name"],
            match_hash, _json(match_key), payload["payload_json"], payload["business_time"], version, "current", candidate["candidate_id"], now,
            role, actor_type, actor_id, actor_name))
        current_after = conn.execute("SELECT * FROM price_current WHERE record_id=?", (record_id,)).fetchone()
        self._insert_audit(conn, candidate=candidate, current=current_after, action=action, actor_type=actor_type,
                           actor_id=actor_id, actor_name=actor_name, role=role, reason="价格候选通过并替换现行值",
                           before=before, after=current_after, applied=True, request_id=request_id, decision_method=action)
        return current_after, True, "已采用为现行价格"

    def submit_candidate(self, payload: Mapping[str, Any], *, source_kind: str = "manual",
                         source_scope: Mapping[str, Any] | None = None, field_sources: Mapping[str, Any] | None = None,
                         idempotency_key: str | None = None, base_version: int | None = None,
                         actor_type: str = "human", actor_id: str | None = None, actor_name: str | None = None) -> dict[str, Any]:
        if source_kind not in {"chat", "manual", "excel", "system"}:
            raise PriceOperationError("invalid_source_kind", "来源类型无效")
        if actor_type not in {"system", "human"}:
            raise PriceOperationError("invalid_operator_type", "操作者类型无效")
        raw = dict(payload)
        if field_sources is not None:
            raw["field_sources"] = dict(field_sources)
        scope = self._scope(source_scope, raw, source_kind)
        missing_scope = [key for key in ("account_id", "source_database", "conversation_id") if not scope.get(key)]
        if missing_scope:
            raise PriceOperationError(
                "scope_required", "价格候选必须绑定账号、来源库和业务会话范围",
                field_errors=[{"field": key, "message": "必须提供明确范围"} for key in missing_scope],
            )
        request_id = _text(idempotency_key) or _text(raw.get("request_id"))
        if not request_id:
            request_id = "derived_" + sha256(_json({"scope": scope, "source_kind": source_kind, "payload": raw}).encode()).hexdigest()
        normalized, sources, normalization_errors = normalize_price_payload(raw, scope, source_kind=source_kind)
        if sources:
            normalized["field_sources"] = sources
        match_key, key_errors = build_match_key(normalized, scope)
        errors = normalization_errors + key_errors
        if source_kind == "chat":
            required_evidence = ("origin", "destination", "weight", "volume", "package_count", "service_time", "price", "quote_company")
            for field in required_evidence:
                if field not in sources and not normalized.get("source_message_id"):
                    errors.append({"field": field, "error_code": "evidence_missing", "message": "字段缺少可核验的消息来源"})
            errors.extend(self._evidence_errors(normalized, sources, scope))
        # Hard rule failures are sufficient to route the candidate to a human;
        # do not send a known-invalid candidate to an external model.
        model_result, model_errors = (None, []) if errors else self._model_review(normalized, sources)
        errors.extend(model_errors)
        match_hash = sha256(_json(match_key).encode("utf-8")).hexdigest() if match_key else None
        candidate_id = "cand_" + uuid.uuid4().hex
        role, _, _ = self._role_and_reviewer()
        configured_settings = self.get_settings()
        model_enabled = self.settings.enabled or bool(configured_settings.get("price_review_llm_enabled"))
        validation = {"ok": not errors, "errors": errors, "rule_version": PRICE_RULE_VERSION, "model_enabled": model_enabled}
        # All database writes, including automatic adoption and its audit, are
        # one transaction.  The model call above is outside the lock.
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM price_candidates WHERE account_id=? AND request_id=?", (scope.get("account_id"), request_id)).fetchone()
            if existing:
                return {**self._candidate_result(existing, idempotent=True), "current": self._current_result(self._find_current(conn, scope, existing["match_key_hash"]))}
            current = self._find_current(conn, scope, match_hash)
            if base_version is None:
                base_version_value = int(current["version"]) if current else 0
            else:
                try:
                    base_version_value = int(base_version)
                except (TypeError, ValueError) as exc:
                    raise PriceOperationError("invalid_base_version", "base_version 必须是整数", field_errors=[{"field": "base_version", "message": "必须是整数"}]) from exc
            status_reason = "; ".join(item["message"] for item in errors)
            review_status = "needs_review" if errors else "pending"
            adoption_status = "not_applied"
            if current and base_version_value != int(current["version"]):
                review_status, adoption_status = "conflict", "conflict"
                status_reason = "现行版本已变化，请刷新后重新提交"
            elif not current and base_version_value != 0:
                review_status, adoption_status = "conflict", "conflict"
                status_reason = "基础版本不存在，请刷新后重新提交"
            automatic = not errors and review_status == "pending" and source_kind in {"chat", "system"} and actor_type == "system"
            if self.settings.enabled:
                automatic = automatic and bool(model_result and model_result.get("status") == "approve")
            if source_kind in {"manual", "excel"}:
                automatic = False
                if not errors:
                    status_reason = "人工录入需显式确认后生效"
            if model_result and model_result.get("status") not in {None, "approve"}:
                automatic = False
                if not errors:
                    review_status, status_reason = "needs_review", "模型建议需人工复核"
            payload_json = dict(normalized)
            payload_json["source_kind"] = source_kind
            payload_json["source_scope"] = scope
            conn.execute("""INSERT INTO price_candidates(
                candidate_id,account_id,source_database,conversation_id,conversation_name,source_kind,source_scope_json,
                payload_json,field_sources_json,match_key_json,match_key_hash,business_time,received_at,maintained_at,
                base_version,request_id,review_status,adoption_status,validation_json,rule_version,model_version,
                model_result_json,status_reason,created_by_type,created_by_id,created_by_name
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                candidate_id, scope.get("account_id"), scope.get("source_database"), scope.get("conversation_id"), scope.get("conversation_name"),
                source_kind, _json(scope), _json(payload_json), _json(sources), _json(match_key) if match_key else None, match_hash,
                normalized.get("business_time"), normalized.get("received_at"), self._now_iso(), base_version_value, request_id,
                 "approved" if automatic else review_status, "not_applied", _json(validation), PRICE_RULE_VERSION,
                 configured_settings.get("model_version") or self.settings.model_version, _json(model_result) if model_result else None, status_reason,
                actor_type, actor_id, actor_name))
            candidate = conn.execute("SELECT * FROM price_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            current_after = current
            applied = False
            if automatic:
                if match_key is None or match_hash is None:
                    raise PriceOperationError("validation_failed", "候选缺少完整匹配条件", field_errors=errors)
                try:
                    current_after, applied, apply_reason = self._apply_candidate_tx(
                        conn, candidate, {"payload_json": _json(payload_json), **payload_json}, match_key, match_hash,
                        actor_type="system", actor_id="system", actor_name="系统执行", role=role,
                        action="auto_apply", request_id=request_id)
                except PriceOperationError:
                    conn.execute("UPDATE price_candidates SET review_status='conflict',adoption_status='conflict',status_reason=? WHERE candidate_id=?", ("提交时现行版本发生冲突，请人工刷新", candidate_id))
                    # Preserve the candidate and conflict audit before the
                    # expected 409-style error escapes the transaction scope.
                    conn.commit()
                    raise
                if applied:
                    conn.execute("UPDATE price_candidates SET adoption_status='applied',status_reason=? WHERE candidate_id=?", (apply_reason, candidate_id))
                else:
                    conn.execute("UPDATE price_candidates SET adoption_status='not_applied_old',status_reason=? WHERE candidate_id=?", (apply_reason, candidate_id))
            result = self._candidate_result(conn.execute("SELECT * FROM price_candidates WHERE candidate_id=?", (candidate_id,)).fetchone())
            result.update({"reviewed": automatic, "applied": applied, "current": self._current_result(current_after), "validation": validation})
            return result

    def list_candidates(self, *, account_id: str | None = None, source_database: str | None = None,
                        conversation_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses, params = [], []
        for col, value in (("account_id", account_id), ("source_database", source_database), ("conversation_id", conversation_id), ("review_status", status)):
            if value:
                clauses.append(f"{col}=?")
                params.append(value)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.storage.connect() as conn:
            rows = conn.execute(f"SELECT * FROM price_candidates {where} ORDER BY maintained_at DESC, candidate_id LIMIT ?", (*params, max(1, min(int(limit), 500)))).fetchall()
        return [self._candidate_result(row) for row in rows]

    def _review_conflict(self, conn: Any, candidate: Any, current: Any, reason: str, request_id: str | None,
                         *, actor_id: str | None, actor_name: str | None, role: str) -> None:
        conn.execute("UPDATE price_candidates SET review_status='conflict',adoption_status='conflict',status_reason=? WHERE candidate_id=?", (reason, candidate["candidate_id"]))
        self._insert_audit(conn, candidate=candidate, current=current, action="review_conflict", actor_type="human",
                           actor_id=actor_id, actor_name=actor_name, role=role, reason=reason, before=current, after=current,
                           applied=False, request_id=request_id, decision_method="version_check")

    def review_candidate(self, candidate_id: str, *, action: str, actor_id: str | None,
                          actor_name: str | None = None, reason: str = "", expected_version: int | None = None,
                          correction: Mapping[str, Any] | None = None, idempotency_key: str | None = None,
                          account_id: str | None = None, source_database: str | None = None,
                          conversation_id: str | None = None) -> dict[str, Any]:
        if action not in {"approve", "reject", "correct_and_approve"}:
            raise PriceOperationError("invalid_review_action", "审核动作必须是 approve、reject 或 correct_and_approve", field_errors=[{"field": "action", "message": "动作无效"}])
        if not _text(reason):
            raise PriceOperationError("reason_required", "人工审核必须填写理由", field_errors=[{"field": "reason", "message": "理由不能为空"}])
        role, configured_id, configured_name = self._require_human(actor_id, actor_name)
        request_id = _text(idempotency_key) or "review_" + candidate_id + "_" + action
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            candidate = conn.execute("SELECT * FROM price_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            if candidate is None:
                raise PriceOperationError("not_found", "找不到价格候选", http_status=404)
            if ((account_id and candidate["account_id"] != account_id)
                    or (source_database and candidate["source_database"] != source_database)
                    or (conversation_id and candidate["conversation_id"] != conversation_id)):
                raise PriceOperationError("scope_forbidden", "价格候选不属于当前账号／会话范围", http_status=403)
            if conn.execute("SELECT 1 FROM price_audit WHERE request_id=? AND action=?", (request_id, "review")).fetchone():
                return {**self._candidate_result(candidate, idempotent=True), "reviewed": True, "applied": candidate["adoption_status"] == "applied"}
            payload = _load_json(candidate["payload_json"], {})
            if action == "correct_and_approve":
                if not isinstance(correction, Mapping) or not correction:
                    raise PriceOperationError("correction_required", "correct_and_approve 必须提供修改后的字段")
                payload.update(dict(correction))
                # Candidate payloads already contain normalized aliases.  A
                # human correction using the public/raw field name must
                # invalidate those aliases first, otherwise normalization
                # would keep the stale value (for example weight_value=1000
                # would win over a corrected weight='2000kg').
                correction_groups = {
                    "origin": ("origin", "origin_raw", "origin_normalized", "origin_kind"),
                    "destination": ("destination", "destination_raw", "destination_normalized", "destination_kind"),
                    "weight": ("weight", "weight_value", "weight_unit", "weight_raw"),
                    "volume": ("volume", "volume_value", "volume_unit", "volume_raw"),
                    "package_count": ("package_count", "package_raw", "package_type"),
                    "service_time": ("service_time", "service_time_raw", "service_time_id"),
                    "price": ("price", "price_raw"),
                    "quote_company": ("quote_company", "quote_company_id", "quote_company_name"),
                }
                correction_keys = set(correction)
                for logical, aliases in correction_groups.items():
                    if logical not in correction_keys and not any(alias in correction_keys for alias in aliases):
                        continue
                    for alias in aliases:
                        if alias not in correction_keys:
                            payload.pop(alias, None)
                payload["field_sources"] = {**_load_json(candidate["field_sources_json"], {}), **(correction.get("field_sources") or {})}
                payload["source_kind"] = "manual"
                payload["manual_correction"] = True
            if action == "reject":
                current = self._find_current(conn, _load_json(candidate["source_scope_json"], {}), candidate["match_key_hash"])
                if current and int(current["version"]) != int(candidate["base_version"] if expected_version is None else expected_version):
                    self._review_conflict(conn, candidate, current, "现行版本已变化，审核未生效", request_id,
                                          actor_id=configured_id, actor_name=configured_name, role=role)
                    conn.commit()
                    raise PriceOperationError("version_conflict", "现行价格已被其他操作修改，审核未生效", http_status=409,
                                               details={"record_id": current["record_id"], "current_version": current["version"], "base_version": candidate["base_version"]})
                conn.execute("UPDATE price_candidates SET review_status='rejected',adoption_status='not_applied',status_reason=? WHERE candidate_id=?", (reason, candidate_id))
                self._insert_audit(conn, candidate=candidate, current=current, action="review", actor_type="human", actor_id=configured_id,
                                   actor_name=configured_name, role=role, reason=reason, before=current, after=current, applied=False,
                                   request_id=request_id, decision_method="human_reject")
                updated = conn.execute("SELECT * FROM price_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
                return {**self._candidate_result(updated), "reviewed": True, "applied": False, "current": self._current_result(current)}
            normalized, sources, norm_errors = normalize_price_payload(payload, _load_json(candidate["source_scope_json"], {}), source_kind="manual" if action == "correct_and_approve" else candidate["source_kind"])
            match_key, key_errors = build_match_key(normalized, _load_json(candidate["source_scope_json"], {}))
            errors = norm_errors + key_errors
            if errors:
                raise PriceOperationError("validation_failed", "修改后的候选未通过字段校验", field_errors=errors)
            match_hash = sha256(_json(match_key).encode()).hexdigest()
            base = int(candidate["base_version"] if expected_version is None else expected_version)
            current = self._find_current(conn, _load_json(candidate["source_scope_json"], {}), match_hash)
            if current and int(current["version"]) != base:
                self._review_conflict(conn, candidate, current, "现行版本已变化，审核未生效", request_id,
                                      actor_id=configured_id, actor_name=configured_name, role=role)
                conn.commit()
                raise PriceOperationError("version_conflict", "现行价格已被其他操作修改，审核未生效", http_status=409,
                                           details={"record_id": current["record_id"], "current_version": current["version"], "base_version": base})
            # A human approval is still subject to the same newest-business-time
            # rule and cannot use Excel/system status columns as authority.
            normalized_payload = dict(normalized)
            normalized_payload["source_kind"] = "manual" if action == "correct_and_approve" else candidate["source_kind"]
            normalized_payload["source_scope"] = _load_json(candidate["source_scope_json"], {})
            normalized_payload["field_sources"] = sources
            normalized_payload["manual_correction"] = action == "correct_and_approve"
            candidate_for_apply = dict(candidate)
            candidate_for_apply["payload_json"] = _json(normalized_payload)
            candidate_for_apply["field_sources_json"] = _json(sources)
            candidate_for_apply["base_version"] = base
            current_after, applied, apply_reason = self._apply_candidate_tx(
                conn, candidate_for_apply, {"payload_json": _json(normalized_payload), **normalized_payload}, match_key, match_hash,
                actor_type="human", actor_id=configured_id, actor_name=configured_name, role=role,
                action="human_apply", request_id=request_id)
            conn.execute("UPDATE price_candidates SET review_status='approved',adoption_status=?,payload_json=?,field_sources_json=?,match_key_json=?,match_key_hash=?,status_reason=? WHERE candidate_id=?",
                         ("applied" if applied else "not_applied_old", _json(normalized_payload), _json(sources), _json(match_key), match_hash, apply_reason, candidate_id))
            # The apply audit is emitted by _apply_candidate_tx; this separate
            # row records the human decision itself and keeps approval distinct.
            self._insert_audit(
                conn, candidate=candidate_for_apply, current=current_after,
                action="review", actor_type="human", actor_id=configured_id,
                actor_name=configured_name, role=role, reason=reason,
                before=current, after=current_after, applied=applied,
                request_id=request_id,
                decision_method="human_correct_and_approve" if action == "correct_and_approve" else "human_approve",
            )
            updated = conn.execute("SELECT * FROM price_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            return {**self._candidate_result(updated), "reviewed": True, "applied": applied, "current": self._current_result(current_after)}

    def deactivate(self, record_id: str, *, actor_id: str | None, actor_name: str | None = None,
                   reason: str = "", expected_version: int | None = None, idempotency_key: str | None = None,
                   account_id: str | None = None, source_database: str | None = None,
                   conversation_id: str | None = None) -> dict[str, Any]:
        if not _text(reason):
            raise PriceOperationError("reason_required", "停用必须填写原因")
        role, configured_id, configured_name = self._require_human(actor_id, actor_name)
        if expected_version is None:
            raise PriceOperationError("base_version_required", "停用必须携带当前版本")
        request_id = _text(idempotency_key) or "deactivate_" + record_id + "_" + str(expected_version)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM price_current WHERE record_id=?", (record_id,)).fetchone()
            if (row is None or (account_id and row["account_id"] != account_id)
                    or (source_database and row["source_database"] != source_database)
                    or (conversation_id and row["conversation_id"] != conversation_id)):
                raise PriceOperationError("not_found", "找不到当前价格或其不属于当前范围", http_status=404)
            if conn.execute("SELECT 1 FROM price_audit WHERE request_id=? AND action='deactivate'", (request_id,)).fetchone():
                return {**self._current_result(row), "idempotent": True, "deactivated": True}
            if int(row["version"]) != int(expected_version):
                raise PriceOperationError("version_conflict", "现行价格版本已变化，请刷新后重试", http_status=409,
                                           details={"current_version": row["version"], "base_version": expected_version})
            if row["state"] == "deactivated":
                return {**self._current_result(row), "idempotent": True, "deactivated": True}
            new_version = int(row["version"]) + 1
            conn.execute("UPDATE price_current SET state='deactivated',version=?,maintained_at=?,last_actor_type='human',last_actor_id=?,last_actor_name=? WHERE record_id=?",
                         (new_version, self._now_iso(), configured_id, configured_name, record_id))
            updated = conn.execute("SELECT * FROM price_current WHERE record_id=?", (record_id,)).fetchone()
            candidate = {"candidate_id": row["candidate_id"], "account_id": row["account_id"], "source_database": row["source_database"], "conversation_id": row["conversation_id"],
                         "base_version": expected_version, "field_sources_json": "{}", "rule_version": PRICE_RULE_VERSION, "model_version": None}
            self._insert_audit(conn, candidate=candidate, current=updated, action="deactivate", actor_type="human", actor_id=configured_id,
                               actor_name=configured_name, role=role, reason=reason, before=row, after=updated, applied=True,
                               request_id=request_id, decision_method="human_deactivate")
            return {**self._current_result(updated), "deactivated": True, "reason": reason}

    def list_prices(self, *, account_id: str | None = None, source_database: str | None = None,
                    conversation_id: str | None = None, conversation_name: str | None = None,
                    company: str | None = None, route: str | None = None, keyword: str | None = None,
                    status: str | None = None, view: str = "current", cursor: str | None = None,
                    limit: int = 100) -> dict[str, Any]:
        if view not in {"current", "pending", "all", "details", "incomplete"}:
            raise PriceOperationError("invalid_view", "价格视图必须是 current、pending、all、details 或 incomplete")
        if view in {"details", "incomplete"}:
            return self._list_price_details(account_id=account_id, source_database=source_database,
                                            conversation_id=conversation_id, conversation_name=conversation_name,
                                            company=company, route=route, keyword=keyword, status=status,
                                            view=view, cursor=cursor, limit=limit)
        offset = 0
        if cursor:
            try:
                offset = int(base64.urlsafe_b64decode(cursor.encode()).decode())
            except Exception as exc:
                raise PriceOperationError("invalid_cursor", "游标无效") from exc
        limit = max(1, min(int(limit), 500))
        clauses, params = [], []
        for col, value in (("account_id", account_id), ("conversation_name", conversation_name)):
            if value:
                clauses.append(f"p.{col} {'LIKE' if col == 'conversation_name' else '='} ?")
                params.append(f"%{value}%" if col == "conversation_name" else value)
        if source_database:
            clauses.append("(p.source_database=? OR instr(p.source_database, ? || ':forwarded:')=1)")
            params.extend([source_database, source_database])
        if conversation_id:
            clauses.append("(p.conversation_id=? OR instr(p.conversation_id, ? || ':forwarded:')=1)")
            params.extend([conversation_id, conversation_id])
        if view == "current":
            clauses.append("p.state='current'")
        elif status in {"current", "deactivated"}:
            clauses.append("p.state=?")
            params.append(status)
        if company:
            clauses.append("(json_extract(p.payload_json,'$.quote_company_name') LIKE ? OR json_extract(p.payload_json,'$.quote_company_id')=?)")
            params.extend([f"%{company}%", company])
        if route:
            clauses.append("(json_extract(p.payload_json,'$.origin_normalized') LIKE ? OR json_extract(p.payload_json,'$.destination_normalized') LIKE ?)")
            params.extend([f"%{route}%", f"%{route}%"])
        if keyword:
            clauses.append("p.payload_json LIKE ?")
            params.append(f"%{keyword}%")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        if view == "pending":
            c_clauses = []
            c_params = []
            for col, value in (("account_id", account_id), ("conversation_name", conversation_name)):
                if value:
                    c_clauses.append(f"c.{col} {'LIKE' if col == 'conversation_name' else '='} ?")
                    c_params.append(f"%{value}%" if col == "conversation_name" else value)
            if source_database:
                c_clauses.append("(c.source_database=? OR instr(c.source_database, ? || ':forwarded:')=1)")
                c_params.extend([source_database, source_database])
            if conversation_id:
                c_clauses.append("(c.conversation_id=? OR instr(c.conversation_id, ? || ':forwarded:')=1)")
                c_params.extend([conversation_id, conversation_id])
            c_clauses.append("c.review_status IN ('pending','needs_review','conflict')")
            if company:
                c_clauses.append("(json_extract(c.payload_json,'$.quote_company_name') LIKE ? OR json_extract(c.payload_json,'$.quote_company_id')=?)")
                c_params.extend([f"%{company}%", company])
            if route:
                c_clauses.append("(json_extract(c.payload_json,'$.origin_normalized') LIKE ? OR json_extract(c.payload_json,'$.destination_normalized') LIKE ?)")
                c_params.extend([f"%{route}%", f"%{route}%"])
            if keyword:
                c_clauses.append("c.payload_json LIKE ?")
                c_params.append(f"%{keyword}%")
            if status:
                if status in {"pending", "needs_review", "conflict", "rejected", "approved"}:
                    c_clauses.append("c.review_status=?")
                    c_params.append(status)
                elif status in {"not_applied", "applied", "not_applied_old", "deactivated"}:
                    c_clauses.append("c.adoption_status=?")
                    c_params.append(status)
            c_where = "WHERE " + " AND ".join(c_clauses)
            with self.storage.connect() as conn:
                total = conn.execute(f"SELECT COUNT(*) FROM price_candidates c {c_where}", c_params).fetchone()[0]
                rows = conn.execute(f"SELECT c.* FROM price_candidates c {c_where} ORDER BY c.maintained_at DESC,c.candidate_id LIMIT ? OFFSET ?", (*c_params, limit, offset)).fetchall()
            items = [self._candidate_result(row) for row in rows]
            with self.storage.connect() as conn:
                for item in items:
                    current = conn.execute(
                        "SELECT * FROM price_current WHERE account_id=? AND source_database=? AND match_key_hash=?",
                        (item["account_id"], item["source_scope"].get("source_database"),
                         conn.execute("SELECT match_key_hash FROM price_candidates WHERE candidate_id=?", (item["candidate_id"],)).fetchone()[0]),
                    ).fetchone()
                    item["current"] = self._current_result(current)
        else:
            with self.storage.connect() as conn:
                total = conn.execute(f"SELECT COUNT(*) FROM price_current p {where}", params).fetchone()[0]
                rows = conn.execute(f"""SELECT p.*,
                    c.review_status AS candidate_review_status,
                    c.adoption_status AS candidate_adoption_status,
                    c.created_by_type AS candidate_created_by_type,
                    c.created_by_name AS candidate_created_by_name,
                    c.validation_json AS candidate_validation_json,
                    c.field_sources_json AS candidate_field_sources_json
                    FROM price_current p
                    LEFT JOIN price_candidates c ON c.candidate_id=p.candidate_id
                    {where} ORDER BY p.business_time DESC,p.record_id LIMIT ? OFFSET ?""", (*params, limit, offset)).fetchall()
            items = [self._current_result(row) for row in rows]
        # A current row may have newer pending candidates.  Keep the latest
        # view single-row while making the review signal explicit.
        if view != "pending" and items:
            with self.storage.connect() as conn:
                for item in items:
                    pending = conn.execute(
                        """SELECT COUNT(*) FROM price_candidates
                           WHERE account_id=? AND source_database=? AND match_key_hash=?
                             AND candidate_id<>? AND review_status IN ('pending','needs_review','conflict')""",
                        (item["account_id"], item["source_scope"].get("source_database"), item["match_key_hash"], item["candidate_id"]),
                    ).fetchone()[0]
                    item["pending_update_count"] = int(pending)
                    item["has_pending_update"] = bool(pending)
        next_cursor = base64.urlsafe_b64encode(str(offset + len(items)).encode()).decode() if offset + len(items) < total else None
        return {"items": items, "total": total, "next_cursor": next_cursor,
                "view": view, "range": {"account_id": account_id, "source_database": source_database, "conversation_id": conversation_id,
                                           "local_imported_scope": True, "history_prices_in_current_view": False}}

    def get_price(self, record_id: str, *, account_id: str | None = None) -> dict[str, Any] | None:
        with self.storage.connect() as conn:
            row = conn.execute("SELECT * FROM price_current WHERE record_id=?", (record_id,)).fetchone()
        if row is None or (account_id and row["account_id"] != account_id):
            return None
        return self._current_result(row)

    def get_price_item(self, *, record_id: str | None = None, candidate_id: str | None = None,
                       account_id: str | None = None, source_database: str | None = None,
                       conversation_id: str | None = None, conversation_name: str | None = None) -> dict[str, Any] | None:
        """Read one price or candidate within the same range boundaries as list_prices."""
        if bool(record_id) == bool(candidate_id):
            raise PriceOperationError("price_id_required", "必须且只能提供 record_id 或 candidate_id")
        with self.storage.connect() as conn:
            if record_id:
                row = conn.execute("""SELECT p.*, c.review_status AS candidate_review_status,
                    c.adoption_status AS candidate_adoption_status, c.created_by_type AS candidate_created_by_type,
                    c.created_by_name AS candidate_created_by_name, c.validation_json AS candidate_validation_json,
                    c.field_sources_json AS candidate_field_sources_json
                    FROM price_current p LEFT JOIN price_candidates c ON c.candidate_id=p.candidate_id
                    WHERE p.record_id=?""", (record_id,)).fetchone()
                if row is None:
                    return None
                if account_id and row["account_id"] != account_id:
                    return None
                if source_database and row["source_database"] != source_database and not row["source_database"].startswith(source_database + ":forwarded:"):
                    return None
                if conversation_id and row["conversation_id"] != conversation_id and not row["conversation_id"].startswith(conversation_id + ":forwarded:"):
                    return None
                if conversation_name and conversation_name.casefold() not in (row["conversation_name"] or "").casefold():
                    return None
                return self._current_result(row)
            row = conn.execute("SELECT * FROM price_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            if row is None:
                return None
            if account_id and row["account_id"] != account_id:
                return None
            if source_database and row["source_database"] != source_database and not row["source_database"].startswith(source_database + ":forwarded:"):
                return None
            if conversation_id and row["conversation_id"] != conversation_id and not row["conversation_id"].startswith(conversation_id + ":forwarded:"):
                return None
            if conversation_name and conversation_name.casefold() not in (row["conversation_name"] or "").casefold():
                return None
            return self._candidate_result(row)

    def preview_import(self, data: bytes, *, source_scope: Mapping[str, Any], request_id: str | None = None) -> dict[str, Any]:
        parsed = parse_price_workbook(data)
        scope = self._scope(source_scope, {}, "excel")
        missing_scope = [key for key in ("account_id", "source_database", "conversation_id") if not scope.get(key)]
        if missing_scope:
            raise PriceOperationError(
                "scope_required", "导入必须绑定账号、来源库和业务会话范围",
                field_errors=[{"field": key, "message": "必须提供明确范围"} for key in missing_scope],
            )
        digest = sha256(data).hexdigest()
        request_id = _text(request_id) or "preview_" + digest
        with self.storage.connect() as conn:
            existing = conn.execute("SELECT * FROM price_import_previews WHERE account_id=? AND request_id=?", (scope["account_id"], request_id)).fetchone()
            if existing:
                return {"preview_id": existing["preview_id"], "file_sha256": existing["file_sha256"], "summary": _load_json(existing["summary_json"], {}), "rows": _load_json(existing["rows_json"], []), "idempotent": True}
        preview_rows = []
        summary = {"new": 0, "modified": 0, "unchanged": 0, "needs_review": 0, "errors": 0, "duplicates": 0}
        seen_keys: dict[str, int] = {}
        for row in parsed["rows"]:
            row_number = row.pop("_row_number")
            row_result: dict[str, Any] = {"row_number": row_number, "action": "new", "errors": [], "old": None, "new": None}
            row_scope = _load_json(row.get("来源范围"), scope)
            if not isinstance(row_scope, Mapping):
                row_scope = scope
            row_scope = self._scope(row_scope, {}, "excel")
            if not _scope_contains(scope, row_scope):
                row_result["errors"].append({"field": "来源范围", "message": "来源范围不在本次导入的账号／会话范围内"})
            row_result["source_scope"] = row_scope
            version_text = _text(row.get("base_version"))
            template_version = _text(row.get("模板版本"))
            if template_version and template_version != PRICE_TEMPLATE_VERSION:
                row_result["errors"].append({"field": "模板版本", "message": "模板版本不受支持"})
            if any(_text(row.get(column)).startswith(("=",)) for column in PRICE_COLUMNS):
                row_result["errors"].append({"field": "workbook", "message": "工作簿中的公式不可提交"})
            payload = {"origin": row.get("起点"), "destination": row.get("终点"), "weight": row.get("重量"), "volume": row.get("体积"),
                       "package_count": row.get("包装数量"), "service_time": row.get("时效"), "price": row.get("价格"),
                       "quote_company_name": row.get("报价公司"), "quote_company_id": row.get("报价公司ID"), "quoted_by_name": row.get("报价方姓名"),
                       "inquiry_company_name": row.get("询价公司"), "inquiry_company_id": row.get("询价公司ID"), "inquired_by_name": row.get("询价人姓名"),
                       "weight_unit": row.get("重量单位"), "volume_unit": row.get("体积单位"), "package_type": row.get("包装类型"),
                       "currency": row.get("币种"), "pricing_method": row.get("计价方式"), "quote_time": row.get("报价时间"),
                       "received_at": row.get("接收时间"), "effective_until": row.get("有效期"), "source_message_id": row.get("来源消息ID"),
                       "field_sources": _load_json(row.get("字段来源"), {}), "source_scope": row_scope, "source_kind": "excel"}
            supplied_sources = payload["field_sources"] if isinstance(payload["field_sources"], Mapping) else {}
            payload["field_sources"] = {
                field: supplied_sources.get(field, {"kind": "excel", "row": row_number, "file_sha256": digest, "column": field})
                for field in ("origin", "destination", "weight", "volume", "package_count", "service_time", "price", "quote_company", "currency", "pricing_method")
            }
            normalized, sources, normalization_errors = normalize_price_payload(payload, row_scope, source_kind="excel")
            key, key_errors = build_match_key(normalized, row_scope)
            row_result["new"] = normalized
            row_result["errors"].extend([{"field": e["field"], "message": e["message"]} for e in normalization_errors + key_errors])
            if not version_text:
                base_version = 0
            else:
                try:
                    base_version = int(version_text)
                except ValueError:
                    base_version = -1
                    row_result["errors"].append({"field": "base_version", "message": "base_version 必须是整数"})
            record_id = _text(row.get("record_id"))
            with self.storage.connect() as conn:
                current = conn.execute("SELECT * FROM price_current WHERE record_id=?", (record_id,)).fetchone() if record_id else None
                if record_id and current is None:
                    row_result["errors"].append({"field": "record_id", "message": "record_id 不属于当前账号或不存在"})
                current_in_scope = bool(current and _scope_contains(scope, {
                    "account_id": current["account_id"], "source_database": current["source_database"],
                    "conversation_id": current["conversation_id"], "conversation_name": current["conversation_name"],
                }))
                if current and not current_in_scope:
                    row_result["errors"].append({"field": "record_id", "message": "record_id 不属于当前导入范围"})
                if current_in_scope:
                    row_result["old"] = self._current_result(current)
                    if base_version != int(current["version"]):
                        row_result["errors"].append({"field": "base_version", "message": "记录版本已变化，请刷新预览"})
                    old_payload = _load_json(current["payload_json"], {})
                    row_result["action"] = "unchanged" if old_payload == normalized else "modified"
                elif key and not record_id:
                    key_hash = sha256(_json(key).encode()).hexdigest()
                    matched = conn.execute("SELECT * FROM price_current WHERE account_id=? AND source_database=? AND match_key_hash=?", (row_scope["account_id"], row_scope.get("source_database"), key_hash)).fetchone()
                    if matched:
                        row_result["action"] = "modified"
                        row_result["old"] = self._current_result(matched)
                        row_result["errors"].append({"field": "record_id", "message": "缺少 record_id 但匹配到现行键，需明确确认"})
            if key:
                key_hash = sha256(_json(key).encode()).hexdigest()
                if key_hash in seen_keys:
                    row_result["errors"].append({"field": "row", "message": f"与第 {seen_keys[key_hash]} 行重复键，不能静默覆盖"})
                    summary["duplicates"] += 1
                else:
                    seen_keys[key_hash] = row_number
            if row_result["errors"]:
                summary["errors"] += 1
            elif row_result["action"] == "new":
                summary["new"] += 1
            elif row_result["action"] == "modified":
                summary["modified"] += 1
            else:
                summary["unchanged"] += 1
            if not row_result["errors"] and (not key or len(key_errors) > 0):
                summary["needs_review"] += 1
            row_result["base_version"] = base_version
            row_result["record_id"] = record_id or None
            row_result["system_columns_ignored"] = {column: row.get(column) for column in SYSTEM_COLUMNS if row.get(column) not in (None, "") and column not in {"record_id", "base_version", "模板版本"}}
            preview_rows.append(row_result)
        preview_id = "preview_" + uuid.uuid4().hex
        now = _now()
        with self.storage.connect() as conn:
            conn.execute("""INSERT INTO price_import_previews(preview_id,account_id,source_scope_json,file_sha256,file_size,template_version,rows_json,summary_json,created_at,expires_at,status,request_id)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (preview_id, scope["account_id"], _json(scope), digest, len(data), PRICE_TEMPLATE_VERSION,
                         _json(preview_rows), _json(summary), _iso(now), _iso(now + timedelta(hours=1)), "open", request_id))
        return {"preview_id": preview_id, "file_sha256": digest, "summary": summary, "rows": preview_rows,
                "template_version": PRICE_TEMPLATE_VERSION, "expires_at": _iso(now + timedelta(hours=1)), "scope": scope}

    def confirm_import(self, preview_id: str, *, selected_rows: list[int], actor_id: str | None,
                       actor_name: str | None = None, data: bytes | None = None, idempotency_key: str | None = None,
                       account_id: str | None = None) -> dict[str, Any]:
        self._require_human(actor_id, actor_name)
        if not selected_rows:
            raise PriceOperationError("selection_required", "必须明确选择要导入的有效行")
        if len(set(selected_rows)) != len(selected_rows):
            raise PriceOperationError("duplicate_rows_selected", "selected_rows 不能包含重复行")
        request_id = _text(idempotency_key) or "confirm_" + preview_id
        with self.storage.connect() as conn:
            preview = conn.execute("SELECT * FROM price_import_previews WHERE preview_id=?", (preview_id,)).fetchone()
            if preview is None:
                raise PriceOperationError("not_found", "找不到 Excel 导入预览", http_status=404)
            if account_id and preview["account_id"] != account_id:
                raise PriceOperationError("scope_forbidden", "导入预览不属于当前账号范围", http_status=403)
            if preview["status"] == "confirmed" and preview["confirmed_request_id"] == request_id:
                return {**(_load_json(preview["confirmation_result_json"], {}) or {"preview_id": preview_id, "confirmed": True}), "idempotent": True}
            if preview["status"] != "open":
                raise PriceOperationError("preview_not_open", "该导入预览已处理，不能重复提交", http_status=409)
            if _parse_time(preview["expires_at"]) and _parse_time(preview["expires_at"]) < self.clock().astimezone(UTC):
                conn.execute("UPDATE price_import_previews SET status='expired' WHERE preview_id=?", (preview_id,))
                raise PriceOperationError("preview_expired", "导入预览已过期，请重新预览", http_status=409)
            if data is not None and sha256(data).hexdigest() != preview["file_sha256"]:
                raise PriceOperationError("preview_content_changed", "文件内容已变化，请重新预览", http_status=409)
            rows = _load_json(preview["rows_json"], [])
            by_number = {int(row["row_number"]): row for row in rows}
            invalid_selected = [row_number for row_number in selected_rows if row_number not in by_number or by_number[row_number].get("errors")]
            if invalid_selected:
                raise PriceOperationError("invalid_rows_selected", "所选行包含错误或不存在，请修正文件或重新选择", field_errors=[{"row": row, "message": "该行不可提交"} for row in invalid_selected])
            scope = _load_json(preview["source_scope_json"], {})
        results = []
        for row_number in selected_rows:
            row = by_number[row_number]
            payload = dict(row["new"])
            row_scope = row.get("source_scope") or scope
            payload["source_scope"] = row_scope
            payload["source_kind"] = "excel"
            payload["manual_correction"] = bool(row.get("old"))
            candidate = self.submit_candidate(payload, source_kind="excel", source_scope=row_scope,
                                               idempotency_key=f"{request_id}:row:{row_number}", base_version=int(row.get("base_version") or 0),
                                               actor_type="human", actor_id=actor_id, actor_name=actor_name)
            results.append({"row_number": row_number, "candidate": candidate})
        result = {"preview_id": preview_id, "confirmed": True, "results": results}
        with self.storage.connect() as conn:
            conn.execute("""UPDATE price_import_previews
                         SET status='confirmed', confirmed_request_id=?, confirmation_result_json=?
                         WHERE preview_id=? AND status='open'""", (request_id, _json(result), preview_id))
        return result

    def preview_company_correction(self, *, account_id: str, source_database: str,
                                   conversation_id: str | None, message_ids: list[str], sender_ids: list[str],
                                   normalized_company_id: str, normalized_company_name: str) -> dict[str, Any]:
        if not account_id or not source_database or not normalized_company_id or not normalized_company_name:
            raise PriceOperationError("correction_fields_required", "账号、规范公司标识和名称不能为空")
        if not message_ids and not sender_ids:
            raise PriceOperationError("correction_scope_required", "公司纠正必须选择明确的消息或发送者范围")
        with self.storage.connect() as conn:
            clauses = ["account_id=?", "(source_database=? OR instr(source_database, ? || ':forwarded:')=1)"]
            params: list[Any] = [account_id, source_database, source_database]
            if conversation_id:
                clauses.append("(conversation_id=? OR instr(conversation_id, ? || ':forwarded:')=1)")
                params.extend([conversation_id, conversation_id])
            selectors = []
            if message_ids:
                placeholders = ",".join("?" for _ in message_ids)
                selectors.append(f"(id IN ({placeholders}) OR message_id IN ({placeholders}))")
                params.extend(message_ids)
                params.extend(message_ids)
            if sender_ids:
                selectors.append("sender_id IN (" + ",".join("?" for _ in sender_ids) + ")"); params.extend(sender_ids)
            if selectors:
                clauses.append("(" + " OR ".join(selectors) + ")")
            rows = [dict(row) for row in conn.execute("SELECT id,message_id,sender_id,sender_name,sender_corp_id,sender_corp_name,company_status FROM messages WHERE " + " AND ".join(clauses), params)]
            affected_message_keys = {row.get("id") for row in rows} | {row.get("message_id") for row in rows}
            affected_sender_ids = {row.get("sender_id") for row in rows}
            conflicts = []
            for price in conn.execute(
                "SELECT * FROM price_current WHERE account_id=? AND (source_database=? OR instr(source_database, ? || ':forwarded:')=1) AND state='current'",
                (account_id, source_database, source_database),
            ):
                payload = _load_json(price["payload_json"], {})
                source_message_id = payload.get("source_message_id")
                quote_matches_affected_sender = (
                    payload.get("quoted_by_id") in affected_sender_ids
                    or source_message_id in affected_message_keys
                )
                if quote_matches_affected_sender:
                    conflicts.append(price["record_id"])
        return {"affected_messages": rows, "affected_count": len(rows), "conflicting_record_ids": conflicts,
                "would_require_review": bool(conflicts), "normalized_company_id": normalized_company_id, "normalized_company_name": normalized_company_name}

    def apply_company_correction(self, *, account_id: str, source_database: str,
                                 conversation_id: str | None, message_ids: list[str], sender_ids: list[str],
                                 original_corp_id: str | None, original_corp_name: str | None,
                                 normalized_company_id: str, normalized_company_name: str,
                                 basis: str, actor_id: str | None, actor_name: str | None,
                                 alias: str | None = None) -> dict[str, Any]:
        if not actor_id or not actor_name:
            raise PriceOperationError("operator_required", "公司纠正需要实际操作人身份")
        preview = self.preview_company_correction(account_id=account_id, source_database=source_database, conversation_id=conversation_id,
                                                  message_ids=message_ids, sender_ids=sender_ids, normalized_company_id=normalized_company_id, normalized_company_name=normalized_company_name)
        if not preview["affected_count"]:
            raise PriceOperationError("correction_no_match", "公司纠正范围没有匹配到消息，未写入纠正记录")
        correction_id = "company_" + uuid.uuid4().hex
        status = "needs_review" if preview["would_require_review"] else "applied"
        with self.storage.connect() as conn:
            conn.execute("""INSERT INTO company_corrections(correction_id,account_id,source_database,conversation_id,message_ids_json,sender_ids_json,original_corp_id,original_corp_name,normalized_company_id,normalized_company_name,basis,actor_id,actor_name,created_at,status,conflict_json)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (correction_id, account_id, source_database, conversation_id, _json(message_ids), _json(sender_ids), original_corp_id, original_corp_name,
                         normalized_company_id, normalized_company_name, basis, actor_id, actor_name, self._now_iso(), status, _json({"record_ids": preview["conflicting_record_ids"]})))
            if alias and status == "applied":
                conn.execute("""INSERT OR REPLACE INTO company_aliases(alias_id,account_id,source_database,alias_name,normalized_company_id,normalized_company_name,basis,actor_id,actor_name,created_at)
                             VALUES (?,?,?,?,?,?,?,?,?,?)""", ("alias_" + uuid.uuid4().hex, account_id, source_database, alias, normalized_company_id, normalized_company_name, basis, actor_id, actor_name, self._now_iso()))
        return {"correction_id": correction_id, "status": status, "preview": preview, "original_preserved": True}

    def correction_rows(self, *, account_id: str | None = None, source_database: str | None = None) -> list[dict[str, Any]]:
        clauses, params = ["status='applied'"], []
        if account_id:
            clauses.append("account_id=?"); params.append(account_id)
        if source_database:
            clauses.append("source_database=?"); params.append(source_database)
        with self.storage.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM company_corrections WHERE " + " AND ".join(clauses), params)]

    def _list_price_details(self, *, account_id: str | None, source_database: str | None,
                            conversation_id: str | None, conversation_name: str | None,
                            company: str | None, route: str | None, keyword: str | None,
                            status: str | None, view: str, cursor: str | None,
                            limit: int) -> dict[str, Any]:
        """Return the maintenance view and reviewable candidates as one feed."""
        offset = 0
        if cursor:
            try:
                offset = int(base64.urlsafe_b64decode(cursor.encode()).decode())
            except Exception as exc:
                raise PriceOperationError("invalid_cursor", "游标无效") from exc
        limit = max(1, min(int(limit), 500))

        def clauses_for(alias: str) -> tuple[list[str], list[Any]]:
            clauses: list[str] = []
            params: list[Any] = []
            for col, value in (("account_id", account_id), ("conversation_name", conversation_name)):
                if value:
                    clauses.append(f"{alias}.{col} {'LIKE' if col == 'conversation_name' else '='} ?")
                    params.append(f"%{value}%" if col == "conversation_name" else value)
            if source_database:
                clauses.append(f"({alias}.source_database=? OR instr({alias}.source_database, ? || ':forwarded:')=1)")
                params.extend([source_database, source_database])
            if conversation_id:
                clauses.append(f"({alias}.conversation_id=? OR instr({alias}.conversation_id, ? || ':forwarded:')=1)")
                params.extend([conversation_id, conversation_id])
            if company:
                clauses.append(f"(json_extract({alias}.payload_json,'$.quote_company_name') LIKE ? OR json_extract({alias}.payload_json,'$.quote_company_id')=?)")
                params.extend([f"%{company}%", company])
            if route:
                clauses.append(f"(json_extract({alias}.payload_json,'$.origin_normalized') LIKE ? OR json_extract({alias}.payload_json,'$.destination_normalized') LIKE ?)")
                params.extend([f"%{route}%", f"%{route}%"])
            if keyword:
                clauses.append(f"{alias}.payload_json LIKE ?")
                params.append(f"%{keyword}%")
            return clauses, params

        current_items: list[dict[str, Any]] = []
        candidate_items: list[dict[str, Any]] = []
        include_current = view == "details" and status not in {"pending", "needs_review", "conflict", "待完善", "待审核"}
        include_candidates = view in {"details", "incomplete"} and status not in {"current", "deactivated", "有效价格"}
        with self.storage.connect() as conn:
            if include_current:
                c_clauses, c_params = clauses_for("p")
                c_clauses.append("p.state='current'")
                if status in {"current", "有效价格"}:
                    pass
                c_where = "WHERE " + " AND ".join(c_clauses)
                rows = conn.execute(f"""SELECT p.*, c.review_status AS candidate_review_status,
                    c.adoption_status AS candidate_adoption_status, c.created_by_type AS candidate_created_by_type,
                    c.created_by_name AS candidate_created_by_name, c.validation_json AS candidate_validation_json,
                    c.field_sources_json AS candidate_field_sources_json
                    FROM price_current p LEFT JOIN price_candidates c ON c.candidate_id=p.candidate_id
                    {c_where} ORDER BY p.business_time DESC,p.record_id""", c_params).fetchall()
                current_items = [self._current_result(row) for row in rows]
            if include_candidates:
                k_clauses, k_params = clauses_for("c")
                k_clauses.append("c.adoption_status <> 'applied'")
                k_clauses.append("c.review_status IN ('pending','needs_review','conflict')")
                if status in {"pending", "needs_review", "conflict"}:
                    k_clauses.append("c.review_status=?")
                    k_params.append(status)
                k_where = "WHERE " + " AND ".join(k_clauses)
                rows = conn.execute(f"SELECT c.* FROM price_candidates c {k_where} ORDER BY c.business_time DESC,c.maintained_at DESC,c.candidate_id", k_params).fetchall()
                for row in rows:
                    item = self._candidate_result(row)
                    if view == "incomplete" and not item["missing_fields"]:
                        continue
                    if status in {"待完善", "待审核"} and item["display_status"] != status:
                        continue
                    candidate_items.append(item)
                # Keep the related current value available to the read-only
                # UI without making it another row in the details feed.
                for item in candidate_items:
                    current = conn.execute(
                        "SELECT * FROM price_current WHERE account_id=? AND source_database=? AND match_key_hash=? AND state='current'",
                        (item["account_id"], item["source_scope"].get("source_database"),
                         conn.execute("SELECT match_key_hash FROM price_candidates WHERE candidate_id=?", (item["candidate_id"],)).fetchone()[0]),
                    ).fetchone()
                    item["current"] = self._current_result(current)

        items = current_items + candidate_items
        # A candidate that became effective is excluded above.  This stable
        # sort also makes cursor paging independent of SQL UNION quirks.
        items.sort(key=lambda item: (item.get("business_time") or "", item.get("maintained_at") or "", item.get("record_id") or item.get("candidate_id") or ""), reverse=True)
        total = len(items)
        page = items[offset:offset + limit]
        next_cursor = base64.urlsafe_b64encode(str(offset + len(page)).encode()).decode() if offset + len(page) < total else None
        return {"items": page, "total": total, "next_cursor": next_cursor, "view": view,
                "range": {"account_id": account_id, "source_database": source_database, "conversation_id": conversation_id,
                          "local_imported_scope": True, "history_prices_in_current_view": False}}

    def sync_chat_candidates(self, *, account_id: str | None = None, conversation_id: str | None = None,
                             conversation_name: str | None = None) -> dict[str, int]:
        """Turn clearly associated quoted responses into candidates.

        Missing company/spec/unit/route evidence remains a candidate needing
        review.  It is never copied to every requested route.
        """
        clauses, params = ["i.category='询价报价'", "r.is_solution=1"], []
        if account_id:
            clauses.append("q.account_id=?"); params.append(account_id)
        if conversation_id:
            clauses.append("(q.conversation_id=? OR instr(q.conversation_id, ? || ':forwarded:')=1)"); params.extend([conversation_id, conversation_id])
        if conversation_name:
            clauses.append("instr(lower(q.conversation_name),lower(?))>0"); params.append(conversation_name)
        with self.storage.connect() as conn:
            rows = [dict(row) for row in conn.execute(f"""SELECT i.id issue_id,i.category,i.clues_json,q.*,r.response_message_id,r.response_kind,r.association_basis,
                rm.text response_text,rm.message_id response_source_message_id,rm.sender_id response_sender_id,rm.sender_name response_sender_name,
                rm.sender_corp_id response_corp_id,rm.sender_corp_name response_corp_name,rm.company_status response_company_status,rm.sent_at response_sent_at,
                rm.source_reference response_source_reference
                FROM issues i JOIN messages q ON q.id=i.message_id JOIN responses r ON r.issue_id=i.id JOIN messages rm ON rm.id=r.response_message_id
                WHERE {' AND '.join(clauses)}""", params)]
        from .wecom_classifier import extract_business_clues
        from .wecom_analysis import split_quoted_reply
        created = 0
        applied = 0
        for row in rows:
            q_clues = _load_json(row["clues_json"], {})
            response_text = split_quoted_reply(row["response_text"] or "")[1]
            r_clues = extract_business_clues(response_text).as_dict()
            saved = q_clues.get("response_entities", {}).get(row["response_message_id"])
            if isinstance(saved, Mapping):
                r_clues.update({key: value for key, value in saved.items() if value is not None})
            options = r_clues.get("quote_options") or [{"destination": None, "amount": r_clues.get("price_quote"), "currency": r_clues.get("currency"), "delivery_time": r_clues.get("delivery_time")}]
            # The classifier's route option extractor intentionally does not
            # own generic ``方案一/方案二`` text.  Parse only explicit amount
            # and method/currency/time tokens here; no value is inferred from
            # message latency or from an unlabelled number.
            if not r_clues.get("quote_options"):
                option_pattern = re.compile(
                    r"(?:(方案\s*[一二三四五六七八九十0-9]+)\s*[:：]?\s*)?"
                    r"(?P<method>总价|全包|单价|每公斤|每千克|每kg|每方|每cbm|/kg|/cbm)?\s*"
                    r"(?P<amount>\d+(?:\.\d+)?)\s*"
                    r"(?P<currency>CNY|RMB|USD|EUR|人民币|美元|美金|欧元|元|块)?\s*"
                    r"(?P<delivery>当天达|当日达|隔日达|次日达|\d+(?:[-~至到]\d+)?\s*(?:个工作日|工作日|天|小时)(?:达|到)?)?",
                    re.I,
                )
                parsed_options: list[dict[str, Any]] = []
                for match in option_pattern.finditer(response_text):
                    explicit = match.group("method") or match.group("currency") or match.group("delivery")
                    if not explicit:
                        continue
                    method = match.group("method")
                    if method in {"总价", "全包"}:
                        method = "total"
                    elif method in {"/kg", "每公斤", "每千克", "每kg"}:
                        method = "per_kg"
                    elif method in {"/cbm", "每方", "每cbm"}:
                        method = "per_cbm"
                    elif method == "单价":
                        # “单价” alone does not identify kg/cbm/box.
                        # Look for an explicit unit suffix attached to this
                        # option; otherwise leave the method empty for review.
                        suffix = response_text[match.end():]
                        method = "per_kg" if re.match(r"\s*/\s*kg\b", suffix, re.I) else "per_cbm" if re.match(r"\s*/\s*cbm\b", suffix, re.I) else None
                    parsed_options.append({"destination": None, "amount": match.group("amount"),
                                           "currency": match.group("currency"), "delivery_time": match.group("delivery"),
                                           "pricing_method": method, "basis": "explicit_amount_method"})
                if parsed_options:
                    options = parsed_options
            requested_routes = q_clues.get("route_items") or []
            if not requested_routes and q_clues.get("origin") and q_clues.get("destination"):
                requested_routes = [{"origin": q_clues["origin"], "destination": q_clues["destination"]}]
            for index, option in enumerate(options):
                option = option if isinstance(option, Mapping) else {}
                # A single requested route is an explicit association even
                # when the supplier did not repeat its destination label.
                label = option.get("destination")
                matched = [route for route in requested_routes if label and route.get("destination") and (label in route["destination"] or route["destination"] in label)]
                if not label and len(requested_routes) == 1:
                    matched = list(requested_routes)
                ambiguous = len(matched) != 1
                route = matched[0] if len(matched) == 1 else (requested_routes[0] if len(requested_routes) == 1 else {})
                amount = option.get("amount")
                if amount in (None, ""):
                    amount = r_clues.get("price_quote")
                # A response without an explicit numeric amount is useful
                # context, but is not a price candidate and must not enter
                # maintenance merely as a reviewable empty row.
                if _decimal(amount) is None:
                    continue
                pricing_method = option.get("pricing_method") or r_clues.get("pricing_method")
                # Never apply a method from an unrelated option in the same
                # response.  The explicit multi-option parser above is the
                # only place where method words become a canonical method.
                if len(options) > 1 and not option.get("pricing_method"):
                    pricing_method = None
                multi_cargo = any(len(q_clues.get(key) or []) > 1 for key in ("weight_items", "volume_items", "package_items"))
                cargo_conditions = dict(q_clues.get("conditions") or {}) if isinstance(q_clues.get("conditions"), Mapping) else {}
                if multi_cargo:
                    cargo_conditions["cargo_association"] = "待确认：询价含多个货物条件，未发现明确方案对应关系"
                if len(options) > 1:
                    # Distinct explicit plans must remain selectable rows even
                    # when their route/specification key is otherwise equal.
                    cargo_conditions["quote_option_key"] = "|".join(_norm_key_text(option.get(name)) for name in ("destination", "amount", "currency", "pricing_method", "delivery_time"))
                payload = {
                    "origin": route.get("origin") or q_clues.get("origin"), "destination": route.get("destination") if not ambiguous else None,
                    "weight": None if multi_cargo else q_clues.get("weight"),
                    "volume": None if multi_cargo else q_clues.get("volume"),
                    "package_count": None if multi_cargo else q_clues.get("packages"),
                    "service_time": option.get("delivery_time") if len(options) > 1 else option.get("delivery_time") or r_clues.get("delivery_time"),
                    "price": amount,
                    "currency": option.get("currency") if len(options) > 1 else option.get("currency") or r_clues.get("currency"),
                    "pricing_method": pricing_method,
                    "conditions": cargo_conditions,
                    "quote_company_id": row.get("response_corp_id"), "quote_company_name": row.get("response_corp_name"),
                    "quoted_by_id": row.get("response_sender_id"), "quoted_by_name": row.get("response_sender_name"),
                    "inquiry_company_id": row.get("sender_corp_id"), "inquiry_company_name": row.get("sender_corp_name"),
                    "inquired_by_id": row.get("sender_id"), "inquired_by_name": row.get("sender_name"),
                    "source_message_id": row.get("response_source_message_id"), "source_reference": row.get("response_source_reference"),
                    "quote_time": row.get("response_sent_at"), "route_ambiguous": ambiguous,
                    "company_status": row.get("response_company_status") or "unknown",
                    "field_sources": {"origin": {"message_id": row["message_id"], "excerpt": q_clues.get("origin")},
                                      "destination": {"message_id": row["message_id"], "excerpt": q_clues.get("destination")},
                                      "weight": {"message_id": row["message_id"], "excerpt": q_clues.get("weight")},
                                      "volume": {"message_id": row["message_id"], "excerpt": q_clues.get("volume")},
                                      "package_count": {"message_id": row["message_id"], "excerpt": q_clues.get("packages")},
                                      "service_time": {"message_id": row["response_source_message_id"], "excerpt": option.get("delivery_time")},
                                      "price": {"message_id": row["response_source_message_id"], "excerpt": option.get("amount")},
                                      "quote_company": {"message_id": row["response_source_message_id"], "excerpt": row.get("response_corp_name")}},
                }
                # Issue row IDs are rebuilt during analysis.  The stable
                # message/category/option key keeps re-analysis and duplicate
                # imports idempotent without treating unrelated sessions as
                # the same quote.
                result = self.submit_candidate(payload, source_kind="chat", source_scope={"account_id": row["account_id"], "source_database": row["source_database"], "conversation_id": row["conversation_id"], "conversation_name": row["conversation_name"]},
                                               idempotency_key=(f"chat:{row['account_id']}:{row['source_database']}:{row['conversation_id']}"
                                                                f":{row['response_source_message_id']}:{row['category'] if 'category' in row else 'quote'}:{index}"),
                                               base_version=None, actor_type="system", actor_id="system", actor_name="系统执行")
                if not result.get("idempotent"):
                    created += 1
                if result.get("applied"):
                    applied += 1
        return {"candidates": created, "applied": applied}


def price_item_for_export(item: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(item)
    result["payload"] = dict(item.get("payload") or {})
    return result


__all__ = [
    "PRICE_SCHEMA", "PRICE_RULE_VERSION", "PRICE_TEMPLATE_VERSION", "DEFAULT_RESPONSIBILITY_ROLE",
    "PRICE_COLUMNS", "PRICE_CORE_COLUMNS", "PriceOperationError", "PriceReviewSettings", "PriceMaintenance",
    "build_match_key", "normalize_price_payload", "build_price_workbook", "build_single_price_workbook", "parse_price_workbook",
]
