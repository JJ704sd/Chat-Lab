from __future__ import annotations

from dataclasses import asdict, dataclass, field
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
    CategoryRule("询价报价", ("询价", "报价", "报个价", "费用", "价格", "运价", "多少钱", "单价", "运费", "成本", "这个呢"), 0.94),
    CategoryRule("提货安排", ("提货", "取货", "上门提", "提货地址", "安排提货", "拉货", "装车"), 0.93),
    CategoryRule("运输方式与线路", ("空运", "海运", "陆运", "铁运", "快递", "专线", "直飞", "转运", "航班", "船期", "港口", "机场", "路线", "航线"), 0.90),
    CategoryRule("时效与轨迹", ("时效", "多久", "几天", "几号到", "哪天到", "什么时候到", "隔日达", "次日达", "当天达", "轨迹", "到哪", "查件", "跟踪", "单号", "转单号", "物流进度", "更新轨迹"), 0.91),
    CategoryRule("入仓预约", ("入仓", "进仓", "预约", "送仓", "仓位", "订舱", "进仓单", "预约号", "交仓"), 0.92),
    CategoryRule("单证报关", ("报关", "清关", "查验", "海关", "申报", "放行", "单证", "提单", "箱单", "发票", "报关单", "商检", "危险品", "危化品", "化工品", "带电", "禁运", "8类"), 0.95),
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

_ACK_ONLY = re.compile(
    r"^\s*(?:@\S+\s+)?(马上|好的|好|收到|已收到|ok|OK|行|可以|在查|稍等|安排中|收到收到|收到已安排|正在处理|在看了|马上看|马上查|马上报)[！!。.]?\s*$",
    re.I,
)
_PRICE = re.compile(
    r"(?:(?:¥|￥|rmb|cny|usd|eur)\s*\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s*(?:元|块|美金|美元|CNY|RMB|USD|EUR|/kg|/cbm|/箱|/件)(?:\s*/\s*(?:kg|cbm|箱|件))?)",
    re.I,
)
_BARE_QUOTE = re.compile(r"(?<![\d.])\d+(?:\.\d+)?(?=\s*(?:隔日达|次日达|当天达))")
_KNOWN_CITIES = "上海|北京|广州|深圳|郑州|浦东|香港|义乌|宁波|厦门|武汉|成都|重庆|青岛|天津|杭州|南京|无锡|苏州|东莞|佛山|大连|长沙|福州|西安|昆明|合肥|南宁|贵阳|兰州|海口|沈阳|长春|哈尔滨|太原|石家庄|济南|西宁|银川|呼和浩特|拉萨|乌鲁木齐|洛杉矶|芝加哥|纽约|美西|美东|ONT8|LAX|JFK|ORD"

_CITY_SHORT_QUOTE = re.compile(
    rf"(?:^|[^\w])(?:@\S+\s+)?(?:{_KNOWN_CITIES})\s*[:：]?\s*(\d{{2,6}}(?:\.\d+)?)\s*(?:元|块|RMB|CNY|USD|美金|/kg|/cbm)?(?:\s*(?:隔日达|次日达|当天达|特快|普快))?\s*$",
    re.I,
)
_DELIVERY_SOLUTION = re.compile(
    rf"^\s*(?:@\S+\s+)?(?:(?:{_KNOWN_CITIES})\s*)?(?:隔日达|次日达|当天达|\d+(?:[-~至到]\d+)?\s*(?:个工作日|工作日|天|小时)(?:达|到)?)\s*[。.!！]?\s*$", re.I
)

