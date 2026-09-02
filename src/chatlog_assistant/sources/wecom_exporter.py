from __future__ import annotations

import csv
import json
from pathlib import Path
import re
from typing import Any, Sequence

def mask_sensitive_text(text: str) -> str:
    """Masks phone numbers, ID cards, specific customer names, and bank accounts."""
    if not text:
        return text
    # Mask 11-digit phone numbers
    text = re.sub(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)", r"\1****\2", text)
    # Mask 18-digit ID card numbers
    text = re.sub(r"(?<!\d)(\d{6})\d{8}(\w{4})(?!\d)", r"\1********\2", text)
    # Mask bank card numbers (16-19 digits)
    text = re.sub(r"(?<!\d)(\d{4})\d{8,11}(\d{4})(?!\d)", r"\1****\2", text)
    return text


def _masked(value: Any) -> Any:
    if isinstance(value, str):
        return mask_sensitive_text(value)
    if isinstance(value, dict):
        result = {key: _masked(item) for key, item in value.items()}
        for key in ("question_sender_name", "responder_name"):
            name = result.get(key)
            if name:
                result[key] = name[0] + "**"
        return result
    if isinstance(value, list):
        return [_masked(item) for item in value]
    return value


def export_issues_to_csv(
    issues: Sequence[dict[str, Any]],
    output_path: Path | str,
    *,
    anonymize: bool = False,
) -> Path:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "issue_id",
        "category",
        "issue_status",
        "status_reason",
        "question_sent_at",
        "question_sender_name",
        "question_corp_name",
        "question_subject_bucket",
        "question_raw_text",
        "responder_name",
        "responder_corp_name",
        "responder_subject_bucket",
        "response_sent_at",
        "response_kind",
        "responder_raw_text",
        "latency_seconds",
        "association_basis",
        "waybill_no",
        "packages",
        "weight",
        "volume",
        "pickup_address", "destination", "dimensions", "price_quote", "currency", "delivery_time",
        "first_response_seconds", "solution_seconds", "urgency_level", "risk_reason", "analysis_source",
        "account_id", "conversation_id",
        "conversation_name",
        "source_reference",
    ]

    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for item in issues:
            row = _masked(item) if anonymize else dict(item)
            clues = row.get("clues") or {}
            response_clues = row.get("response_clues") or {}
            row["waybill_no"] = clues.get("waybill_no") or ""
            row["packages"] = clues.get("packages") or ""
            row["weight"] = clues.get("weight") or ""
            row["volume"] = clues.get("volume") or ""
            for key in ("pickup_address", "destination", "dimensions", "urgency_level", "analysis_source"):
                row[key] = clues.get(key)
            for key in ("price_quote", "currency", "delivery_time"):
                row[key] = response_clues.get(key) or clues.get(key)
            row["risk_reason"] = (clues.get("risk_evaluation") or {}).get("risk_reason")

            # Chat text is data, including when opened by a spreadsheet application.
            writer.writerow({key: "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value
                             for key, value in row.items()})

    return out


def export_issues_to_json(
    issues: Sequence[dict[str, Any]],
    output_path: Path | str,
    *,
    anonymize: bool = False,
) -> Path:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    for item in issues:
        row = _masked(item) if anonymize else dict(item)
        rows.append(row)

    with open(out, "w", encoding="utf-8") as f:
        json.dump({"issues": rows, "total": len(rows)}, f, ensure_ascii=False, indent=2)

    return out
