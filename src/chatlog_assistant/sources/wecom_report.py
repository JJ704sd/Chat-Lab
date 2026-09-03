"""Evidence-backed, question-level reporting over persisted local analysis."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
import csv
from pathlib import Path
import re
from statistics import mean, median

from .wecom_classifier import extract_business_clues
from .wecom_analysis import split_quoted_reply, message_order_key
from .wecom_subject import sender_label


def display_safe_text(text):
    """Raw evidence stays in SQLite; credential values don't enter the dashboard."""
    text = re.sub(r'(?i)([?&](?:sid|token|access_token|password|secret|auth|key)=)[^&#\s]+',r'\1[已隐藏]',text)
    text = re.sub(r'(?im)((?:密码|口令|password|secret)\s*[:：=]\s*)\S+',r'\1[已隐藏]',text)
    text = re.sub(r'(?m)^(\d{5,12})[，,、:：\s]+(?=\S*[A-Za-z])(?=\S*\d)\S{6,}$',r'\1，[凭据已隐藏]',text)
    text = re.sub(r'(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)',r'\1****\2',text)
    return text


def display_safe_value(value):
    """Also redact quoted text and extracted addresses in nested evidence."""
    if isinstance(value, str):
        return display_safe_text(value)
    if isinstance(value, dict):
        return {key: display_safe_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [display_safe_value(item) for item in value]
    return value


def latency_stats(values):
    values = sorted(v for v in values if v is not None)
    return {"sample_count": len(values), "mean_seconds": round(mean(values), 2) if values else None,
            "median_seconds": median(values) if values else None,
            "min_seconds": min(values) if values else None, "max_seconds": max(values) if values else None}


def category_statistics(events):
    """Count every category label on every business round (multi-label aware)."""
    counts = Counter()
    for event in events:
        counts.update(event.get("category_statuses", {}).keys())
    return [{"category": category, "count": count}
            for category, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def region(location):
    if not location:
        return "未知"
    for label, cities in (("华北", "北京 天津 河北 山西 内蒙古 首都 大兴 沧州 吴桥 保定 太原 石家庄 呼和浩特"),
                          ("华东", "上海 浦东 虹桥 江苏 无锡 苏州 南京 杭州 浙江 山东 安徽 福建 江西 宁波 义乌 厦门 福州 泉州 漳州 晋江 合肥 芜湖 滁州 温州 温岭 乐清 永嘉 台州 金华 嘉兴 湖州 德清 绍兴 柯桥 丽水 缙云 慈溪 南通 海门 如皋 昆山 常熟 常州 扬州 宜兴 江阴 兴化 青岛 威海 济南 高密"),
                          ("华南", "广东 广州 深圳 广深 白云 东莞 佛山 广西 海南 香港 中山 惠州 珠海 肇庆 云浮 江门 汕头 汕尾 桂林 南宁 海口"),
                          ("西南", "成都 重庆 四川 贵州 云南 西藏 昆明 贵阳 拉萨 天府 双流 成渝"),
                          ("华中", "郑州 河南 武汉 湖北 湖南 长沙 鄂州 南阳 周口"),
                          ("东北", "辽宁 吉林 黑龙江 沈阳 大连 营口 长春 哈尔滨"),
                          ("西北", "陕西 西安 甘肃 青海 宁夏 新疆 乌鲁木齐 兰州 西宁 银川")):
        if any(city in location for city in cities.split()):
            return label
    return "未知"


def build_report(messages, issues, responses, *, subject=None, category=None, status=None):
    messages = sorted(messages, key=message_order_key)
    for row in messages:
        row["provenance"] = json.loads(row.pop("provenance_json", "{}"))
        row["sent_at"] = row["sent_at"] or None
        row["company_status"] = row.get("company_status") or ("known" if row.get("sender_corp_name") else "unknown")
        row["sender_label"] = sender_label(
            row.get("sender_name"), row.get("sender_corp_name"),
            company_status=row["company_status"],
        )
        safe = display_safe_text(row['text'])
        row['display_redacted'] = safe != row['text']
        row['text'] = safe
    verified_names = {m['conversation_name'].casefold() for m in messages if m['provenance'].get('origin')=='database'}
    excluded = [m for m in messages if m['provenance'].get('origin')!='database' and m['conversation_name'].casefold() in verified_names]
    messages = [m for m in messages if m not in excluded]
    by_id = {m["id"]: m for m in messages}
    available_categories = sorted({issue["category"] for issue in issues
                                   if issue["message_id"] in by_id
                                   and (not subject or by_id[issue["message_id"]]["subject_bucket"] == subject)})
    by_question, by_issue = defaultdict(list), defaultdict(list)
    for issue in issues:
        by_question[issue["message_id"]].append(issue)
    for response in responses:
        by_issue[response["issue_id"]].append(response)
    events = []
    for question in messages:
        categories = by_question[question["id"]]
        if not categories or (subject and question["subject_bucket"] != subject):
            continue
        if category:
            primary = [i for i in categories if i["category"] == category]
        else:
            # Route/specification tags are secondary to a quotation request.
            primary = [i for i in categories if i["category"] == "询价报价"] or categories
        if not primary:
            continue
        linked = {}
        for issue in primary:
            for response in by_issue[issue["id"]]:
                key = response["response_message_id"]
                if key not in linked or response["is_solution"] > linked[key]["is_solution"]:
                    linked[key] = dict(response)
        evidence = []
        for key, response in linked.items():
            message = by_id.get(key)
            if message is None:
                continue
            clues = extract_business_clues(split_quoted_reply(message["text"])[1]).as_dict()
            evidence.append({"message_id": key, "source_message_id": message["message_id"],
                             "sent_at": message["sent_at"], "sender_name": message["sender_name"],
                             "sender_label": message["sender_label"],
                             "sender_corp_id": message.get("sender_corp_id"),
                             "sender_corp_name": message.get("sender_corp_name"),
                             "company_status": message.get("company_status", "unknown"),
                             "subject_bucket": message["subject_bucket"], "text": message["text"],
                             "source_reference": message["source_reference"],
                             "kind": response["response_kind"], "is_solution": bool(response["is_solution"]),
                             "latency_seconds": response["latency_seconds"], "basis": response["association_basis"],
                             "clues": clues})
        evidence.sort(key=lambda r: message_order_key(by_id[r['message_id']]))
        solutions = [r for r in evidence if r["is_solution"]]
        acks = [r for r in evidence if r["kind"] == "即时响应"]
        event_status = "solved" if solutions else "in_progress" if any(r["kind"] == "状态更新" for r in evidence) else "acknowledged" if acks else "unreplied"
        if status and event_status != status:
            continue
        clues = json.loads(primary[0]["clues_json"])
        saved = clues.pop("response_entities", {})
        for response in evidence:
            if response["message_id"] in saved:
                response["clues"] = saved[response["message_id"]]
        events.append({"round": len(events) + 1, "message_id": question["id"],
                       "source_message_id": question["message_id"], "sent_at": question["sent_at"],
                       "sender_name": question["sender_name"], "sender_label": question["sender_label"],
                       "sender_corp_id": question.get("sender_corp_id"),
                       "sender_corp_name": question.get("sender_corp_name"),
                       "company_status": question.get("company_status", "unknown"),
                       "subject_bucket": question["subject_bucket"],
                       "subject_basis": question["subject_basis"], "text": question["text"],
                       "source_reference": question["source_reference"], "parent_id": question["parent_id"],
                       "nesting_depth": question["nesting_depth"], "provenance": question["provenance"],
                       "clues": clues, "status": event_status,
                       "needs_review": bool(clues.get('needs_review')),
                       "category_statuses": {i["category"]: i["status"] for i in categories},
                       "first_response_seconds": evidence[0]["latency_seconds"] if evidence else None,
                       "ack_seconds": acks[0]["latency_seconds"] if acks else None,
                       "solution_seconds": solutions[0]["latency_seconds"] if solutions else None,
                       "final_solution_seconds": solutions[-1]["latency_seconds"] if solutions else None,
                       "first_ack_at": acks[0]["sent_at"] if acks else None,
                       "final_solution": solutions[-1] if solutions else None, "responses": evidence})
    counts = Counter(event["status"] for event in events)
    routes = {}
    def same_location(left, right):
        if not left or not right:
            return False
        aliases = {'首都':'北京首都','大兴':'北京大兴','浦东':'上海浦东','虹桥':'上海虹桥','天府':'成都天府','双流':'成都双流','白云':'广州白云'}
        left, right = left.removesuffix('机场'), right.removesuffix('机场')
        left, right = aliases.get(left,left), aliases.get(right,right)
        return left in right or right in left
    def route(key, event):
        value = routes.setdefault(key, {'rounds':set(), 'solved_rounds':set(), 'quotes':[]})
        value['rounds'].add(event['round'])
        return value
    for event in events:
        clue = event['clues']
        requested = list(dict.fromkeys((r.get('origin'),r.get('destination')) for r in clue.get('route_items') or []))
        if not requested:
            requested = [(clue.get('origin'), d) for d in clue.get('destination_items') or [clue.get('destination')]]
        for key in requested:
            route(key,event)
        covered = set()
        for response in [r for r in event['responses'] if r['is_solution']]:
            answer = response['clues']
            options = answer.get('quote_options') or [{'destination':None,'amount':answer.get('price_quote'),
                                                       'currency':answer.get('currency'),'delivery_time':answer.get('delivery_time')}]
            for option in options:
                label = option.get('destination')
                matched = [key for key in requested if same_location(label,key[1])] if label else []
                basis = 'quoted_destination_matches_request'
                if not matched and label and len(requested)>1:
                    matched = [key for key in requested if same_location(label,key[0])]
                    basis = 'quoted_origin_matches_request'
                if not matched and len(requested)==1 and not label:
                    matched, basis = requested, 'single_requested_route'
                if len(matched)!=1:
                    # Keep ambiguous quotes visible instead of assigning one
                    # bare price to every destination in a request.
                    matched, basis = [(clue.get('origin'),None)], 'route_not_uniquely_identified'
                key=matched[0]
                group=route(key,event)
                group['solved_rounds'].add(event['round'])
                group['quotes'].append({'round':event['round'],'amount':option.get('amount'),
                    'currency':option.get('currency'),'delivery_time':option.get('delivery_time'),
                    'quoted_location':label,'association_basis':basis,'message_id':response['message_id'],
                    'sent_at':response['sent_at']})
                if basis!='route_not_uniquely_identified':
                    covered.add(key)
        event['route_coverage']={'requested_routes':[{'origin':a,'destination':b} for a,b in requested],
                                 'routes_with_attributed_solution':[{'origin':a,'destination':b} for a,b in sorted(covered,key=str)],
                                 'routes_without_attributed_solution':[{'origin':a,'destination':b} for a,b in requested if (a,b) not in covered]}
    route_rows = [{'origin':a,'destination':b,'region':f'{region(a)}→{region(b)}',
                   'question_count':len(value['rounds']),'solved_count':len(value['solved_rounds']),
                   'quotes':value['quotes']} for (a,b),value in routes.items()]
    special = [e for e in events if e["clues"].get("special_goods")]
    rejected = [e for e in special if e["final_solution"] and
                re.search(r"做不了|接不了|走不了|无法承运|拒收|不能接|不接", split_quoted_reply(e["final_solution"]["text"])[1])]
    handled = sum(e["status"] == "solved" for e in special)
    restricted = [e for e in events if e['clues'].get('shipment_constraints')]
    restricted_solved = sum(e['status']=='solved' for e in restricted)
    timestamps = [m["sent_at"] for m in messages if m["sent_at"]]
    coverage = {"message_count": len(messages), "first_message_at": min(timestamps) if timestamps else None,
                "conversation_message_count": len(messages),
                "filtered_message_count": len({event["message_id"] for event in events}
                                               | {response["message_id"] for event in events
                                                  for response in event["responses"]}),
                "last_message_at": max(timestamps) if timestamps else None,
                "missing_timestamp_count": sum(m["sent_at"] is None for m in messages),
                "forward_count": sum(m["message_type"] == "合并转发记录" for m in messages),
                "unexpanded_forward_count": sum(m["parse_status"] in ("unexpanded_forward", "partial_forward") for m in messages),
                "unparsed_media_count": sum(m["parse_status"] == "unparsed_media" for m in messages),
                "nested_message_count": sum(m["nesting_depth"] > 0 for m in messages),
                "max_nesting_depth": max((m["nesting_depth"] for m in messages), default=0),
                "database_backed_count": sum(m["provenance"].get("origin") == "database" for m in messages),
                "unverified_source_count": sum(m["provenance"].get("origin") != "database" for m in messages),
                "excluded_unverified_source_count": len(excluded),
                "display_redacted_count": sum(m['display_redacted'] for m in messages),
                "available_forwards_expanded": all(m['parse_status']=='expanded_forward' for m in messages if m['message_type']=='合并转发记录'),
                "full_history_verified": False,
                "note": ("真实数据库与转发载荷已按本地快照核验；云端完整历史、未解析媒体内容仍未证明。已解决表示有报价或处置结论。" if verified_names
                         else "本地可用材料范围；原库与全部转发层完整性尚未证明。手工或规范化导入不能证明真实来源。")}
    return display_safe_value({"coverage": coverage, "metrics": {"question_count": len(events), "solved_count": counts["solved"],
            "ack_only_count": counts["acknowledged"], "unreplied_count": counts["unreplied"], "in_progress_count": counts["in_progress"],
            "ack_latency": latency_stats(e["ack_seconds"] for e in events),
            "first_response_latency": latency_stats(e["first_response_seconds"] for e in events),
            "solution_latency": latency_stats(e["solution_seconds"] for e in events),
            "final_solution_latency": latency_stats(e["final_solution_seconds"] for e in events)},
            "special_goods": {"question_count": len(special), "handled_count": handled, "rejected_count": len(rejected),
                              "handling_rate": handled / len(special) if special else None,
                              "rejection_rate": len(rejected) / len(special) if special else None},
            "shipment_constraints": {"question_count":len(restricted),'handled_count':restricted_solved,
                                     'handling_rate':restricted_solved/len(restricted) if restricted else None},
             "association_quality": {'heuristic_ack_count':sum(r['basis']=='nearest_open_question_within_5m' for e in events for r in e['responses']),
                                     'questions_flagged_for_review':sum(e['needs_review'] for e in events)},
             "category_counts": category_statistics(events), "available_categories": available_categories,
             "routes": route_rows, "events": events, "messages": messages})


def write_report(report, output_dir):
    """Persist the same complete evidence view served by the dashboard."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "analysis_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "messages.jsonl").write_text("\n".join(json.dumps(m, ensure_ascii=False) for m in report["messages"]) + "\n", encoding="utf-8")
    fields = ["round", "sent_at", "sender_name", "subject_bucket", "text", "origin", "destination", "pickup_address", "packages", "weight", "volume", "dimensions",
              "special_goods", "first_ack_at", "ack_seconds", "first_response_seconds", "solution_seconds", "final_solution_seconds",
              "final_solution_at", "solution_text", "status", "source_reference", "parent_id",
              "route_items", "package_items", "weight_items", "volume_items", "dimension_items", "document_requirements", "shipment_constraints", "route_coverage", "needs_review"]
    flat = []
    for event in report["events"]:
        row = {key: event.get(key) for key in fields}
        row.update({key: event["clues"].get(key) for key in ("origin", "destination", "pickup_address", "packages", "weight", "volume", "dimensions", "special_goods")})
        solution = event["final_solution"]
        row["final_solution_at"] = solution["sent_at"] if solution else None
        row["solution_text"] = split_quoted_reply(solution["text"])[1] if solution else None
        for key in ("route_items", "package_items", "weight_items", "volume_items", "dimension_items", "document_requirements", "shipment_constraints"):
            row[key] = json.dumps(event['clues'].get(key), ensure_ascii=False)
        row['route_coverage'] = json.dumps(event['route_coverage'], ensure_ascii=False)
        flat.append(row)
    with (output / "events.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in flat:
            safe = {k: ("'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v) for k, v in row.items()}
            writer.writerow(safe)
    coverage, metrics = report["coverage"], report["metrics"]
    verified = coverage['database_backed_count'] > 0 and coverage['unverified_source_count'] == 0
    lines = ["# 中技AI cosplay：" + ("真实本地数据库分析" if verified else "来源未核验材料分析"), "",
             coverage['note'], "",
             f"当前材料：{coverage['message_count']} 条；数据库来源 {coverage['database_backed_count']} 条；来源未核验 {coverage['unverified_source_count']} 条；未完整展开转发 {coverage['unexpanded_forward_count']} 条。",
             f"时间范围：{coverage['first_message_at']} 至 {coverage['last_message_at']}。", "",
             f"嵌套子消息 {coverage['nested_message_count']} 条；最大实际深度 {coverage['max_nesting_depth']}；未解析媒体 {coverage['unparsed_media_count']} 条。同名未核验材料另存，当前口径排除 {coverage['excluded_unverified_source_count']} 条。", "",
             f"按提问去重：{metrics['question_count']} 笔，已有方案 {metrics['solved_count']}，仅确认 {metrics['ack_only_count']}，未回复 {metrics['unreplied_count']}，跟进中 {metrics['in_progress_count']}。",
             "已有方案指报价或明确处置结论；拒单属于有处置结论，不代表承运成功。类别标签的状态另存 JSON，不重复计为问题。", "",
             "|轮次|提问时间|提问人／主体|路线|规格|首次确认／秒|方案／秒|最终回复|状态|",
             "|---|---|---|---|---|---|---|---|---|"]
    def cell(value):
        return str(value if value is not None else "未知").replace("|", "\\|").replace("\n", "<br>")
    for row in flat:
        route_items = json.loads(row['route_items']) or [{'origin':row['origin'],'destination':row['destination']}]
        specs = [item for key in ('package_items','weight_items','volume_items','dimension_items') for item in (json.loads(row[key]) or [])]
        lines.append("|" + "|".join(cell(v) for v in [row["round"], row["sent_at"], report["events"][int(row["round"]) - 1].get("sender_label") or f"{row['sender_name']}／{row['subject_bucket']}",
                      '；'.join(f"{item['origin'] or '未知'} → {item['destination'] or '未知'}" for item in route_items),
                      " / ".join(specs),
                      row["ack_seconds"], row["final_solution_seconds"], row["solution_text"], row["status"]]) + "|")
    lines.extend(["", "确认时延只统计明确的即时响应；直接给出方案的消息不补造确认时间。缺少业务 SLA，未标记‘超时’。", ""])
    for label, key in (("首次确认", "ack_latency"), ("首次有效回复", "first_response_latency"), ("首次方案", "solution_latency"), ("最终方案", "final_solution_latency")):
        stats = metrics[key]
        lines.append(f"- {label}：样本 {stats['sample_count']}，平均 {stats['mean_seconds']} 秒，中位数 {stats['median_seconds']} 秒。")
    lines.extend(["", f"特殊货物统计：`{json.dumps(report['special_goods'], ensure_ascii=False)}`", "",
                  f"特殊限制统计：`{json.dumps(report['shipment_constraints'], ensure_ascii=False)}`", "",
                  f"关联质量：`{json.dumps(report['association_quality'], ensure_ascii=False)}`。启发式关联须结合证据审阅，不作为人工核准。", "",
                  "金额与时效仅保留原文；原文未注明币种时为 null。全部原消息、引用、响应、关联依据和来源哈希见 analysis_report.json 与 messages.jsonl。",
                  "", "主群展示讨论："])
    question_ids = {e["message_id"] for e in report["events"]}
    for message in report["messages"]:
        if message["nesting_depth"] == 0 and message["id"] not in question_ids and re.search(r"html|视频|兜底|明早", message["text"], re.I):
            lines.append(f"- {message['sent_at']} {message['sender_name']}：{message['text']}")
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with (output / 'routes.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=['origin','destination','region','question_count','solved_count','quotes'])
        writer.writeheader()
        for route in report['routes']:
            writer.writerow({**route, 'quotes':json.dumps(route['quotes'],ensure_ascii=False)})
    return [str(output / name) for name in ("report.md", "events.csv", "routes.csv", "messages.jsonl", "analysis_report.json")]
