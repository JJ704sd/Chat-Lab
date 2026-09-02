from __future__ import annotations

import csv
import json
from pathlib import Path
import re
from typing import Any, Sequence

from .wecom_storage import WecomLocalStorage


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
        "conversation_name",
        "source_reference",
    ]

    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for item in issues:
            row = dict(item)
            clues = row.get("clues") or {}
            row["waybill_no"] = clues.get("waybill_no") or ""
            row["packages"] = clues.get("packages") or ""
            row["weight"] = clues.get("weight") or ""
            row["volume"] = clues.get("volume") or ""

            if anonymize:
                row["question_raw_text"] = mask_sensitive_text(str(row.get("question_raw_text") or ""))
                row["responder_raw_text"] = mask_sensitive_text(str(row.get("responder_raw_text") or ""))
                if row.get("question_sender_name"):
                    name = str(row["question_sender_name"])
                    row["question_sender_name"] = name[0] + "**" if len(name) > 1 else name
                if row.get("responder_name"):
                    name = str(row["responder_name"])
                    row["responder_name"] = name[0] + "**" if len(name) > 1 else name
            writer.writerow(row)

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
        row = dict(item)
        if anonymize:
            row["question_raw_text"] = mask_sensitive_text(str(row.get("question_raw_text") or ""))
            row["responder_raw_text"] = mask_sensitive_text(str(row.get("responder_raw_text") or ""))
            if row.get("question_sender_name"):
                name = str(row["question_sender_name"])
                row["question_sender_name"] = name[0] + "**" if len(name) > 1 else name
            if row.get("responder_name"):
                name = str(row["responder_name"])
                row["responder_name"] = name[0] + "**" if len(name) > 1 else name
        rows.append(row)

    with open(out, "w", encoding="utf-8") as f:
        json.dump({"issues": rows, "total": len(rows)}, f, ensure_ascii=False, indent=2)

    return out
