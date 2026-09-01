from __future__ import annotations

from dataclasses import dataclass
import re

from .models import Classification, ResponseAssessment


@dataclass(frozen=True, slots=True)
class _Rule:
    category: str
    keywords: tuple[str, ...]
    base_confidence: float


LOGISTICS_CATEGORIES = (
    "询价报价",
    "提送货安排",
    "时效与航线",
    "订舱与下单",
    "报关与清关",
    "轨迹与状态",
    "运输异常",
    "理赔与索赔",
    "账单与结算",
    "包装要求",
    "货物规格",
    "其他物流问题",
)

SEMANTIC_CATEGORIES = LOGISTICS_CATEGORIES + (
    "角色与人设",
    "提示词与设定",
    "效果评价",
    "需求与迭代",
    "故障与失败",
    "任务分发",
    "合规与边界",
)

RESPONSE_KINDS = ("即时响应", "报价方案", "处理方案", "状态更新", "一般回复")

_SEMANTIC_HINTS = (
    "人设",
    "提示词",
    "prompt",
    "cosplay",
    "角色扮演",
    "智能体",
    "大模型",
    "系统提示",
    "ai",
)

_RULES = (
    _Rule("询价报价", ("询价", "报价", "报个价", "费用", "价格", "运价", "多少钱"), 0.94),
    _Rule("提送货安排", ("提货", "取货", "送货", "送到", "派送", "上门提", "提货地址"), 0.93),
    _Rule("时效与航线", ("时效", "多久", "航班", "船期", "隔日达", "机场", "港口", "航线"), 0.88),
    _Rule("订舱与下单", ("订舱", "下单", "托书", "舱位", "订仓"), 0.94),
    _Rule("报关与清关", ("报关", "清关", "查验", "海关", "申报", "放行"), 0.95),
    _Rule("轨迹与状态", ("轨迹", "到哪", "物流状态", "签收", "查件", "跟踪", "单号"), 0.90),
    _Rule("运输异常", ("异常", "延误", "破损", "丢失", "扣货", "漏液", "退运", "未到"), 0.95),
    _Rule("理赔与索赔", ("理赔", "索赔", "赔付", "赔偿"), 0.97),
    _Rule("账单与结算", ("账单", "对账", "发票", "付款", "结算", "开票"), 0.94),
    _Rule("包装要求", ("包装", "木箱", "托盘", "打板", "缠膜", "纸箱"), 0.89),
    _Rule("货物规格", ("ctns", "kgs", "cbm", "plt", "件数", "重量", "体积", "尺寸"), 0.84),
)

_REQUEST_MARKERS = (
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
    "?",
    "？",
)

_EXPLICIT_REQUEST_MARKERS = (
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
    "?",
    "？",
)

_ACK_ONLY = re.compile(r"^\s*(马上|好的|好|收到|已收到|ok|行|可以|在查|稍等|安排中)[！!。.]?\s*$", re.I)
_PRICE = re.compile(r"(?:¥|￥|rmb|cny|usd|eur|\d+(?:\.\d+)?\s*(?:元|块|美金|美元))", re.I)
_SOLUTION_TERMS = ("已安排", "已提货", "已送达", "已放行", "已处理", "解决", "改走", "改为", "建议", "方案", "可安排")
_STATUS_TERMS = ("处理中", "已联系", "已确认", "预计", "正在", "进度", "状态")


def classify_issue(text: str) -> list[Classification]:
    compact = text.casefold()
    has_explicit_request = any(marker.casefold() in compact for marker in _EXPLICIT_REQUEST_MARKERS)
    has_request = any(marker.casefold() in compact for marker in _REQUEST_MARKERS)
    looks_like_solution = bool(_PRICE.search(compact)) or any(term in compact for term in _SOLUTION_TERMS)
    if looks_like_solution and not has_explicit_request:
        return []
    matches: list[Classification] = []

    for rule in _RULES:
        evidence = tuple(keyword for keyword in rule.keywords if keyword.casefold() in compact)
        if not evidence:
            continue
        if rule.category == "货物规格" and not has_request:
            continue
        confidence = min(0.99, rule.base_confidence + 0.01 * (len(evidence) - 1))
        matches.append(Classification(rule.category, confidence, evidence))

    if not matches and has_request:
        matches.append(Classification("其他物流问题", 0.62, tuple(marker for marker in _REQUEST_MARKERS if marker in text)))
    return matches


def needs_semantic(text: str, matches: list[Classification]) -> bool:
    compact = text.casefold()
    if any(item.category == "其他物流问题" for item in matches):
        return True
    if any(hint.casefold() in compact for hint in _SEMANTIC_HINTS):
        return True
    if matches:
        return False
    return any(marker.casefold() in compact for marker in _EXPLICIT_REQUEST_MARKERS)


def assess_response(text: str) -> ResponseAssessment:
    compact = text.strip()
    if _ACK_ONLY.match(compact):
        return ResponseAssessment("即时响应", False, 0.96)
    if _PRICE.search(compact):
        return ResponseAssessment("报价方案", True, 0.93)
    if any(term in compact for term in _SOLUTION_TERMS):
        return ResponseAssessment("处理方案", True, 0.90)
    if any(term in compact for term in _STATUS_TERMS):
        return ResponseAssessment("状态更新", False, 0.82)
    return ResponseAssessment("一般回复", False, 0.58)


def summarize_problem(text: str, limit: int = 140) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"
