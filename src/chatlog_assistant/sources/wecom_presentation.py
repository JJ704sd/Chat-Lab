"""Management demo using attributed, pre-imported local source material."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import base64
import io
from functools import lru_cache
import json
from pathlib import Path
import re
import tempfile
from typing import Any

from .trimanson_rates import match_pdf_rate, parse_trimanson_pdf
from .wecom_airfreight import AirfreightOperationError


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        stream.write(data)
        temp = Path(stream.name)
    temp.replace(path)


def _number(pattern: str, text: str) -> str | None:
    result = re.search(pattern, text, re.I)
    return str(Decimal(result[1])) if result else None


def _clean(text: str) -> str:
    return re.sub(r"\s+", "", text).strip(",，")


def extract_inquiries(messages: list[dict]) -> list[dict]:
    inquiries: list[dict] = []
    keys: dict[tuple, dict] = {}
    for message in messages:
        text = message.get("body") or ""
        if message.get("role") != "sales":
            continue
        # These are three-letter destination tokens, not two-character airlines.
        months = r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
        destination_text = re.sub(r"\b\d{1,2}[\s/-]+" + months + r"\b|\b" + months + r"[\s/-]+\d{1,4}\b", " ", text)
        destinations = list(dict.fromkeys(re.findall(r"(?<![A-Z])([A-Z]{3})(?![A-Z])", destination_text)))
        destinations = [d for d in destinations if d not in {"CBM", "KGS", "PAL", "PLT", "PKG", "PCS", "CAN", "CTN", "TNS", "TOO"}]
        if not destinations:
            continue
        gross = _number(r"(?:总重(?:为)?|总|共)\s*(\d+(?:\.\d+)?)\s*(?:KG|公斤)?", text)
        gross = gross or _number(r"(\d+(?:\.\d+)?)\s*(?:KGS?\b|公斤)", text)
        volume = _number(r"(\d+(?:\.\d+)?)\s*(?:CBM|立方)", text)
        pieces = _number(r"(\d+)\s*(?:CTNS?|TNS|PCS|PKGS|PAL(?:LET)?S?|个托盘|托盘|托|木箱|箱)", text)
        shorthand = re.search(r"(?<![\d.*])([0-9]+)\s*/+\s*(\d+(?:\.\d+)?)\s*K?\s*/+\s*(\d+(?:\.\d+)?)\s*(?:CBM)?", text, re.I)
        inferred = False
        if shorthand and (gross is None or volume is None):
            pieces = pieces or shorthand[1]
            gross = gross or shorthand[2]
            volume = volume or shorthand[3]
            inferred = True
        if not gross or not volume or Decimal(gross) <= 0 or Decimal(volume) <= 0:
            continue
        # A product summary such as "共10.11CBM" is a volume, never a total weight.
        total = re.search(r"(?:总重(?:为)?|总)\s*(\d+(?:\.\d+)?)", text)
        unit_weight = _number(r"(\d+(?:\.\d+)?)\s*(?:KGS?\b|公斤)", text)
        gross = total[1] if total else unit_weight or gross
        key = (message["sender"], tuple(destinations), Decimal(gross), Decimal(volume))
        if key in keys:
            keys[key]["message_orders"].append(message["capture_order"])
            continue
        pending = []
        if inferred:
            pending.append("斜杠简写的件数、重量和体积单位需核验")
        if len(destinations) > 1:
            pending.append("多个目的港待明确选择")
        if "电池" in text:
            pending.append("含电池，需核对品类与承运要求")
        volume_weight = Decimal(volume) * Decimal(1000000) / Decimal(6000)
        item = {"id": f"inquiry-{message['capture_order']:03d}", "sender": message["sender"],
                "destinations": destinations, "destination": "/".join(destinations),
                "gross_kg": gross, "volume_cbm": volume, "pieces": pieces,
                "cargo_location": next((c for c in ("深圳", "广州", "中山") if c in text), None),
                "original": text, "displayed_time": message.get("displayed_time"),
                "message_orders": [message["capture_order"]], "pending": pending, "responses": [],
                "preliminary_weight": {"volume_kg": str(volume_weight.quantize(Decimal("0.01"))),
                    "chargeable_kg": str(max(Decimal(gross), volume_weight).quantize(Decimal("0.01"))),
                    "rule": "体积×1,000,000÷6,000；与毛重取大，仅供演示测算，分泡与取整待确认"}}
        keys[key] = item
        inquiries.append(item)

    previous_supplier = 0
    for message in messages:
        if message.get("role") != "supplier" or not message.get("body"):
            continue
        quote = message.get("quoted_text")
        if quote:
            matched = [q for q in inquiries if _clean(q["original"]) == _clean(quote)]
            association = "explicit_quote"
        else:
            matched = [q for q in inquiries if any(previous_supplier < n < message["capture_order"] for n in q["message_orders"])]
            association = "adjacent_context"
        if len(matched) == 1:
            matched[0]["responses"].append({**message, "association": association,
                "association_label": "原消息明确引用" if association == "explicit_quote" else "相邻上下文关联，待人工核对",
                "currency": None, "unit": "KG" if re.search(r"/\s*KG", message["body"], re.I) else None})
        previous_supplier = message["capture_order"]
    return inquiries


class PresentationService:
    def __init__(self, root: Path):
        self.root = root

    def install_pdf(self, data: bytes, filename: str) -> dict:
        try:
            card = parse_trimanson_pdf(data)
        except Exception as exc:
            raise AirfreightOperationError("pdf_unreadable", "PDF 无法解析，请检查文件是否完整") from exc
        if card is None:
            raise AirfreightOperationError("unsupported_rate_pdf", "尚未支持该 PDF 版式，请导入 Trimanson ET co-load 原始价表")
        card["file_name"] = Path(filename).name
        _write(self.root / f"rate-{card['source_sha256']}.pdf", data)
        _write(self.root / "active-rate.json", _canonical(card))
        return card

    def rate_card(self) -> dict | None:
        path = self.root / "active-rate.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def pdf_bytes(self, digest: str | None = None) -> bytes:
        card = self.rate_card()
        if not card and not digest:
            raise AirfreightOperationError("missing_pdf", "尚未导入真实价表", http_status=404)
        digest = digest or card["source_sha256"]
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise AirfreightOperationError("invalid_pdf", "价表索引无效")
        path = self.root / f"rate-{digest}.pdf"
        if not path.is_file():
            raise AirfreightOperationError("missing_pdf", "价表原件缺失，请重新导入", http_status=404)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise AirfreightOperationError("pdf_changed", "价表原件校验失败，请重新导入")
        return data

    def pdf_page(self, digest: str, page: int) -> dict:
        return _pdf_page(self.pdf_bytes(digest or None), page)

    def snapshot(self) -> dict:
        path = self.root / "source-chat.json"
        source = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        card = self.rate_card()
        if source is not None:
            mode = source.get("source_mode")
            verification = source.get("verification") or {}
            snapshots = verification.get("snapshots") or []
            local_verified = (mode == "wecom_local_snapshot" and verification.get("verified") is True
                              and bool(snapshots) and all(s.get("verified") is True for s in snapshots))
            if source.get("anonymized") is not True or not (mode == "wecom_ui_observation" or local_verified):
                raise AirfreightOperationError("source_not_ready", "请导入脱敏的界面观察样本或已校验的本机数据库样本")
        messages = source.get("messages", []) if source else []
        inquiries = extract_inquiries(messages)
        revision = hashlib.sha256(_canonical({"source": source, "card": card})).hexdigest()
        for item in inquiries:
            item["pdf_match"] = match_pdf_rate(card, item["destination"], item["preliminary_weight"]["chargeable_kg"], source["message_date"]) if card else {"status": "missing_card", "reason": "尚未导入价表"}
            price_responses = [r for r in item["responses"] if re.search(r"(?<![A-Z])(?:TK|SQ|CZ|HU|YG|KJ|O3|3U|C6)(?![A-Z])", r.get("reply_body", r["body"]), re.I) and re.search(r"\d", r.get("reply_body", r["body"]))]
            item["price_responses"] = price_responses
            item["pending"] += ["供应商回复的币种与计价口径", "附加费、舱位、有效期及销售加价"]
            if any(r["association"] == "adjacent_context" for r in price_responses):
                item["pending"].append("回复与当前询价的对应关系")
        return {"ready": bool(source and card), "revision": revision, "source": source, "rate_card": card,
                "inquiries": inquiries, "default_inquiry_id": next((q["id"] for q in reversed(inquiries) if q["price_responses"]), inquiries[0]["id"] if inquiries else None),
                "metrics": {"visible_rows": len(messages), "readable_messages": sum(bool(m.get("body")) for m in messages),
                    "inquiries": len(inquiries), "destinations": len({d for q in inquiries for d in q["destinations"]}),
                    "chat_price_candidates": sum(bool(q["price_responses"]) for q in inquiries),
                    "pdf_matches": sum(q["pdf_match"]["status"] == "candidate" for q in inquiries),
                    "rate_destinations": len(card["rows"]) if card else 0,
                    "numeric_rate_destinations": sum(r["status"] == "listed" for r in card["rows"]) if card else 0}}

    def create_draft(self, inquiry_id: str, revision: str) -> dict:
        data = self.snapshot()
        if data["revision"] != revision:
            raise AirfreightOperationError("source_changed", "资料已更新，请刷新后重新生成草稿", http_status=409)
        inquiry = next((q for q in data["inquiries"] if q["id"] == inquiry_id), None)
        if inquiry is None:
            raise AirfreightOperationError("inquiry_missing", "询价不存在", http_status=404)
        replies = inquiry["price_responses"]
        reply_lines = "\n".join(f"- {r['body']}（{r['association_label']}）" for r in replies) or "- 暂无可直接采用的供应商回复，需要补充询价。"
        body = (f"空运报价草稿｜{inquiry['destination']}\n\n"
                f"目的港：{inquiry['destination']}\n货物所在地：{inquiry['cargo_location'] or '待确认'}\n"
                f"毛重：{inquiry['gross_kg']} kg\n体积：{inquiry['volume_cbm']} CBM\n\n"
                f"供应商原始回复：\n{reply_lines}\n\n"
                f"ET PDF 核对：{inquiry['pdf_match']['reason']}\n\n"
                "报价币种、计价单位、附加费、有效期、舱位及销售加价：待确认。\n"
                "最终单价与总费用：待确认，未计算全包价。\n\n"
                "此为内部审核草稿，尚未向客户发送。")
        draft_id = hashlib.sha256((revision + inquiry_id).encode()).hexdigest()[:24]
        path = self.root / "drafts" / f"{draft_id}.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        draft = {"draft_id": draft_id, "source_revision": revision, "inquiry_id": inquiry_id,
                 "created_at": datetime.now(timezone.utc).isoformat(), "status": "pending_confirmation",
                 "sent": False, "total": None, "currency": None, "body": body,
                 "evidence": {"source_mode": data["source"]["source_mode"], "message_orders": inquiry["message_orders"],
                              "reply_orders": [r["capture_order"] for r in replies],
                              "pdf_sha256": data["rate_card"]["source_sha256"] if data["rate_card"] else None},
                 "pending": inquiry["pending"]}
        _write(path, _canonical(draft))
        return draft


@lru_cache(maxsize=12)
def _pdf_page(data: bytes, number: int) -> dict:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as document:
        if number < 1 or number > len(document.pages):
            raise AirfreightOperationError("page_missing", "PDF 页码不存在", http_status=404)
        page = document.pages[number - 1]
        # A merged notes cell can make a table row's bounding box span several
        # destinations. Use the first cell's height for the visual row focus.
        row_regions = []
        for table in page.find_tables():
            for row in table.rows:
                if row.cells and row.cells[0] is not None:
                    first = row.cells[0]
                    row_regions.append({"bbox": list(row.bbox),
                                        "focus_bbox": [row.bbox[0], first[1], row.bbox[2], first[3]]})
        output = io.BytesIO()
        page.to_image(resolution=180).original.save(output, format="PNG")
        return {"width": page.width, "height": page.height, "page_count": len(document.pages),
                "row_regions": row_regions,
                "image": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()}