_EXTRA_LOCATIONS = "北京首都机场|北京大兴机场|上海浦东机场|成都天府机场|成都双流机场|广州白云机场|重庆机场|上海浦东|上海虹桥|北京首都|北京大兴|成都天府|成都双流|广州白云|深圳南山|青岛即墨|首都|大兴|天府|双流|白云|虹桥|鄂州|湖北|常州|珠海|昆山|南通|海门|如皋|云浮|江门|丽水|缙云|慈溪|桂林|绍兴|柯桥|湖州|乐清|温岭|扬州|宜兴|江阴|沧州|吴桥|中山|惠州|肇庆|台州|嘉兴|金华|泉州|漳州|汕头|汕尾|德清|成渝"
_LOCATION = rf"(?:深圳宝安机场|广深仓库|广深|新疆|芜湖|晋江|南阳|常熟|营口|威海|高密|兴化|永嘉|泉州|周口|{_EXTRA_LOCATIONS}|{_KNOWN_CITIES})(?:机场|仓库|仓)?"
_DELIVERY_TEXT = r"大后天到|后天到|明天到|当天到|隔日达|次日达|当天达|次日到|隔天到|(?:\d+(?:\s*[-~至到]\s*\d+)?|[一二两三四五六七八九十]+)\s*(?:个工作日|工作日|个小时|小时|天)"
_ROUTE_PRICE = re.compile(rf"(?P<destination>{_LOCATION})\s*[:：]?\s*(?P<amount>\d{{2,6}}(?:\.\d+)?)(?![\d.])(?!(?:\s*)(?:kgs?|cbm|ctns?|pcs|plts?|cases?|pkgs?|kg|吨|公斤|千克|箱|托|件|立方|[*×]))", re.I)


def _without_mentions(text: str) -> str:
    # Native rich text serializes each @mention as its own line.
    body = re.sub(r"^(?:\s*@[^\n]+\n)+", "", text).strip()
    return re.sub(r"^(?:@\S+\s+)+", "", body).strip()


def extract_quote_options(text: str) -> list[dict[str, Any]]:
    body = _without_mentions(text)
    matches = list(_ROUTE_PRICE.finditer(body))
    options = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        tail = body[match.end():end]
        delivery = re.search(_DELIVERY_TEXT, tail)
        currency = re.match(r"\s*(CNY|RMB|USD|EUR|人民币|美元|美金|欧元|元|块)", tail, re.I)
        options.append({"destination": match['destination'], "amount": match['amount'],
                        "delivery_time": delivery.group(0) if delivery else None,
                        "currency": currency.group(1) if currency else None,
                        "raw": body[match.start():end].strip(), "basis": "explicit_destination_amount"})
        # A second service tier can share a destination explicitly stated once.
        alternative = re.search(rf"(?P<amount>\d{{2,6}}(?:\.\d+)?)\s*(?P<time>{_DELIVERY_TEXT})", tail)
        if alternative:
            options.append({"destination": match['destination'], "amount": alternative['amount'],
                            "delivery_time": alternative['time'], "currency": None,
                            "raw": alternative.group(0), "basis": "same_destination_service_alternative"})
    if not options:
        bare = re.fullmatch(r"(?:自交|专车)?\s*(\d{2,6}(?:\.\d+)?)\s*((?:[^\d]*)(?:\d+个?小时)?)", body)
        if bare and (not bare[2] or re.search(r"当天|次日|隔日|早班车|在途|元|块", bare[2])):
            delivery = re.search(_DELIVERY_TEXT, bare[2])
            options.append({"destination": None, "amount": bare[1],
                            "delivery_time": delivery.group(0) if delivery else None,
                            "currency": "元" if "元" in bare[2] else "块" if "块" in bare[2] else None,
                            "raw": body, "basis": "bare_amount_requires_reply_context"})
    return options

_SOLUTION_TERMS = (
    "已安排",
    "已提货",
    "已送达",
    "已放行",
    "已处理",
    "已出单",
    "已预约",
    "改走",
    "改为",
    "建议",
    "方案",
    "可安排",
    "请核对",
    "单号为",
    "转单号：",
    "转单号:",
    "做不了",
    "接不了",
    "走不了",
    "无法安排",
    "无法承运",
    "不接",
    "不能接",
    "做不到",
    "拒收",
    "时效赶不上",
)
_STATUS_TERMS = ("处理中", "已联系", "已确认", "预计", "正在", "进度", "状态", "在排查", "在跟进", "在查")

