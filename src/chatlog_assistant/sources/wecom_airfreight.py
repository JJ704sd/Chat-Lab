"""Local-first airfreight demo domain.

The existing pickup-fleet tables and service remain the compatibility boundary
for the old product.  This module owns only the additive airfreight tables and
the deterministic demo workflow described in ``docs/wecom-airfreight-demo-spec.md``.

There are deliberately no network clients, Office runners, model calls, or
implicit imports in this module.  Files are treated as untrusted bytes and all
currency/rate values are persisted as decimal text rather than binary floats.
"""
from __future__ import annotations

from contextlib import contextmanager
import base64
import binascii
import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sqlite3
import struct
import tempfile
from typing import Any, Iterable, Mapping, Sequence
import uuid
import zipfile
import zlib
from xml.etree import ElementTree as ET


AIRFREIGHT_PARSER_VERSION = "airfreight-native-1.0"
AIRFREIGHT_RULE_VERSION = "airfreight-demo-rules-1"
OCR_BASELINE = "builtin-demo-text-1 (仅识别合成夹具文本；其他图片转人工)"
DEMO_SCENARIO = "airfreight-local-demo-v1"
DEMO_ACCOUNT = "demo-account-1"
DEMO_SNAPSHOT = "demo-snapshot-20260903"
DEMO_CONVERSATION = "demo-conversation-cosplay-1"
DEMO_CONVERSATION_NAME = "中技AI cosplay"
WEIGHT_LABELS = ("+45", "+100", "+300", "+500", "+1000")
WEIGHT_LIMITS = (Decimal("45"), Decimal("100"), Decimal("300"), Decimal("500"), Decimal("1000"))

# The server, rather than the browser, owns this map.  It is deliberately
# small and declarative: it makes it possible to reject a client-provided
# future step before that request can reach a business operation.
DEMO_STEPS: dict[str, tuple[str, ...]] = {
    "A": (
        "开始每日索价演示", "模拟索价与供应商回复", "接收多文件回复", "逐文件解析",
        "查看字段证据", "跨文件查重", "人工审核", "发布最新价卡", "查看发布结果",
    ),
    "B": (
        "选择本地群聊数据", "选择账号、目标群和时间", "预览完整性", "确认导入并分析",
        "查看聊天证据", "解析询价与多票截图", "处理待确认项", "计算计费重",
        "匹配最新价卡", "生成内部测算", "人工确认报价", "生成报价预览",
    ),
}

# These are pause categories, not a permission bypass.  A user action is
# still represented by an explicit execute/retry request with a current state
# version and a unique idempotency key.
MANUAL_DEMO_STEPS = {
    ("A", 7), ("A", 8),
    ("B", 1), ("B", 2), ("B", 4), ("B", 7), ("B", 9), ("B", 11),
}


class AirfreightOperationError(Exception):
    """Stable, caller-readable error for the local airfreight API."""

    def __init__(self, error_code: str, message: str, *, http_status: int = 422,
                 details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.http_status = http_status
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        result = {"error_code": self.error_code, "message": self.message}
        result.update(self.details)
        return result


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return default


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _decimal_text(value: Any) -> str | None:
    parsed = _decimal(value)
    if parsed is None:
        return None
    return format(parsed.normalize(), "f")


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_name(value: Any) -> str:
    name = Path(str(value or "file")).name
    name = re.sub(r"[^\w.()\-一-龥 ]+", "_", name, flags=re.UNICODE).strip(" .")
    return (name or "file")[:160]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mask_path(value: str | None) -> str | None:
    if not value:
        return value
    if value.startswith("demo://"):
        return value
    return "本地附件（路径已隐藏）"


def _xml_text(element: ET.Element) -> str:
    return "".join(element.itertext()).strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)


_FONT_5X7: dict[str, tuple[int, ...]] = {
    " ": (0, 0, 0, 0, 0, 0, 0), "-": (0, 0, 0, 31, 0, 0, 0), "/": (1, 2, 4, 8, 16, 0, 0),
    ":": (0, 4, 0, 0, 4, 0, 0), ".": (0, 0, 0, 0, 0, 6, 6), "+": (0, 4, 4, 31, 4, 4, 0),
    "0": (14, 17, 19, 21, 25, 17, 14), "1": (4, 12, 4, 4, 4, 4, 14),
    "2": (14, 17, 1, 2, 4, 8, 31), "3": (30, 1, 1, 14, 1, 1, 30),
    "4": (2, 6, 10, 18, 31, 2, 2), "5": (31, 16, 16, 30, 1, 1, 30),
    "6": (14, 16, 16, 30, 17, 17, 14), "7": (31, 1, 2, 4, 8, 8, 8),
    "8": (14, 17, 17, 14, 17, 17, 14), "9": (14, 17, 17, 15, 1, 1, 14),
    "A": (14, 17, 17, 31, 17, 17, 17), "B": (30, 17, 17, 30, 17, 17, 30),
    "C": (14, 17, 16, 16, 16, 17, 14), "D": (30, 17, 17, 17, 17, 17, 30),
    "E": (31, 16, 16, 30, 16, 16, 31), "F": (31, 16, 16, 30, 16, 16, 16),
    "G": (14, 17, 16, 23, 17, 17, 15), "H": (17, 17, 17, 31, 17, 17, 17),
    "I": (14, 4, 4, 4, 4, 4, 14), "J": (7, 2, 2, 2, 18, 18, 12),
    "K": (17, 18, 20, 24, 20, 18, 17), "L": (16, 16, 16, 16, 16, 16, 31),
    "M": (17, 27, 21, 21, 17, 17, 17), "N": (17, 25, 21, 19, 17, 17, 17),
    "O": (14, 17, 17, 17, 17, 17, 14), "P": (30, 17, 17, 30, 16, 16, 16),
    "Q": (14, 17, 17, 17, 21, 18, 13), "R": (30, 17, 17, 30, 20, 18, 17),
    "S": (15, 16, 16, 14, 1, 1, 30), "T": (31, 4, 4, 4, 4, 4, 4),
    "U": (17, 17, 17, 17, 17, 17, 14), "V": (17, 17, 17, 17, 17, 10, 4),
    "W": (17, 17, 17, 21, 21, 21, 10), "X": (17, 17, 10, 4, 10, 17, 17),
    "Y": (17, 17, 10, 4, 4, 4, 4), "Z": (31, 1, 2, 4, 8, 16, 31),
}


def _demo_png(text: str) -> bytes:
    """Build a legible local cargo screenshot without an image dependency."""
    width, height = 1200, 720
    pixels = bytearray(bytes((246, 249, 252, 255)) * width * height)

    def rect(x: int, y: int, w: int, h: int, color: tuple[int, int, int, int]) -> None:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(width, x + w), min(height, y + h)
        row = bytes(color) * max(0, x1 - x0)
        for py in range(y0, y1):
            start = (py * width + x0) * 4
            pixels[start:start + len(row)] = row

    def label(x: int, y: int, value: str, scale: int = 3,
              color: tuple[int, int, int, int] = (25, 42, 61, 255)) -> None:
        cursor = x
        for char in value.upper():
            glyph = _FONT_5X7.get(char, _FONT_5X7[" "])
            for gy, bits in enumerate(glyph):
                for gx in range(5):
                    if bits & (1 << (4 - gx)):
                        rect(cursor + gx * scale, y + gy * scale, scale, scale, color)
            cursor += 6 * scale

    rect(0, 0, width, 92, (18, 78, 132, 255))
    label(42, 27, "AIR FREIGHT CARGO INQUIRY", 5, (255, 255, 255, 255))
    label(916, 34, "DEMO / NOT SENT", 3, (255, 224, 158, 255))
    rect(42, 128, 536, 516, (255, 255, 255, 255))
    rect(622, 128, 536, 516, (255, 255, 255, 255))
    rect(42, 128, 536, 54, (159, 196, 226, 255))
    rect(622, 128, 536, 54, (159, 196, 226, 255))
    label(66, 145, "TICKET 1 / HKG-ADD-BRU", 3)
    label(646, 145, "TICKET 2 / HKG-ADD-TLV", 3)
    left = ["MEAS CM  40 X 30 X 20", "CTNS     2 + 2", "GROSS    75 KG", "CBM      0.096", "CARGO    COSTUME SAMPLES"]
    right = ["MEAS CM  100 X 80 X 60", "CTNS     3", "GROSS    120 KG", "CBM      0.480", "CARGO    DISPLAY FIXTURES"]
    for index, line in enumerate(left):
        label(72, 224 + index * 68, line, 3)
        rect(66, 258 + index * 68, 480, 2, (220, 229, 237, 255))
    for index, line in enumerate(right):
        label(652, 224 + index * 68, line, 3)
        rect(646, 258 + index * 68, 480, 2, (220, 229, 237, 255))
    label(42, 676, "LOCAL SYNTHETIC EVIDENCE - TWO TICKETS KEPT SEPARATE", 2, (79, 96, 113, 255))
    raw = b"".join(b"\x00" + pixels[y * width * 4:(y + 1) * width * 4] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" +
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) +
            _png_chunk(b"tEXt", b"airfreight_demo\x00" + text.encode("utf-8")) +
            _png_chunk(b"IDAT", zlib.compress(raw)) + _png_chunk(b"IEND", b""))


def _pdf_text(value: Any) -> str:
    return str(value or "").encode("latin-1", "replace").decode("latin-1")


