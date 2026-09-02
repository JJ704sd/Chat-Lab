from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import re
from typing import Any

from .wecom_protobuf import parse_wecom_content
from .wecom_parser import WecomUnifiedRecord


# 10 categories required by spec:
# 询价报价, 提货安排, 运输方式与线路, 时效与轨迹, 入仓预约, 单证报关, 派送签收, 费用对账, 延误/丢损/异常, 其他待分类问题
LOGISTICS_CATEGORIES = (
    "询价报价",
    "提货安排",
    "运输方式与线路",
    "时效与轨迹",
    "入仓预约",
    "单证报关",
    "派送签收",
    "费用对账",
    "延误/丢损/异常",
    "其他待分类问题",
)


@dataclass(frozen=True, slots=True)
class CategoryRule:
    category: str
    keywords: tuple[str, ...]
    base_confidence: float


RULES = (
    CategoryRule("询价报价", ("询价", "报价", "报个价", "费用", "价格", "运价", "多少钱", "单价", "运费", "成本"), 0.94),
    CategoryRule("提货安排", ("提货", "取货", "上门提", "提货地址", "安排提货", "拉货", "装车"), 0.93),
    CategoryRule("运输方式与线路", ("空运", "海运", "陆运", "铁运", "快递", "专线", "直飞", "转运", "航班", "船期", "港口", "机场", "路线", "航线"), 0.90),
    CategoryRule("时效与轨迹", ("时效", "多久", "几天", "隔日达", "轨迹", "到哪", "查件", "跟踪", "单号", "转单号", "物流进度", "更新轨迹"), 0.91),
    CategoryRule("入仓预约", ("入仓", "进仓", "预约", "送仓", "仓位", "订舱", "进仓单", "预约号", "交仓"), 0.92),
    CategoryRule("单证报关", ("报关", "清关", "查验", "海关", "申报", "放行", "单证", "提单", "箱单", "发票", "报关单", "商检"), 0.95),
    CategoryRule("派送签收", ("派送", "送达", "签收", "送货", "自提", "末端", "交货", "收货人", "收件"), 0.92),
    CategoryRule("费用对账", ("账单", "对账", "付款", "结算", "开票", "水单", "欠款", "账期", "汇款", "退款"), 0.94),
    CategoryRule("延误/丢损/异常", ("异常", "延误", "破损", "丢失", "扣货", "漏液", "退运", "未到", "卡关", "查扣", "损坏", "受损", "湿损", "少件", "丢件", "索赔", "理赔"), 0.96),
)

REQUEST_MARKERS = (
    "请",
    "麻烦",
    "帮忙",
    "帮我",
    "需要",
    "是否",
    "怎么",
    "为什么",
    "安排",
    "查询",
    "报价",
    "提货",
    "送到",
    "查一下",
    "确认下",
    "?",
    "？",
)

EXPLICIT_REQUEST_MARKERS = (
    "请",
    "麻烦",
    "帮忙",
    "帮我",
    "需要",
    "是否",
    "怎么",
    "为什么",
    "多久",
    "查询",
    "查一下",
    "确认下",
    "?",
    "？",
)

_ACK_ONLY = re.compile(r"^\s*(马上|好的|好|收到|已收到|ok|OK|行|可以|在查|稍等|安排中|收到收到|收到已安排|正在处理|在看了)[！!。.]?\s*$", re.I)
_PRICE = re.compile(r"(?:¥|￥|rmb|cny|usd|eur|\d+(?:\.\d+)?\s*(?:元|块|美金|美元|/kg|/cbm|/箱|/件))", re.I)
_SOLUTION_TERMS = ("已安排", "已提货", "已送达", "已放行", "已处理", "已出单", "已预约", "改走", "改为", "建议", "方案", "可安排", "请核对", "单号为", "转单号：", "转单号:")
_STATUS_TERMS = ("处理中", "已联系", "已确认", "预计", "正在", "进度", "状态", "在排查", "在跟进", "在查")

# Regex for business clues
_RE_WAYBILL = re.compile(r"\b([A-Z0-9]{8,22})\b", re.I)
_RE_SPECS_CTNS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ctns?|箱|件)", re.I)
_RE_SPECS_KGS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:kgs?|kg|公斤|千克)", re.I)
_RE_SPECS_CBM = re.compile(r"(\d+(?:\.\d+)?)\s*(?:cbm|方|立方)", re.I)
_RE_ORIGIN_DEST = re.compile(r"(?:从|起运[地点]?[:：]?\s*)([\u4e00-\u9fa5A-Za-z]+).{0,6}(?:到|发往|目的[地国]?[:：]?\s*)([\u4e00-\u9fa5A-Za-z]+)")


