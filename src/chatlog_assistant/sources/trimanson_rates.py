"""Read the ET co-load PDF table; preserve missing rates and source cells."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import io
import re
from typing import Any


BREAKS = ("+45", "+100", "+300", "+500", "+1000")


def amount(value: str | None) -> str | None:
    text = (value or "").strip().replace(",", "")
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    try:
        return str(Decimal(text))
    except InvalidOperation:
        return None


def parse_trimanson_pdf(data: bytes) -> dict[str, Any] | None:
    """Return None for unsupported templates; never substitute fixture prices."""
    if not data.startswith(b"%PDF") or len(data) > 10 * 1024 * 1024:
        return None
    import pdfplumber
    from pdfplumber.utils.exceptions import PdfminerException
    try:
        document = pdfplumber.open(io.BytesIO(data))
    except PdfminerException:
        return None
    with document as pdf:
        if not 2 <= len(pdf.pages) <= 10:
            return None
        texts = [p.extract_text() or "" for p in pdf.pages]
        title = re.search(r"ET CO-LOAD RATES from (\d{1,2}-[A-Za-z]{3}-\d{4}) until further notice", texts[0])
        if not title or "HKD" not in "\n".join(texts) or "Trimanson" not in "\n".join(texts):
            return None
        valid_from = datetime.strptime(title[1], "%d-%b-%Y").date().isoformat()
        rows = []
        seen = set()
        for page_number, page in enumerate(pdf.pages, 1):
            for table in page.find_tables():
                cells = table.extract()
                for row_index, values in enumerate(cells):
                    if len(values) < 10:
                        continue
                    destination = (values[0] or "").strip().lstrip("* ")
                    if not re.fullmatch(r"[A-Z]{3}", destination):
                        continue
                    raw = [(v or "").strip() for v in values[:10]]
                    if destination in seen:
                        raise ValueError("价表中出现重复航点，请人工核对")
                    seen.add(destination)
                    rows.append({
                        "destination": destination, "city": raw[1],
                        "minimum": amount(raw[2]), "below_45_raw": raw[3],
                        "breaks": {label: amount(raw[index + 4]) for index, label in enumerate(BREAKS)},
                        "notes": raw[9], "raw_cells": raw,
                        "page": page_number, "row": row_index + 1,
                        "bbox": list(table.rows[row_index].bbox),
                        "status": "listed" if any(amount(v) is not None for v in raw[4:9]) else "inquiry_required",
                    })
        if not rows:
            return None
        fee_text = texts[1]
        fees = []
        fee_names = {
            "FUEL SURCHARGE": "燃油附加费", "SECURITY CHARGE": "安保费",
            "CARGO HANDLING CHARGE": "货物处理费", "AVIATION SECURITY CHARGE": "航空安保费",
            "X-RAY FEE": "X 光检查费", "TERMINAL HANDLING CHARGE": "货站处理费",
            "AIRLINE DOCUMENTATION FEE": "航空单证费", "DATA TRANSFER FEE": "数据传输费",
            "HANDLING CHARGE": "操作费",
        }
        for line in fee_text.splitlines():
            label = line.split(":", 1)[0].strip()
            if label in fee_names:
                match = re.search(r"HKD\s+(\d+(?:\.\d+)?)", line)
                fees.append({"name": fee_names[label], "amount": match[1] if match else None,
                             "currency": "HKD", "raw": line, "page": 2,
                             "basis": "gross_weight" if "Gross Weight" in line else
                                      "chargeable_weight" if "Chargeable Weight" in line else
                                      "mawb" if "/MAWB" in line else "needs_confirmation"})
        return {
            "supplier": "Trimanson Express Ltd.", "airline": "ET", "currency": "HKD",
            "valid_from": valid_from, "valid_to": None, "validity": "有效至另行通知",
            "source_sha256": hashlib.sha256(data).hexdigest(), "page_count": len(pdf.pages),
            "rows": rows, "fees": fees, "text_pages": [i + 1 for i, t in enumerate(texts) if t.strip()],
            "image_pages": [i + 1 for i, t in enumerate(texts) if not t.strip()],
            "volume_share_raw": "1/3 volume share (2/3 volume rebated)" if "1/3 volume share" in fee_text else None,
            "warnings": ["价表有效至另行通知，采用前需确认未被后续价表替代", "起运交仓条件、分泡与附加费适用口径需确认"]
                        + (["图片页包含补充条款，需结合原件核验"] if any(not t.strip() for t in texts) else []),
        }


def match_pdf_rate(card: dict[str, Any], destination: str, weight: str | None, on_date: str) -> dict[str, Any]:
    """A candidate base rate is never an all-in quote or a confirmed sale."""
    if on_date < card["valid_from"]:
        return {"status": "not_effective", "reason": "价表尚未生效"}
    row = next((r for r in card["rows"] if r["destination"] == destination), None)
    if row is None:
        return {"status": "not_covered", "reason": "该目的港未被本次 ET 价表覆盖"}
    if not weight or Decimal(weight) <= 0:
        return {"status": "needs_weight", "reason": "缺少可用重量", "row": row}
    kg = Decimal(weight)
    if kg < 45:
        return {"status": "needs_confirmation", "reason": "低于 45kg，需核对最低收费与 N 价口径", "row": row}
    label = next(label for label in reversed(BREAKS) if kg >= Decimal(label[1:]))
    value = row["breaks"].get(label)
    if value is None:
        return {"status": "inquiry_required", "reason": "此重量档未提供数值运价，需询价", "row": row, "weight_break": label}
    return {"status": "candidate", "reason": "已找到基础运价；计费重规则、适用条件与附加费待确认",
            "amount": value, "currency": card["currency"], "unit": "KG", "weight_break": label, "row": row}


def legacy_extraction(card: dict[str, Any]) -> dict[str, Any]:
    observations = []
    for row in card["rows"]:
        for label, price in row["breaks"].items():
            observations.append({"airline_name": "Ethiopian Airlines", "airline_code": "ET-071",
                "supplier_name": card["supplier"], "origin": "", "hub": None,
                "destination": row["destination"], "route": row["destination"],
                "effective_date": card["valid_from"], "validity_mode": "until_further_notice",
                "currency": "HKD", "unit": "KG", "weight_break_label": label,
                "amount": price, "rate_status": "candidate" if price is not None else "inquiry_required",
                "raw_text": " | ".join(row["raw_cells"]),
                "locator": {"page": row["page"], "row": row["row"], "rect": row["bbox"]}})
    return {"status": "parsed", "parser_path": "trimanson_et_table", "pages": card["page_count"],
            "warnings": card["warnings"], "errors": [], "fields": [
                {"field": "currency", "raw": "HKD", "normalized": "HKD", "confidence": "0.99", "locator": {"page": 2}},
            ], "rate_observations": observations, "restrictions": card["warnings"], "structured_rate_card": card}