# Regex for business clues
_RE_WAYBILL = re.compile(r"\b([A-Z0-9]{8,22})\b", re.I)
_RE_SPECS_CTNS = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:ctns?|plts?|pallets?|pieces?|pcs|cases?|pkgs?|box(?:es)?|wooden\s*cases?|托盘|托|拖|木箱|箱|件|包|桶)", re.I)
_RE_SPECS_KGS = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:kgs?|kg|公斤|千克|吨|tons?|t)", re.I)
_RE_SPECS_CBM = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:cbm|m3|m³|立方|方)", re.I)
_RE_ORIGIN_DEST_1 = re.compile(
    r"(?:从|起运[地点]?[:：]?\s*)([\u4e00-\u9fa5A-Za-z]+).{0,6}(?:到|发往|目的[地国]?[:：]?\s*)([\u4e00-\u9fa5A-Za-z]+)"
)
_RE_ORIGIN_DEST_2 = re.compile(
    r"(?:货交|交)\s*([\u4e00-\u9fa5A-Za-z]{2,8})\s*(?:送|发|送到)\s*([\u4e00-\u9fa5A-Za-z]{2,8})"
)
_RE_ORIGIN_DEST_3 = re.compile(
    r"([\u4e00-\u9fa5A-Za-z]{2,8})\s*[-—–/]\s*([\u4e00-\u9fa5A-Za-z]{2,8})"
)



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
    pickup_address: str | None = None
    dimensions: str | None = None
    delivery_time: str | None = None
    currency: str | None = None
    package_items: list[str] | None = None
    weight_items: list[str] | None = None
    volume_items: list[str] | None = None
    dimension_items: list[str] | None = None
    special_goods: list[str] | None = None
    document_requirements: list[str] | None = None
    origin_basis: str | None = None
    destination_items: list[str] | None = None
    quote_options: list[dict[str, Any]] | None = None
    shipment_constraints: list[str] | None = None
    route_items: list[dict[str, Any]] | None = None
    location_mentions: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_business_clues(text: str) -> BusinessClues:
    quote_options = extract_quote_options(text)
    ctns_m = _RE_SPECS_CTNS.search(text)
    kgs_m = _RE_SPECS_KGS.search(text)
    cbm_m = _RE_SPECS_CBM.search(text)

    # Origin and destination extraction
    origin = None
    destination = None
    od1 = _RE_ORIGIN_DEST_1.search(text)
    if od1:
        origin = od1.group(1).strip()
        destination = od1.group(2).strip()
    else:
        od2 = _RE_ORIGIN_DEST_2.search(text)
        if od2:
            origin = od2.group(1).strip()
            destination = od2.group(2).strip()
        else:
            od3 = _RE_ORIGIN_DEST_3.search(text)
            if od3:
                # Avoid non-location words like '8-31' or '2025-2026'
                p1, p2 = od3.group(1).strip(), od3.group(2).strip()
                if re.search(_KNOWN_CITIES, p1, re.I) and re.search(_KNOWN_CITIES, p2, re.I):
                    origin, destination = p1, p2

    price_m = _PRICE.search(text) or _BARE_QUOTE.search(text) or _CITY_SHORT_QUOTE.search(text)
    pickup = re.search(r"(?:(?:提货|取货)地址|提)\s*[:：，,]\s*([^\n；;]+)", text)
    pickup_text = pickup.group(1).strip() if pickup else None
    origin_basis = "route_text" if origin else None
    if pickup:
        city = re.search(_LOCATION, pickup.group(1), re.I)
        if city:
            origin = city.group(0)
            origin_basis = "pickup_address_city"
    dest_match = re.search(r"(?:送货地址\s*[:：]|送到|发往|目的地\s*[:：]|提送|提交|到|送\s*[:：]?)\s*([^\n；;。]+)", text)
    if dest_match:
        destination = dest_match.group(1).strip()
        if re.fullmatch(r'呢[？?]?|吗[？?]?|几号[？?]?|几天[？?]?|哪天[？?]?',destination):
            destination = None
            dest_match = None

    # Keep every explicitly named destination; don't turn a multi-airport
    # request into a single route or infer a missing origin from a reply.
    destination_items = []
    if destination:
        destination_items = list(dict.fromkeys(m.group(0) for m in re.finditer(_LOCATION, destination, re.I)))
        if destination_items:
            destination = destination_items[0]
    if not origin and (kgs_m or cbm_m or ctns_m):
        boundary = re.search(r"提送|送到|发往|\bto\b|提|送", text, re.I)
        prefix = text[:boundary.start()] if boundary else ""
        city = re.search(r"(?:省|自治区)?([\u4e00-\u9fff]{2,7})市", prefix)
        locations = list(re.finditer(_LOCATION, prefix, re.I))
        if locations:
            origin, origin_basis = locations[0].group(0), "explicit_route_location"
        elif city:
            origin, origin_basis = city[1].split('省')[-1], "explicit_address_city"
        if not pickup and boundary and '提' in boundary.group(0):
            address = re.search(r"((?:[\u4e00-\u9fff]{2,4}省)?[^\n/]*?(?:市|区|县|镇|路|街)[^\n]*)$", prefix)
            if address:
                pickup = address
                pickup_text = address.group(1).strip()
    if not destination_items and not dest_match:
        tail_match = re.search(r"提\s*([^\n]+)$", text)
        if tail_match:
            destination_items = list(dict.fromkeys(m.group(0) for m in re.finditer(_LOCATION, tail_match[1], re.I)))
            if destination_items:
                destination = destination_items[0]

    route_pattern = re.compile(rf"(?P<origin>{_LOCATION})(?P<address>[^。；;]{{0,240}}?)(?:提送|送到|送货|提货到|提交|提货|提|送|到|交|[-—–]+)\s*(?P<destination>{_LOCATION})", re.I)
    explicit_routes = list(route_pattern.finditer(text))
    route_items = []
    for index, match in enumerate(explicit_routes):
        end = explicit_routes[index + 1].start() if index + 1 < len(explicit_routes) else len(text)
        destinations = list(dict.fromkeys(m.group(0) for m in re.finditer(_LOCATION, text[match.start('destination'):end], re.I)))
        for target in destinations:
            route_items.append({'origin': match['origin'], 'destination': target, 'basis': 'explicit_route_text'})
    if explicit_routes:
        first = explicit_routes[0]
        origin, origin_basis = first['origin'], 'explicit_route_text'
        destination_items = list(dict.fromkeys(item['destination'] for item in route_items))
        destination = destination_items[0] if destination_items else destination
        if not pickup_text and re.search(r'路|街|号|园|镇|区', first['address']) and not re.search(r'kg|cbm|ctn', first['address'], re.I):
            pickup_text = (first['origin'] + first['address']).strip()
    elif origin and destination_items:
        route_items = [{'origin': origin, 'destination': value, 'basis': origin_basis} for value in destination_items]

    dimensions = re.compile(
        r"\d+(?:[.,]\d+)?\s*(?:mm|cm|m)?\s*[*×xX\-]\s*\d+(?:[.,]\d+)?\s*(?:mm|cm|m)?\s*[*×xX\-]\s*\d+(?:[.,]\d+)?\s*(?:mm|cm|m|毫米|厘米|米)?(?:\s*(?:[-‑–—=*/]\s*\d+|\(\d+\)))?",
        re.I,
    )
    dimension_items = [m.group(0).strip() for m in dimensions.finditer(text)
                       if not re.fullmatch(r"20\d\d-\d{1,2}-\d{1,2}", m.group(0).strip())]
    delivery = re.search(
        _DELIVERY_TEXT, text, re.I
    )
    currency = (
        re.search(r"CNY|RMB|USD|EUR|人民币|美元|美金|欧元|元|块|¥|￥", price_m.group(0), re.I)
        if price_m
        else None
    )
    order = re.search(r"(?:订单号|订单编号|order)\s*[:：]?\s*([A-Za-z0-9_-]{6,30})", text, re.I)

    # Waybill extraction: looking for tracking numbers
    wb_match = None
    for m in re.finditer(
        r"(?:单号|运单号|转单号|tracking|waybill)[:：\s]*([A-Za-z0-9_-]{6,30})", text, re.I
    ):
        wb_match = m.group(1).strip()
        break

    return BusinessClues(
        waybill_no=wb_match,
        order_no=order.group(1) if order else None,
        origin=origin,
        destination=destination,
        packages=ctns_m.group(0) if ctns_m else None,
        weight=kgs_m.group(0) if kgs_m else None,
        volume=cbm_m.group(0) if cbm_m else None,
        price_quote=(price_m.group(1) if price_m.re is _CITY_SHORT_QUOTE else price_m.group(0)) if price_m else quote_options[0]['amount'] if quote_options else None,
        pickup_address=pickup_text,
        dimensions=dimension_items[0] if dimension_items else None,
        delivery_time=delivery.group(0) if delivery else None,
        currency=currency.group(0) if currency else None,
        package_items=[m.group(0) for m in _RE_SPECS_CTNS.finditer(text)] or None,
        weight_items=[m.group(0) for m in _RE_SPECS_KGS.finditer(text)] or None,
        volume_items=[m.group(0) for m in _RE_SPECS_CBM.finditer(text)] or None,
        dimension_items=dimension_items or None,
        special_goods=re.findall(r"氢氧化钾溶液|(?:[1-9](?:\.\d)?类)?危险品|危化品|化工品|带电|锂电池|禁运品", text) or None,
        document_requirements=re.findall(r"报关|清关|商检|箱单|报关单|提单|单证", text) or None,
        origin_basis=origin_basis,
        destination_items=destination_items or None,
        quote_options=quote_options or None,
        shipment_constraints=re.findall(r"超高|超长|超重|不可堆叠|不能堆叠|不叠放|不能倒置|不能自叠|单件重量|注意长度|厢式货车", text) or None,
        route_items=route_items or None,
        location_mentions=list(dict.fromkeys(m.group(0) for m in re.finditer(_LOCATION, text, re.I))) or None,
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
    looks_like_solution = (
        bool(_PRICE.search(compact) or _BARE_QUOTE.search(compact) or _CITY_SHORT_QUOTE.search(compact) or _DELIVERY_SOLUTION.match(compact) or extract_quote_options(text))
        or any(term in compact for term in _SOLUTION_TERMS)
    )

    if looks_like_solution and not has_explicit_request:
        return []

    matches: list[LogisticsIssueClassification] = []
    for rule in RULES:
        evidence = tuple(kw for kw in rule.keywords if kw.casefold() in compact)
        if not evidence:
            continue
        conf = min(0.99, rule.base_confidence + 0.01 * (len(evidence) - 1))
        matches.append(LogisticsIssueClassification(rule.category, conf, evidence))
    if re.search(r'(?:\d+|[一二两三四五六七八九十]+)天.*[?？]', text) and not any(m.category=='时效与轨迹' for m in matches):
        matches.append(LogisticsIssueClassification('时效与轨迹',0.94,('具体天数确认',)))

    # Freight spec inquiry detection (e.g. "广州-浦东 541.55kg/4.22cbm/20pcs" or "39ctn/624.2kg/0.36cbm 货交上海送郑州")
    ctns_m = _RE_SPECS_CTNS.search(text)
    kgs_m = _RE_SPECS_KGS.search(text)
    cbm_m = _RE_SPECS_CBM.search(text)
    has_specs = sum(bool(x) for x in (ctns_m, kgs_m, cbm_m)) >= 2
    has_route = bool(_RE_ORIGIN_DEST_1.search(text) or _RE_ORIGIN_DEST_2.search(text) or _RE_ORIGIN_DEST_3.search(text))

    if has_specs and not any(m.category == "询价报价" for m in matches):
        ev = []
        if ctns_m:
            ev.append(ctns_m.group(0))
        if kgs_m:
            ev.append(kgs_m.group(0))
        if cbm_m:
            ev.append(cbm_m.group(0))
        matches.append(LogisticsIssueClassification("询价报价", 0.93, tuple(ev)))

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
    if (_PRICE.search(compact) or _BARE_QUOTE.search(compact) or _CITY_SHORT_QUOTE.search(compact)
            or _DELIVERY_SOLUTION.match(compact) or extract_quote_options(text)
            or re.fullmatch(rf"\s*(?:{_LOCATION})?\s*(?:时效)?\s*(?:{_DELIVERY_TEXT})\s*", _without_mentions(text))):
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