@dataclass(frozen=True, slots=True)
class BusinessClues:
    waybill_no: str | None
    order_no: str | None
    origin: str | None
    destination: str | None
    packages: str | None
    weight: str | None
    volume: str | None
    price_quote: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "waybill_no": self.waybill_no,
            "order_no": self.order_no,
            "origin": self.origin,
            "destination": self.destination,
            "packages": self.packages,
            "weight": self.weight,
            "volume": self.volume,
            "price_quote": self.price_quote,
        }


def extract_business_clues(text: str) -> BusinessClues:
    ctns_m = _RE_SPECS_CTNS.search(text)
    kgs_m = _RE_SPECS_KGS.search(text)
    cbm_m = _RE_SPECS_CBM.search(text)
    od_m = _RE_ORIGIN_DEST.search(text)
    price_m = _PRICE.search(text)

    # Waybill extraction: looking for common tracking numbers (avoiding small numbers)
    wb_match = None
    for m in re.finditer(r"(?:单号|运单号|转单号|tracking|waybill)[:：\s]*([A-Za-z0-9_-]{6,30})", text, re.I):
        wb_match = m.group(1).strip()
        break

    return BusinessClues(
        waybill_no=wb_match,
        order_no=None,
        origin=od_m.group(1) if od_m else None,
        destination=od_m.group(2) if od_m else None,
        packages=ctns_m.group(0) if ctns_m else None,
        weight=kgs_m.group(0) if kgs_m else None,
        volume=cbm_m.group(0) if cbm_m else None,
        price_quote=price_m.group(0) if price_m else None,
    )


@dataclass(frozen=True, slots=True)
class LogisticsIssueClassification:
    category: str
    confidence: float
    evidence: tuple[str, ...]


def classify_logistics_issue(text: str) -> list[LogisticsIssueClassification]:
    compact = text.casefold()
    has_explicit_request = any(marker.casefold() in compact for marker in EXPLICIT_REQUEST_MARKERS)
    has_request = any(marker.casefold() in compact for marker in REQUEST_MARKERS)
    looks_like_solution = bool(_PRICE.search(compact)) or any(term in compact for term in _SOLUTION_TERMS)

    if looks_like_solution and not has_explicit_request:
        return []

    matches: list[LogisticsIssueClassification] = []
    for rule in RULES:
        evidence = tuple(kw for kw in rule.keywords if kw.casefold() in compact)
        if not evidence:
            continue
        conf = min(0.99, rule.base_confidence + 0.01 * (len(evidence) - 1))
        matches.append(LogisticsIssueClassification(rule.category, conf, evidence))

    if not matches and has_request:
        matches.append(
            LogisticsIssueClassification(
                "其他待分类问题", 0.60, tuple(marker for marker in REQUEST_MARKERS if marker in text)
            )
        )
    return matches


@dataclass(frozen=True, slots=True)
class LogisticsResponseAssessment:
    kind: str  # 即时响应 | 报价方案 | 处理方案 | 状态更新 | 一般回复
    is_solution: bool
    status_contribution: str  # acknowledged | solved | in_progress | none
    solution_text: str | None
    confidence: float


def assess_logistics_response(text: str) -> LogisticsResponseAssessment:
    compact = text.strip()
    if _ACK_ONLY.match(compact):
        return LogisticsResponseAssessment(
            kind="即时响应",
            is_solution=False,
            status_contribution="acknowledged",
            solution_text=None,
            confidence=0.96,
        )
    if _PRICE.search(compact):
        return LogisticsResponseAssessment(
            kind="报价方案",
            is_solution=True,
            status_contribution="solved",
            solution_text=compact,
            confidence=0.94,
        )
    if any(term in compact for term in _SOLUTION_TERMS):
        return LogisticsResponseAssessment(
            kind="处理方案",
            is_solution=True,
            status_contribution="solved",
            solution_text=compact,
            confidence=0.91,
        )
    if any(term in compact for term in _STATUS_TERMS):
        return LogisticsResponseAssessment(
            kind="状态更新",
            is_solution=False,
            status_contribution="in_progress",
            solution_text=None,
            confidence=0.85,
        )
    return LogisticsResponseAssessment(
        kind="一般回复",
        is_solution=False,
        status_contribution="none",
        solution_text=None,
        confidence=0.55,
    )