def _pdf_escape(value: Any) -> str:
    return _pdf_text(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_fit(value: Any, width: float, font_size: float) -> str:
    text = _pdf_text(value)
    limit = max(3, int(width / max(font_size * 0.53, 1)))
    return text if len(text) <= limit else text[:max(1, limit - 3)] + "..."


def _table_pdf(*, title: str, number: str, details: Sequence[str], columns: Sequence[tuple[str, float]],
               rows: Sequence[Sequence[Any]], notes: Sequence[str], marker: str) -> bytes:
    """Generate a small dependency-free landscape PDF with a real table."""
    page_width, page_height = 842.0, 595.0
    margin, row_height, header_height = 32.0, 24.0, 28.0
    table_width = sum(width for _label, width in columns)
    table_top = page_height - 110.0 - len(details) * 15.0
    rows_per_page = max(1, int((table_top - 100.0) // row_height))
    chunks = [list(rows[index:index + rows_per_page]) for index in range(0, len(rows), rows_per_page)] or [[]]

    def text_command(x: float, y: float, value: Any, *, size: float = 10.0,
                     font: str = "F1", color: tuple[float, float, float] = (0, 0, 0)) -> str:
        return f"{color[0]} {color[1]} {color[2]} rg BT /{font} {size:g} Tf {x:g} {y:g} Td ({_pdf_escape(value)}) Tj ET"

    streams: list[bytes] = []
    for page_index, page_rows in enumerate(chunks, 1):
        commands = [
            "0.071 0.306 0.518 rg 0 535 842 60 re f",
            text_command(margin, 560, title, size=18, font="F2", color=(1, 1, 1)),
            text_command(margin, 543, "ZHONGJI LOGISTICS | AIR FREIGHT", size=9, color=(0.86, 0.93, 0.98)),
            text_command(675, 558, "DEMO - NOT SENT", size=10, font="F2", color=(1, 0.83, 0.45)),
            text_command(675, 542, f"{number}  |  {page_index}/{len(chunks)}", size=8, color=(0.88, 0.93, 0.98)),
        ]
        y = 516.0
        for detail in details:
            commands.append(text_command(margin, y, detail, size=11, font="F2" if y == 516 else "F1", color=(0.05, 0.25, 0.48)))
            y -= 15.0
        y -= 8.0
        x = margin
        for label_text, width in columns:
            commands.extend([
                f"0.62 0.77 0.89 rg {x:g} {y - header_height:g} {width:g} {header_height:g} re f",
                f"0 0 0 RG 0.55 w {x:g} {y - header_height:g} {width:g} {header_height:g} re S",
                text_command(x + 5, y - 18, _pdf_fit(label_text, width - 10, 10), size=10, font="F2"),
            ])
            x += width
        y -= header_height
        for row_index, row in enumerate(page_rows):
            x = margin
            if row_index % 2:
                commands.append(f"0.965 0.975 0.985 rg {margin:g} {y - row_height:g} {table_width:g} {row_height:g} re f")
            for cell, (_label_text, width) in zip(row, columns):
                commands.extend([
                    f"0 0 0 RG 0.45 w {x:g} {y - row_height:g} {width:g} {row_height:g} re S",
                    text_command(x + 5, y - 16, _pdf_fit(cell, width - 10, 9), size=9, font="F2"),
                ])
                x += width
            y -= row_height
        note_y = 55.0
        commands.append("0.96 0.97 0.98 rg 32 22 778 48 re f")
        for note in notes[:3]:
            commands.append(text_command(40, note_y, note, size=8, color=(0.25, 0.31, 0.37)))
            note_y -= 12
        streams.append(("\n".join(commands) + "\n").encode("latin-1", "replace"))

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # pages tree filled after page/content object numbers are known
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    ]
    page_ids: list[int] = []
    for stream in streams:
        page_id = len(objects) + 1
        content_id = page_id + 1
        page_ids.append(page_id)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 842 595] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R >>".encode("ascii"))
        objects.append(b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"endstream")
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii")
    body = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n% " + marker.encode("ascii", "ignore") + b"\n")
    offsets = [0]
    for object_id, obj in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{object_id} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    body.extend(f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return bytes(body)


def _demo_pdf(marker: str) -> bytes:
    values = [
        ("BRU", "4.25", "3.85", "3.45", "3.15", "2.95"),
        ("BOM", "3.90", "3.50", "3.10", "2.80", "2.60"),
        ("DEL", "3.80", "3.40", "3.00", "2.70", "2.50"),
        ("MAA", "4.00", "3.60", "3.20", "2.90", "2.70"),
        ("TLV", "4.15", "3.75", "3.35", "3.05", "2.85"),
    ]
    return _table_pdf(
        title="ETHIOPIAN AIRLINES RATE SHEET", number="ET-HK-2026.08.19",
        details=("Origin: HKG  |  Hub: ADD  |  Valid: until further notice", "Currency: USD  |  Unit: KG"),
        columns=(("Destination", 118), ("+45", 90), ("+100", 90), ("+300", 90), ("+500", 90), ("+1000", 90)),
        rows=values, notes=("Rates are synthetic local demo data.", "Destination surcharges and space remain subject to confirmation."),
        marker=f"DEMO_{marker}",
    )


def _demo_xlsx() -> bytes:
    rows = [
        ["Destination", "+45", "+100", "+300", "+500", "+1000", "Currency", "Unit"],
        ["BRU", "4.25", "3.85", "3.45", "3.15", "2.95", "USD", "KG"],
        ["EZE", "5.25", "4.85", "4.45", "4.15", "3.95", "USD", "KG"],
    ]
    sheet_rows: list[str] = []
    for row_num, row in enumerate(rows, 1):
        cells: list[str] = []
        for col_num, value in enumerate(row, 1):
            col = ""
            n = col_num
            while n:
                n, rem = divmod(n - 1, 26)
                col = chr(65 + rem) + col
            cell_type = "inlineStr" if not _decimal(value) else "n"
            if cell_type == "inlineStr":
                cells.append(f'<c r="{col}{row_num}" t="inlineStr"><is><t>{value}</t></is></c>')
            else:
                cells.append(f'<c r="{col}{row_num}"><v>{value}</v></c>')
        sheet_rows.append(f'<row r="{row_num}">' + "".join(cells) + "</row>")
    # Keep a cached formula, a harmless merge relation, and a hidden demo row
    # in the fixture so the parser can prove that it reads structure/display
    # values without evaluating or expanding workbook instructions.
    sheet_rows[1] = sheet_rows[1].replace("</row>", '<c r="I2"><f>SUM(B2:F2)</f><v>17.65</v></c></row>')
    sheet_rows.append('<row r="4" hidden="1"><c r="J4" t="inlineStr"><is><t>hidden demo note</t></is></c></row>')
    workbook = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="ET Rates" sheetId="1" r:id="rId1"/></sheets></workbook>'''
    rels = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'''
    sheet = (b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>''' +
             "".join(sheet_rows).encode() + b'''</sheetData><mergeCells count="1"><mergeCell ref="A4:B4"/></mergeCells></worksheet>''')
    content_types = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'''
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return output.getvalue()


def _demo_docx() -> bytes:
    document = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"><w:body><w:p><w:r><w:t>ET 空运价卡条件：Small Box 与目的地附加费需人工确认。</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Destination</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Restriction</w:t></w:r></w:p></w:tc></w:tr><w:tr><w:tc><w:p><w:r><w:t>BLR/BOM/DEL/MAA</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>via ADD; over 1000 kgs inquiry</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="cargo-multi-ticket.png"/></wp:inline></w:drawing></w:r></w:p></w:body></w:document>'''
    document = document.encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>")
        archive.writestr("word/document.xml", document)
        archive.writestr("word/media/cargo-multi-ticket.png", _demo_png("DEMO_CARGO_MULTI_TICKET"))
    return output.getvalue()


ET_DESTINATIONS: dict[str, tuple[str, ...]] = {
    "BRU": ("4.25", "3.85", "3.45", "3.15", "2.95"),
    "EZE": ("5.25", "4.85", "4.45", "4.15", "3.95"),
    "GRU": ("5.05", "4.65", "4.25", "3.95", "3.75"),
    "JED": ("3.75", "3.35", "2.95", "2.65", "2.45"),
    "KWI": ("3.85", "3.45", "3.05", "2.75", "2.55"),
    "RUH": ("3.95", "3.55", "3.15", "2.85", "2.65"),
    "TLV": ("4.15", "3.75", "3.35", "3.05", "2.85"),
}


def _demo_csv_rows() -> str:
    rows = [["airline_name", "airline_code", "origin", "hub", "destination", "route", "effective_date", "currency", "unit", "weight_break_label", "amount"]]
    for destination, values in ET_DESTINATIONS.items():
        for label, value in zip(WEIGHT_LABELS, values):
            rows.append(["Qatar Airways", "QR", "HKG", "DOH", destination, f"HKG-DOH-{destination}", "2026-08-20", "USD", "KG", label, str(Decimal(value) + Decimal("0.55"))])
    return "\n".join(",".join(row) for row in rows) + "\n"


def _demo_conflict_csv() -> str:
    return "airline_name,airline_code,origin,hub,destination,route,effective_date,currency,unit,weight_break_label,amount\nEthiopian Airlines,ET-071,HKG,ADD,BRU,HKG-ADD-BRU,2026-08-19,USD,KG,+45,4.99\n"


def _demo_xls_xml() -> bytes:
    """Return an Excel-openable SpreadsheetML 2003 workbook."""
    rows = (
        ("Destination", "+45", "+100", "+300", "+500", "+1000", "Currency", "Unit"),
        ("BRU", "4.45", "4.05", "3.65", "3.35", "3.15", "USD", "KG"),
    )
    xml_rows = []
    for row in rows:
        cells = "".join(
            f'<Cell><Data ss:Type="{"Number" if _decimal(value) is not None else "String"}">{value}</Data></Cell>'
            for value in row
        )
        xml_rows.append(f"<Row>{cells}</Row>")
    return ('''<?xml version="1.0"?>
<?mso-application progid="Excel.Sheet"?>
<!-- DEMO_ET_XLS -->
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
 xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
 <DocumentProperties xmlns="urn:schemas-microsoft-com:office:office"><Title>ET demo rate sheet</Title></DocumentProperties>
 <Worksheet ss:Name="ET Rates"><Table>''' + "".join(xml_rows) + '''</Table></Worksheet>
</Workbook>''').encode("utf-8")


def demo_file_payloads() -> list[dict[str, Any]]:
    """Return fresh, openable deterministic demo files; no user data is bundled."""
    return [
        {"file_name": "ET-HK-2026.8.19.pdf", "data": _demo_pdf("ET_PDF"), "critical": True, "demo_role": "et_pdf", "reply_group": "rates"},
        {"file_name": "ET-HK-2026.8.19.xls", "data": _demo_xls_xml(), "critical": True, "demo_role": "et_xls", "reply_group": "rates"},
        {"file_name": "ET-HK-2026.8.19.xlsx", "data": _demo_xlsx(), "critical": False, "demo_role": "et_xlsx", "reply_group": "rates"},
        {"file_name": "ET-conditions.docx", "data": _demo_docx(), "critical": False, "demo_role": "conditions_docx", "reply_group": "terms"},
        {"file_name": "alternative-carrier.csv", "data": _demo_csv_rows().encode("utf-8"), "critical": False, "demo_role": "alternative_csv", "reply_group": "terms"},
        {"file_name": "same-key-conflict.csv", "data": _demo_conflict_csv().encode("utf-8"), "critical": True, "demo_role": "conflict_csv", "reply_group": "terms"},
        {"file_name": "cargo-multi-ticket.png", "data": _demo_png("DEMO_CARGO_MULTI_TICKET"), "critical": False, "demo_role": "cargo_image", "reply_group": "evidence"},
        {"file_name": "damaged-optional.pdf", "data": b"not a real pdf", "critical": False, "demo_role": "damaged", "reply_group": "evidence"},
    ]


AIRFREIGHT_SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batch (
    batch_id TEXT PRIMARY KEY,
    business_line TEXT NOT NULL DEFAULT 'airfreight',
    batch_kind TEXT NOT NULL,
    source_scope_json TEXT NOT NULL,
    request_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    total_files INTEGER NOT NULL DEFAULT 0,
    processed_files INTEGER NOT NULL DEFAULT 0,
    critical_failures INTEGER NOT NULL DEFAULT 0,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(business_line, request_id)
);
CREATE INDEX IF NOT EXISTS idx_air_import_batch_status ON import_batch(business_line, status, created_at);

CREATE TABLE IF NOT EXISTS source_artifact (
    artifact_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    business_line TEXT NOT NULL DEFAULT 'airfreight',
    file_name TEXT NOT NULL,
    actual_type TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    magic TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    local_ref TEXT,
    availability_status TEXT NOT NULL,
    security_status TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source_json TEXT NOT NULL DEFAULT '{}',
    preview_json TEXT NOT NULL DEFAULT '{}',
    is_critical INTEGER NOT NULL DEFAULT 0,
    UNIQUE(batch_id, sha256)
);
CREATE INDEX IF NOT EXISTS idx_air_artifact_batch ON source_artifact(batch_id, availability_status);

CREATE TABLE IF NOT EXISTS extraction_run (
    extraction_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    model_version TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    parser_path TEXT NOT NULL,
    page_count INTEGER,
    sheet_count INTEGER,
    warning_json TEXT NOT NULL DEFAULT '[]',
    error_json TEXT NOT NULL DEFAULT '[]',
    result_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_air_extraction_artifact ON extraction_run(artifact_id, started_at);

CREATE TABLE IF NOT EXISTS field_evidence (
    evidence_id TEXT PRIMARY KEY,
    business_line TEXT NOT NULL DEFAULT 'airfreight',
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    raw_value_json TEXT NOT NULL,
    normalized_value_json TEXT NOT NULL,
    locator_json TEXT NOT NULL,
    source_artifact_id TEXT,
    parser_version TEXT NOT NULL,
    confidence TEXT NOT NULL,
    extraction_method TEXT NOT NULL,
    original_value_json TEXT,
    corrected_value_json TEXT,
    corrected_by TEXT,
    corrected_at TEXT,
    correction_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_air_field_evidence_entity ON field_evidence(entity_type, entity_id, field_name);

CREATE TABLE IF NOT EXISTS rate_card_version (
    version_id TEXT PRIMARY KEY,
    business_line TEXT NOT NULL DEFAULT 'airfreight',
    supplier_name TEXT NOT NULL,
    airline_name TEXT NOT NULL,
    airline_code TEXT NOT NULL,
    origin_airport TEXT NOT NULL,
    hub_airport TEXT,
    version_date TEXT,
    document_updated_at TEXT,
    valid_from TEXT,
    valid_to TEXT,
    validity_mode TEXT NOT NULL DEFAULT 'explicit_range',
    currency TEXT,
    status TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'pending',
    published_current INTEGER NOT NULL DEFAULT 0,
    weight_rule_confirmed INTEGER NOT NULL DEFAULT 0,
    source_batch_id TEXT NOT NULL,
    source_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(business_line, source_batch_id, airline_code)
);
CREATE INDEX IF NOT EXISTS idx_air_rate_version_current ON rate_card_version(business_line, published_current, airline_code, valid_from);

CREATE TABLE IF NOT EXISTS route_rate (
    rate_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL,
    origin_airport TEXT NOT NULL,
    hub_airport TEXT,
    destination_airport TEXT NOT NULL,
    destination_city TEXT,
    routing TEXT NOT NULL,
    weight_break_label TEXT NOT NULL,
    amount_text TEXT,
    currency TEXT,
    pricing_unit TEXT,
    status TEXT NOT NULL,
    conflict_group_id TEXT,
    source_artifact_id TEXT,
    original_amount_text TEXT,
    selected_at TEXT,
    UNIQUE(version_id, destination_airport, weight_break_label)
);
CREATE INDEX IF NOT EXISTS idx_air_route_rate_lookup ON route_rate(destination_airport, weight_break_label, status);

CREATE TABLE IF NOT EXISTS rate_value_observation (
    observation_id TEXT PRIMARY KEY,
    rate_id TEXT,
    version_id TEXT NOT NULL,
    source_artifact_id TEXT NOT NULL,
    destination_airport TEXT NOT NULL,
    weight_break_label TEXT NOT NULL,
    amount_text TEXT,
    currency TEXT,
    pricing_unit TEXT,
    observed_at TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    locator_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_air_rate_observation_key ON rate_value_observation(version_id, destination_airport, weight_break_label);
CREATE UNIQUE INDEX IF NOT EXISTS idx_air_rate_observation_dedupe
ON rate_value_observation(version_id, source_artifact_id, destination_airport, weight_break_label);

CREATE TABLE IF NOT EXISTS conflict_group (
    conflict_group_id TEXT PRIMARY KEY,
    business_line TEXT NOT NULL DEFAULT 'airfreight',
    version_id TEXT NOT NULL,
    match_key_json TEXT NOT NULL,
    value_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution_reason TEXT
);

CREATE TABLE IF NOT EXISTS surcharge_rule (
    surcharge_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL,
    name TEXT NOT NULL,
    amount_text TEXT,
    currency TEXT,
    pricing_unit TEXT,
    charge_basis TEXT,
    minimum_charge_text TEXT,
    applicable_scope_json TEXT NOT NULL DEFAULT '{}',
    effective_from TEXT,
    status TEXT NOT NULL,
    original_text TEXT NOT NULL,
    source_artifact_id TEXT,
    evidence_id TEXT
);

CREATE TABLE IF NOT EXISTS route_restriction (
    restriction_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL,
    destination_airport TEXT NOT NULL,
    restriction_type TEXT NOT NULL,
    cargo_condition TEXT,
    document_requirement TEXT,
    original_text TEXT NOT NULL,
    status TEXT NOT NULL,
    source_artifact_id TEXT,
    evidence_id TEXT
);

CREATE TABLE IF NOT EXISTS internal_rate_rule (
    rule_id TEXT PRIMARY KEY,
    business_line TEXT NOT NULL DEFAULT 'airfreight',
    version_id TEXT NOT NULL,
    formula TEXT NOT NULL,
    adjustment_per_kg_text TEXT NOT NULL,
    currency TEXT,
    applicable_scope_json TEXT NOT NULL DEFAULT '{}',
    source_evidence_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending_confirmation',
    confirmed_by TEXT,
    confirmed_at TEXT,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS local_chat_import (
    import_id TEXT PRIMARY KEY,
    business_line TEXT NOT NULL DEFAULT 'airfreight',
    account_id TEXT NOT NULL,
    source_snapshot TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    conversation_name TEXT NOT NULL,
    target_root_message_id TEXT,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    preview_digest TEXT NOT NULL,
    preview_summary_json TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    confirmed_by TEXT,
    confirmed_at TEXT,
    status TEXT NOT NULL,
    analysis_status TEXT NOT NULL DEFAULT 'pending',
    request_id TEXT NOT NULL,
    UNIQUE(business_line, request_id)
);
CREATE INDEX IF NOT EXISTS idx_air_chat_import_scope ON local_chat_import(account_id, conversation_id, start_at, end_at);

CREATE TABLE IF NOT EXISTS airfreight_chat_message (
    row_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    source_snapshot TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    conversation_name TEXT NOT NULL,
    author_id TEXT NOT NULL,
    author_name TEXT NOT NULL,
    author_company TEXT,
    explicit_role TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    message_type TEXT NOT NULL,
    text TEXT NOT NULL,
    reply_to_message_id TEXT,
    parent_path_json TEXT NOT NULL DEFAULT '[]',
    attachment_refs_json TEXT NOT NULL DEFAULT '[]',
    availability_json TEXT NOT NULL DEFAULT '{}',
    source_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(import_id, message_id)
);

CREATE TABLE IF NOT EXISTS quote_request (
    quote_request_id TEXT PRIMARY KEY,
    import_id TEXT,
    business_line TEXT NOT NULL DEFAULT 'airfreight',
    quote_key TEXT NOT NULL,
    source_message_ids_json TEXT NOT NULL DEFAULT '[]',
    source_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
    origin_airport TEXT,
    destination_airport TEXT,
    routing TEXT,
    trade_term TEXT,
    status TEXT NOT NULL,
    confidence TEXT NOT NULL,
    missing_fields_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE(business_line, import_id, quote_key)
);

CREATE TABLE IF NOT EXISTS package_group (
    package_group_id TEXT PRIMARY KEY,
    quote_request_id TEXT NOT NULL,
    length_text TEXT,
    width_text TEXT,
    height_text TEXT,
    dimension_unit TEXT,
    dimension_semantics TEXT NOT NULL DEFAULT 'unknown',
    gross_weight_text TEXT,
    gross_weight_unit TEXT,
    gross_weight_semantics TEXT NOT NULL DEFAULT 'unknown',
    ctn_text TEXT,
    package_count_text TEXT,
    package_count_semantics TEXT NOT NULL DEFAULT 'unknown',
    cbm_text TEXT,
    cbm_semantics TEXT NOT NULL DEFAULT 'unknown',
    volume_weight_text TEXT,
    chargeable_weight_text TEXT,
    visual_group_id TEXT,
    source_json TEXT NOT NULL DEFAULT '{}',
    semantic_status TEXT NOT NULL DEFAULT 'needs_manual_review'
);

CREATE TABLE IF NOT EXISTS cargo_component (
    component_id TEXT PRIMARY KEY,
    quote_request_id TEXT NOT NULL,
    package_group_id TEXT NOT NULL,
    unit TEXT,
    qty_text TEXT,
    description TEXT,
    source_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS quote_option (
    option_id TEXT PRIMARY KEY,
    quote_request_id TEXT NOT NULL,
    option_no TEXT NOT NULL,
    airline_name TEXT NOT NULL,
    airline_code TEXT NOT NULL,
    air_freight_text TEXT,
    currency TEXT,
    pricing_unit TEXT,
    routing TEXT,
    frequency TEXT,
    transit_time TEXT,
    cargo_summary_json TEXT NOT NULL DEFAULT '{}',
    calculation_json TEXT NOT NULL DEFAULT '{}',
    rate_version_id TEXT,
    status TEXT NOT NULL DEFAULT 'candidate',
    confirmed_by TEXT,
    confirmed_at TEXT,
    UNIQUE(quote_request_id, option_no)
);

CREATE TABLE IF NOT EXISTS review_decision (
    decision_id TEXT PRIMARY KEY,
    business_line TEXT NOT NULL DEFAULT 'airfreight',
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_name TEXT NOT NULL,
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(business_line, idempotency_key, action)
);

CREATE TABLE IF NOT EXISTS demo_run (
    demo_id TEXT PRIMARY KEY,
    scenario TEXT NOT NULL,
    flow TEXT NOT NULL DEFAULT 'A',
    mode TEXT NOT NULL DEFAULT 'single_step',
    current_step INTEGER NOT NULL DEFAULT 0,
    selected_step INTEGER NOT NULL DEFAULT 0,
    last_completed_step INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ready',
    state_version INTEGER NOT NULL DEFAULT 0,
    completed_steps_json TEXT NOT NULL DEFAULT '[]',
    events_json TEXT NOT NULL DEFAULT '[]',
    scope_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    blocked_reason_json TEXT NOT NULL DEFAULT '{}',
    weight_rules_confirmed INTEGER NOT NULL DEFAULT 0,
    quote_confirmed INTEGER NOT NULL DEFAULT 0,
    reset_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO demo_run(demo_id,scenario,updated_at)
VALUES('airfreight','airfreight-local-demo-v1','1970-01-01T00:00:00+00:00');

-- Kept separate from the legacy compact events_json column.  This log is
-- append-only and records failed, blocked and retried attempts as facts.
CREATE TABLE IF NOT EXISTS demo_step_event (
    event_id TEXT PRIMARY KEY,
    demo_id TEXT NOT NULL,
    flow TEXT NOT NULL,
    step INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    action TEXT NOT NULL,
    previous_status TEXT NOT NULL,
    next_status TEXT NOT NULL,
    summary TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(demo_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_air_demo_step_event_run
ON demo_step_event(demo_id, attempt, created_at);

CREATE TABLE IF NOT EXISTS airfreight_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL
);
INSERT OR IGNORE INTO airfreight_settings(setting_key,setting_value) VALUES
    ('reviewer_id',''),('reviewer_name',''),('reviewer_role','空运价卡审核人');
"""


class AirfreightService:
    """Airfreight application service backed by the existing SQLite connection."""

    _preview_cache: dict[str, dict[str, Any]] = {}
    _artifact_cache: dict[str, bytes] = {}

    def __init__(self, storage: Any, *, initialize: bool = True,
                 source_db_paths: Sequence[str | Path] | None = None,
                 include_demo_fixtures: bool = False) -> None:
        self.storage = storage
        # Source databases are supplied by the local server configuration,
        # never by an HTTP request.  The browser only selects opaque source
        # records discovered from this fixed, read-only allow-list.
        requested_paths = list(source_db_paths or [storage.path])
        paths: list[Path] = []
        for requested in requested_paths:
            try:
                path = Path(requested).resolve()
            except (OSError, RuntimeError):
                continue
            if path not in paths:
                paths.append(path)
        self.source_db_paths = tuple(paths)
        # Synthetic records are useful for isolated tests only.  They are
        # intentionally opt-in so a production-like local page can never call
        # them "真实本地聊天" by accident.
        self.include_demo_fixtures = bool(include_demo_fixtures)
        if initialize:
            self.initialize()

    @property
    def _cache_prefix(self) -> str:
        return str(Path(self.storage.path).resolve())

    def initialize(self) -> None:
        with self.storage.connect() as conn:
            conn.executescript(AIRFREIGHT_SCHEMA)
            # ``CREATE TABLE IF NOT EXISTS`` cannot extend databases created
            # by the initial airfreight draft.  These additive columns retain
            # the old compact row and introduce isolated A/B state safely.
            existing = {row[1] for row in conn.execute("PRAGMA table_info(demo_run)")}
            additions = (
                ("mode", "TEXT NOT NULL DEFAULT 'single_step'"),
                ("selected_step", "INTEGER NOT NULL DEFAULT 0"),
                ("last_completed_step", "INTEGER NOT NULL DEFAULT 0"),
                ("status", "TEXT NOT NULL DEFAULT 'ready'"),
                ("state_version", "INTEGER NOT NULL DEFAULT 0"),
                ("scope_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("result_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("blocked_reason_json", "TEXT NOT NULL DEFAULT '{}'"),
            )
            for name, definition in additions:
                if name not in existing:
                    conn.execute(f"ALTER TABLE demo_run ADD COLUMN {name} {definition}")
            chat_import_columns = {row[1] for row in conn.execute("PRAGMA table_info(local_chat_import)")}
            if "target_root_message_id" not in chat_import_columns:
                conn.execute("ALTER TABLE local_chat_import ADD COLUMN target_root_message_id TEXT")
            conn.execute("INSERT OR IGNORE INTO demo_run(demo_id,scenario,updated_at) VALUES('airfreight',?,?)", (DEMO_SCENARIO, _iso_now()))
            self._has_airfreight_settings(conn)

    @staticmethod
    def _has_airfreight_settings(conn: Any) -> bool:
        # Kept as a tiny helper so older databases created during development
        # can be upgraded without assuming a particular SQLite row shape.
        conn.execute("""CREATE TABLE IF NOT EXISTS airfreight_settings(setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL)""")
        conn.execute("INSERT OR IGNORE INTO airfreight_settings(setting_key,setting_value) VALUES('reviewer_id','')")
        conn.execute("INSERT OR IGNORE INTO airfreight_settings(setting_key,setting_value) VALUES('reviewer_name','')")
        conn.execute("INSERT OR IGNORE INTO airfreight_settings(setting_key,setting_value) VALUES('reviewer_role','空运价卡审核人')")
        return True

    def _settings(self, conn: Any) -> dict[str, str]:
        try:
            return {row["setting_key"]: row["setting_value"] for row in conn.execute("SELECT setting_key,setting_value FROM airfreight_settings")}
        except Exception:
            return {"reviewer_id": "", "reviewer_name": "", "reviewer_role": "空运价卡审核人"}

    def settings(self) -> dict[str, Any]:
        with self.storage.connect() as conn:
            settings = self._settings(conn)
        return {
            "reviewer_configured": bool(settings.get("reviewer_id") and settings.get("reviewer_name")),
            "reviewer_id": settings.get("reviewer_id") or None,
            "reviewer_name": settings.get("reviewer_name") or None,
            "reviewer_role": settings.get("reviewer_role") or "空运价卡审核人",
            "external_visual_model": {"enabled": False, "provider": None, "model": None, "data_scope": "未配置；不会外传附件"},
            "ocr_baseline": OCR_BASELINE,
        }

    def configure_reviewer(self, reviewer_id: str, reviewer_name: str, *, role: str = "空运价卡审核人") -> dict[str, Any]:
        if not reviewer_id or not reviewer_name:
            raise AirfreightOperationError("reviewer_required", "审核身份必须同时包含 ID 和显示名")
        with self.storage.connect() as conn:
            self._settings(conn)
            conn.executemany("INSERT OR REPLACE INTO airfreight_settings(setting_key,setting_value) VALUES(?,?)", [
                ("reviewer_id", str(reviewer_id)[:120]), ("reviewer_name", str(reviewer_name)[:120]), ("reviewer_role", str(role)[:120]),
            ])
        return self.settings()

    def configure_demo_reviewer(self) -> dict[str, Any]:
        return self.configure_reviewer("demo-reviewer", "演示审核员")

    def _require_reviewer(self, actor_id: str | None, actor_name: str | None) -> tuple[str, str]:
        with self.storage.connect() as conn:
            settings = self._settings(conn)
        if not settings.get("reviewer_id") or not settings.get("reviewer_name"):
            raise AirfreightOperationError("reviewer_required", "尚未配置实际空运审核身份；当前只允许解析和预览", http_status=403)
        if actor_id != settings["reviewer_id"] or actor_name != settings["reviewer_name"]:
            raise AirfreightOperationError("reviewer_identity_mismatch", "请求身份与本地已配置空运审核身份不一致", http_status=403)
        return settings["reviewer_id"], settings["reviewer_name"]

    def _record_decision(self, conn: Any, *, object_type: str, object_id: str, action: str,
                         actor_id: str, actor_name: str, reason: str, idempotency_key: str,
                         before: Any = None, after: Any = None) -> dict[str, Any]:
        existing = conn.execute("SELECT * FROM review_decision WHERE business_line='airfreight' AND idempotency_key=? AND action=?", (idempotency_key, action)).fetchone()
        if existing:
            return {"decision_id": existing["decision_id"], "idempotent": True}
        decision_id = "decision_" + uuid.uuid4().hex
        conn.execute("""INSERT INTO review_decision(decision_id,object_type,object_id,action,actor_id,actor_name,reason,occurred_at,idempotency_key,before_json,after_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (decision_id, object_type, object_id, action, actor_id, actor_name, reason or "", _iso_now(), idempotency_key, _json(before or {}), _json(after or {})))
        return {"decision_id": decision_id, "idempotent": False}

    # ------------------------------------------------------------------
    # Demo state and deterministic files
    # ------------------------------------------------------------------
    @staticmethod
    def _demo_id(flow: str) -> str:
        return f"airfreight:{flow}"

    @staticmethod
    def _empty_demo_state(flow: str) -> dict[str, Any]:
        return {
            "demo_id": AirfreightService._demo_id(flow), "scenario": DEMO_SCENARIO,
            "flow": flow, "mode": "single_step", "current_step": 1,
            "selected_step": 1, "last_completed_step": 0, "completed_steps": [],
            "status": "ready", "state_version": 0, "scope": {}, "result": {},
            "blocked_reason": {}, "weight_rules_confirmed": False,
            "quote_confirmed": False, "reset_count": 0, "updated_at": None,
            "events": [],
        }

    def _demo_state_row(self, conn: Any, flow: str, *, create: bool = True) -> Any | None:
        demo_id = self._demo_id(flow)
        row = conn.execute("SELECT * FROM demo_run WHERE demo_id=?", (demo_id,)).fetchone()
        if row is None and create:
            now = _iso_now()
            conn.execute(
                """INSERT INTO demo_run(
                    demo_id,scenario,flow,mode,current_step,selected_step,last_completed_step,
                    status,state_version,completed_steps_json,events_json,scope_json,result_json,
                    blocked_reason_json,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (demo_id, DEMO_SCENARIO, flow, "single_step", 1, 1, 0, "ready", 0,
                 "[]", "[]", "{}", "{}", "{}", now),
            )
            row = conn.execute("SELECT * FROM demo_run WHERE demo_id=?", (demo_id,)).fetchone()
        return row

    @staticmethod
    def _demo_state(row: Any, events: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        if row is None:
            raise ValueError("demo state row is required")
        state = {
            "demo_id": row["demo_id"], "scenario": row["scenario"], "flow": row["flow"],
            "mode": row["mode"] or "single_step", "current_step": int(row["current_step"] or 1),
            "selected_step": int(row["selected_step"] or row["current_step"] or 1),
            "last_completed_step": int(row["last_completed_step"] or 0),
            "completed_steps": _loads(row["completed_steps_json"], []),
            "status": row["status"] or "ready", "state_version": int(row["state_version"] or 0),
            "scope": _loads(row["scope_json"], {}), "result": _loads(row["result_json"], {}),
            "blocked_reason": _loads(row["blocked_reason_json"], {}),
            "weight_rules_confirmed": bool(row["weight_rules_confirmed"]),
            "quote_confirmed": bool(row["quote_confirmed"]), "reset_count": int(row["reset_count"] or 0),
            "updated_at": row["updated_at"],
        }
        state["events"] = [dict(item) for item in events] if events is not None else _loads(row["events_json"], [])
        return state

    def _events_for_run(self, conn: Any, demo_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = conn.execute(
            """SELECT event_id,flow,step,attempt,action,previous_status,next_status,summary,
                      result_json,evidence_json,error_code,idempotency_key,created_at
               FROM demo_step_event WHERE demo_id=? ORDER BY created_at,event_id LIMIT ?""",
            (demo_id, max(1, min(limit, 250))),
        ).fetchall()
        return [
            dict(row) | {"result": _loads(row["result_json"], {}), "evidence": _loads(row["evidence_json"], {})}
            for row in rows
        ]

    def _read_demo_state(self, conn: Any, flow: str, *, create: bool = False) -> dict[str, Any]:
        row = self._demo_state_row(conn, flow, create=create)
        if row is None:
            return self._empty_demo_state(flow)
        return self._demo_state(row, self._events_for_run(conn, row["demo_id"]))

    def _demo_outcome(self, conn: Any, state: Mapping[str, Any]) -> dict[str, Any]:
        """Return a read-only, run-bound summary for the completion card.

        It deliberately starts from the IDs bound to this run instead of the
        dashboard's historical totals, so a completed replay cannot borrow
        unrelated files, quotes or rate cards.
        """
        result = state.get("result") or {}
        batch_id = str(result.get("batch_id") or "")
        import_id = str(result.get("import_id") or "")
        outcome: dict[str, Any] = {
            "batch_id": batch_id or None,
            "import_id": import_id or None,
            "source_data_mode": None,
            "source_disclaimer": None,
            "processed_files": 0,
            "failed_files": 0,
            "open_conflicts": 0,
            "published_rate_cards": 0,
            "published_route_rates": 0,
            "quote_requests": 0,
            "calculated_quote_requests": 0,
            "quote_options": 0,
            "unresolved_risks": 0,
            "risk_reasons": [],
        }
        if batch_id:
            files = conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN COALESCE((
                              SELECT er.status FROM extraction_run er
                              WHERE er.artifact_id=sa.artifact_id
                              ORDER BY er.started_at DESC, er.extraction_id DESC LIMIT 1
                          ), 'pending') <> 'pending' THEN 1 ELSE 0 END) AS processed,
                          SUM(CASE WHEN COALESCE((
                              SELECT er.status FROM extraction_run er
                              WHERE er.artifact_id=sa.artifact_id
                              ORDER BY er.started_at DESC, er.extraction_id DESC LIMIT 1
                          ), 'pending') IN ('rejected','needs_manual_review') THEN 1 ELSE 0 END) AS incomplete
                   FROM source_artifact sa WHERE batch_id=?""",
                (batch_id,),
            ).fetchone()
            outcome["processed_files"] = int(files["processed"] or 0) if files else 0
            outcome["failed_files"] = int(files["incomplete"] or 0) if files else 0
            conflict = conn.execute(
                "SELECT COUNT(*) FROM conflict_group WHERE version_id IN (SELECT version_id FROM rate_card_version WHERE source_batch_id=?) AND status='open'",
                (batch_id,),
            ).fetchone()[0]
            outcome["open_conflicts"] = int(conflict)
            outcome["published_rate_cards"] = int(conn.execute(
                "SELECT COUNT(*) FROM rate_card_version WHERE source_batch_id=? AND review_status='published'", (batch_id,)
            ).fetchone()[0])
            outcome["published_route_rates"] = int(conn.execute(
                "SELECT COUNT(*) FROM route_rate WHERE version_id IN (SELECT version_id FROM rate_card_version WHERE source_batch_id=?) AND status='published'", (batch_id,)
            ).fetchone()[0])
            if outcome["failed_files"]:
                outcome["risk_reasons"].append("批次含拒绝或需人工复核的文件")
            if outcome["open_conflicts"]:
                outcome["risk_reasons"].append("仍有未解决的跨文件冲突")
        if import_id:
            outcome["quote_requests"] = int(conn.execute(
                "SELECT COUNT(*) FROM quote_request WHERE import_id=?", (import_id,)
            ).fetchone()[0])
            outcome["calculated_quote_requests"] = int(conn.execute(
                "SELECT COUNT(*) FROM quote_request WHERE import_id=? AND status IN ('calculated','matched')", (import_id,)
            ).fetchone()[0])
            outcome["quote_options"] = int(conn.execute(
                """SELECT COUNT(*) FROM quote_option
                   WHERE quote_request_id IN (SELECT quote_request_id FROM quote_request WHERE import_id=?)
                     AND status='confirmed'""",
                (import_id,),
            ).fetchone()[0])
            imported = conn.execute("SELECT preview_summary_json FROM local_chat_import WHERE import_id=?", (import_id,)).fetchone()
            summary = _loads(imported["preview_summary_json"], {}) if imported else {}
            if summary.get("fixture"):
                outcome["source_data_mode"] = "synthetic_demo"
                outcome["source_disclaimer"] = "当前群聊与询价字段来自明确标注的合成演示数据，不代表真实企微记录或正式报价。"
            else:
                outcome["source_data_mode"] = "real_local"
                if summary.get("default_target_root_status") not in (None, "", "uniquely_located", "selected_nondefault_range"):
                    outcome["risk_reasons"].append("默认真实目标存在定位或完整性缺口")
                if int(summary.get("referenced_but_unavailable_count") or 0):
                    outcome["risk_reasons"].append("存在仅引用、未取得本地副本的附件")
                if int(summary.get("forward_parse_gap_count") or 0):
                    outcome["risk_reasons"].append("存在未完整展开的嵌套转发")
        outcome["unresolved_risks"] = len(outcome["risk_reasons"])
        return outcome

    def demo_state(self, flow: str | None = None) -> dict[str, Any]:
        selected_flow = str(flow or "A").upper()
        if selected_flow not in DEMO_STEPS:
            raise AirfreightOperationError("invalid_demo_flow", "演示流程无效", http_status=400)
        # GET remains a true read: a first page load returns the deterministic
        # empty state instead of silently creating a run.
        with self.storage.connect() as conn:
            flows = {key: self._read_demo_state(conn, key, create=False) for key in DEMO_STEPS}
            for item in flows.values():
                item["outcome"] = self._demo_outcome(conn, item)
        return {
            "active_flow": selected_flow, "flows": flows, "runs": flows,
            "settings": self.settings(), "batch": self._latest_demo_batch(),
            "step_names": {key: list(value) for key, value in DEMO_STEPS.items()},
        }

    def _append_demo_event(self, conn: Any, *, state: Mapping[str, Any], step: int,
                           action: str, previous_status: str, next_status: str,
                           summary: str, result: Mapping[str, Any] | None,
                           evidence: Mapping[str, Any] | None, error_code: str | None,
                           idempotency_key: str) -> dict[str, Any]:
        existing = conn.execute(
            "SELECT * FROM demo_step_event WHERE demo_id=? AND idempotency_key=?",
            (state["demo_id"], idempotency_key),
        ).fetchone()
        if existing:
            return dict(existing) | {"idempotent": True}
        attempt = conn.execute(
            "SELECT COUNT(*) FROM demo_step_event WHERE demo_id=? AND step=? AND action IN ('execute','retry')",
            (state["demo_id"], step),
        ).fetchone()[0] + 1
        event_id = "demo_event_" + uuid.uuid4().hex
        conn.execute(
            """INSERT INTO demo_step_event(
                event_id,demo_id,flow,step,attempt,action,previous_status,next_status,summary,
                result_json,evidence_json,error_code,idempotency_key,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, state["demo_id"], state["flow"], step, attempt, action,
             previous_status, next_status, summary, _json(result or {}),
             _json(evidence or {}), error_code, idempotency_key, _iso_now()),
        )
        return {"event_id": event_id, "attempt": attempt, "idempotent": False}

    @staticmethod
    def _require_state_version(state: Mapping[str, Any], value: Any) -> int:
        try:
            expected = int(value)
        except (TypeError, ValueError) as exc:
            raise AirfreightOperationError("state_version_required", "演示操作必须携带当前 state_version", http_status=409) from exc
        if expected != int(state["state_version"]):
            raise AirfreightOperationError(
                "stale_state_version", "演示状态已变化，请刷新后重试", http_status=409,
                details={"expected_state_version": expected, "current_state_version": state["state_version"]},
            )
        return expected

    def _persist_demo_state(self, conn: Any, state: Mapping[str, Any], *,
                            status: str | None = None, current_step: int | None = None,
                            selected_step: int | None = None, last_completed_step: int | None = None,
                            completed_steps: Sequence[int] | None = None, mode: str | None = None,
                            scope: Mapping[str, Any] | None = None, result: Mapping[str, Any] | None = None,
                            blocked_reason: Mapping[str, Any] | None = None,
                            weight_rules_confirmed: bool | None = None,
                            quote_confirmed: bool | None = None) -> None:
        values = {
            "status": status if status is not None else state["status"],
            "current_step": current_step if current_step is not None else state["current_step"],
            "selected_step": selected_step if selected_step is not None else state["selected_step"],
            "last_completed_step": last_completed_step if last_completed_step is not None else state["last_completed_step"],
            "completed_steps_json": _json(list(completed_steps) if completed_steps is not None else state["completed_steps"]),
            "mode": mode if mode is not None else state["mode"],
            "scope_json": _json(dict(scope) if scope is not None else state["scope"]),
            "result_json": _json(dict(result) if result is not None else state["result"]),
            "blocked_reason_json": _json(dict(blocked_reason) if blocked_reason is not None else state["blocked_reason"]),
            "weight_rules_confirmed": int(weight_rules_confirmed if weight_rules_confirmed is not None else state["weight_rules_confirmed"]),
            "quote_confirmed": int(quote_confirmed if quote_confirmed is not None else state["quote_confirmed"]),
            "updated_at": _iso_now(),
        }
        conn.execute(
            """UPDATE demo_run SET mode=:mode,current_step=:current_step,selected_step=:selected_step,
                 last_completed_step=:last_completed_step,status=:status,
                 state_version=state_version+1,completed_steps_json=:completed_steps_json,
                 scope_json=:scope_json,result_json=:result_json,blocked_reason_json=:blocked_reason_json,
                 weight_rules_confirmed=:weight_rules_confirmed,quote_confirmed=:quote_confirmed,
                 updated_at=:updated_at WHERE demo_id=:demo_id""",
            values | {"demo_id": state["demo_id"]},
        )

    def _operation_response(self, conn: Any, flow: str, *, idempotent: bool = False,
                            result: Mapping[str, Any] | None = None) -> dict[str, Any]:
        state = self._read_demo_state(conn, flow, create=False)
        return {"flow": flow, "state": state, "result": dict(result or state.get("result") or {}), "idempotent": idempotent}

    def reset_demo(self) -> dict[str, Any]:
        with self.storage.connect() as conn:
            for flow in DEMO_STEPS:
                state = self._read_demo_state(conn, flow, create=True)
                reset_count = int(state["reset_count"]) + 1
                conn.execute("DELETE FROM demo_step_event WHERE demo_id=?", (state["demo_id"],))
                conn.execute(
                    """UPDATE demo_run SET mode='single_step',current_step=1,selected_step=1,
                         last_completed_step=0,status='ready',state_version=state_version+1,
                         completed_steps_json='[]',events_json='[]',scope_json='{}',result_json='{}',
                         blocked_reason_json='{}',weight_rules_confirmed=0,quote_confirmed=0,
                         reset_count=?,updated_at=? WHERE demo_id=?""",
                    (reset_count, _iso_now(), state["demo_id"]),
                )
            result = {key: self._read_demo_state(conn, key, create=False) for key in DEMO_STEPS}
        return {
            "flows": result, "raw_evidence_preserved": True,
            "message": "仅重置演示运行状态和轨迹；真实来源、原始附件、候选、审核、价卡和报价证据保留。",
        }

    def _latest_demo_batch(self) -> dict[str, Any] | None:
        with self.storage.connect() as conn:
            row = conn.execute("SELECT batch_id FROM import_batch WHERE batch_kind='demo_rate_card' ORDER BY created_at DESC LIMIT 1").fetchone()
        return self.get_batch(row["batch_id"]) if row else None

    def ensure_demo_batch(self) -> dict[str, Any]:
        with self.storage.connect() as conn:
            row = conn.execute("SELECT batch_id FROM import_batch WHERE batch_kind='demo_rate_card' AND request_id=? ORDER BY created_at DESC LIMIT 1", ("demo-rate-card-v2",)).fetchone()
        if row:
            return self.get_batch(row["batch_id"])
        return self.create_batch(files=demo_file_payloads(), batch_kind="demo_rate_card", source_scope={"source": "脱敏合成夹具", "account_id": DEMO_ACCOUNT}, request_id="demo-rate-card-v2")

    def _batch_from_state(self, state: Mapping[str, Any]) -> dict[str, Any]:
        batch_id = (state.get("result") or {}).get("batch_id")
        if not batch_id:
            raise AirfreightOperationError("demo_batch_required", "请先完成“接收多文件回复”步骤", http_status=409)
        return self.get_batch(str(batch_id))

    def _import_from_state(self, state: Mapping[str, Any]) -> str:
        import_id = (state.get("result") or {}).get("import_id")
        if not import_id:
            raise AirfreightOperationError("chat_import_required", "请先确认当前本地群聊范围导入", http_status=409)
        return str(import_id)

    def _run_demo_business_step(self, flow: str, step: int, state: Mapping[str, Any],
                                payload: Mapping[str, Any], *, actor_id: str | None,
                                actor_name: str | None, idempotency_key: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Run one bounded business action and return status/output/pause detail.

        The surrounding transition writes a running event first and always
        appends a terminal event afterwards, including recoverable failures.
        """
        action = DEMO_STEPS[flow][step - 1]
        if flow == "A":
            if step == 1:
                return "succeeded", {"action": action, "send_mode": "simulation_only", "real_wecom_write": False,
                                      "supplier": "Ethiopian Airlines / ET-071", "message": "演示模拟：不会连接、发送或写入真实企微。"}, {}
            if step == 2:
                return "succeeded", {"action": action, "messages": [
                    {"kind": "simulation_message", "role": "zhongji", "text": "演示模拟：@供应商，请提供 HKG-ADD 最新空运价卡。"},
                    {"kind": "simulation_message", "role": "supplier", "text": "演示模拟：已收到本地价表回复，不会发送企微。"},
                ], "real_wecom_write": False}, {}
            batch = self._batch_from_state(state) if step >= 4 else None
            if step == 3:
                batch = self.ensure_demo_batch()
                group_meta = {
                    "rates": {"author_name": "ET Pricing Desk", "author_company": "Ethiopian Airlines", "sent_at": "09:03", "message": "Please find the latest HKG export rate sheets attached."},
                    "terms": {"author_name": "ET Pricing Desk", "author_company": "Ethiopian Airlines", "sent_at": "09:05", "message": "Additional conditions and an alternative-carrier comparison are attached."},
                    "evidence": {"author_name": "Forwarding Agent", "author_company": "Local Demo Supplier", "sent_at": "09:07", "message": "Cargo screenshot attached. One optional PDF is damaged so the isolation behavior remains visible."},
                }
                replies = []
                for group_key in ("rates", "terms", "evidence"):
                    files = [item for item in batch.get("artifacts", []) if item.get("source", {}).get("reply_group") == group_key]
                    replies.append({"reply_group": group_key, **group_meta[group_key], "files": files})
                return "succeeded", {"action": action, "batch_id": batch["batch_id"], "files": batch.get("artifacts", []),
                                      "supplier_replies": replies, "source_kind": "脱敏合成夹具／演示模拟"}, {}
            if step == 4:
                parsed = self.parse_batch(batch["batch_id"])
                return "succeeded", {"action": action, "batch_id": batch["batch_id"], "batch": parsed}, {}
            if step == 5:
                return "succeeded", {"action": action, "batch_id": batch["batch_id"], "batch": self.get_batch(batch["batch_id"])}, {}
            if step == 6:
                conflicts = self.list_conflicts(batch["batch_id"])
                output = {"action": action, "batch_id": batch["batch_id"], "conflicts": conflicts,
                          "duplicate_policy": "金额不属于可比键；同键不同值进入冲突组"}
                if conflicts:
                    return "needs_manual_decision", output, {"reason": "存在同键不同金额或日期冲突", "who": "已配置空运审核人", "evidence": "冲突组与原始字段证据", "next_action": "在待审核与证据中选择来源后重试当前步骤"}
                return "succeeded", output, {}
            if step == 7:
                reviewer = self._require_reviewer(actor_id, actor_name)
                conflicts = self.list_conflicts(batch["batch_id"])
                if conflicts:
                    return "needs_manual_decision", {"action": action, "conflicts": conflicts}, {"reason": "冲突尚未解决", "who": reviewer[1], "evidence": "冲突候选", "next_action": "选择来源或修正金额"}
                return "succeeded", {"action": action, "reviewer": {"id": reviewer[0], "name": reviewer[1]}, "review_recorded": True}, {}
            if step == 8:
                reviewer = self._require_reviewer(actor_id, actor_name)
                conflicts = self.list_conflicts(batch["batch_id"])
                if conflicts:
                    return "needs_manual_decision", {"action": action, "conflicts": conflicts}, {"reason": "冲突尚未解决，禁止发布", "who": reviewer[1], "evidence": "冲突候选", "next_action": "解决冲突"}
                published: list[dict[str, Any]] = []
                for version in self.list_rate_cards(batch_id=batch["batch_id"], view="candidates")["items"]:
                    if version["review_status"] == "published":
                        published.append(version)
                    else:
                        published.append(self.publish_rate_card(
                            version["version_id"], actor_id=reviewer[0], actor_name=reviewer[1],
                            reason=str(payload.get("reason") or "演示人工审核后发布"),
                            idempotency_key=f"{idempotency_key}:publish:{version['version_id']}",
                        ))
                return "succeeded", {"action": action, "published": published, "source_preserved": True}, {}
            return "succeeded", {"action": action, "published_rate_cards": self.list_rate_cards(view="current"), "completed": True}, {}

        # Flow B discovers configured, legal, read-only local sources.  A
        # separately labeled synthetic route is available only after an
        # explicit presenter choice; it never alters a real import in place.
        if step == 1:
            return "succeeded", {"action": action, "sources": self.list_chat_sources(), "no_database_write": True}, {}
        if step == 2:
            requested_scope = payload.get("scope")
            if not isinstance(requested_scope, Mapping):
                return "needs_manual_decision", {"action": action, "sources": self.list_chat_sources()}, {
                    "reason": "尚未选择账号、来源快照、会话和时间范围", "who": "演示者",
                    "evidence": "本机来源元数据", "next_action": "明确选择一个来源范围",
                }
            resolved = self.resolve_chat_scope(requested_scope)
            return "succeeded", {"action": action, "scope": resolved["scope"], "selection": resolved["selection"],
                                  "default_target_status": resolved["default_target_status"]}, {}
        if step == 3:
            scope = state.get("scope") or (state.get("result") or {}).get("scope")
            if not scope:
                raise AirfreightOperationError("chat_scope_required", "请先明确选择本地群聊范围", http_status=409)
            preview = self.preview_chat(scope)
            # Message bodies are returned only by the read-only preview API.
            # Keep the persisted demo event to its bound summary and pointers.
            stored_preview = {key: value for key, value in preview.items() if key != "messages"}
            return "succeeded", {"action": action, "preview": stored_preview, "scope": preview["scope"]}, {}
        if step == 4:
            prior = state.get("result") or {}
            preview = prior.get("preview") or {}
            if not preview:
                raise AirfreightOperationError("preview_confirmation_required", "请先完成当前范围完整性预览", http_status=409)
            if payload.get("confirm") is not True:
                return "needs_manual_decision", {"action": action, "preview": preview}, {
                    "reason": "导入会写入本地演示分析库，必须由用户确认", "who": "演示者",
                    "evidence": "预览摘要、账号、会话、来源快照和时间范围", "next_action": "勾选并确认导入",
                }
            imported = self.confirm_chat_import({
                "preview_id": preview.get("preview_id"), "preview_digest": preview.get("preview_digest"),
                "scope": preview.get("scope"), "idempotency_key": f"{idempotency_key}:chat-import",
            }, actor_id=actor_id, actor_name=actor_name)
            # ``get_chat_import`` deliberately returns messages for the
            # controlled evidence route.  A demo run and its append-only
            # event log must keep only a pointer and a non-body summary, or
            # a later status read would duplicate sensitive chat content.
            return "succeeded", {
                "action": action,
                "import_id": imported["import_id"],
                "import_summary": {
                    "status": imported["status"],
                    "conversation_name": imported["conversation_name"],
                    "start_at": imported["start_at"],
                    "end_at": imported["end_at"],
                    "target_root_message_id": imported.get("target_root_message_id"),
                    "preview_summary": imported.get("preview_summary", {}),
                    "message_count": len(imported.get("messages", [])),
                    "body_persisted_in_demo_run": False,
                },
            }, {}
        import_id = self._import_from_state(state)
        if step == 5:
            evidence = self.chat_evidence(import_id)
            result = {
                "action": action, "import_id": import_id, "evidence_import_id": import_id,
                "message_count": len(evidence.get("messages", [])),
                "preview_summary": evidence.get("preview_summary", {}),
                "message": "完整聊天树按需打开；未在演示状态中复制正文。",
            }
            summary = evidence.get("preview_summary", {})
            root_status = str(summary.get("default_target_root_status") or "")
            real_target_blocked = root_status in {"uniquely_located_incomplete", "ambiguous_root", "root_not_found"}
            if real_target_blocked and payload.get("use_synthetic_demo") is True:
                preview, synthetic_import = self._activate_synthetic_demo_import(
                    actor_id=actor_id,
                    actor_name=actor_name,
                    idempotency_key=f"{idempotency_key}:synthetic-demo",
                )
                synthetic_evidence = self.chat_evidence(synthetic_import["import_id"])
                return "succeeded", {
                    "action": action,
                    "import_id": synthetic_import["import_id"],
                    "evidence_import_id": synthetic_import["import_id"],
                    "scope": preview["scope"],
                    "message_count": len(synthetic_evidence.get("messages", [])),
                    "preview_summary": synthetic_evidence.get("preview_summary", {}),
                    "demo_data_mode": "synthetic",
                    "synthetic_disclaimer": "已切换到独立的合成演示数据；不代表真实企微记录或正式报价。",
                    "message": "已按演示者明确选择切换到合成聊天树；原真实导入保留且未被修改。",
                }, {}
            if root_status == "uniquely_located_incomplete":
                return "blocked", result, {
                    "reason": "目标根消息虽已定位，但完整嵌套转发或附件存在本机可见缺口",
                    "who": "本机数据维护者",
                    "evidence": "完整性预览中的未展开转发、附件可得状态和时间范围",
                    "next_action": "补齐可合法读取的本机来源后重新预览；或明确切换到合成演示数据继续讲解。当前不能标记真实场景完成",
                    "synthetic_demo_available": bool(self._fixture_sources()),
                }
            if root_status in {"ambiguous_root", "root_not_found"}:
                return "blocked", result, {
                    "reason": "默认真实目标根消息未能唯一定位",
                    "who": "演示者",
                    "evidence": "账号、来源快照、会话和根消息候选",
                    "next_action": "选择明确来源范围后重新预览；或明确切换到合成演示数据继续讲解",
                    "synthetic_demo_available": bool(self._fixture_sources()),
                }
            if summary.get("fixture"):
                result["demo_data_mode"] = "synthetic"
                result["synthetic_disclaimer"] = "当前为明确标注的合成演示数据，不代表真实企微记录或正式报价。"
                result["message"] = "合成演示聊天树按需打开；其来源和报价候选持续标注为非真实数据。"
            return "succeeded", result, {}
        if step == 6:
            parsed = self.parse_quotes(import_id)
            return "succeeded", {"action": action, "import_id": import_id, "quotes": parsed}, {}
        if step == 7:
            quotes = self.list_quotes(import_id=import_id)["items"]
            pending = [item for item in quotes if item["status"] == "needs_manual_review" or item.get("missing_fields")]
            if pending:
                return "needs_manual_decision", {"action": action, "quotes": pending}, {
                    "reason": "存在低置信度字段、缺失字段或不明确的票据关系", "who": "已配置空运审核人",
                    "evidence": "原聊天、附件状态和字段证据", "next_action": "修正字段后重试当前步骤",
                }
            return "succeeded", {"action": action, "quotes": quotes, "message": "没有待确认字段；未发送真实追问。"}, {}
        if step == 8:
            results = [self.calculate_chargeable_weight(item["quote_request_id"]) for item in self.list_quotes(import_id=import_id)["items"]]
            if any(item.get("status") != "calculated" for item in results):
                return "blocked", {"action": action, "items": results}, {"reason": "包装语义、尺寸、CTN 或重量不完整", "who": "业务人员", "evidence": "包装组字段证据", "next_action": "修正包装组后重试"}
            return "succeeded", {"action": action, "items": results}, {}
        if step == 9:
            matches = [self.match_rates(item["quote_request_id"]) for item in self.list_quotes(import_id=import_id)["items"]]
            unresolved = [item for item in matches if item.get("status") != "matched"]
            if unresolved:
                no_published_card = [item for item in unresolved if (item.get("blocker") or {}).get("code") == "no_published_rate_card"]
                if no_published_card:
                    return "needs_manual_decision", {"action": action, "items": matches}, {
                        "code": "no_published_rate_card",
                        "reason": "当前没有已发布且在目标日期有效的空运价卡",
                        "who": "已配置空运审核人",
                        "evidence": "候选价卡、审核发布状态和目标日期",
                        "next_action": "前往流程 A 完成候选价卡的冲突审核与发布；发布后重试当前步骤",
                    }
                needs_weight_confirmation = any(
                    option.get("status") == "needs_manual_review"
                    and option.get("blocker_code") == "weight_rule_confirmation_required"
                    for item in unresolved for option in item.get("items", [])
                )
                if needs_weight_confirmation:
                    return "needs_manual_decision", {"action": action, "items": matches}, {
                        "code": "weight_rule_confirmation_required",
                        "reason": "已发布价卡的重量档边界尚未明确确认",
                        "who": "已配置空运审核人",
                        "evidence": "价卡版本、重量档、超范围与取整规则",
                        "next_action": "前往最新价卡明确确认重量档边界后重试当前步骤",
                    }
                return "needs_manual_decision", {"action": action, "items": matches}, {
                    "code": "inquiry_required",
                    "reason": "重量档、需单询或可用价卡尚未满足",
                    "who": "已配置空运审核人",
                    "evidence": "价卡版本、重量档和限制",
                    "next_action": "查看价卡与限制后转人工单询，或补齐可用价卡后重试",
                }
            return "succeeded", {"action": action, "items": matches}, {}
        if step == 10:
            calculations = [self.generate_internal_calculation(item["quote_request_id"]) for item in self.list_quotes(import_id=import_id)["items"]]
            if any(item.get("status") != "calculated" for item in calculations):
                return "blocked", {"action": action, "items": calculations}, {"reason": "没有可确认的内部测算", "who": "业务人员", "evidence": "价卡和计费重", "next_action": "补齐匹配前置条件"}
            return "succeeded", {"action": action, "items": calculations, "label": "内部测算 ≠ 对客报价"}, {}
        if step == 11:
            reviewer = self._require_reviewer(actor_id, actor_name)
            options: list[dict[str, Any]] = []
            for quote in self.list_quotes(import_id=import_id)["items"]:
                options.extend(self.confirm_quote(
                    quote["quote_request_id"], actor_id=reviewer[0], actor_name=reviewer[1],
                    reason=str(payload.get("reason") or "演示人工确认报价选项"),
                    idempotency_key=f"{idempotency_key}:quote:{quote['quote_request_id']}",
                    sales_adjustment_per_kg="0.20",
                )["items"])
            return "succeeded", {"action": action, "items": options, "confirmed_by": reviewer[1],
                                  "sales_adjustment_per_kg": "0.20", "currency": "USD"}, {}
        return "succeeded", {"action": action, "preview": self.quote_preview(import_id=import_id), "completed": True, "not_sent": True}, {}

    def transition_demo(self, flow: str, action: str, *, state_version: Any,
                        payload: Mapping[str, Any] | None = None, actor_id: str | None = None,
                        actor_name: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        flow = str(flow or "").upper()
        if flow not in DEMO_STEPS:
            raise AirfreightOperationError("invalid_demo_flow", "演示流程无效", http_status=400)
        action = str(action or "").strip().lower()
        if action not in {"execute", "retry", "continue", "select", "set_mode"}:
            raise AirfreightOperationError("invalid_demo_action", "演示动作无效", http_status=400)
        payload = dict(payload or {})
        if not idempotency_key:
            raise AirfreightOperationError("idempotency_key_required", "演示写操作必须携带幂等标识", http_status=400)
        idempotency_key = str(idempotency_key)[:240]

        # State-only actions can remain inside a single SQLite transaction.
        if action in {"continue", "select", "set_mode"}:
            with self.storage.connect() as conn:
                state = self._read_demo_state(conn, flow, create=True)
                existing = conn.execute("SELECT * FROM demo_step_event WHERE demo_id=? AND idempotency_key=?", (state["demo_id"], idempotency_key)).fetchone()
                if existing:
                    return self._operation_response(conn, flow, idempotent=True, result=_loads(existing["result_json"], {}))
                self._require_state_version(state, state_version)
                if action == "set_mode":
                    mode = str(payload.get("mode") or "")
                    if mode not in {"single_step", "auto"}:
                        raise AirfreightOperationError("invalid_demo_mode", "演示模式只能为 single_step 或 auto", http_status=400)
                    result = {"mode": mode, "message": "模式已切换；不会重跑已完成业务写入。"}
                    # Mode changes affect presentation only.  In particular,
                    # a paused human decision must keep its original reason,
                    # evidence and next action so a presenter can still
                    # complete the current step after changing modes.
                    self._persist_demo_state(conn, state, mode=mode, result=dict(state.get("result") or {}) | result)
                    self._append_demo_event(conn, state=state, step=state["current_step"], action=action, previous_status=state["status"], next_status=state["status"], summary="切换演示模式", result=result, evidence={}, error_code=None, idempotency_key=idempotency_key)
                    return self._operation_response(conn, flow, result=result)
                if action == "select":
                    try:
                        selected = int(payload.get("step"))
                    except (TypeError, ValueError) as exc:
                        raise AirfreightOperationError("invalid_demo_step", "回看步骤必须是整数", http_status=400) from exc
                    if selected < 1 or selected > len(DEMO_STEPS[flow]):
                        raise AirfreightOperationError("invalid_demo_step", "回看步骤超出范围", http_status=400)
                    if selected > state["last_completed_step"] and selected != state["current_step"]:
                        raise AirfreightOperationError("future_step_locked", "未来步骤尚未满足前置条件", http_status=409, details={"current_step": state["current_step"], "last_completed_step": state["last_completed_step"]})
                    result = {"selected_step": selected, "read_only": selected != state["current_step"]}
                    self._persist_demo_state(conn, state, selected_step=selected, result=dict(state.get("result") or {}) | result)
                    self._append_demo_event(conn, state=state, step=selected, action=action, previous_status=state["status"], next_status=state["status"], summary="回看已完成步骤", result=result, evidence={}, error_code=None, idempotency_key=idempotency_key)
                    return self._operation_response(conn, flow, result=result)
                # Continue is deliberately separate from execute: it is the
                # only transition that makes the next step ready.
                if state["status"] != "succeeded" or state["current_step"] != state["last_completed_step"]:
                    raise AirfreightOperationError("continue_not_ready", "当前步骤尚未成功完成，不能进入下一步", http_status=409, details={"status": state["status"], "current_step": state["current_step"]})
                if state["current_step"] >= len(DEMO_STEPS[flow]):
                    result = {"completed": True, "message": "流程已完成；可只读复盘或重置演示运行。"}
                    self._persist_demo_state(conn, state, status="completed", result=dict(state.get("result") or {}) | result, blocked_reason={})
                    self._append_demo_event(conn, state=state, step=state["current_step"], action=action, previous_status="succeeded", next_status="completed", summary="流程完成", result=result, evidence={}, error_code=None, idempotency_key=idempotency_key)
                    return self._operation_response(conn, flow, result=result)
                next_step = state["current_step"] + 1
                result = {"next_step": next_step, "next_action": DEMO_STEPS[flow][next_step - 1]}
                self._persist_demo_state(conn, state, status="ready", current_step=next_step, selected_step=next_step, result=dict(state.get("result") or {}) | result, blocked_reason={})
                self._append_demo_event(conn, state=state, step=next_step, action=action, previous_status="succeeded", next_status="ready", summary="进入下一步骤", result=result, evidence={}, error_code=None, idempotency_key=idempotency_key)
                return self._operation_response(conn, flow, result=result)

        # ``execute`` and ``retry`` call bounded application services, some of
        # which use their own transactions.  Persist an append-only running
        # record first; either a succeeding terminal event or a recoverable
        # failure/blocked event is then guaranteed.
        with self.storage.connect() as conn:
            state = self._read_demo_state(conn, flow, create=True)
            existing = conn.execute("SELECT * FROM demo_step_event WHERE demo_id=? AND idempotency_key=?", (state["demo_id"], idempotency_key)).fetchone()
            if existing:
                return self._operation_response(conn, flow, idempotent=True, result=_loads(existing["result_json"], {}))
            self._require_state_version(state, state_version)
            requested_step = payload.get("step", state["current_step"])
            try:
                requested_step = int(requested_step)
            except (TypeError, ValueError) as exc:
                raise AirfreightOperationError("invalid_demo_step", "演示步骤必须是整数", http_status=400) from exc
            if requested_step != state["current_step"]:
                raise AirfreightOperationError("out_of_order_step", "只能执行当前 ready 步骤；未来步骤已锁定", http_status=409, details={"current_step": state["current_step"]})
            if action == "execute" and state["status"] != "ready":
                raise AirfreightOperationError("step_not_ready", "当前步骤不是 ready 状态；请继续、修正或重试", http_status=409, details={"status": state["status"]})
            if action == "retry" and state["status"] not in {"failed", "blocked", "needs_manual_decision"}:
                raise AirfreightOperationError("retry_not_allowed", "只有失败、阻塞或待人工决定的步骤可以重试", http_status=409, details={"status": state["status"]})
            if state["selected_step"] != state["current_step"]:
                raise AirfreightOperationError("read_only_step", "当前处于历史步骤回看，请先返回当前步骤再执行", http_status=409)
            self._persist_demo_state(
                conn,
                state,
                status="running",
                result=dict(state.get("result") or {}) | {"action": DEMO_STEPS[flow][requested_step - 1], "running": True},
                blocked_reason={},
            )
            self._append_demo_event(conn, state=state, step=requested_step, action="running", previous_status=state["status"], next_status="running", summary="开始执行步骤", result={"action": DEMO_STEPS[flow][requested_step - 1]}, evidence={}, error_code=None, idempotency_key=f"{idempotency_key}:running")

        try:
            # Reload after the running transition so scope/result state is
            # exactly what the service, not stale frontend memory, owns.
            with self.storage.connect() as conn:
                running_state = self._read_demo_state(conn, flow, create=False)
            outcome, result, pause = self._run_demo_business_step(flow, requested_step, running_state, payload, actor_id=actor_id, actor_name=actor_name, idempotency_key=idempotency_key)
        except AirfreightOperationError as exc:
            outcome, result, pause = ("blocked" if exc.error_code in {"chat_scope_required", "preview_confirmation_required", "chat_import_required", "demo_batch_required", "quote_parse_required", "conflict_requires_resolution"} else "failed"), exc.as_dict(), {"reason": exc.message, "error_code": exc.error_code, "next_action": "查看证据、修正后重试"}
        except Exception:
            outcome, result, pause = "failed", {"error_code": "internal_error", "message": "步骤执行失败；未公开内部错误细节。"}, {"reason": "本地处理失败", "error_code": "internal_error", "next_action": "查看本地受控日志后重试"}

        with self.storage.connect() as conn:
            state = self._read_demo_state(conn, flow, create=False)
            completed = list(state["completed_steps"])
            if outcome == "succeeded" and requested_step not in completed:
                completed.append(requested_step)
            next_scope = state["scope"]
            if isinstance(result.get("scope"), Mapping):
                next_scope = dict(result["scope"])
            # Keep run-bound identifiers (batch, preview, import) while each
            # step adds its own result.  This is server state, not a frontend
            # cache; a refresh can therefore resume the same bound workflow.
            result = dict(state.get("result") or {}) | dict(result) | {
                "flow": flow, "step": requested_step, "action": DEMO_STEPS[flow][requested_step - 1],
                "running": False,
            }
            self._persist_demo_state(
                conn, state, status=outcome, selected_step=requested_step,
                last_completed_step=requested_step if outcome == "succeeded" else state["last_completed_step"],
                completed_steps=completed, scope=next_scope, result=result,
                blocked_reason=pause if outcome != "succeeded" else {},
                quote_confirmed=True if flow == "B" and requested_step == 11 and outcome == "succeeded" else None,
            )
            self._append_demo_event(conn, state=state, step=requested_step, action=action, previous_status="running", next_status=outcome, summary=str(result.get("message") or result.get("action") or DEMO_STEPS[flow][requested_step - 1]), result=result, evidence={"scope": next_scope} if next_scope else {}, error_code=result.get("error_code"), idempotency_key=idempotency_key)
            return self._operation_response(conn, flow, result=result)

    def demo_step(self, flow: str, step: int, *, actor_id: str | None = None,
                  actor_name: str | None = None) -> dict[str, Any]:
        """Compatibility shim for local integrations from the initial draft.

        The HTTP API no longer exposes this unversioned shortcut.  Keeping it
        internal avoids silently changing direct Python callers while making
        the versioned state-machine path the only browser write boundary.
        """
        state = self.demo_state(flow)["flows"][str(flow).upper()]
        return self.transition_demo(flow, "execute", state_version=state["state_version"],
                                    payload={"step": step}, actor_id=actor_id, actor_name=actor_name,
                                    idempotency_key="compat-" + uuid.uuid4().hex)

    # ------------------------------------------------------------------
    # Safe multi-file parsing and rate-card normalization
    # ------------------------------------------------------------------
    def _artifact_dir(self) -> Path:
        path = Path(self.storage.path).resolve().parent / ".airfreight_artifacts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _extract_data(self, item: Mapping[str, Any]) -> bytes:
        if "data" in item and isinstance(item["data"], (bytes, bytearray)):
            return bytes(item["data"])
        encoded = item.get("data_base64") or item.get("file_base64") or item.get("content_base64")
        if encoded:
            try:
                return base64.b64decode(str(encoded), validate=True)
            except (ValueError, binascii.Error) as exc:
                raise AirfreightOperationError("invalid_file_encoding", "文件 Base64 编码无效", http_status=400) from exc
        if "path" in item:
            raise AirfreightOperationError("path_upload_forbidden", "不接受客户端路径；请通过本地文件选择器传入副本")
        raise AirfreightOperationError("file_content_required", "每个文件必须提供本地二进制副本")

    @staticmethod
    def _type_for(file_name: str, data: bytes) -> tuple[str, str, str, list[str]]:
        ext = Path(file_name).suffix.lower()
        warnings: list[str] = []
        if data.startswith(b"%PDF"):
            actual = "pdf"
            mime = "application/pdf"
            magic = "%PDF"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            actual = "png"
            mime = "image/png"
            magic = "png"
        elif data.startswith(b"\xff\xd8\xff"):
            actual = "jpeg"
            mime = "image/jpeg"
            magic = "jpeg"
        elif data.startswith(b"PK\x03\x04"):
            names: list[str]
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    names = archive.namelist()
            except zipfile.BadZipFile:
                names = []
            if any(name.startswith("word/") for name in names):
                actual = "docx"
                mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                magic = "zip/docx"
            elif any(name.startswith("xl/") for name in names):
                actual = "xlsx"
                mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                magic = "zip/xlsx"
            else:
                actual = "zip"
                mime = "application/zip"
                magic = "zip"
        elif data.startswith(b"\xd0\xcf\x11\xe0"):
            if ext == ".doc":
                actual, mime, magic = "doc", "application/msword", "ole"
            else:
                actual, mime, magic = "xls", "application/vnd.ms-excel", "ole"
        else:
            head = data[:512].decode("utf-8-sig", "ignore")
            if "DEMO_ET_XLS" in head or ext == ".xls" and ("\t" in head or "<table" in head.lower()):
                actual, mime, magic = "xls", "application/vnd.ms-excel", "legacy-text"
            elif ext in {".csv"} or ("," in head and "\n" in head):
                actual, mime, magic = "csv", "text/csv", "text"
            elif ext == ".doc":
                actual, mime, magic = "doc", "application/msword", "text"
            else:
                actual, mime, magic = "unknown", mimetypes.guess_type(file_name)[0] or "application/octet-stream", "unknown"
        extension_types = {
            ".pdf": {"pdf"}, ".xls": {"xls"}, ".xlsx": {"xlsx"}, ".doc": {"doc"}, ".docx": {"docx"},
            ".csv": {"csv"}, ".jpg": {"jpeg"}, ".jpeg": {"jpeg"}, ".png": {"png"},
        }
        if ext not in extension_types:
            warnings.append("unsupported_extension")
        elif actual not in extension_types[ext]:
            warnings.append("extension_type_mismatch")
        return actual, mime, magic, warnings

    @staticmethod
    def _security_scan(data: bytes, actual: str) -> tuple[str, list[str]]:
        lower = data.lower()
        text_lower = data.decode("utf-8", "ignore").lower()
        warnings: list[str] = []
        dangerous = {
            b"vbaproject": "发现 Office 宏项目",
            b"externalLinks": "发现 Excel 外部链接部件",
            b"javascript:": "发现 PDF／文档脚本文本",
            b"<w:fldsimple": "发现 Word 域代码",
        }
        for marker, message in dangerous.items():
            if marker.lower() in lower:
                warnings.append(message)
        if "ignore previous" in text_lower or "忽略规则" in text_lower or "直接通过" in text_lower or "system prompt" in text_lower:
            warnings.append("附件中包含提示性文字；仅作为业务数据展示")
        if actual in {"doc", "xls"} and data.startswith(b"\xd0\xcf\x11\xe0"):
            warnings.append("旧版 Office 二进制需要配置安全本地转换器；本机未执行转换")
        if actual == "unknown":
            return "rejected", warnings + ["未知二进制类型"]
        if any("宏项目" in item or "脚本文本" in item or "域代码" in item for item in warnings):
            return "rejected", warnings
        return "safe_with_warnings" if warnings else "safe", warnings

    def create_batch(self, *, files: Iterable[Mapping[str, Any]] | None, batch_kind: str = "manual_rate_card",
                     source_scope: Mapping[str, Any] | None = None, request_id: str | None = None) -> dict[str, Any]:
        request_id = str(request_id or ("batch-" + uuid.uuid4().hex))
        source_scope = dict(source_scope or {})
        if files is None:
            files = demo_file_payloads()
        file_items = list(files)
        if not file_items:
            raise AirfreightOperationError("files_required", "解析批次至少需要一个文件", http_status=400)
        if len(file_items) > 20:
            raise AirfreightOperationError("too_many_files", "单批最多处理 20 个文件", http_status=413)
        with self.storage.connect() as conn:
            existing = conn.execute("SELECT batch_id FROM import_batch WHERE business_line='airfreight' AND request_id=?", (request_id,)).fetchone()
            if existing:
                existing_batch = conn.execute("SELECT source_scope_json,batch_kind FROM import_batch WHERE batch_id=?", (existing["batch_id"],)).fetchone()
                if existing_batch and (_json(_loads(existing_batch["source_scope_json"], {})) != _json(source_scope) or existing_batch["batch_kind"] != batch_kind):
                    raise AirfreightOperationError("idempotency_scope_mismatch", "批次幂等键已经绑定其他来源范围或批次类型", http_status=409)
                return self.get_batch(existing["batch_id"])
            batch_id = "af_batch_" + uuid.uuid4().hex
            now = _iso_now()
            conn.execute("""INSERT INTO import_batch(batch_id,batch_kind,source_scope_json,request_id,created_at,status,total_files,summary_json)
                           VALUES(?,?,?,?,?,'created',?,?)""", (batch_id, batch_kind, _json(source_scope), request_id, now, len(file_items), _json({"local_only": True, "external_model": False})))
            total_bytes = 0
            for item in file_items:
                file_name = _safe_name(item.get("file_name") or item.get("name"))
                data = self._extract_data(item)
                if len(data) > 20 * 1024 * 1024:
                    raise AirfreightOperationError("file_too_large", f"文件 {file_name} 超过 20MB 限制", http_status=413)
                total_bytes += len(data)
                if total_bytes > 50 * 1024 * 1024:
                    raise AirfreightOperationError("batch_too_large", "单批文件总量超过 50MB 限制", http_status=413)
                actual, mime, magic, type_warnings = self._type_for(file_name, data)
                security_status, security_warnings = self._security_scan(data, actual)
                warnings = type_warnings + security_warnings
                artifact_id = "artifact_" + uuid.uuid4().hex
                local_ref: str | None = None
                if item.get("demo_role"):
                    local_ref = f"demo://{item['demo_role']}"
                else:
                    target = self._artifact_dir() / f"{artifact_id}_{file_name}"
                    target.write_bytes(data)
                    local_ref = str(target)
                self._artifact_cache[f"{self._cache_prefix}:{artifact_id}"] = data
                availability = "available" if security_status != "rejected" and actual != "unknown" else "unavailable"
                conn.execute("""INSERT INTO source_artifact(artifact_id,batch_id,file_name,actual_type,mime_type,magic,size_bytes,sha256,local_ref,availability_status,security_status,parser_version,received_at,source_json,preview_json,is_critical)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (artifact_id, batch_id, file_name, actual, mime, magic, len(data), _sha256(data), local_ref, availability, security_status, AIRFREIGHT_PARSER_VERSION, now, _json({"demo_role": item.get("demo_role"), "reply_group": item.get("reply_group"), "message_source": item.get("message_source")}), _json({"warnings": warnings}), 1 if item.get("critical") else 0))
                conn.execute("""INSERT INTO extraction_run(extraction_id,artifact_id,parser_version,started_at,status,parser_path,warning_json,error_json)
                               VALUES(?,?,?,?,?,?,?,?)""", ("extraction_" + uuid.uuid4().hex, artifact_id, AIRFREIGHT_PARSER_VERSION, now, "pending", "pending", _json(warnings), "[]"))
        return self.get_batch(batch_id)

    def _artifact_bytes(self, artifact: Mapping[str, Any]) -> bytes:
        # SQLite rows support keyed lookup but do not implement ``get``;
        # normalize both row and dict callers at this boundary.
        artifact = dict(artifact)
        key = f"{self._cache_prefix}:{artifact['artifact_id']}"
        if key in self._artifact_cache:
            return self._artifact_cache[key]
        ref = str(artifact.get("local_ref") or "")
        if ref.startswith("demo://"):
            # Recreate only deterministic fixtures after a process restart.
            role = _loads(artifact.get("source_json"), {}).get("demo_role")
            for item in demo_file_payloads():
                if item.get("demo_role") == role:
                    self._artifact_cache[key] = item["data"]
                    return item["data"]
        if ref and Path(ref).is_file():
            data = Path(ref).read_bytes()
            if _sha256(data) != artifact.get("sha256"):
                raise AirfreightOperationError("artifact_hash_mismatch", "本地附件哈希已变化，拒绝解析")
            self._artifact_cache[key] = data
            return data
        raise AirfreightOperationError("artifact_unavailable", "附件二进制副本不可取得，不能伪造预览")

    def list_batches(self, *, limit: int = 50) -> dict[str, Any]:
        with self.storage.connect() as conn:
            rows = conn.execute("SELECT * FROM import_batch WHERE business_line='airfreight' ORDER BY created_at DESC LIMIT ?", (max(1, min(int(limit), 100)),)).fetchall()
        return {"items": [dict(row) | {"source_scope": _loads(row["source_scope_json"], {}), "summary": _loads(row["summary_json"], {})} for row in rows], "total": len(rows)}

    def _public_artifact(self, row: Any) -> dict[str, Any]:
        source = _loads(row["source_json"], {})
        preview = _loads(row["preview_json"], {})
        return {
            "artifact_id": row["artifact_id"], "batch_id": row["batch_id"], "file_name": row["file_name"], "actual_type": row["actual_type"],
            "mime_type": row["mime_type"], "magic": row["magic"], "size_bytes": row["size_bytes"], "sha256": row["sha256"],
            "local_ref": _mask_path(row["local_ref"]), "availability_status": row["availability_status"], "security_status": row["security_status"],
            "parser_version": row["parser_version"], "received_at": row["received_at"], "warnings": preview.get("warnings", []), "source": {"demo_role": source.get("demo_role"), "reply_group": source.get("reply_group")},
            "preview_url": f"/api/airfreight/artifacts/{row['artifact_id']}/preview" if row["availability_status"] == "available" else None,
            "download_url": f"/api/airfreight/artifacts/{row['artifact_id']}/download" if row["availability_status"] == "available" else None,
        }

    def get_artifact_bytes(self, artifact_id: str) -> tuple[bytes, str, str] | None:
        with self.storage.connect() as conn:
            row = conn.execute("SELECT * FROM source_artifact WHERE artifact_id=? AND business_line='airfreight'", (artifact_id,)).fetchone()
        if row is None or row["availability_status"] != "available":
            return None
        return self._artifact_bytes(row), row["mime_type"], row["file_name"]

    def _parse_pdf(self, data: bytes, artifact: Mapping[str, Any]) -> dict[str, Any]:
        text = data.decode("latin-1", "ignore")
        if "DEMO_ET_PDF" not in text:
            from .trimanson_rates import legacy_extraction, parse_trimanson_pdf
            try:
                real_card = parse_trimanson_pdf(data)
            except (ValueError, ImportError):
                real_card = None
            if real_card:
                return legacy_extraction(real_card)
            strings = re.findall(r"\(([^()]*)\)\s*Tj", text)
            extracted = " ".join(strings).replace("\\(", "(").replace("\\)", ")")
            if not extracted.strip():
                return {"status": "needs_manual_review", "parser_path": "local_ocr", "ocr_baseline": OCR_BASELINE, "pages": 1, "warnings": ["扫描 PDF 无可提取文本；本机 OCR 基线不可用"], "errors": ["needs_manual_review"], "fields": [], "rate_observations": []}
            # Do not turn arbitrary extracted PDF text into the deterministic
            # Ethiopian demo rate table.  A real PDF still needs a table-aware
            # parser or human review unless it carries the explicit fixture
            # marker handled above.
            return {"status": "needs_manual_review", "parser_path": "native_text_probe", "ocr_baseline": OCR_BASELINE, "pages": 1, "warnings": ["已提取到文字，但未识别为本地演示价卡；未猜测航司、路线或金额"], "errors": ["pdf_table_parser_unavailable", "needs_manual_review"], "fields": [{"field": "document_text_preview", "raw": extracted[:2000], "normalized": extracted[:2000], "confidence": "0.60", "locator": {"page": 1, "block": "text probe"}}], "rate_observations": []}
        observations: list[dict[str, Any]] = []
        for destination, values in ET_DESTINATIONS.items():
            for label, amount in zip(WEIGHT_LABELS, values):
                observations.append({"airline_name": "Ethiopian Airlines", "airline_code": "ET-071", "supplier_name": "Ethiopian Airlines", "origin": "HKG", "hub": "ADD", "destination": destination, "route": f"HKG-ADD-{destination}", "effective_date": "2026-08-19", "validity_mode": "until_further_notice", "currency": "USD", "unit": "KG", "weight_break_label": label, "amount": amount, "raw_text": f"{destination} {label} {amount}", "locator": {"page": 1, "block": "main rate table", "rect": [40, 120, 560, 720]}})
        return {"status": "parsed", "parser_path": "native_structure", "pages": 1, "warnings": [], "errors": [], "fields": [
            {"field": "supplier_name", "raw": "Ethiopian Airlines", "normalized": "Ethiopian Airlines", "confidence": "0.99", "locator": {"page": 1, "block": "title"}},
            {"field": "airline_code", "raw": "ET-071", "normalized": "ET-071", "confidence": "0.99", "locator": {"page": 1, "block": "title"}},
            {"field": "effective_date", "raw": "2026-08-19", "normalized": "2026-08-19", "confidence": "0.98", "locator": {"page": 1, "block": "title"}},
            {"field": "validity_mode", "raw": "until further notice", "normalized": "until_further_notice", "confidence": "0.97", "locator": {"page": 1, "block": "title"}},
            {"field": "currency", "raw": "USD", "normalized": "USD", "confidence": "0.96", "locator": {"page": 1, "block": "rate table"}},
            {"field": "frequency", "raw": "D1/2/4/5/6/7", "normalized": "D1/2/4/5/6/7", "confidence": "0.96", "locator": {"page": 1, "block": "service details"}},
            {"field": "transit_time", "raw": "3-5 days", "normalized": "3-5 days", "confidence": "0.96", "locator": {"page": 1, "block": "service details"}},
        ], "rate_observations": observations, "restrictions": []}

    def _parse_legacy_xls(self, data: bytes, artifact: Mapping[str, Any]) -> dict[str, Any]:
        if data.startswith(b"\xd0\xcf\x11\xe0"):
            return {"status": "needs_manual_review", "parser_path": "manual", "sheets": 0, "warnings": ["旧 DOC/XLS 二进制需要安全本地转换器；本机未执行 Office"], "errors": ["legacy_converter_unavailable"], "fields": [], "rate_observations": []}
        text = data.decode("utf-8-sig", "replace")
        if "DEMO_ET_XLS" in text:
            rows = [["Destination", "+45", "+100", "+300", "+500", "+1000", "Currency", "Unit"], ["BRU", "4.45", "4.05", "3.65", "3.35", "3.15", "USD", "KG"]]
            observations = []
            for row in rows[1:]:
                for label, amount in zip(WEIGHT_LABELS, row[1:6]):
                    observations.append({"airline_name": "Ethiopian Airlines", "airline_code": "ET-071", "supplier_name": "Ethiopian Airlines", "origin": "HKG", "hub": "ADD", "destination": row[0], "route": f"HKG-ADD-{row[0]}", "effective_date": "2026-08-19", "validity_mode": "until_further_notice", "currency": row[6], "unit": row[7], "weight_break_label": label, "amount": amount, "raw_text": f"{row[0]} {label} {amount}", "locator": {"sheet": "ET Rates", "cell": f"{chr(66 + WEIGHT_LABELS.index(label))}2", "display_value": amount}})
            return {"status": "parsed", "parser_path": "native_structure", "sheets": 1, "warnings": ["XLS 对 PDF 对应值差异仅形成内部映射候选，不覆盖供应商原始价"], "errors": [], "fields": [{"field": "valid_to", "raw": "", "normalized": None, "confidence": "0.80", "locator": {"sheet": "ET Rates", "range": "A1:H2"}}], "rate_observations": observations, "internal_deltas": [{"destination": "BRU", "adjustment_per_kg": "0.20", "pdf_source": "ET-HK-2026.8.19.pdf", "xls_source": "ET-HK-2026.8.19.xls", "evidence": "XLS 对应值 - PDF 对应值 = 0.20"}]}
        delimiter = "\t" if "\t" in text else ","
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        if not rows:
            return {"status": "needs_manual_review", "parser_path": "native_structure", "sheets": 0, "warnings": [], "errors": ["empty_workbook"], "fields": [], "rate_observations": []}
        return self._rows_to_observations(rows, artifact, sheet="Sheet1")

    def _parse_xlsx(self, data: bytes, artifact: Mapping[str, Any]) -> dict[str, Any]:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
                sheet_names = [name for name in names if name.startswith("xl/worksheets/") and name.endswith(".xml")]
                if not sheet_names:
                    raise ValueError("no_worksheets")
                shared_strings: list[str] = []
                if "xl/sharedStrings.xml" in names:
                    shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                    shared_strings = [_xml_text(item) for item in shared_root if _local_name(item.tag) == "si"]
                rows: list[list[str]] = []
                merged_ranges: list[str] = []
                formula_cells: list[str] = []
                hidden_elements: list[str] = []
                for sheet_name in sheet_names[:20]:
                    root = ET.fromstring(archive.read(sheet_name))
                    merged_ranges.extend(item.attrib.get("ref", "") for item in root.iter() if _local_name(item.tag) == "mergeCell" and item.attrib.get("ref"))
                    hidden_elements.extend(f"{sheet_name}:{_local_name(item.tag)}" for item in root.iter() if item.attrib.get("hidden") in {"1", "true"} or item.attrib.get("state") in {"hidden", "veryHidden"})
                    for row in root.iter():
                        if _local_name(row.tag) != "row":
                            continue
                        cells: list[tuple[int, str]] = []
                        for cell in row:
                            if _local_name(cell.tag) != "c":
                                continue
                            ref = cell.attrib.get("r", "A1")
                            match = re.match(r"([A-Z]+)", ref)
                            col_index = 0
                            for letter in (match.group(1) if match else "A"):
                                col_index = col_index * 26 + ord(letter) - 64
                            value = ""
                            for child in cell:
                                if _local_name(child.tag) == "v":
                                    value = child.text or ""
                                elif _local_name(child.tag) == "is":
                                    value = _xml_text(child)
                                elif _local_name(child.tag) == "f":
                                    formula_cells.append(f"{sheet_name}!{ref}")
                            if cell.attrib.get("t") == "s" and value.isdigit():
                                index = int(value)
                                value = shared_strings[index] if index < len(shared_strings) else ""
                            cells.append((col_index, value))
                        if cells:
                            max_col = max(col for col, _ in cells)
                            line = [""] * max_col
                            for col, value in cells:
                                line[col - 1] = value
                            rows.append(line)
                result = self._rows_to_observations(rows, artifact, sheet="ET Rates")
                result["sheets"] = len(sheet_names)
                warnings = list(result.get("warnings", [])) + ["读取显示值与结构；不执行公式、宏或外部链接"]
                if formula_cells:
                    warnings.append(f"检测到 {len(formula_cells)} 个公式单元格；使用缓存显示值，不计算公式")
                if merged_ranges:
                    warnings.append(f"保留合并区域 {', '.join(merged_ranges[:20])}")
                if hidden_elements:
                    warnings.append(f"检测到隐藏工作表／行列 {len(hidden_elements)} 处；未自动展开")
                result["warnings"] = warnings
                result["workbook_structure"] = {"merged_ranges": merged_ranges, "formula_cells": formula_cells, "hidden_elements": hidden_elements, "shared_string_count": len(shared_strings)}
                return result
        except (zipfile.BadZipFile, ET.ParseError, ValueError) as exc:
            return {"status": "needs_manual_review", "parser_path": "native_structure", "sheets": 0, "warnings": [], "errors": ["workbook_structure_invalid", str(exc)], "fields": [], "rate_observations": []}

    def _rows_to_observations(self, rows: list[list[str]], artifact: Mapping[str, Any], *, sheet: str) -> dict[str, Any]:
        if len(rows) < 2:
            return {"status": "needs_manual_review", "parser_path": "native_structure", "sheets": 1, "warnings": [], "errors": ["header_or_rows_missing"], "fields": [], "rate_observations": []}
        headers = [str(item).strip().casefold() for item in rows[0]]
        required = {"destination", "weight_break_label", "amount"}
        if not required.issubset(set(headers)):
            # The small ET fixture uses a cross-tab shape and is normalized separately.
            if headers and headers[0] == "destination" and "+45" in headers:
                observations = []
                for row_no, row in enumerate(rows[1:], 2):
                    if not row or not row[0]:
                        continue
                    for offset, label in enumerate(WEIGHT_LABELS, 1):
                        if offset < len(row) and _decimal(row[offset]) is not None:
                            observations.append({"airline_name": "Ethiopian Airlines", "airline_code": "ET-071", "supplier_name": "Ethiopian Airlines", "origin": "HKG", "hub": "ADD", "destination": row[0].strip().upper(), "route": f"HKG-ADD-{row[0].strip().upper()}", "effective_date": "2026-08-19", "validity_mode": "until_further_notice", "currency": row[6] if len(row) > 6 else None, "unit": row[7] if len(row) > 7 else None, "weight_break_label": label, "amount": row[offset], "raw_text": " | ".join(row), "locator": {"sheet": sheet, "row": row_no, "range": f"A{row_no}:H{row_no}"}})
                return {"status": "parsed", "parser_path": "native_structure", "sheets": 1, "warnings": [], "errors": [], "fields": [], "rate_observations": observations}
            return {"status": "needs_manual_review", "parser_path": "native_structure", "sheets": 1, "warnings": [], "errors": ["required_columns_missing"], "fields": [], "rate_observations": []}
        observations: list[dict[str, Any]] = []
        for row_no, row in enumerate(rows[1:], 2):
            item = {headers[index]: row[index].strip() if index < len(row) else "" for index in range(len(headers))}
            if not item.get("destination") or not item.get("weight_break_label") or item.get("amount") in (None, ""):
                continue
            observations.append({"airline_name": item.get("airline_name") or "演示承运人", "airline_code": item.get("airline_code") or "DEMO", "supplier_name": item.get("supplier_name") or item.get("airline_name") or "演示承运人", "origin": (item.get("origin") or "HKG").upper(), "hub": (item.get("hub") or "ADD").upper(), "destination": item["destination"].upper(), "route": item.get("route") or f"{item.get('origin','HKG')}-{item.get('hub','ADD')}-{item['destination']}", "effective_date": item.get("effective_date") or "2026-08-19", "validity_mode": item.get("validity_mode") or "explicit_range", "currency": item.get("currency") or None, "unit": item.get("unit") or None, "weight_break_label": item.get("weight_break_label") or "", "amount": None if item.get("amount") in {"/", "／"} else item.get("amount"), "rate_status": "inquiry_required" if item.get("amount") in {"/", "／"} else "candidate", "raw_text": " | ".join(row), "locator": {"sheet": sheet, "row": row_no, "columns": headers}})
        return {"status": "parsed" if observations else "needs_manual_review", "parser_path": "native_structure", "sheets": 1, "warnings": [], "errors": [] if observations else ["no_rate_rows"], "fields": [], "rate_observations": observations}

    def _parse_docx(self, data: bytes, artifact: Mapping[str, Any]) -> dict[str, Any]:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                root = ET.fromstring(archive.read("word/document.xml"))
                paragraphs = [" ".join(_xml_text(node).split()) for node in root.iter() if _local_name(node.tag) == "p" and _xml_text(node).strip()]
                tables: list[list[str]] = []
                for table in [node for node in root.iter() if _local_name(node.tag) == "tbl"]:
                    table_rows: list[str] = []
                    for row in [node for node in table.iter() if _local_name(node.tag) == "tr"]:
                        table_rows.append(" | ".join(_xml_text(cell) for cell in row if _local_name(cell.tag) == "tc"))
                    if table_rows:
                        tables.append(table_rows)
                images = [name for name in archive.namelist() if name.startswith("word/media/")]
                warnings = ["DOCX 内嵌图片已登记；图片字段继续走本地 OCR／人工路径"] if images else []
                document_text = "\n".join(paragraphs + [item for rows in tables for item in rows])
                has_restriction = bool(re.search(r"(?:via\s+ADD|over\s+1000\s*kgs?|BLR|BOM|DEL|MAA|单询|需单询)", document_text, re.IGNORECASE))
                has_surcharge = bool(re.search(r"(?:destination\s+surcharge|目的地附加费|Small\s+Box)", document_text, re.IGNORECASE))
                restrictions = [{"destination": "BLR/BOM/DEL/MAA", "restriction_type": "inquiry_required", "original_text": "via ADD; over 1000 kgs inquiry", "source": "table row", "status": "pending"}] if has_restriction else []
                surcharges = [{"name": "destination surcharge", "amount": None, "currency": None, "pricing_unit": None, "charge_basis": None, "minimum_charge": None, "scope": {"destinations": "destination dependent"}, "original_text": "目的地附加费 as applicable；适用范围和金额未明确", "status": "inquiry_required"}] if has_surcharge else []
                return {"status": "parsed", "parser_path": "native_structure", "pages": None, "sheets": 0, "warnings": warnings, "errors": [], "fields": [{"field": "document_text", "raw": document_text, "normalized": document_text, "confidence": "0.94", "locator": {"paragraphs": len(paragraphs), "tables": len(tables), "embedded_images": images}}], "tables": tables, "embedded_images": images, "rate_observations": [], "restrictions": restrictions, "surcharges": surcharges}
        except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
            return {"status": "needs_manual_review", "parser_path": "native_structure", "warnings": [], "errors": ["docx_structure_invalid", str(exc)], "fields": [], "rate_observations": []}

    def _parse_csv(self, data: bytes, artifact: Mapping[str, Any]) -> dict[str, Any]:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = data.decode("gb18030")
            except UnicodeDecodeError:
                return {"status": "needs_manual_review", "parser_path": "native_structure", "warnings": [], "errors": ["encoding_unknown"], "fields": [], "rate_observations": []}
        try:
            dialect = csv.Sniffer().sniff(text[:4096])
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
        return self._rows_to_observations(rows, artifact, sheet="CSV")

    def _parse_image(self, data: bytes, artifact: Mapping[str, Any]) -> dict[str, Any]:
        # This is intentionally a narrow local baseline for the deterministic
        # fixture.  Arbitrary images without a fixture marker never become a
        # fake successful OCR result.
        text = ""
        if data.startswith(b"\x89PNG"):
            for match in re.finditer(b"tEXt\x00?", data):
                pass
            marker = re.search(b"airfreight_demo\x00(DEMO_[A-Z0-9_]+)", data)
            if marker:
                text = marker.group(1).decode("utf-8", "ignore")
        if text == "DEMO_CARGO_MULTI_TICKET":
            return {"status": "parsed", "parser_path": "local_ocr", "ocr_baseline": OCR_BASELINE, "pixels": {"width": 1200, "height": 720}, "warnings": ["合成夹具使用内置文字标记；真实图片仍需本地 OCR 引擎或人工复核"], "errors": [], "fields": [{"field": "multi_ticket_layout", "raw": "两票货物；BRU 与 TLV 区域隔离", "normalized": {"tickets": 2, "routes": ["HKG-ADD-BRU", "HKG-ADD-TLV"]}, "confidence": "0.91", "locator": {"rect": [0, 0, 1200, 720], "visual_group_ids": ["ticket-1", "ticket-2"]}}], "quote_layout": [{"quote_key": "ticket-1", "route": "HKG-ADD-BRU", "rect": [0, 0, 600, 720]}, {"quote_key": "ticket-2", "route": "HKG-ADD-TLV", "rect": [600, 0, 1200, 720]}]}
        quality = "low_quality" if len(data) < 100 else "ocr_engine_unavailable"
        return {"status": "needs_manual_review", "parser_path": "local_ocr", "ocr_baseline": OCR_BASELINE, "warnings": ["未安装可用于任意图片的 OCR 引擎"], "errors": [quality, "needs_manual_review"], "fields": [], "original_evidence": {"artifact_id": artifact["artifact_id"], "locator": {"rect": [0, 0, None, None]}}}

    def _parse_artifact(self, artifact: Mapping[str, Any]) -> dict[str, Any]:
        data = self._artifact_bytes(artifact)
        actual = artifact["actual_type"]
        security = artifact["security_status"]
        if security == "rejected":
            return {"status": "rejected", "parser_path": "manual", "warnings": _loads(artifact.get("preview_json"), {}).get("warnings", []), "errors": ["security_check_failed"], "fields": [], "rate_observations": []}
        if any(item in _loads(artifact.get("preview_json"), {}).get("warnings", []) for item in ("extension_type_mismatch", "unsupported_extension")):
            return {"status": "rejected", "parser_path": "manual", "warnings": ["扩展名与实际类型不一致"], "errors": ["extension_type_mismatch"], "fields": [], "rate_observations": []}
        if actual == "pdf":
            return self._parse_pdf(data, artifact)
        if actual == "xls":
            return self._parse_legacy_xls(data, artifact)
        if actual == "xlsx":
            return self._parse_xlsx(data, artifact)
        if actual == "docx":
            return self._parse_docx(data, artifact)
        if actual == "doc":
            return {"status": "needs_manual_review", "parser_path": "manual", "warnings": ["旧 DOC 无安全本地转换器；未执行 Office"], "errors": ["legacy_converter_unavailable"], "fields": [], "rate_observations": []}
        if actual == "csv":
            return self._parse_csv(data, artifact)
        if actual in {"png", "jpeg"}:
            return self._parse_image(data, artifact)
        return {"status": "rejected", "parser_path": "manual", "warnings": [], "errors": ["unsupported_type"], "fields": [], "rate_observations": []}

    def parse_batch(self, batch_id: str) -> dict[str, Any]:
        with self.storage.connect() as conn:
            batch = conn.execute("SELECT * FROM import_batch WHERE batch_id=? AND business_line='airfreight'", (batch_id,)).fetchone()
            if batch is None:
                raise AirfreightOperationError("batch_not_found", "找不到空运解析批次", http_status=404)
            artifacts = conn.execute("SELECT * FROM source_artifact WHERE batch_id=? ORDER BY file_name,artifact_id", (batch_id,)).fetchall()
            for artifact in artifacts:
                run = conn.execute("SELECT * FROM extraction_run WHERE artifact_id=? ORDER BY started_at DESC LIMIT 1", (artifact["artifact_id"],)).fetchone()
                if run and run["status"] in {"parsed", "needs_manual_review", "rejected"}:
                    continue
                try:
                    result = self._parse_artifact(dict(artifact))
                except AirfreightOperationError as exc:
                    result = {"status": "needs_manual_review", "parser_path": "manual", "warnings": [], "errors": [exc.error_code], "fields": [], "rate_observations": []}
                conn.execute("""UPDATE extraction_run SET finished_at=?,status=?,parser_path=?,page_count=?,sheet_count=?,warning_json=?,error_json=?,result_json=? WHERE extraction_id=?""", (_iso_now(), result.get("status", "needs_manual_review"), result.get("parser_path", "manual"), result.get("pages"), result.get("sheets"), _json(result.get("warnings", [])), _json(result.get("errors", [])), _json(result), run["extraction_id"] if run else ""))
            self._normalize_batch(conn, batch_id)
        return self.get_batch(batch_id)

    def _normalize_batch(self, conn: Any, batch_id: str) -> None:
        batch = conn.execute("SELECT * FROM import_batch WHERE batch_id=?", (batch_id,)).fetchone()
        if not batch:
            return
        artifacts = conn.execute("SELECT * FROM source_artifact WHERE batch_id=?", (batch_id,)).fetchall()
        parsed_runs = []
        for artifact in artifacts:
            run = conn.execute("SELECT * FROM extraction_run WHERE artifact_id=? ORDER BY started_at DESC LIMIT 1", (artifact["artifact_id"],)).fetchone()
            if run:
                parsed_runs.append((artifact, _loads(run["result_json"], {})))
        # Reuse the version already attached to this batch.  Normalization is
        # deliberately safe to call again after a retry or a read-only page
        # refresh; a new UUID must not create duplicate versions/conflicts.
        version_ids: dict[str, str] = {
            row["airline_code"]: row["version_id"]
            for row in conn.execute(
                "SELECT airline_code,version_id FROM rate_card_version WHERE source_batch_id=?",
                (batch_id,),
            ).fetchall()
            if row["airline_code"]
        }
        for artifact, result in parsed_runs:
            # The paired ET XLS is intentionally a maintenance-layer
            # observation in this demo.  Its +0.20 difference is preserved in
            # internal_rate_rule evidence; it must not silently replace or
            # create a second supplier raw rate.
            demo_role = _loads(artifact["source_json"], {}).get("demo_role")
            rate_observations = [] if demo_role == "et_xls" else result.get("rate_observations", [])
            for observation in rate_observations:
                airline_code = str(observation.get("airline_code") or "DEMO")
                version_id = version_ids.get(airline_code)
                if version_id is None:
                    existing_version = conn.execute(
                        "SELECT version_id FROM rate_card_version WHERE source_batch_id=? AND airline_code=? ORDER BY created_at,version_id LIMIT 1",
                        (batch_id, airline_code),
                    ).fetchone()
                    version_id = existing_version["version_id"] if existing_version else "ratever_" + uuid.uuid4().hex
                    version_ids[airline_code] = version_id
                    if existing_version is None:
                        conn.execute("""INSERT OR IGNORE INTO rate_card_version(version_id,supplier_name,airline_name,airline_code,origin_airport,hub_airport,version_date,document_updated_at,valid_from,valid_to,validity_mode,currency,status,review_status,source_batch_id,source_json,created_at)
                                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (version_id, observation.get("supplier_name") or observation.get("airline_name") or "未知供应商", observation.get("airline_name") or "未知航司", airline_code, observation.get("origin") or "", observation.get("hub"), observation.get("effective_date"), observation.get("document_updated_at"), observation.get("effective_date"), None, observation.get("validity_mode") or "explicit_range", observation.get("currency"), "candidate", "pending", batch_id, _json({"business_line": "airfreight", "source_batch_id": batch_id}), _iso_now()))
                existing = conn.execute("SELECT * FROM route_rate WHERE version_id=? AND destination_airport=? AND weight_break_label=?", (version_id, observation.get("destination"), observation.get("weight_break_label"))).fetchone()
                if existing is None:
                    rate_id = "rate_" + uuid.uuid4().hex
                    rate_status = observation.get("rate_status") or ("inquiry_required" if str(observation.get("amount") or "").strip() in {"/", "／"} else "candidate")
                    conn.execute("""INSERT INTO route_rate(rate_id,version_id,origin_airport,hub_airport,destination_airport,destination_city,routing,weight_break_label,amount_text,currency,pricing_unit,status,source_artifact_id,original_amount_text)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (rate_id, version_id, observation.get("origin") or "", observation.get("hub"), observation.get("destination") or "", observation.get("destination"), observation.get("route") or "", observation.get("weight_break_label") or "", observation.get("amount"), observation.get("currency"), observation.get("unit"), rate_status, artifact["artifact_id"], observation.get("amount")))
                    existing = conn.execute("SELECT * FROM route_rate WHERE rate_id=?", (rate_id,)).fetchone()
                conn.execute("""INSERT OR IGNORE INTO rate_value_observation(observation_id,rate_id,version_id,source_artifact_id,destination_airport,weight_break_label,amount_text,currency,pricing_unit,observed_at,raw_text,locator_json)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", ("obs_" + uuid.uuid4().hex, existing["rate_id"], version_id, artifact["artifact_id"], observation.get("destination") or "", observation.get("weight_break_label") or "", observation.get("amount"), observation.get("currency"), observation.get("unit"), _iso_now(), observation.get("raw_text") or "", _json(observation.get("locator") or {})))
                base_amount = _decimal(existing["amount_text"])
                observed_amount = _decimal(observation.get("amount"))
                if base_amount is not None and observed_amount is not None and base_amount != observed_amount:
                    group = existing["conflict_group_id"]
                    if not group:
                        group = "conflict_" + uuid.uuid4().hex
                        values = conn.execute("SELECT source_artifact_id,amount_text FROM rate_value_observation WHERE rate_id=?", (existing["rate_id"],)).fetchall()
                        conn.execute("INSERT INTO conflict_group(conflict_group_id,version_id,match_key_json,value_json,created_at) VALUES(?,?,?,?,?)", (group, version_id, _json({"destination": existing["destination_airport"], "weight_break_label": existing["weight_break_label"], "currency": existing["currency"], "unit": existing["pricing_unit"]}), _json([dict(row) for row in values]), _iso_now()))
                        conn.execute("UPDATE route_rate SET conflict_group_id=?,status='conflict' WHERE rate_id=?", (group, existing["rate_id"]))
                for field_name, raw_value in (("destination_airport", observation.get("destination")), ("weight_break_label", observation.get("weight_break_label")), ("amount", observation.get("amount")), ("currency", observation.get("currency")), ("pricing_unit", observation.get("unit"))):
                    duplicate = conn.execute("SELECT evidence_id FROM field_evidence WHERE entity_type='route_rate' AND entity_id=? AND field_name=? AND source_artifact_id=? AND raw_value_json=?", (existing["rate_id"], field_name, artifact["artifact_id"], _json(raw_value))).fetchone()
                    if not duplicate:
                        conn.execute("""INSERT INTO field_evidence(evidence_id,entity_type,entity_id,field_name,raw_value_json,normalized_value_json,locator_json,source_artifact_id,parser_version,confidence,extraction_method)
                                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""", ("evidence_" + uuid.uuid4().hex, "route_rate", existing["rate_id"], field_name, _json(raw_value), _json(raw_value), _json(observation.get("locator") or {}), artifact["artifact_id"], AIRFREIGHT_PARSER_VERSION, "0.98" if raw_value not in (None, "") else "0.40", result.get("parser_path") or "native_structure"))
            for field in result.get("fields", []):
                entity_id = version_ids.get("ET-071") or next(iter(version_ids.values()), batch_id)
                duplicate = conn.execute("SELECT evidence_id FROM field_evidence WHERE entity_type='rate_card_version' AND entity_id=? AND field_name=? AND source_artifact_id=? AND raw_value_json=?", (entity_id, field.get("field") or "unknown", artifact["artifact_id"], _json(field.get("raw")))).fetchone()
                if not duplicate:
                    conn.execute("""INSERT INTO field_evidence(evidence_id,entity_type,entity_id,field_name,raw_value_json,normalized_value_json,locator_json,source_artifact_id,parser_version,confidence,extraction_method)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""", ("evidence_" + uuid.uuid4().hex, "rate_card_version", entity_id, field.get("field") or "unknown", _json(field.get("raw")), _json(field.get("normalized")), _json(field.get("locator") or {}), artifact["artifact_id"], AIRFREIGHT_PARSER_VERSION, str(field.get("confidence") or "0"), result.get("parser_path") or "native_structure"))
            for surcharge in result.get("surcharges", []):
                version_id = version_ids.get("ET-071") or next(iter(version_ids.values()), None)
                if version_id:
                    exists = conn.execute("SELECT surcharge_id FROM surcharge_rule WHERE version_id=? AND name=? AND original_text=?", (version_id, surcharge.get("name") or "unknown", surcharge.get("original_text") or "")).fetchone()
                    if not exists:
                        conn.execute("""INSERT INTO surcharge_rule(surcharge_id,version_id,name,amount_text,currency,pricing_unit,charge_basis,minimum_charge_text,applicable_scope_json,effective_from,status,original_text,source_artifact_id)
                                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", ("surcharge_" + uuid.uuid4().hex, version_id, surcharge.get("name") or "unknown", surcharge.get("amount"), surcharge.get("currency"), surcharge.get("pricing_unit"), surcharge.get("charge_basis"), surcharge.get("minimum_charge"), _json(surcharge.get("scope") or {}), None, surcharge.get("status") or "pending", surcharge.get("original_text") or "", artifact["artifact_id"]))
            for restriction in result.get("restrictions", []):
                version_id = version_ids.get("ET-071") or next(iter(version_ids.values()), None)
                if version_id:
                    destinations = [item.strip().upper() for item in str(restriction.get("destination") or "").split("/") if item.strip()] or [""]
                    for destination in destinations:
                        exists = conn.execute("SELECT restriction_id FROM route_restriction WHERE version_id=? AND destination_airport=? AND original_text=?", (version_id, destination, restriction.get("original_text"))).fetchone()
                        if not exists:
                            conn.execute("""INSERT INTO route_restriction(restriction_id,version_id,destination_airport,restriction_type,cargo_condition,original_text,status,source_artifact_id)
                                           VALUES(?,?,?,?,?,?,?,?)""", ("restriction_" + uuid.uuid4().hex, version_id, destination, restriction.get("restriction_type") or "inquiry_required", restriction.get("cargo_condition"), restriction.get("original_text") or "", "pending", artifact["artifact_id"]))
            for delta in result.get("internal_deltas", []):
                version_id = version_ids.get("ET-071") or next(iter(version_ids.values()), None)
                if version_id:
                    exists = conn.execute("SELECT rule_id FROM internal_rate_rule WHERE version_id=? AND adjustment_per_kg_text=?", (version_id, delta.get("adjustment_per_kg"))).fetchone()
                    if not exists:
                        conn.execute("""INSERT INTO internal_rate_rule(rule_id,version_id,formula,adjustment_per_kg_text,currency,applicable_scope_json,source_evidence_json)
                                       VALUES(?,?,?,?,?,?,?)""", ("internal_" + uuid.uuid4().hex, version_id, "internal_rate = supplier_base_rate + adjustment_per_kg", delta.get("adjustment_per_kg") or "0.20", None, _json({"destination": delta.get("destination"), "weight_breaks": list(WEIGHT_LABELS), "enabled_scope_confirmed": False}), _json(delta)))
        conflicts = conn.execute("SELECT COUNT(*) AS n FROM conflict_group WHERE version_id IN (SELECT version_id FROM rate_card_version WHERE source_batch_id=?) AND status='open'", (batch_id,)).fetchone()["n"]
        failed = conn.execute("SELECT COUNT(*) AS n FROM extraction_run WHERE artifact_id IN (SELECT artifact_id FROM source_artifact WHERE batch_id=?) AND status IN ('needs_manual_review','rejected')", (batch_id,)).fetchone()["n"]
        processed = conn.execute("SELECT COUNT(*) AS n FROM extraction_run WHERE artifact_id IN (SELECT artifact_id FROM source_artifact WHERE batch_id=?) AND status <> 'pending'", (batch_id,)).fetchone()["n"]
        candidates = conn.execute("SELECT COUNT(*) AS n FROM rate_card_version WHERE source_batch_id=?", (batch_id,)).fetchone()["n"]
        status = "needs_review" if failed or conflicts else ("parsed" if processed == len(artifacts) else "partial")
        summary = {"local_only": True, "processed_files": processed, "failed_files": failed, "candidate_versions": candidates, "conflict_groups": conflicts, "unresolved_weight_boundaries": True, "external_model_used": False}
        conn.execute("UPDATE import_batch SET status=?,processed_files=?,critical_failures=?,candidate_count=?,conflict_count=?,summary_json=? WHERE batch_id=?", (status, processed, failed, candidates, conflicts, _json(summary), batch_id))

    def _public_version(self, row: Any, conn: Any) -> dict[str, Any]:
        rates = [dict(item) for item in conn.execute("SELECT * FROM route_rate WHERE version_id=? ORDER BY destination_airport,weight_break_label", (row["version_id"],)).fetchall()]
        restrictions = [dict(item) for item in conn.execute("SELECT * FROM route_restriction WHERE version_id=?", (row["version_id"],)).fetchall()]
        surcharges = [dict(item) for item in conn.execute("SELECT * FROM surcharge_rule WHERE version_id=?", (row["version_id"],)).fetchall()]
        rules = [dict(item) for item in conn.execute("SELECT * FROM internal_rate_rule WHERE version_id=?", (row["version_id"],)).fetchall()]
        for item in surcharges:
            item["applicable_scope"] = _loads(item.pop("applicable_scope_json", "{}"), {})
        for item in rules:
            item["applicable_scope"] = _loads(item.pop("applicable_scope_json", "{}"), {})
            item["source_evidence"] = _loads(item.pop("source_evidence_json", "{}"), {})
        return {"version_id": row["version_id"], "business_line": row["business_line"], "supplier_name": row["supplier_name"], "airline_name": row["airline_name"], "airline_code": row["airline_code"], "origin_airport": row["origin_airport"], "hub_airport": row["hub_airport"], "version_date": row["version_date"], "valid_from": row["valid_from"], "valid_to": row["valid_to"], "validity_mode": row["validity_mode"], "currency": row["currency"], "status": row["status"], "review_status": row["review_status"], "published_current": bool(row["published_current"]), "weight_rule_confirmed": bool(row["weight_rule_confirmed"]), "source_batch_id": row["source_batch_id"], "rates": rates, "restrictions": restrictions, "surcharges": surcharges, "internal_rules": rules}

    def list_rate_cards(self, *, batch_id: str | None = None, view: str = "current", destination: str | None = None) -> dict[str, Any]:
        with self.storage.connect() as conn:
            clauses = ["business_line='airfreight'"]
            params: list[Any] = []
            if view == "current":
                clauses.append("published_current=1")
            elif view in {"candidates", "pending"}:
                clauses.append("review_status <> 'published'")
            if batch_id:
                clauses.append("source_batch_id=?"); params.append(batch_id)
            rows = conn.execute("SELECT * FROM rate_card_version WHERE " + " AND ".join(clauses) + " ORDER BY valid_from DESC,created_at DESC", params).fetchall()
            items = [self._public_version(row, conn) for row in rows]
            if destination:
                items = [item | {"rates": [rate for rate in item["rates"] if rate["destination_airport"] == destination.upper()]} for item in items]
        return {"items": items, "total": len(items), "view": view, "business_line": "airfreight"}

    def published_rate_card_sheet(self, version_id: str) -> dict[str, Any]:
        """Return a presentation-ready, source-linked published rate card."""
        matches = [item for item in self.list_rate_cards(view="all")["items"] if item["version_id"] == version_id]
        if not matches:
            raise AirfreightOperationError("rate_card_not_found", "找不到空运价卡版本", http_status=404)
        version = matches[0]
        if version.get("review_status") != "published":
            raise AirfreightOperationError("rate_card_not_published", "只有已发布价卡可以生成发布单", http_status=409)
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for rate in version.get("rates", []):
            key = (str(rate.get("destination_airport") or "—"), str(rate.get("routing") or "—"))
            row = grouped.setdefault(key, {"destination": key[0], "routing": key[1], **{label: "—" for label in WEIGHT_LABELS}})
            row[str(rate.get("weight_break_label") or "")] = rate.get("amount_text") or "INQUIRY"
        rows = [grouped[key] for key in sorted(grouped)]
        batch = self.get_batch(version["source_batch_id"])
        source_files = [
            {key: artifact.get(key) for key in ("artifact_id", "file_name", "actual_type", "preview_url", "download_url")}
            for artifact in batch.get("artifacts", []) if artifact.get("availability_status") == "available"
        ]
        valid_to = version.get("valid_to") or "Until further notice"
        number = "RC-" + re.sub(r"[^A-Za-z0-9]+", "-", f"{version.get('airline_code')}-{version.get('version_date') or version.get('valid_from') or 'CURRENT'}").strip("-").upper()
        return {
            "document_type": "published_rate_card", "document_number": number,
            "title": "Latest Published Airfreight Rate Card", "brand": "ZHONGJI LOGISTICS / 中技物流",
            "demo_label": "DEMO — NOT SENT", "version_id": version_id, "airline_name": version.get("airline_name"),
            "airline_code": version.get("airline_code"), "origin": version.get("origin_airport"), "hub": version.get("hub_airport"),
            "currency": version.get("currency"), "unit": next((rate.get("pricing_unit") for rate in version.get("rates", []) if rate.get("pricing_unit")), "KG"),
            "valid_from": version.get("valid_from"), "valid_to": valid_to, "published": True,
            "weight_breaks": list(WEIGHT_LABELS), "rows": rows, "source_files": source_files,
            "source_batch_id": version.get("source_batch_id"),
            "pdf_url": f"/api/airfreight/rate-cards/{version_id}/published-sheet.pdf",
        }

    def published_rate_card_pdf(self, version_id: str) -> tuple[bytes, str]:
        document = self.published_rate_card_sheet(version_id)
        rows = [
            [row["destination"], row["routing"], *(row.get(label) or "—" for label in WEIGHT_LABELS)]
            for row in document["rows"]
        ]
        pdf = _table_pdf(
            title="PUBLISHED AIRFREIGHT RATE CARD", number=document["document_number"],
            details=(
                f"Airline: {document['airline_name']} ({document['airline_code']})  |  Origin: {document['origin']}  |  Hub: {document['hub'] or '-'}",
                f"Currency: {document['currency']}  |  Unit: {document['unit']}  |  Valid: {document['valid_from']} to {document['valid_to']}",
            ),
            columns=(("Destination", 94), ("Routing", 174), ("+45", 86), ("+100", 86), ("+300", 86), ("+500", 86), ("+1000", 86)),
            rows=rows,
            notes=("Published from locally processed source files; source evidence remains available in the demo.",
                   "Weight breaks follow the explicitly reviewed demo boundary policy.", "DEMO ONLY - no customer or WeCom message was sent."),
            marker="DEMO_PUBLISHED_RATE_CARD",
        )
        filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", document["document_number"]) + ".pdf"
        return pdf, filename

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        with self.storage.connect() as conn:
            batch = conn.execute("SELECT * FROM import_batch WHERE batch_id=? AND business_line='airfreight'", (batch_id,)).fetchone()
            if batch is None:
                raise AirfreightOperationError("batch_not_found", "找不到空运解析批次", http_status=404)
            artifacts = conn.execute("SELECT * FROM source_artifact WHERE batch_id=? ORDER BY file_name,artifact_id", (batch_id,)).fetchall()
            runs = {row["artifact_id"]: row for row in conn.execute("SELECT * FROM extraction_run WHERE artifact_id IN (SELECT artifact_id FROM source_artifact WHERE batch_id=?) ORDER BY started_at", (batch_id,)).fetchall()}
            versions = conn.execute("SELECT * FROM rate_card_version WHERE source_batch_id=? ORDER BY airline_code", (batch_id,)).fetchall()
            conflict_rows = conn.execute("SELECT * FROM conflict_group WHERE version_id IN (SELECT version_id FROM rate_card_version WHERE source_batch_id=?)", (batch_id,)).fetchall()
            result = {"batch_id": batch["batch_id"], "batch_kind": batch["batch_kind"], "source_scope": _loads(batch["source_scope_json"], {}), "request_id": batch["request_id"], "created_at": batch["created_at"], "status": batch["status"], "total_files": batch["total_files"], "processed_files": batch["processed_files"], "critical_failures": batch["critical_failures"], "candidate_count": batch["candidate_count"], "conflict_count": batch["conflict_count"], "summary": _loads(batch["summary_json"], {}), "artifacts": []}
            for artifact in artifacts:
                item = self._public_artifact(artifact)
                run = runs.get(artifact["artifact_id"])
                item["extraction"] = {"extraction_id": run["extraction_id"], "status": run["status"], "parser_version": run["parser_version"], "parser_path": run["parser_path"], "page_count": run["page_count"], "sheet_count": run["sheet_count"], "warnings": _loads(run["warning_json"], []), "errors": _loads(run["error_json"], []), "result": _loads(run["result_json"], {})} if run else None
                result["artifacts"].append(item)
            result["rate_cards"] = [self._public_version(row, conn) for row in versions]
            result["conflicts"] = [dict(row) | {"match_key": _loads(row["match_key_json"], {}), "values": _loads(row["value_json"], [])} for row in conflict_rows]
        return result

    def list_conflicts(self, batch_id: str | None = None) -> list[dict[str, Any]]:
        with self.storage.connect() as conn:
            if batch_id:
                rows = conn.execute("SELECT * FROM conflict_group WHERE version_id IN (SELECT version_id FROM rate_card_version WHERE source_batch_id=?) AND status='open'", (batch_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM conflict_group WHERE status='open' ORDER BY created_at DESC").fetchall()
        return [dict(row) | {"match_key": _loads(row["match_key_json"], {}), "values": _loads(row["value_json"], [])} for row in rows]

    def field_evidence(self, *, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        with self.storage.connect() as conn:
            rows = conn.execute("SELECT * FROM field_evidence WHERE business_line='airfreight' AND entity_type=? AND entity_id=? ORDER BY evidence_id", (entity_type, entity_id)).fetchall()
        return [dict(row) | {"raw_value": _loads(row["raw_value_json"]), "normalized_value": _loads(row["normalized_value_json"]), "locator": _loads(row["locator_json"], {})} for row in rows]

    def resolve_conflict(self, conflict_group_id: str, *, rate_id: str | None = None, corrected_amount: Any = None,
                         actor_id: str | None, actor_name: str | None, reason: str, idempotency_key: str) -> dict[str, Any]:
        actor_id, actor_name = self._require_reviewer(actor_id, actor_name)
        with self.storage.connect() as conn:
            prior = conn.execute("SELECT decision_id FROM review_decision WHERE business_line='airfreight' AND idempotency_key=? AND action='resolve_conflict'", (idempotency_key,)).fetchone()
            if prior:
                return {"conflict_group_id": conflict_group_id, "resolved": True, "idempotent": True, "decision_id": prior["decision_id"]}
            group = conn.execute("SELECT * FROM conflict_group WHERE conflict_group_id=? AND status='open'", (conflict_group_id,)).fetchone()
            if group is None:
                raise AirfreightOperationError("conflict_not_found", "找不到开放中的冲突组", http_status=404)
            if not rate_id:
                rate_id = conn.execute("SELECT rate_id FROM route_rate WHERE conflict_group_id=?", (conflict_group_id,)).fetchone()[0]
            rate = conn.execute("SELECT * FROM route_rate WHERE rate_id=?", (rate_id,)).fetchone()
            if rate is None:
                raise AirfreightOperationError("rate_not_found", "找不到冲突费率")
            before = dict(rate)
            if corrected_amount not in (None, ""):
                amount = _decimal_text(corrected_amount)
                if amount is None or _decimal(amount) < 0:
                    raise AirfreightOperationError("invalid_rate", "修正费率必须是非负十进制")
                conn.execute("UPDATE route_rate SET amount_text=?,selected_at=?,status='candidate' WHERE rate_id=?", (amount, _iso_now(), rate_id))
            else:
                conn.execute("UPDATE route_rate SET status='candidate',selected_at=? WHERE rate_id=?", (_iso_now(), rate_id))
            conn.execute("UPDATE conflict_group SET status='resolved',resolved_at=?,resolved_by=?,resolution_reason=? WHERE conflict_group_id=?", (_iso_now(), actor_id, reason or "", conflict_group_id))
            after = dict(conn.execute("SELECT * FROM route_rate WHERE rate_id=?", (rate_id,)).fetchone())
            decision = self._record_decision(conn, object_type="conflict_group", object_id=conflict_group_id, action="resolve_conflict", actor_id=actor_id, actor_name=actor_name, reason=reason, idempotency_key=idempotency_key, before=before, after=after)
        return {"conflict_group_id": conflict_group_id, "resolved": True, "rate": after, **decision}

    def publish_rate_card(self, version_id: str, *, actor_id: str | None, actor_name: str | None, reason: str, idempotency_key: str) -> dict[str, Any]:
        actor_id, actor_name = self._require_reviewer(actor_id, actor_name)
        with self.storage.connect() as conn:
            version = conn.execute("SELECT * FROM rate_card_version WHERE version_id=? AND business_line='airfreight'", (version_id,)).fetchone()
            if version is None:
                raise AirfreightOperationError("rate_card_not_found", "找不到空运价卡版本", http_status=404)
            existing_decision = conn.execute("SELECT * FROM review_decision WHERE business_line='airfreight' AND idempotency_key=? AND action='publish_rate_card'", (idempotency_key,)).fetchone()
            if existing_decision:
                return {"version_id": version_id, "published": True, "idempotent": True, "decision_id": existing_decision["decision_id"]}
            conflicts = conn.execute("SELECT COUNT(*) AS n FROM conflict_group WHERE version_id=? AND status='open'", (version_id,)).fetchone()["n"]
            if conflicts:
                raise AirfreightOperationError("conflict_requires_resolution", "同键不同值冲突尚未解决，不能发布", details={"conflicts": self.list_conflicts(version["source_batch_id"])})
            before_current = conn.execute("SELECT * FROM rate_card_version WHERE airline_code=? AND published_current=1 ORDER BY valid_from DESC LIMIT 1", (version["airline_code"],)).fetchone()
            if before_current and before_current["valid_from"] and version["valid_from"] and str(version["valid_from"]) < str(before_current["valid_from"]):
                decision = self._record_decision(conn, object_type="rate_card_version", object_id=version_id, action="publish_rate_card", actor_id=actor_id, actor_name=actor_name, reason=reason, idempotency_key=idempotency_key, before=dict(before_current), after={"not_current": True})
                return {"version_id": version_id, "published": False, "not_current": True, "source_preserved": True, **decision}
            conn.execute("UPDATE rate_card_version SET published_current=0,status=CASE WHEN version_id=? THEN status ELSE 'superseded' END WHERE airline_code=? AND published_current=1", (version_id, version["airline_code"]))
            conn.execute("UPDATE rate_card_version SET published_current=1,status='published',review_status='published' WHERE version_id=?", (version_id,))
            conn.execute("UPDATE route_rate SET status=CASE WHEN status='inquiry_required' THEN status ELSE 'published' END WHERE version_id=?", (version_id,))
            decision = self._record_decision(conn, object_type="rate_card_version", object_id=version_id, action="publish_rate_card", actor_id=actor_id, actor_name=actor_name, reason=reason, idempotency_key=idempotency_key, before=dict(before_current) if before_current else {}, after=dict(conn.execute("SELECT * FROM rate_card_version WHERE version_id=?", (version_id,)).fetchone()))
        return {"version_id": version_id, "published": True, "source_preserved": True, **decision}

    def confirm_weight_rules(self, version_id: str, *, boundaries: Mapping[str, Any],
                             actor_id: str | None, actor_name: str | None, reason: str,
                             idempotency_key: str) -> dict[str, Any]:
        """Persist an explicit, reviewer-approved interpretation of ET labels.

        ``+45`` etc. remain source labels.  They only become selectable after
        this operation records all edge behavior that the source table itself
        does not prove.  No nearest-break fallback exists anywhere else.
        """
        actor_id, actor_name = self._require_reviewer(actor_id, actor_name)
        if not isinstance(boundaries, Mapping):
            raise AirfreightOperationError("weight_rule_required", "必须提供重量档边界配置", http_status=400)
        required = ("boundary_policy", "below_45", "above_1000", "rounding", "hit_order")
        missing = [key for key in required if not str(boundaries.get(key) or "").strip()]
        if missing:
            raise AirfreightOperationError("weight_rule_incomplete", "重量档边界、超范围、取整和命中顺序必须由人工明确确认", http_status=422,
                                            details={"missing": missing})
        if str(boundaries["boundary_policy"]) != "lower_inclusive_upper_exclusive":
            raise AirfreightOperationError("unsupported_weight_rule", "本地 Demo 当前只支持明确的下限包含、上限不包含规则", http_status=422)
        order = list(boundaries["hit_order"]) if isinstance(boundaries["hit_order"], (list, tuple)) else []
        if order != list(WEIGHT_LABELS):
            raise AirfreightOperationError("weight_rule_order_invalid", "命中顺序必须明确列出 +45、+100、+300、+500、+1000", http_status=422)
        with self.storage.connect() as conn:
            row = conn.execute("SELECT * FROM rate_card_version WHERE version_id=? AND business_line='airfreight'", (version_id,)).fetchone()
            if row is None:
                raise AirfreightOperationError("rate_card_not_found", "找不到空运价卡版本", http_status=404)
            existing = conn.execute("SELECT * FROM review_decision WHERE business_line='airfreight' AND action='confirm_weight_rules' AND idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                return {"version_id": version_id, "weight_rule_confirmed": True, "idempotent": True, "decision_id": existing["decision_id"]}
            source = _loads(row["source_json"], {}) or {}
            source["weight_rule_config"] = dict(boundaries)
            conn.execute("UPDATE rate_card_version SET weight_rule_confirmed=1,source_json=? WHERE version_id=?", (_json(source), version_id))
            decision = self._record_decision(conn, object_type="rate_card_version", object_id=version_id,
                                             action="confirm_weight_rules", actor_id=actor_id, actor_name=actor_name,
                                             reason=reason, idempotency_key=idempotency_key,
                                             before={"weight_rule_confirmed": bool(row["weight_rule_confirmed"])},
                                             after={"weight_rule_confirmed": True, "boundaries": dict(boundaries)})
        return {"version_id": version_id, "weight_rule_confirmed": True, "boundaries": dict(boundaries), **decision}

    def confirm_internal_rule(self, rule_id: str, *, actor_id: str | None, actor_name: str | None, reason: str, idempotency_key: str) -> dict[str, Any]:
        actor_id, actor_name = self._require_reviewer(actor_id, actor_name)
        with self.storage.connect() as conn:
            row = conn.execute("SELECT * FROM internal_rate_rule WHERE rule_id=?", (rule_id,)).fetchone()
            if row is None:
                raise AirfreightOperationError("internal_rule_not_found", "找不到内部费率规则候选", http_status=404)
            scope = _loads(row["applicable_scope_json"], {})
            scope["enabled_scope_confirmed"] = True
            conn.execute("UPDATE internal_rate_rule SET status='enabled',currency=COALESCE(currency,(SELECT currency FROM rate_card_version WHERE version_id=?)),applicable_scope_json=?,confirmed_by=?,confirmed_at=?,reason=? WHERE rule_id=?", (row["version_id"], _json(scope), actor_id, _iso_now(), reason or "", rule_id))
            decision = self._record_decision(conn, object_type="internal_rate_rule", object_id=rule_id, action="confirm_internal_rule", actor_id=actor_id, actor_name=actor_name, reason=reason, idempotency_key=idempotency_key, before=dict(row), after=dict(conn.execute("SELECT * FROM internal_rate_rule WHERE rule_id=?", (rule_id,)).fetchone()))
        return {"rule_id": rule_id, "status": "enabled", **decision}

    # ------------------------------------------------------------------
    # Local chat discovery, preview, import, evidence and quotations
    # ------------------------------------------------------------------
    @staticmethod
    def _demo_messages() -> list[dict[str, Any]]:
        """Deterministic, explicitly synthetic test-only messages.

        They are never included by the normal local HTTP service.  Keeping the
        fixture here lets contract tests exercise recursive evidence without
        checking real chats into Git.
        """
        return [
            {"message_id": "demo-msg-001", "author_id": "zhongji-ops", "author_name": "中技报价专员", "author_company": "中技", "explicit_role": "zhongji", "sent_at": "2026-09-03T09:00:00+08:00", "message_type": "text", "text": "请询 ET：HKG-ADD-BRU 与 TLV，附件是多票货物截图。", "reply_to_message_id": None, "parent_path": [], "attachment_refs": [{"name": "cargo-multi-ticket.png", "artifact": "available"}]},
            {"message_id": "demo-msg-002", "author_id": "supplier-et", "author_name": "ET 代理", "author_company": "Ethiopian Airlines", "explicit_role": "supplier", "sent_at": "2026-09-03T09:03:00+08:00", "message_type": "file", "text": "ET-HK-2026.8.19.pdf / ET-HK-2026.8.19.xls，BRU 五档；目的地附加费需单询。", "reply_to_message_id": "demo-msg-001", "parent_path": [], "attachment_refs": [{"name": "ET-HK-2026.8.19.pdf", "artifact": "available"}, {"name": "ET-HK-2026.8.19.xls", "artifact": "available"}]},
            {"message_id": "demo-msg-003", "author_id": "supplier-forwarder", "author_name": "转发代理", "author_company": "代理公司", "explicit_role": "supplier", "sent_at": "2026-09-03T09:06:00+08:00", "message_type": "forward", "text": "合并转发：BLR/BOM/DEL/MAA via ADD；over 1000 kgs 需单询。", "reply_to_message_id": "demo-msg-001", "parent_path": ["demo-msg-003", "forwarded-child-1"], "attachment_refs": [{"name": "sdkfileid:missing-001", "artifact": "referenced_but_unavailable"}]},
            {"message_id": "demo-msg-004", "author_id": "zhongji-sales", "author_name": "中技业务", "author_company": "中技", "explicit_role": "zhongji", "sent_at": "2026-09-03T09:10:00+08:00", "message_type": "text", "text": "请保留原图证据；暂不把未取得的附件算入完整性。", "reply_to_message_id": "demo-msg-003", "parent_path": [], "attachment_refs": []},
        ]

    @staticmethod
    def _snapshot_token(path: Path) -> str:
        stat = path.stat()
        # A token can verify the exact local snapshot without returning its
        # absolute path to the page, logs or analysis tables.
        material = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8", "surrogatepass")
        return "snapshot_" + _sha256(material)[:24]

    @contextmanager
    def _readonly_source_connection(self, path: Path) -> Iterable[sqlite3.Connection]:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _account_display(account_id: str) -> str:
        value = str(account_id or "")
        if len(value) <= 4:
            return "本地账号 " + (value or "未知")
        return "本地账号 ···" + value[-4:]

    def _fixture_sources(self) -> list[dict[str, Any]]:
        if not self.include_demo_fixtures:
            return []
        rows = []
        for account_id, snapshot, conversation_id, count in (
            (DEMO_ACCOUNT, DEMO_SNAPSHOT, DEMO_CONVERSATION, 4),
            ("demo-account-2", "demo-snapshot-20260903-b", "demo-conversation-cosplay-2", 2),
        ):
            source_key = "fixture_" + _sha256(f"{account_id}|{snapshot}|{conversation_id}".encode())[:24]
            rows.append({
                "source_key": source_key, "source_snapshot": snapshot, "account_id": account_id,
                "account_display": self._account_display(account_id), "conversation_id": conversation_id,
                "conversation_name": DEMO_CONVERSATION_NAME, "source_database": "fixture",
                "source_type": "脱敏合成测试夹具", "authentication_level": "synthetic_fixture",
                "snapshot_at": "2026-09-03T18:00:00+08:00", "start_at": "2026-09-03T08:00:00+08:00",
                "end_at": "2026-09-03T18:00:00+08:00", "message_count": count,
                "fixture": True, "path": None,
            })
        return rows

    def _source_catalog(self) -> list[dict[str, Any]]:
        """Discover metadata from configured local analysis snapshots only.

        The query intentionally contains no message text column.  It is safe
        to call from a GET route and does not create analysis rows, perform a
        rebuild, or read another conversation's body.
        """
        items = self._fixture_sources()
        for path in self.source_db_paths:
            if not path.is_file():
                continue
            try:
                snapshot = self._snapshot_token(path)
                with self._readonly_source_connection(path) as conn:
                    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if "messages" not in tables:
                        continue
                    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
                    required = {"account_id", "source_database", "conversation_id", "conversation_name", "sent_at"}
                    if not required.issubset(columns):
                        continue
                    rows = conn.execute(
                        """SELECT account_id,source_database,conversation_id,conversation_name,
                                  MIN(sent_at) AS start_at,MAX(sent_at) AS end_at,COUNT(*) AS message_count
                           FROM messages
                           WHERE source_database NOT LIKE '%:forwarded:%'
                           GROUP BY account_id,source_database,conversation_id,conversation_name
                           ORDER BY conversation_name,account_id,conversation_id
                           LIMIT 500"""
                    ).fetchall()
                snapshot_at = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
                for row in rows:
                    source_key = "source_" + _sha256(
                        f"{snapshot}|{row['account_id']}|{row['source_database']}|{row['conversation_id']}".encode("utf-8", "surrogatepass")
                    )[:24]
                    items.append({
                        "source_key": source_key, "source_snapshot": snapshot,
                        "account_id": row["account_id"], "account_display": self._account_display(row["account_id"]),
                        "conversation_id": row["conversation_id"], "conversation_name": row["conversation_name"],
                        "source_database": row["source_database"], "source_type": "本机规范化企微快照",
                        "authentication_level": "normalized_local_snapshot", "snapshot_at": snapshot_at,
                        "start_at": row["start_at"], "end_at": row["end_at"], "message_count": row["message_count"],
                        "fixture": False, "path": path,
                    })
            except (OSError, sqlite3.Error):
                # A locked, unavailable or malformed local file simply does
                # not become a selectable source.  Its path is never exposed.
                continue
        return items

    @staticmethod
    def _public_source(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "source_key": item["source_key"], "source_snapshot": item["source_snapshot"],
            "account_id": item["account_id"], "account_display": item["account_display"],
            "conversation_id": item["conversation_id"], "conversation_name": item["conversation_name"],
            "source_type": item["source_type"], "authentication_level": item["authentication_level"],
            "snapshot_at": item["snapshot_at"], "start_at": item["start_at"], "end_at": item["end_at"],
            "message_count": item["message_count"], "fixture": bool(item.get("fixture")),
            "explicit_selection_required": True,
        }

    def list_chat_sources(self) -> dict[str, Any]:
        records = self._source_catalog()
        target_candidates = [
            item for item in records if not item.get("fixture") and item["conversation_name"] == DEMO_CONVERSATION_NAME
            and str(item["start_at"] or "")[:10] <= "2026-09-03" <= str(item["end_at"] or "")[:10]
        ]
        return {
            "items": [self._public_source(item) for item in records], "total": len(records),
            "default_search": DEMO_CONVERSATION_NAME, "local_only": True, "body_loaded": False,
            "default_target": {
                "conversation_name": DEMO_CONVERSATION_NAME, "target_date": "2026-09-03",
                "candidate_count": len(target_candidates),
                "status": "ready_for_preview" if len(target_candidates) == 1 else ("ambiguous" if target_candidates else "not_found"),
                "message": "仅列出本机可得元数据；未读取聊天正文。",
            },
            "synthetic_demo_available": any(item.get("fixture") for item in records),
            "synthetic_demo_notice": "合成演示数据始终与真实本机来源分开标记，不能作为真实记录或正式报价依据。",
        }

    def _activate_synthetic_demo_import(self, *, actor_id: str | None, actor_name: str | None,
                                        idempotency_key: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Create a separate, explicitly synthetic import for a presenter.

        This is intentionally invoked only from a user-selected B5 retry.  A
        failed real-source completeness check therefore remains visible in its
        own import and event history instead of being overwritten by a fixture.
        """
        fixture = next((item for item in self._source_catalog() if item.get("fixture")), None)
        if fixture is None:
            raise AirfreightOperationError(
                "synthetic_demo_unavailable",
                "当前服务未启用合成演示数据；请使用带演示数据的本地 Demo 服务。",
                http_status=409,
            )
        scope = {
            "source_key": fixture["source_key"],
            "start_at": fixture["start_at"],
            "end_at": fixture["end_at"],
        }
        preview = self.preview_chat(scope)
        imported = self.confirm_chat_import({
            "preview_id": preview["preview_id"],
            "preview_digest": preview["preview_digest"],
            "scope": preview["scope"],
            "idempotency_key": idempotency_key,
        }, actor_id=actor_id, actor_name=actor_name)
        return preview, imported

    def resolve_chat_scope(self, scope: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve an explicit selection against the server-owned catalog."""
        if not isinstance(scope, Mapping):
            raise AirfreightOperationError("chat_scope_required", "必须选择本机来源范围", http_status=400)
        catalog = self._source_catalog()
        source_key = str(scope.get("source_key") or "")
        if source_key:
            matches = [item for item in catalog if item["source_key"] == source_key]
        else:
            required = ("account_id", "source_snapshot", "conversation_id", "conversation_name")
            if not all(scope.get(key) for key in required):
                raise AirfreightOperationError("chat_scope_required", "必须选择账号、来源快照、会话和时间范围", http_status=400)
            matches = [item for item in catalog if all(str(item[key]) == str(scope[key]) for key in required)]
        if not matches:
            raise AirfreightOperationError("source_snapshot_unavailable", "所选本机来源快照不可用或已变化，请重新发现来源", http_status=409)
        if len(matches) != 1:
            raise AirfreightOperationError("ambiguous_chat_scope", "同名或重复来源无法唯一确定，请选择明确账号、快照和会话", http_status=409,
                                            details={"candidate_count": len(matches)})
        selected = matches[0]
        # A client cannot switch any part of an opaque selection after the
        # catalog lookup.  This prevents cross-account/source scope writes.
        for key in ("account_id", "source_snapshot", "conversation_id", "conversation_name"):
            if scope.get(key) not in (None, "") and str(scope[key]) != str(selected[key]):
                raise AirfreightOperationError("source_scope_mismatch", "所选范围与本机来源记录不一致", http_status=409)
        start_at, end_at = str(scope.get("start_at") or ""), str(scope.get("end_at") or "")
        if not start_at or not end_at:
            raise AirfreightOperationError("chat_scope_required", "必须选择起止时间", http_status=400)
        try:
            start = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
            end = datetime.fromisoformat(end_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AirfreightOperationError("invalid_chat_time_range", "时间范围格式无效", http_status=400) from exc
        if end <= start:
            raise AirfreightOperationError("invalid_chat_time_range", "结束时间必须晚于开始时间", http_status=400)
        safe_scope = {
            "source_key": selected["source_key"], "account_id": selected["account_id"],
            "source_snapshot": selected["source_snapshot"], "conversation_id": selected["conversation_id"],
            "conversation_name": selected["conversation_name"], "start_at": start_at, "end_at": end_at,
        }
        # The root is discovered from the selected range during preview.  It
        # is later echoed only as part of that bound preview, never accepted
        # as a free-form cross-conversation selector.
        requested_root = str(scope.get("target_root_message_id") or "")
        if requested_root:
            safe_scope["target_root_message_id"] = requested_root
        exact_target = (not selected.get("fixture") and selected["conversation_name"] == DEMO_CONVERSATION_NAME
                        and start.date().isoformat() <= "2026-09-03" <= end.date().isoformat())
        return {
            "record": selected, "scope": safe_scope, "selection": self._public_source(selected),
            "default_target_status": "synthetic_demo" if selected.get("fixture") else (
                "candidate_requires_preview" if exact_target else "selected_nondefault_range"
            ),
        }

    def _scope_key(self, scope: Mapping[str, Any]) -> str:
        return _sha256(_json({key: scope.get(key) for key in ("source_key", "account_id", "source_snapshot", "conversation_id", "conversation_name", "start_at", "end_at")}).encode())

    @staticmethod
    def _attachment_refs_from_source_row(row: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Return only locally evidenced attachment availability.

        Existing normalized messages may carry a media reference but not a
        binary replica.  We never fetch a remote sdkfileid or invent a file
        preview, so the conservative default is referenced_but_unavailable.
        """
        message_type = str(row.get("message_type") or "").lower()
        provenance = _loads(row.get("provenance_json"), {}) or {}
        attachments = provenance.get("attachments") or provenance.get("attachment_refs") or []
        if isinstance(attachments, Mapping):
            attachments = [attachments]
        output: list[dict[str, Any]] = []
        for item in attachments if isinstance(attachments, list) else []:
            if isinstance(item, Mapping):
                name = str(item.get("name") or item.get("file_name") or item.get("sdkfileid") or "本地来源附件引用")
                local_copy = bool(item.get("local_copy_verified"))
                output.append({"name": name[:160], "artifact": "available" if local_copy else "referenced_but_unavailable"})
        if not output and message_type in {"file", "image", "attachment", "video"}:
            output.append({"name": f"{message_type} 附件引用", "artifact": "referenced_but_unavailable"})
        return output

    @staticmethod
    def _explicit_role_from_source_row(row: Mapping[str, Any]) -> str:
        provenance = _loads(row.get("provenance_json"), {}) or {}
        explicit = str(provenance.get("explicit_role") or provenance.get("business_role") or "")
        if explicit in {"zhongji", "supplier", "unknown"}:
            return explicit
        # The existing normalizer's subject bucket is a recorded source
        # classification, not a string match against a company name.
        return "zhongji" if row.get("subject_bucket") == "zhongji" else "unknown"

    @staticmethod
    def _source_row_identifiers(row: Mapping[str, Any]) -> set[str]:
        return {
            str(row[key]) for key in ("id", "message_id", "root_message_id")
            if row.get(key) not in (None, "")
        }

    def _collect_source_messages(
        self,
        record: Mapping[str, Any],
        start_at: str,
        end_at: str,
        *,
        target_root_message_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
        """Read only the selected roots, their exact forward trees and context.

        For the default real scenario a unique outer ``群聊的聊天记录``
        message narrows the otherwise date-wide selection to that one root.
        Adjacent messages help explain the root but do not pull their own
        forward trees into the import.
        """
        if record.get("fixture"):
            messages = [dict(item) for item in self._demo_messages() if start_at <= item["sent_at"] <= end_at]
            return messages, [], {
                "candidate_root_count": 0, "target_root_message_id": None,
                "target_root_source_row_id": None, "target_root_unique": False,
            }
        path = record.get("path")
        if not isinstance(path, Path) or not path.is_file() or self._snapshot_token(path) != record["source_snapshot"]:
            raise AirfreightOperationError("source_snapshot_changed", "本机来源快照已经变化，请重新预览", http_status=409)
        try:
            with self._readonly_source_connection(path) as conn:
                selected_rows = [dict(row) for row in conn.execute(
                    """SELECT * FROM messages WHERE account_id=? AND source_database=? AND conversation_id=?
                       AND sent_at>=? AND sent_at<=? ORDER BY sent_at,id""",
                    (record["account_id"], record["source_database"], record["conversation_id"], start_at, end_at),
                ).fetchall()]
                candidates = [dict(row) for row in conn.execute(
                    """SELECT * FROM messages
                       WHERE account_id=? AND ((source_database=? AND conversation_id=?) OR source_database LIKE ?)
                       ORDER BY sent_at,id LIMIT 5000""",
                    (record["account_id"], record["source_database"], record["conversation_id"], record["source_database"] + ":forwarded:%"),
                ).fetchall()]

                marker_roots = [row for row in selected_rows if "群聊的聊天记录" in str(row.get("text") or "")]
                requested_root = str(target_root_message_id or "")
                root_row: dict[str, Any] | None = None
                if requested_root:
                    matches = [row for row in marker_roots if requested_root in self._source_row_identifiers(row)]
                    if len(matches) != 1:
                        raise AirfreightOperationError("target_root_unavailable", "预览绑定的目标外层消息已变化或不在所选范围内，请重新预览", http_status=409)
                    root_row = matches[0]
                elif (record["conversation_name"] == DEMO_CONVERSATION_NAME
                      and start_at[:10] <= "2026-09-03" <= end_at[:10]
                      and len(marker_roots) == 1):
                    root_row = marker_roots[0]

                root_message_id = str(root_row.get("message_id") or root_row.get("id")) if root_row else None
                if root_row:
                    core_rows = [root_row]
                    # Context is measured around the actual target root, not
                    # around the user's broad date-range boundary.
                    root_time = str(root_row.get("sent_at") or "")
                    before = [dict(row) for row in conn.execute(
                        """SELECT * FROM messages WHERE account_id=? AND source_database=? AND conversation_id=?
                           AND sent_at<? ORDER BY sent_at DESC,id DESC LIMIT 1""",
                        (record["account_id"], record["source_database"], record["conversation_id"], root_time),
                    ).fetchall()]
                    after = [dict(row) for row in conn.execute(
                        """SELECT * FROM messages WHERE account_id=? AND source_database=? AND conversation_id=?
                           AND sent_at>? ORDER BY sent_at,id LIMIT 1""",
                        (record["account_id"], record["source_database"], record["conversation_id"], root_time),
                    ).fetchall()]
                else:
                    core_rows = selected_rows
                    before = [dict(row) for row in conn.execute(
                        """SELECT * FROM messages WHERE account_id=? AND source_database=? AND conversation_id=?
                           AND sent_at<? ORDER BY sent_at DESC,id DESC LIMIT 1""",
                        (record["account_id"], record["source_database"], record["conversation_id"], start_at),
                    ).fetchall()]
                    after = [dict(row) for row in conn.execute(
                        """SELECT * FROM messages WHERE account_id=? AND source_database=? AND conversation_id=?
                           AND sent_at>? ORDER BY sent_at,id LIMIT 1""",
                        (record["account_id"], record["source_database"], record["conversation_id"], end_at),
                    ).fetchall()]
        except sqlite3.Error as exc:
            raise AirfreightOperationError("source_read_failed", "无法读取所选本机来源快照", http_status=422) from exc

        included: dict[str, dict[str, Any]] = {}
        context_identifiers: set[str] = set()
        for row in core_rows:
            included[str(row.get("id") or row.get("message_id"))] = row
        for row in [*before, *after]:
            included[str(row.get("id") or row.get("message_id"))] = row
            context_identifiers.update(self._source_row_identifiers(row))

        # Only the selected roots seed recursive branch expansion.  Adjacent
        # context remains a one-message context window by design.
        branch_identifiers: set[str] = set()
        for row in core_rows:
            branch_identifiers.update(self._source_row_identifiers(row))
        changed = True
        while changed:
            changed = False
            for row in candidates:
                key = str(row.get("id") or row.get("message_id"))
                if key in included:
                    continue
                if str(row.get("parent_id") or "") in branch_identifiers or str(row.get("root_message_id") or "") in branch_identifiers:
                    included[key] = row
                    branch_identifiers.update(self._source_row_identifiers(row))
                    changed = True

        rows = sorted(included.values(), key=lambda value: (str(value.get("sent_at") or ""), str(value.get("id") or "")))
        by_id = {str(row.get("id")): row for row in rows if row.get("id")}
        by_message_id = {str(row.get("message_id")): row for row in rows if row.get("message_id")}
        messages: list[dict[str, Any]] = []
        for row in rows:
            path_values: list[str] = []
            parent = str(row.get("parent_id") or "")
            visited: set[str] = set()
            while parent and parent not in visited:
                visited.add(parent)
                parent_row = by_id.get(parent) or by_message_id.get(parent)
                if not parent_row:
                    break
                parent_message = str(parent_row.get("message_id") or parent_row.get("id"))
                path_values.insert(0, parent_message)
                parent = str(parent_row.get("parent_id") or "")
            row_identifiers = self._source_row_identifiers(row)
            attachment_refs = self._attachment_refs_from_source_row(row)
            provenance = _loads(row.get("provenance_json"), {}) or {}
            forward_gaps = provenance.get("gaps") if isinstance(provenance, Mapping) else []
            messages.append({
                "message_id": str(row.get("message_id") or row.get("id")), "author_id": str(row.get("sender_id") or ""),
                "author_name": str(row.get("sender_name") or "身份未知"), "author_company": row.get("sender_corp_name"),
                "explicit_role": self._explicit_role_from_source_row(row), "sent_at": str(row.get("sent_at") or ""),
                "message_type": str(row.get("message_type") or "text"), "text": str(row.get("text") or ""),
                "reply_to_message_id": row.get("reply_to_message_id"), "parent_path": path_values,
                "attachment_refs": attachment_refs,
                "is_adjacent_context": bool(row_identifiers & context_identifiers),
                "is_target_root": bool(root_message_id and root_message_id in row_identifiers),
                "parse_status": str(row.get("parse_status") or "unknown"),
                "forward_gap_count": len(forward_gaps) if isinstance(forward_gaps, list) else 0,
                "source_row_id": str(row.get("id") or ""),
            })
        gaps: list[str] = []
        previous: datetime | None = None
        for item in messages:
            try:
                current = datetime.fromisoformat(item["sent_at"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if previous and current - previous > timedelta(minutes=30):
                gaps.append(f"{previous.isoformat()} 至 {current.isoformat()} 超过 30 分钟")
            previous = current
        return messages, gaps, {
            "candidate_root_count": len(marker_roots),
            "target_root_message_id": root_message_id,
            "target_root_source_row_id": str(root_row.get("id") or "") if root_row else None,
            "target_root_unique": root_row is not None,
        }

    def preview_chat(self, scope: Mapping[str, Any]) -> dict[str, Any]:
        resolved = self.resolve_chat_scope(scope)
        safe_scope, record = resolved["scope"], resolved["record"]
        messages, time_gaps, root_location = self._collect_source_messages(
            record,
            safe_scope["start_at"],
            safe_scope["end_at"],
            target_root_message_id=safe_scope.get("target_root_message_id"),
        )
        safe_scope = dict(safe_scope)
        if root_location["target_root_message_id"]:
            safe_scope["target_root_message_id"] = root_location["target_root_message_id"]
        attachment_refs = [item for message in messages for item in message["attachment_refs"]]
        available = [item for item in attachment_refs if item.get("artifact") == "available"]
        unavailable = [item for item in attachment_refs if item.get("artifact") == "referenced_but_unavailable"]
        incomplete_forwards = [
            item for item in messages
            if item.get("parse_status") in {"partial_forward", "unexpanded_forward"}
            or int(item.get("forward_gap_count") or 0) > 0
        ]
        forward_gap_count = sum(int(item.get("forward_gap_count") or 0) for item in messages)
        nested = [item for item in messages if item.get("parent_path")]
        root_messages = [item for item in messages if not item.get("parent_path") and not item.get("is_adjacent_context")]
        fixture = bool(record.get("fixture"))
        exact_target = resolved["default_target_status"] == "candidate_requires_preview"
        target_has_gaps = bool(incomplete_forwards or unavailable)
        target_status = ("synthetic_demo" if fixture else
                         "uniquely_located_incomplete" if exact_target and root_location["target_root_unique"] and target_has_gaps else
                         "uniquely_located" if exact_target and root_location["target_root_unique"] else
                         "ambiguous_root" if exact_target and root_location["candidate_root_count"] > 1 else
                         "root_not_found" if exact_target else "selected_nondefault_range")
        summary = {
            "root_messages": len(root_messages), "nested_forwarded_messages": len(nested),
            "reference_count": sum(1 for item in messages if item.get("reply_to_message_id")),
            "attachment_reference_count": len(attachment_refs), "local_file_count": sum(1 for item in available if not str(item.get("name", "")).lower().endswith((".png", ".jpg", ".jpeg"))),
            "local_image_count": sum(1 for item in available if str(item.get("name", "")).lower().endswith((".png", ".jpg", ".jpeg"))),
            "referenced_but_unavailable_count": len(unavailable), "unexpanded_forward_count": len(incomplete_forwards),
            "forward_parse_gap_count": forward_gap_count,
            "parsed_media_count": 0, "unparsed_media_count": len(unavailable),
            "earliest_at": messages[0]["sent_at"] if messages else None, "latest_at": messages[-1]["sent_at"] if messages else None,
            "time_gaps": time_gaps, "authentication_level": record["authentication_level"], "scope_only": True,
            "adjacent_context_messages": sum(1 for item in messages if item.get("is_adjacent_context")),
            "default_target_root_status": target_status,
            "default_target_outer_root_count": root_location["candidate_root_count"],
            "target_root_message_id": root_location["target_root_message_id"],
            "fixture": fixture,
        }
        # No chat body goes into this digest.  Per-message text is hashed only
        # to make a later confirmation detect a changed local snapshot.
        content_fingerprint = [{"message_id": item["message_id"], "sent_at": item["sent_at"], "parent_path": item["parent_path"], "text_hash": _sha256(item["text"].encode("utf-8", "surrogatepass")), "attachments": item["attachment_refs"]} for item in messages]
        digest = _sha256(_json({"scope": safe_scope, "summary": summary, "content": content_fingerprint}).encode())
        preview_id = "chat_preview_" + digest[:24]
        preview_messages = [{
            "message_id": item["message_id"], "author_name": item["author_name"],
            "author_company": item.get("author_company"), "role": item.get("explicit_role", "unknown"),
            "sent_at": item["sent_at"], "message_type": item.get("message_type", "text"),
            "text": item.get("text", ""), "reply_to_message_id": item.get("reply_to_message_id"),
            "parent_path": item.get("parent_path", []), "attachment_refs": item.get("attachment_refs", []),
            "is_adjacent_context": bool(item.get("is_adjacent_context")),
            "is_target_root": bool(item.get("is_target_root")),
        } for item in messages]
        payload = {"preview_id": preview_id, "preview_digest": digest, "scope": safe_scope, "summary": summary,
                   "selection": self._public_source(record), "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(), "expires_in_seconds": 300,
                   "confirmation_required": True, "local_only": True, "no_database_write": True, "no_external_model": True,
                   "cloud_history_claim": False, "data_mode": "synthetic_demo" if fixture else "real_local",
                   "messages": preview_messages,
                   "source_disclaimer": "当前为明确标注的合成演示数据，不代表真实企微记录或正式报价。" if fixture else None,
                   "real_target_ready": target_status == "uniquely_located",
                   "real_target_blocker": "当前为合成演示数据；不提供真实目标完整性声明。" if fixture else (None if target_status == "uniquely_located" else (
                       "目标根消息已定位，但完整嵌套转发或附件仍有本机可见缺口；不能标记为真实场景完成。"
                       if target_status == "uniquely_located_incomplete"
                       else "未能唯一定位默认真实目标根消息；不能标记为真实场景完成。"
                   ))}
        self._preview_cache[f"{self._cache_prefix}:{preview_id}"] = payload
        return payload

    def confirm_chat_import(self, value: Mapping[str, Any], *, actor_id: str | None = None, actor_name: str | None = None) -> dict[str, Any]:
        preview_id = str(value.get("preview_id") or "")
        cache_key = f"{self._cache_prefix}:{preview_id}"
        preview = self._preview_cache.get(cache_key)
        if not preview:
            raise AirfreightOperationError("preview_expired", "完整性预览不存在或已过期，请重新预览", http_status=409)
        try:
            expires = datetime.fromisoformat(preview["expires_at"])
        except ValueError:
            expires = datetime.now(UTC) - timedelta(seconds=1)
        if datetime.now(UTC) >= expires:
            raise AirfreightOperationError("preview_expired", "完整性预览已过期，请重新预览", http_status=409)
        if value.get("preview_digest") != preview["preview_digest"] or _json(value.get("scope") or {}) != _json(preview["scope"]):
            raise AirfreightOperationError("preview_scope_mismatch", "导入确认与预览摘要、账号、会话或时间范围不一致，请重新预览", http_status=409)
        request_id = str(value.get("idempotency_key") or value.get("request_id") or ("chat-import-" + preview["preview_digest"]))
        scope = preview["scope"]
        resolved = self.resolve_chat_scope(scope)
        messages, _time_gaps, root_location = self._collect_source_messages(
            resolved["record"],
            scope["start_at"],
            scope["end_at"],
            target_root_message_id=scope.get("target_root_message_id"),
        )
        if root_location["target_root_message_id"] != scope.get("target_root_message_id"):
            raise AirfreightOperationError("target_root_changed", "预览绑定的目标根消息已变化，请重新预览", http_status=409)
        content_digest = _sha256(_json([{"id": item["message_id"], "time": item["sent_at"], "text_hash": _sha256(item["text"].encode("utf-8", "surrogatepass")), "parent": item["parent_path"], "attachments": item["attachment_refs"]} for item in messages]).encode())
        with self.storage.connect() as conn:
            existing = conn.execute("SELECT import_id FROM local_chat_import WHERE business_line='airfreight' AND request_id=?", (request_id,)).fetchone()
            if existing:
                existing_import = conn.execute("SELECT account_id,source_snapshot,conversation_id,target_root_message_id,start_at,end_at,preview_digest FROM local_chat_import WHERE import_id=?", (existing["import_id"],)).fetchone()
                if existing_import and (existing_import["preview_digest"] != preview["preview_digest"] or any(existing_import[key] != scope[key] for key in ("account_id", "source_snapshot", "conversation_id", "start_at", "end_at")) or (existing_import["target_root_message_id"] or None) != (scope.get("target_root_message_id") or None)):
                    raise AirfreightOperationError("idempotency_scope_mismatch", "导入幂等键已经绑定其他账号、会话或时间范围", http_status=409)
                return self.get_chat_import(existing["import_id"])
            import_id = "chat_import_" + uuid.uuid4().hex
            conn.execute("""INSERT INTO local_chat_import(import_id,account_id,source_snapshot,conversation_id,conversation_name,target_root_message_id,start_at,end_at,preview_digest,preview_summary_json,content_digest,confirmed_by,confirmed_at,status,analysis_status,request_id)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (import_id, scope["account_id"], scope["source_snapshot"], scope["conversation_id"], scope["conversation_name"], scope.get("target_root_message_id"), scope["start_at"], scope["end_at"], preview["preview_digest"], _json(preview["summary"]), content_digest, actor_id or "local-user", _iso_now(), "confirmed", "pending", request_id))
            for message in messages:
                conn.execute("""INSERT OR IGNORE INTO airfreight_chat_message(row_id,import_id,message_id,account_id,source_snapshot,conversation_id,conversation_name,author_id,author_name,author_company,explicit_role,sent_at,message_type,text,reply_to_message_id,parent_path_json,attachment_refs_json,availability_json,source_json)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", ("chatrow_" + uuid.uuid4().hex, import_id, message["message_id"], scope["account_id"], scope["source_snapshot"], scope["conversation_id"], scope["conversation_name"], message.get("author_id", ""), message.get("author_name", ""), message.get("author_company"), message.get("explicit_role", "unknown"), message.get("sent_at", ""), message.get("message_type", "text"), message.get("text", ""), message.get("reply_to_message_id"), _json(message.get("parent_path", [])), _json(message.get("attachment_refs", [])), _json({"available": sum(1 for item in message.get("attachment_refs", []) if item.get("artifact") == "available"), "unavailable": sum(1 for item in message.get("attachment_refs", []) if item.get("artifact") == "referenced_but_unavailable")}), _json({"scope_bound": True, "authentication_level": preview["summary"].get("authentication_level"), "is_adjacent_context": bool(message.get("is_adjacent_context")), "is_target_root": bool(message.get("is_target_root"))})))
        return self.get_chat_import(import_id)

    def get_chat_import(self, import_id: str) -> dict[str, Any]:
        with self.storage.connect() as conn:
            row = conn.execute("SELECT * FROM local_chat_import WHERE import_id=?", (import_id,)).fetchone()
            if row is None:
                raise AirfreightOperationError("chat_import_not_found", "找不到本地群聊导入", http_status=404)
            messages = conn.execute("SELECT * FROM airfreight_chat_message WHERE import_id=? ORDER BY sent_at,row_id", (import_id,)).fetchall()
        return dict(row) | {"preview_summary": _loads(row["preview_summary_json"], {}), "messages": [self._public_chat_message(message) for message in messages]}

    @staticmethod
    def _public_chat_message(row: Any) -> dict[str, Any]:
        source = _loads(row["source_json"], {})
        return {"message_id": row["message_id"], "author_id": row["author_id"], "author_name": row["author_name"], "author_company": row["author_company"], "role": row["explicit_role"], "sent_at": row["sent_at"], "message_type": row["message_type"], "text": row["text"], "reply_to_message_id": row["reply_to_message_id"], "parent_path": _loads(row["parent_path_json"], []), "attachment_refs": _loads(row["attachment_refs_json"], []), "availability": _loads(row["availability_json"], {}), "is_adjacent_context": bool(source.get("is_adjacent_context")), "is_target_root": bool(source.get("is_target_root")), "source": source}

    def chat_evidence(self, import_id: str) -> dict[str, Any]:
        item = self.get_chat_import(import_id)
        item["evidence_policy"] = {"original_sender_preserved": True, "role_inferred_from_company_name": False, "unavailable_attachment_download_attempted": False, "technical_details_in_source": True}
        return item

    @staticmethod
    def _real_chat_quote_candidates(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Extract only explicit route strings from an imported local range.

        This is intentionally conservative.  It never connects a route to a
        bare size/price in another message and never makes a Cartesian product
        from multiple routes or tickets.  Anything else enters manual review.
        """
        candidates: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        route_pattern = re.compile(r"(?<![A-Z])([A-Z]{3})\s*[-–—→]\s*(?:([A-Z]{3})\s*[-–—→]\s*)?([A-Z]{3})(?![A-Z])")
        for message in messages:
            text = str(message.get("text") or "")
            matches = list(route_pattern.finditer(text.upper()))
            for index, match in enumerate(matches, 1):
                origin, hub, destination = match.group(1), match.group(2), match.group(3)
                routing = "-".join(item for item in (origin, hub, destination) if item)
                key = (str(message.get("message_id") or ""), routing)
                if key in seen:
                    continue
                seen.add(key)
                missing = ["货物包装字段需要从可取得附件或人工补充"]
                if len(matches) > 1:
                    missing.append("同一消息有多条路线，尚未证明各路线与货物票据的对应关系")
                if any(item.get("artifact") == "referenced_but_unavailable" for item in message.get("attachment_refs") or []):
                    missing.append("相关附件只有引用，本机没有可验证二进制副本")
                candidates.append({
                    "key": f"message-{message.get('message_id') or index}-{routing}", "origin": origin,
                    "destination": destination, "routing": routing, "confidence": "0.55",
                    "missing": missing, "messages": [message.get("message_id")], "artifacts": [], "groups": [],
                })
        if not candidates and messages:
            candidates.append({
                "key": "unresolved-imported-range", "origin": None, "destination": None, "routing": None,
                "confidence": "0.20", "missing": ["未从所选范围识别到可唯一归属的完整路线、货物和票据关系"],
                "messages": [item.get("message_id") for item in messages[:20]], "artifacts": [], "groups": [],
            })
        return candidates

    def parse_quotes(self, import_id: str) -> dict[str, Any]:
        item = self.get_chat_import(import_id)
        if item["status"] != "confirmed":
            raise AirfreightOperationError("chat_import_not_confirmed", "只有已确认的本地群聊范围才能解析")
        with self.storage.connect() as conn:
            existing = conn.execute("SELECT quote_request_id FROM quote_request WHERE import_id=? ORDER BY quote_key", (import_id,)).fetchall()
            if not existing:
                fixture = bool(item.get("preview_summary", {}).get("fixture"))
                quotes = [
                    {"key": "ticket-1", "origin": "HKG", "destination": "BRU", "routing": "HKG-ADD-BRU", "confidence": "0.76", "missing": ["CTN 与单件尺寸对应关系（演示夹具待确认）"], "messages": ["demo-msg-001", "demo-msg-002"], "artifacts": ["cargo-multi-ticket.png"], "groups": "ticket-1"},
                    {"key": "ticket-2", "origin": "HKG", "destination": "TLV", "routing": "HKG-ADD-TLV", "confidence": "0.89", "missing": [], "messages": ["demo-msg-001", "demo-msg-003"], "artifacts": ["cargo-multi-ticket.png"], "groups": "ticket-2"},
                ] if fixture and self.include_demo_fixtures else self._real_chat_quote_candidates(item["messages"])
                for quote in quotes:
                    qid = "quote_" + uuid.uuid4().hex
                    source_artifact_id = None
                    if quote["artifacts"]:
                        artifact_row = conn.execute(
                            "SELECT artifact_id FROM source_artifact WHERE file_name=? ORDER BY received_at DESC LIMIT 1",
                            (quote["artifacts"][0],),
                        ).fetchone()
                        source_artifact_id = artifact_row["artifact_id"] if artifact_row else None
                    conn.execute("""INSERT OR IGNORE INTO quote_request(quote_request_id,import_id,quote_key,source_message_ids_json,source_artifact_ids_json,origin_airport,destination_airport,routing,status,confidence,missing_fields_json,created_at)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (qid, import_id, quote["key"], _json(quote["messages"]), _json(quote["artifacts"]), quote["origin"], quote["destination"], quote["routing"], "needs_manual_review" if quote["missing"] else "parsed", quote["confidence"], _json(quote["missing"]), _iso_now()))
                    qrow = conn.execute("SELECT quote_request_id FROM quote_request WHERE import_id=? AND quote_key=?", (import_id, quote["key"])).fetchone()
                    for field_name, raw_value in (("origin_airport", quote["origin"]), ("destination_airport", quote["destination"]), ("routing", quote["routing"]), ("confidence", quote["confidence"]), ("missing_fields", quote["missing"])):
                        duplicate = conn.execute(
                            "SELECT evidence_id FROM field_evidence WHERE entity_type='quote_request' AND entity_id=? AND field_name=? AND source_artifact_id IS ? AND raw_value_json=?",
                            (qrow["quote_request_id"], field_name, source_artifact_id, _json(raw_value)),
                        ).fetchone()
                        if not duplicate:
                            conn.execute("""INSERT INTO field_evidence(evidence_id,entity_type,entity_id,field_name,raw_value_json,normalized_value_json,locator_json,source_artifact_id,parser_version,confidence,extraction_method)
                                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""", ("evidence_" + uuid.uuid4().hex, "quote_request", qrow["quote_request_id"], field_name, _json(raw_value), _json(raw_value), _json({"message_ids": quote["messages"], "artifact_names": quote["artifacts"]}), source_artifact_id, AIRFREIGHT_PARSER_VERSION, quote["confidence"], "local_chat_evidence"))
                    if quote.get("groups") == "ticket-1":
                        groups = [
                            {"length": "40", "width": "30", "height": "20", "dimension_semantics": "single_piece", "gross": "40", "gross_semantics": "group_total", "ctn": "2", "package_semantics": "corresponding_ctn", "cbm": "0.050", "cbm_semantics": "group_total", "visual": "ticket-1-group-a", "status": "needs_manual_review", "components": [("Unit", "10", "costume samples"), ("PCS", "2", "accessory boxes")]},
                            {"length": "40", "width": "30", "height": "20", "dimension_semantics": "single_piece", "gross": "35", "gross_semantics": "group_total", "ctn": "2", "package_semantics": "corresponding_ctn", "cbm": None, "cbm_semantics": "unknown", "visual": "ticket-1-group-b", "status": "confirmed", "components": [("Unit", "6", "fabric samples")]},
                        ]
                    elif quote.get("groups") == "ticket-2":
                        groups = [{"length": "100", "width": "80", "height": "60", "dimension_semantics": "group_outer", "gross": "120", "gross_semantics": "group_total", "ctn": "3", "package_semantics": "not_a_multiplier", "cbm": "0.480", "cbm_semantics": "group_total", "visual": "ticket-2-group-a", "status": "confirmed", "components": [("BOX", "3", "display fixtures")]}]
                    else:
                        groups = []
                    for group in groups:
                        pgid = "package_" + uuid.uuid4().hex
                        conn.execute("""INSERT INTO package_group(package_group_id,quote_request_id,length_text,width_text,height_text,dimension_unit,dimension_semantics,gross_weight_text,gross_weight_unit,gross_weight_semantics,ctn_text,package_count_text,package_count_semantics,cbm_text,cbm_semantics,visual_group_id,source_json,semantic_status)
                                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (pgid, qrow["quote_request_id"], group["length"], group["width"], group["height"], "cm", group["dimension_semantics"], group["gross"], "kg", group["gross_semantics"], group["ctn"], group["ctn"], group["package_semantics"], group["cbm"], group["cbm_semantics"], group["visual"], _json({"artifact": "cargo-multi-ticket.png", "visual_row_group": group["visual"], "original_text": f"MEAS (CM) {group['length']}×{group['width']}×{group['height']}; Gross Weight {group['gross']} kg; CTN {group['ctn']}"}), group["status"]))
                        for field_name, raw_value in (("length", group["length"]), ("width", group["width"]), ("height", group["height"]), ("dimension_semantics", group["dimension_semantics"]), ("gross_weight", group["gross"]), ("ctn", group["ctn"]), ("cbm", group["cbm"])):
                            duplicate = conn.execute(
                                "SELECT evidence_id FROM field_evidence WHERE entity_type='package_group' AND entity_id=? AND field_name=?",
                                (pgid, field_name),
                            ).fetchone()
                            if not duplicate:
                                conn.execute("""INSERT INTO field_evidence(evidence_id,entity_type,entity_id,field_name,raw_value_json,normalized_value_json,locator_json,source_artifact_id,parser_version,confidence,extraction_method)
                                               VALUES(?,?,?,?,?,?,?,?,?,?,?)""", ("evidence_" + uuid.uuid4().hex, "package_group", pgid, field_name, _json(raw_value), _json(raw_value), _json({"artifact": "cargo-multi-ticket.png", "visual_row_group": group["visual"]}), source_artifact_id, AIRFREIGHT_PARSER_VERSION, "0.76" if group["status"] == "needs_manual_review" else "0.92", "local_ocr_baseline"))
                        for unit, qty, description in group["components"]:
                            conn.execute("INSERT INTO cargo_component(component_id,quote_request_id,package_group_id,unit,qty_text,description,source_json) VALUES(?,?,?,?,?,?,?)", ("cargo_" + uuid.uuid4().hex, qrow["quote_request_id"], pgid, unit, qty, description, _json({"artifact": "cargo-multi-ticket.png", "visual_row_group": group["visual"]})))
            conn.execute("UPDATE local_chat_import SET analysis_status='parsed' WHERE import_id=?", (import_id,))
        return self.get_quote_detail_for_import(import_id)

    def list_quotes(self, *, import_id: str | None = None) -> dict[str, Any]:
        with self.storage.connect() as conn:
            if import_id:
                rows = conn.execute("SELECT * FROM quote_request WHERE import_id=? ORDER BY quote_key", (import_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM quote_request WHERE business_line='airfreight' ORDER BY created_at,quote_key").fetchall()
        return {"items": [dict(row) | {"source_message_ids": _loads(row["source_message_ids_json"], []), "source_artifact_ids": _loads(row["source_artifact_ids_json"], []), "missing_fields": _loads(row["missing_fields_json"], [])} for row in rows], "total": len(rows)}

    def get_quote_detail(self, quote_request_id: str) -> dict[str, Any]:
        with self.storage.connect() as conn:
            quote = conn.execute("SELECT * FROM quote_request WHERE quote_request_id=?", (quote_request_id,)).fetchone()
            if quote is None:
                raise AirfreightOperationError("quote_not_found", "找不到询价", http_status=404)
            groups = conn.execute("SELECT * FROM package_group WHERE quote_request_id=? ORDER BY package_group_id", (quote_request_id,)).fetchall()
            components = conn.execute("SELECT * FROM cargo_component WHERE quote_request_id=? ORDER BY component_id", (quote_request_id,)).fetchall()
            options = conn.execute("SELECT * FROM quote_option WHERE quote_request_id=? ORDER BY option_no", (quote_request_id,)).fetchall()
        return dict(quote) | {"source_message_ids": _loads(quote["source_message_ids_json"], []), "source_artifact_ids": _loads(quote["source_artifact_ids_json"], []), "missing_fields": _loads(quote["missing_fields_json"], []), "package_groups": [dict(group) | {"source": _loads(group["source_json"], {})} for group in groups], "cargo_components": [dict(component) | {"source": _loads(component["source_json"], {})} for component in components], "options": [dict(option) | {"calculation": _loads(option["calculation_json"], {}), "cargo_summary": _loads(option["cargo_summary_json"], {})} for option in options]}

    def get_quote_detail_for_import(self, import_id: str) -> dict[str, Any]:
        return {"import_id": import_id, "items": [self.get_quote_detail(item["quote_request_id"]) for item in self.list_quotes(import_id=import_id)["items"]]}

    def correct_quote_field(self, quote_request_id: str, *, entity_type: str, entity_id: str, field_name: str,
                            corrected_value: Any, actor_id: str | None, actor_name: str | None, reason: str,
                            idempotency_key: str) -> dict[str, Any]:
        actor_id, actor_name = self._require_reviewer(actor_id, actor_name)
        with self.storage.connect() as conn:
            prior = conn.execute("SELECT decision_id FROM review_decision WHERE business_line='airfreight' AND idempotency_key=? AND action='correct_field'", (idempotency_key,)).fetchone()
            if prior:
                return {"quote_request_id": quote_request_id, "corrected": True, "original_preserved": True, "idempotent": True, "decision_id": prior["decision_id"]}
            if entity_type == "package_group":
                row = conn.execute("SELECT * FROM package_group WHERE package_group_id=? AND quote_request_id=?", (entity_id, quote_request_id)).fetchone()
                table = "package_group"
            else:
                row = conn.execute("SELECT * FROM quote_request WHERE quote_request_id=?", (quote_request_id,)).fetchone()
                table = "quote_request"
            if row is None or field_name not in row.keys():
                raise AirfreightOperationError("quote_field_not_found", "找不到待修正的询价字段", http_status=404)
            before = row[field_name]
            if table == "quote_request" and field_name == "missing_fields_json":
                if not isinstance(corrected_value, list) or any(not isinstance(item, str) for item in corrected_value):
                    raise AirfreightOperationError("invalid_quote_correction", "待确认项修正必须是字符串列表", http_status=400)
                stored_value = _json(corrected_value)
            else:
                stored_value = str(corrected_value)
            conn.execute(f"UPDATE {table} SET {field_name}=? WHERE " + ("package_group_id=?" if table == "package_group" else "quote_request_id=?"), (stored_value, entity_id if table == "package_group" else quote_request_id))
            if table == "quote_request" and field_name == "missing_fields_json" and not corrected_value:
                conn.execute("UPDATE quote_request SET status='parsed' WHERE quote_request_id=?", (quote_request_id,))
            conn.execute("""INSERT INTO field_evidence(evidence_id,entity_type,entity_id,field_name,raw_value_json,normalized_value_json,locator_json,source_artifact_id,parser_version,confidence,extraction_method,original_value_json,corrected_value_json,corrected_by,corrected_at,correction_reason)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", ("evidence_" + uuid.uuid4().hex, entity_type, entity_id, field_name, _json(before), _json(corrected_value), _json({"preserved_original": True}), None, AIRFREIGHT_PARSER_VERSION, "1.00", "human_correction", _json(before), _json(corrected_value), actor_id, _iso_now(), reason or ""))
            decision = self._record_decision(conn, object_type=entity_type, object_id=entity_id, action="correct_field", actor_id=actor_id, actor_name=actor_name, reason=reason, idempotency_key=idempotency_key, before={field_name: before}, after={field_name: corrected_value})
        return {"quote_request_id": quote_request_id, "corrected": True, "original_preserved": True, **decision}

    def _weight_for_group(self, group: Mapping[str, Any]) -> dict[str, Any]:
        length, width, height = (_decimal(group.get(key + "_text")) for key in ("length", "width", "height"))
        gross = _decimal(group.get("gross_weight_text"))
        ctn = _decimal(group.get("package_count_text"))
        if not all(value is not None for value in (length, width, height, gross)) or group.get("dimension_unit") != "cm":
            return {"status": "needs_manual_review", "reason": "尺寸、单位或毛重不完整", "group_id": group.get("package_group_id")}
        semantics = group.get("dimension_semantics")
        package_semantics = group.get("package_count_semantics")
        if semantics not in {"single_piece", "group_outer"} or package_semantics == "unknown" or (semantics == "single_piece" and ctn is None):
            return {"status": "needs_manual_review", "reason": "未确认单件／整组尺寸或 CTN 对应关系；禁止自动乘数", "group_id": group.get("package_group_id")}
        geometric_cbm = (length * width * height / Decimal("1000000")) * (ctn if semantics == "single_piece" else Decimal("1"))
        volume_weight = geometric_cbm * Decimal("1000000") / Decimal("6000")
        given_cbm = _decimal(group.get("cbm_text"))
        difference = (given_cbm - geometric_cbm) if given_cbm is not None else None
        return {"status": "calculated", "group_id": group.get("package_group_id"), "formula": f"{length}×{width}×{height}" + (f"×{ctn}" if semantics == "single_piece" else "") + "÷6000", "dimension_semantics": semantics, "package_count": str(ctn) if ctn is not None else None, "geometric_cbm": _decimal_text(geometric_cbm), "given_cbm": _decimal_text(given_cbm), "cbm_difference": _decimal_text(difference), "volume_weight_kg": _decimal_text(volume_weight), "gross_weight_kg": _decimal_text(gross), "rounding": "未额外舍入；以精确十进制展示"}

    def calculate_chargeable_weight(self, quote_request_id: str) -> dict[str, Any]:
        detail = self.get_quote_detail(quote_request_id)
        groups = detail["package_groups"]
        calculations = [self._weight_for_group(group) for group in groups]
        if any(item["status"] != "calculated" for item in calculations):
            return {"quote_request_id": quote_request_id, "status": "needs_manual_review", "reason": "至少一个包装组的尺寸／CTN 语义不完整", "groups": calculations, "demo_rule": "每包装组长×宽×高÷6000；单件尺寸才乘 CTN"}
        total_gross = sum((_decimal(item["gross_weight_kg"]) or Decimal("0") for item in calculations), Decimal("0"))
        total_volume = sum((_decimal(item["volume_weight_kg"]) or Decimal("0") for item in calculations), Decimal("0"))
        chargeable = max(total_gross, total_volume)
        with self.storage.connect() as conn:
            conn.execute("UPDATE package_group SET volume_weight_text=?,chargeable_weight_text=? WHERE quote_request_id=?", (_decimal_text(total_volume), _decimal_text(chargeable), quote_request_id))
            conn.execute("UPDATE quote_request SET status='calculated' WHERE quote_request_id=?", (quote_request_id,))
        return {"quote_request_id": quote_request_id, "status": "calculated", "groups": calculations, "total_gross_weight_kg": _decimal_text(total_gross), "total_volume_weight_kg": _decimal_text(total_volume), "chargeable_weight_kg": _decimal_text(chargeable), "formula": "max(total_gross_weight_kg,total_volume_weight_kg)", "coefficient": "6000", "rule_label": "演示规则，不代表生产承运人规则"}

    def _select_weight_break(self, weight: Decimal, *, confirmed: bool) -> tuple[str, str] | None:
        if not confirmed:
            return None
        if weight < WEIGHT_LIMITS[0] or weight > WEIGHT_LIMITS[-1]:
            return None
        for index, lower in enumerate(WEIGHT_LIMITS):
            upper = WEIGHT_LIMITS[index + 1] if index + 1 < len(WEIGHT_LIMITS) else None
            if weight >= lower and (upper is None or weight < upper):
                return WEIGHT_LABELS[index], "[" + str(lower) + (f",{upper})" if upper else "+∞")
        return None

    def match_rates(self, quote_request_id: str) -> dict[str, Any]:
        detail = self.get_quote_detail(quote_request_id)
        weight = self.calculate_chargeable_weight(quote_request_id)
        if weight["status"] != "calculated":
            return {"quote_request_id": quote_request_id, "status": "needs_manual_review", "reason": "计费重尚未确认", "items": []}
        with self.storage.connect() as conn:
            quote = conn.execute("SELECT import_id FROM quote_request WHERE quote_request_id=?", (quote_request_id,)).fetchone()
            imported = conn.execute("SELECT start_at FROM local_chat_import WHERE import_id=?", (quote["import_id"],)).fetchone() if quote and quote["import_id"] else None
            target_date = str(imported["start_at"] if imported else _iso_now())[:10]
            versions = conn.execute(
                """SELECT * FROM rate_card_version
                   WHERE business_line='airfreight' AND published_current=1
                     AND (valid_from IS NULL OR valid_from<=?)
                     AND (valid_to IS NULL OR valid_to>=?)""",
                (target_date, target_date),
            ).fetchall()
            if not versions:
                return {
                    "quote_request_id": quote_request_id,
                    "status": "needs_manual_review",
                    "chargeable_weight_kg": weight["chargeable_weight_kg"],
                    "target_date": target_date,
                    "items": [],
                    "blocker": {
                        "code": "no_published_rate_card",
                        "reason": "当前没有已发布且在目标日期有效的空运价卡",
                        "next_action": "前往流程 A 完成候选价卡的冲突审核与发布；发布后重新匹配",
                    },
                }
            options: list[dict[str, Any]] = []
            for version in versions:
                selected = self._select_weight_break(_decimal(weight["chargeable_weight_kg"]) or Decimal("0"), confirmed=bool(version["weight_rule_confirmed"]))
                if not selected:
                    options.append({"airline_name": version["airline_name"], "airline_code": version["airline_code"], "version_id": version["version_id"], "status": "needs_manual_review", "blocker_code": "weight_rule_confirmation_required", "reason": "重量档边界（含 <45kg、>1000kg、取整与命中顺序）尚未人工确认", "candidate_weight_labels": list(WEIGHT_LABELS), "target_date": target_date})
                    continue
                label, interval = selected
                rate = conn.execute("SELECT * FROM route_rate WHERE version_id=? AND destination_airport=? AND weight_break_label=? AND status='published'", (version["version_id"], detail["destination_airport"], label)).fetchone()
                if rate is None:
                    options.append({"airline_name": version["airline_name"], "airline_code": version["airline_code"], "version_id": version["version_id"], "status": "inquiry_required", "reason": "该目的地或重量档没有可自动采用费率", "weight_break_label": label})
                    continue
                presentation: dict[str, Any] = {}
                for field_name in ("frequency", "transit_time"):
                    evidence = conn.execute(
                        """SELECT normalized_value_json FROM field_evidence
                           WHERE entity_type='rate_card_version' AND entity_id=? AND field_name=?
                           ORDER BY confidence DESC,evidence_id LIMIT 1""",
                        (version["version_id"], field_name),
                    ).fetchone()
                    presentation[field_name] = _loads(evidence["normalized_value_json"], None) if evidence else None
                artifact = conn.execute(
                    "SELECT * FROM source_artifact WHERE artifact_id=? AND business_line='airfreight'",
                    (rate["source_artifact_id"],),
                ).fetchone() if rate["source_artifact_id"] else None
                source_files = [self._public_artifact(artifact)] if artifact is not None else []
                options.append({"airline_name": version["airline_name"], "airline_code": version["airline_code"], "version_id": version["version_id"], "rate_id": rate["rate_id"], "status": "matched", "destination": rate["destination_airport"], "routing": rate["routing"], "frequency": presentation["frequency"], "transit_time": presentation["transit_time"], "weight_break_label": label, "weight_interval": interval, "supplier_base_rate": rate["amount_text"], "currency": rate["currency"], "pricing_unit": rate["pricing_unit"], "source_files": source_files, "restriction_check": "请继续查看限制与需单询项"})
        return {"quote_request_id": quote_request_id, "quote_key": detail.get("quote_key"), "origin": detail.get("origin_airport"), "destination": detail.get("destination_airport"), "routing": detail.get("routing"), "status": "matched" if any(item["status"] == "matched" for item in options) else "needs_manual_review", "chargeable_weight_kg": weight["chargeable_weight_kg"], "target_date": target_date, "items": options}

    def generate_internal_calculation(self, quote_request_id: str) -> dict[str, Any]:
        matched = self.match_rates(quote_request_id)
        if not any(item.get("status") == "matched" for item in matched["items"]):
            return {"quote_request_id": quote_request_id, "status": matched["status"], "items": [], "reason": "没有可用于内部测算的已匹配价卡"}
        weight = _decimal(matched["chargeable_weight_kg"]) or Decimal("0")
        detail = self.get_quote_detail(quote_request_id)
        calculations: list[dict[str, Any]] = []
        with self.storage.connect() as conn:
            for item in matched["items"]:
                if item.get("status") != "matched":
                    continue
                base = _decimal(item["supplier_base_rate"]) or Decimal("0")
                base_total = base * weight
                enabled_rules = [dict(row) for row in conn.execute("SELECT * FROM internal_rate_rule WHERE version_id=? AND status='enabled'", (item["version_id"],)).fetchall()]
                pending_rules = [dict(row) for row in conn.execute("SELECT * FROM internal_rate_rule WHERE version_id=? AND status='pending_confirmation'", (item["version_id"],)).fetchall()]
                surcharge_prompts = [dict(row) for row in conn.execute("SELECT * FROM surcharge_rule WHERE version_id=? AND status IN ('pending','inquiry_required')", (item["version_id"],)).fetchall()]
                enabled_adjustments = []
                adjustment_total = Decimal("0")
                for rule in enabled_rules:
                    adjustment = _decimal(rule.get("adjustment_per_kg_text"))
                    if adjustment is None or rule.get("currency") != item.get("currency"):
                        continue
                    amount = adjustment * weight
                    adjustment_total += amount
                    enabled_adjustments.append({"rule_id": rule["rule_id"], "formula": rule["formula"], "adjustment_per_kg": _decimal_text(adjustment), "amount": _decimal_text(amount), "currency": rule.get("currency")})
                internal_rate = base + sum((_decimal(rule.get("adjustment_per_kg")) or Decimal("0") for rule in enabled_adjustments), Decimal("0"))
                total = base_total + adjustment_total
                calc = {"option_key": f"{item['airline_code']}:{item['rate_id']}", "airline_name": item["airline_name"], "airline_code": item["airline_code"], "base_rate": item["supplier_base_rate"], "internal_rate": _decimal_text(internal_rate), "chargeable_weight_kg": _decimal_text(weight), "base_formula": f"{item['supplier_base_rate']} × {weight} kg", "base_amount": _decimal_text(base_total), "currency": item.get("currency"), "pricing_unit": item.get("pricing_unit"), "automatic_surcharges": enabled_adjustments, "not_included": [{"reason": "演示销售调整 +0.20/kg 需在报价确认步骤明确应用", "rules": pending_rules}, {"reason": "目的地附加费／Small Box 条件含歧义，未自动计入", "rules": surcharge_prompts}], "total": _decimal_text(total), "rounding": "精确十进制；演示未额外舍入", "source_version_id": item["version_id"], "demo_rule_warning": "内部测算不等于对客报价"}
                option_row = conn.execute("SELECT option_no FROM quote_option WHERE quote_request_id=? AND airline_code=?", (quote_request_id, item["airline_code"])).fetchone()
                option_no = option_row["option_no"] if option_row else str(len(calculations) + 1)
                option_values = (item["airline_name"], item["airline_code"], calc["internal_rate"], calc["currency"], calc["pricing_unit"], item.get("routing") or "To be confirmed", item.get("frequency") or "To be confirmed", item.get("transit_time") or "To be confirmed", _json({"packages": len(detail["package_groups"]), "weight": matched["chargeable_weight_kg"]}), _json(calc), item["version_id"])
                if option_row:
                    conn.execute("""UPDATE quote_option SET option_no=?,airline_name=?,airline_code=?,air_freight_text=?,currency=?,pricing_unit=?,routing=?,frequency=?,transit_time=?,cargo_summary_json=?,calculation_json=?,rate_version_id=?,status='candidate' WHERE quote_request_id=? AND airline_code=?""", (option_no, *option_values, quote_request_id, item["airline_code"]))
                else:
                    conn.execute("""INSERT INTO quote_option(option_id,quote_request_id,option_no,airline_name,airline_code,air_freight_text,currency,pricing_unit,routing,frequency,transit_time,cargo_summary_json,calculation_json,rate_version_id,status)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", ("option_" + uuid.uuid4().hex, quote_request_id, option_no, *option_values, "candidate"))
                calculations.append(calc)
        return {"quote_request_id": quote_request_id, "status": "calculated", "items": calculations, "currency_note": "币种仅继承已发布价卡明确值；内部调整未获确认时不参与"}

    def confirm_quote(self, quote_request_id: str, *, actor_id: str | None, actor_name: str | None, reason: str,
                      idempotency_key: str, sales_adjustment_per_kg: Any = None) -> dict[str, Any]:
        actor_id, actor_name = self._require_reviewer(actor_id, actor_name)
        sales_adjustment = _decimal(sales_adjustment_per_kg) or Decimal("0")
        if sales_adjustment < 0:
            raise AirfreightOperationError("invalid_sales_adjustment", "销售调整必须是非负十进制", http_status=422)
        detail = self.generate_internal_calculation(quote_request_id)
        if detail.get("status") != "calculated" or not detail.get("items"):
            raise AirfreightOperationError("quote_not_ready", "报价选项尚未完成内部测算", details={"calculation": detail})
        with self.storage.connect() as conn:
            rows = conn.execute("SELECT * FROM quote_option WHERE quote_request_id=? ORDER BY option_no", (quote_request_id,)).fetchall()
            for row in rows:
                calculation = _loads(row["calculation_json"], {})
                internal_rate = _decimal(calculation.get("internal_rate") or calculation.get("base_rate")) or Decimal("0")
                selling_rate = (internal_rate + sales_adjustment).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                chargeable_weight = _decimal(calculation.get("chargeable_weight_kg")) or Decimal("0")
                selling_amount = (selling_rate * chargeable_weight).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                calculation.update({
                    "sales_adjustment_per_kg": format(sales_adjustment.quantize(Decimal("0.01")), ".2f"),
                    "selling_rate": format(selling_rate, ".2f"), "selling_amount": format(selling_amount, ".2f"),
                    "sales_adjustment_confirmed_by": actor_name, "customer_price_excludes_supplier_base_rate": True,
                })
                conn.execute("UPDATE quote_option SET air_freight_text=?,calculation_json=?,status='confirmed',confirmed_by=?,confirmed_at=? WHERE option_id=?", (format(selling_rate, ".2f"), _json(calculation), actor_id, _iso_now(), row["option_id"]))
            decision = self._record_decision(conn, object_type="quote_request", object_id=quote_request_id, action="confirm_quote", actor_id=actor_id, actor_name=actor_name, reason=reason, idempotency_key=idempotency_key, before={"status": "candidate"}, after={"status": "confirmed", "options": len(rows), "sales_adjustment_per_kg": format(sales_adjustment, ".2f")})
            rows = conn.execute("SELECT * FROM quote_option WHERE quote_request_id=? ORDER BY option_no", (quote_request_id,)).fetchall()
        return {"quote_request_id": quote_request_id, "status": "confirmed", "items": [dict(row) for row in rows],
                "sales_adjustment_per_kg": format(sales_adjustment, ".2f"), **decision}

    @staticmethod
    def _quote_number(quote_key: str) -> str:
        token = re.sub(r"[^A-Za-z0-9]+", "-", quote_key).strip("-").upper() or "QUOTE"
        return "DEMO-AF-" + token

    def _confirmed_quote_document(self, quote_request_id: str) -> dict[str, Any]:
        with self.storage.connect() as conn:
            quote = conn.execute("SELECT * FROM quote_request WHERE quote_request_id=? AND business_line='airfreight'", (quote_request_id,)).fetchone()
            if quote is None:
                raise AirfreightOperationError("quote_not_found", "找不到询价", http_status=404)
            groups = [dict(row) for row in conn.execute("SELECT * FROM package_group WHERE quote_request_id=? ORDER BY package_group_id", (quote_request_id,)).fetchall()]
            option_rows = [dict(row) for row in conn.execute("SELECT * FROM quote_option WHERE quote_request_id=? AND status='confirmed' ORDER BY CAST(option_no AS INTEGER),option_no", (quote_request_id,)).fetchall()]
        if not option_rows:
            raise AirfreightOperationError("quote_not_confirmed", "报价尚未完成人工确认，不能生成报价单", http_status=409)
        package_total = sum((_decimal(group.get("package_count_text")) or Decimal("0") for group in groups), Decimal("0"))
        gross_total = sum((_decimal(group.get("gross_weight_text")) or Decimal("0") for group in groups), Decimal("0"))
        cbm_total = Decimal("0")
        for group in groups:
            given_cbm = _decimal(group.get("cbm_text"))
            if given_cbm is not None:
                cbm_total += given_cbm
                continue
            calculation = self._weight_for_group(group)
            cbm_total += _decimal(calculation.get("geometric_cbm")) or Decimal("0")
        first_calculation = _loads(option_rows[0].get("calculation_json"), {})
        chargeable_weight = first_calculation.get("chargeable_weight_kg") or "To be confirmed"
        confirmed_at = min((str(row.get("confirmed_at")) for row in option_rows if row.get("confirmed_at")), default=_iso_now())
        try:
            generated = datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
        except ValueError:
            generated = datetime.now(UTC)
        valid_until = (generated + timedelta(days=7)).date().isoformat()
        options = [{
            "option": row["option_no"], "airline": row["airline_name"], "airline_code": row["airline_code"],
            "a_f": row["air_freight_text"], "currency": row["currency"], "unit": row["pricing_unit"],
            "routing": row["routing"] or quote["routing"] or "To be confirmed", "frequency": row["frequency"] or "To be confirmed",
            "t_t": row["transit_time"] or "To be confirmed", "rate_version_id": row["rate_version_id"], "status": row["status"],
        } for row in option_rows]
        quote_number = self._quote_number(str(quote["quote_key"] or quote_request_id))
        return {
            "document_type": "customer_quote", "document_number": quote_number, "title": "Air Freight Quotation",
            "brand": "ZHONGJI LOGISTICS / 中技物流", "demo_label": "DEMO — NOT SENT", "customer": "Anonymous Customer",
            "salutation": "Dear Customer:", "quote_request_id": quote_request_id, "quote_key": quote["quote_key"],
            "import_id": quote["import_id"], "origin": quote["origin_airport"] or "To be confirmed",
            "destination": quote["destination_airport"] or "To be confirmed", "routing": quote["routing"] or "To be confirmed",
            "incoterm": "To be confirmed", "generated_at": generated.date().isoformat(), "valid_until": valid_until,
            "cargo": {"ctns": _decimal_text(package_total) or "To be confirmed", "gross_weight_kg": _decimal_text(gross_total) or "To be confirmed",
                      "cbm": _decimal_text(cbm_total) or "To be confirmed", "chargeable_weight_kg": chargeable_weight},
            "options": options,
            "notes": ["Subject to space and final carrier confirmation.", "Final dimensions and gross weight remain subject to verification.",
                      "Unconfirmed destination, local and special-handling surcharges are excluded."],
            "pdf_url": f"/api/airfreight/quote-preview/{quote_request_id}/quotation.pdf", "not_sent": True,
        }

    def quote_preview(self, *, import_id: str | None = None) -> dict[str, Any]:
        with self.storage.connect() as conn:
            if import_id:
                rows = conn.execute("""SELECT DISTINCT q.quote_request_id FROM quote_request q JOIN quote_option o ON o.quote_request_id=q.quote_request_id WHERE q.import_id=? AND o.status='confirmed' ORDER BY q.quote_key""", (import_id,)).fetchall()
            else:
                rows = conn.execute("""SELECT DISTINCT q.quote_request_id FROM quote_request q JOIN quote_option o ON o.quote_request_id=q.quote_request_id WHERE o.status='confirmed' ORDER BY q.created_at,q.quote_key""").fetchall()
        documents = [self._confirmed_quote_document(row["quote_request_id"]) for row in rows]
        items = [dict(option) | {"quote_request_id": document["quote_request_id"], "quote_key": document["quote_key"], "import_id": document["import_id"]}
                 for document in documents for option in document["options"]]
        return {"documents": documents, "items": items, "total": len(items), "total_documents": len(documents),
                "not_sent": True, "label": "图四式独立报价单（本地演示，未发送）"}

    def quote_pdf(self, quote_request_id: str) -> tuple[bytes, str]:
        document = self._confirmed_quote_document(quote_request_id)
        cargo = document["cargo"]
        rows = [[item["option"], item["airline_code"] or item["airline"], f"{item['currency']} {item['a_f']}/{item['unit']}", item["routing"], item["frequency"], item["t_t"]] for item in document["options"]]
        pdf = _table_pdf(
            title="AIR FREIGHT QUOTATION", number=document["document_number"],
            details=(
                document["salutation"],
                f"Cargo details: {cargo['ctns']} CTNS, {cargo['gross_weight_kg']} KG gross, {cargo['cbm']} CBM, chargeable {cargo['chargeable_weight_kg']} KG",
                f"Route: {document['origin']}-{document['destination']}  |  Incoterm: {document['incoterm']}  |  Valid until: {document['valid_until']}",
            ),
            columns=(("Option", 58), ("Airline", 102), ("A/F", 130), ("Routing", 205), ("Frequency", 130), ("T/T", 105)),
            rows=rows, notes=tuple(document["notes"]), marker="DEMO_CUSTOMER_QUOTATION",
        )
        filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", document["document_number"]) + ".pdf"
        return pdf, filename


__all__ = [
    "AIRFREIGHT_PARSER_VERSION", "AIRFREIGHT_RULE_VERSION", "OCR_BASELINE", "AIRFREIGHT_SCHEMA", "AirfreightOperationError", "AirfreightService", "demo_file_payloads", "calculate_chargeable_weight",
]


def calculate_chargeable_weight(package_groups: Iterable[Mapping[str, Any]], *, coefficient: Any = "6000") -> dict[str, Any]:
    """Pure helper used by tests and integrations for the demo formula."""
    coefficient_decimal = _decimal(coefficient)
    if coefficient_decimal in (None, Decimal("0")):
        raise AirfreightOperationError("invalid_volume_coefficient", "体积重系数必须是正十进制")
    results: list[dict[str, Any]] = []
    for group in package_groups:
        length = _decimal(group.get("length_cm", group.get("length_text")))
        width = _decimal(group.get("width_cm", group.get("width_text")))
        height = _decimal(group.get("height_cm", group.get("height_text")))
        gross = _decimal(group.get("gross_weight_kg", group.get("gross_weight_text")))
        count = _decimal(group.get("package_count", group.get("package_count_text")))
        semantics = group.get("dimension_semantics", "unknown")
        if None in (length, width, height, gross) or semantics not in {"single_piece", "group_outer"} or (semantics == "single_piece" and count is None):
            return {"status": "needs_manual_review", "groups": results, "reason": "尺寸、毛重、CTN 或单件／整组语义不完整"}
        multiplier = count if semantics == "single_piece" else Decimal("1")
        cbm = length * width * height / Decimal("1000000") * multiplier
        volume_weight = cbm * Decimal("1000000") / coefficient_decimal
        results.append({"status": "calculated", "cbm": _decimal_text(cbm), "volume_weight_kg": _decimal_text(volume_weight), "gross_weight_kg": _decimal_text(gross), "formula": f"{length}×{width}×{height}" + (f"×{count}" if semantics == "single_piece" else "") + f"÷{coefficient_decimal}"})
    total_gross = sum((_decimal(item["gross_weight_kg"]) or Decimal("0") for item in results), Decimal("0"))
    total_volume = sum((_decimal(item["volume_weight_kg"]) or Decimal("0") for item in results), Decimal("0"))
    return {"status": "calculated", "groups": results, "total_gross_weight_kg": _decimal_text(total_gross), "total_volume_weight_kg": _decimal_text(total_volume), "chargeable_weight_kg": _decimal_text(max(total_gross, total_volume)), "coefficient": _decimal_text(coefficient_decimal)}
