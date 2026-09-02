"""Conversation analysis; SQLite and remote transport stay at the boundary."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json
import re
from typing import Any

from .wecom_classifier import (
    assess_logistics_response,
    classify_logistics_issue, extract_business_clues, RULES, LogisticsIssueClassification, LogisticsResponseAssessment,
)


def split_quoted_reply(text: str) -> tuple[str, str]:
    """Keep a quoted question out of the reply's intent and extracted entities."""
    match = re.match(r'^\s*[“"](.+?)[”"]\s*(?:\n\s*-+\s*)?\n(.+)$', text, re.S)
    if match:
        return (match.group(1).strip(), match.group(2).strip())
    match = re.match(r'^\s*\[引用\s*(.+?)\]\s*(.+)$', text, re.S)
    if match:
        return (match.group(1).strip(), match.group(2).strip())
    match = re.match(r'^\s*\*(.+?)\*\s*(.+)$', text, re.S)
    if match:
        return (match.group(1).strip(), match.group(2).strip())
    match = re.match(r'^\s*([^\n:：]{1,60}[:：].+?)\n(?:-+\s*\n)?(@[^\n]+(?:\n.*)?)$', text, re.S)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return ("", text.strip())



def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def _sender_names(row: dict) -> set[str]:
    name = row.get("sender_name") or ""
    return {name.strip(), re.split(r"\s*@", name, maxsplit=1)[0].strip(), str(row.get("sender_id") or "")}


def message_order_key(row):
    provenance = row.get('provenance') or json.loads(row.get('provenance_json') or '{}')
    position = tuple(int(value) for value in re.findall(r'\[(\d+)\]',provenance.get('native_payload_path','')))
    return (row.get('sent_at') or '',position,row.get('source_reference') or row['id'])


def analyze_conversation(messages: list[dict[str, Any]], analyzer: Any = None) -> dict[str, dict[str, Any]]:
    messages = sorted(messages, key=message_order_key)
    analyses = {}
    for row in messages:
        if row["parse_status"] != "parsed" or row["message_type"] not in {"文本", "text", "quote", "引用回复"}:
            continue
        quote, body = split_quoted_reply(row["text"])
        provenance = row.get("provenance") or json.loads(row.get("provenance_json") or "{}")
        quote = provenance.get("quoted_text") or quote
        body = provenance.get("unquoted_text", body)
        assessment = assess_logistics_response(body)
        classes = [] if assessment.kind == "即时响应" else classify_logistics_issue(body)
        preliminary = extract_business_clues(body).as_dict()
        if (classes and re.search(r"html|视频|前端|放映|PPT|系统|\bUI\b|登录|账号|设计|模型|市场同事|报价员|价格维护|汇报", body, re.I)
                and not any(preliminary.get(k) for k in ('packages','weight','volume','waybill_no','order_no'))):
            classes = []
        if re.fullmatch(r'https?://\S+', body) or row.get('sender_name') == '运小星':
            classes = []
        if re.search(r'主题[:：]|副标题[:：]|这个环境|AI.*方向|系统.*维护', body):
            classes = []
        risk_terms = [term for term in ("投诉", "索赔", "赔偿", "丢失", "破损") if term in body]
        urgency = 4 if risk_terms else 3 if any(term in body for term in ("加急", "尽快", "催促", "马上处理")) else 1
        analyses[row["id"]] = {
            "quote": quote, "body": body, "classes": classes, "summary": body[:140],
            "assessment": assessment, "clues": preliminary,
            "analysis_source": "rules", "urgency_level": urgency,
            "risk_evaluation": {"has_complaint_risk": bool(risk_terms), "risk_reason": "、".join(risk_terms) or None},
            "semantic_reply_to": None, "responses": [], "needs_review": False,
            "mentioned_sender_ids": provenance.get("mentioned_sender_ids", []),
        }

    if analyzer and analyzer.enabled:
        targets = [key for key, value in analyses.items()
                   if (any(c.category == "其他待分类问题" for c in value["classes"])
                       or (not value["classes"] and value["assessment"].kind == "一般回复" and len(value["body"]) > 3))]
        updates = analyzer.analyze_context(messages, targets) if targets else {}
        for key, update in updates.items():
            current = analyses[key]
            current.update({k: v for k, v in update.items() if k != "clues"})
            current["clues"].update({k: v for k, v in update.get("clues", {}).items() if v is not None})
            current["analysis_source"] = "rules+llm"

    by_id = {row['id']: row for row in messages}
    reference_rows = {}
    for row in messages:
        for key in (row['id'], row.get('message_id'), row.get('server_id')):
            if key:
                reference_rows.setdefault(key, []).append(row)
    def resolve(reference):
        candidates = {r['id']: r for r in reference_rows.get(reference, [])}
        return next(iter(candidates.values())) if len(candidates) == 1 else None
    def seconds(later, earlier):
        return (datetime.fromisoformat(later['sent_at']) - datetime.fromisoformat(earlier['sent_at'])).total_seconds()

    # Link requested detail replies before promoting terse specifications to
    # inquiries. A dimension supplied to a clarification is not a new order.
    detail_of = {}
    clarifications = []
    for index, row in enumerate(messages):
        current = analyses.get(row['id'])
        if current is None:
            continue
        previous = messages[index-1] if index else None
        prior = analyses.get(previous['id']) if previous else None
        if (prior and previous['sender_id']==row['sender_id'] and 0<=seconds(row,previous)<=10
                and prior['clues'].get('route_items')
                and not any(prior['clues'].get(k) for k in ('packages','weight','volume','dimensions'))
                and sum(bool(current['clues'].get(k)) for k in ('packages','weight','volume','dimensions'))>=2
                and not current['clues'].get('location_mentions')
                and not re.search(r'[?？]|报价|这个呢',current['body'])):
            detail_of[row['id']]=previous['id']
            current['classes']=[]
            current['clues']['detail_of_message_id']=previous['id']
            prior['needs_review']=True
            continue
        target = resolve(row.get('reply_to_message_id'))
        if target and target['id'] in detail_of:
            target=by_id[detail_of[target['id']]]
        if (target and target['id'] in analyses and target['sender_id'] != row['sender_id']
                and re.search(r'有尺寸吗|可以叠吗|多少重|吨吗|浙江哪|什么地址|请提供.*尺寸',current['body'])):
            current['classes'] = []
            current['assessment'] = LogisticsResponseAssessment('状态更新',False,'in_progress',None,0.96)
            clarifications.append((row,target))
            continue
        for clarification, original in reversed(clarifications):
            if original['sender_id'] != row['sender_id'] or not 0 <= seconds(row,clarification) <= 300:
                continue
            ask = analyses[clarification['id']]['body']
            clue = current['clues']
            supplied = ((('尺寸' in ask) and clue.get('dimensions') and not clue.get('route_items'))
                        or (re.search(r'浙江哪|什么地址',ask) and len(current['body'])<40 and clue.get('location_mentions'))
                        or ('叠' in ask and re.fullmatch(r'可以|不行|不能|不可以|不可堆叠',current['body'])))
            if supplied and not re.search(r'[?？]|报价|这个呢', current['body']):
                detail_of[row['id']] = original['id']
                current['classes'] = []
                current['clues']['detail_of_message_id'] = original['id']
                break
    for row in messages:
        current = analyses.get(row['id'])
        original = resolve(row.get('reply_to_message_id'))
        if original:
            original = by_id.get(detail_of.get(original['id'],original['id']))
        target = analyses.get(original['id']) if original else None
        if not current or not target or not original:
            continue
        # A structured native reply with a price can establish the intent of a
        # terse shipment specification; no route or price is copied into it.
        if (current['assessment'].kind == '报价方案' and current['clues'].get('price_quote')
                and original['sender_id'] != row['sender_id']
                and target['assessment'].kind == '一般回复'
                and (not target['classes'] or all(c.category == '其他待分类问题' for c in target['classes'])
                     or any(target['clues'].get(k) for k in ('packages','weight','volume','dimensions','route_items')))
                and not any(c.category == '询价报价' for c in target['classes'])):
            target['classes'].append(LogisticsIssueClassification('询价报价', 0.97, ('原生引用报价对应的原始诉求',)))

    questions = []
    for row in messages:
        current = analyses.get(row["id"])
        if current is None:
            continue
        if current["classes"]:
            if re.search(r'这个呢|时效|几天|几号到|哪天到|价格有变|再看看|交你还是',current['body']):
                ancestor = resolve(row.get('reply_to_message_id'))
                visited = set()
                while ancestor and ancestor['id'] not in visited:
                    visited.add(ancestor['id'])
                    if ancestor['sender_id'] == row['sender_id'] and analyses.get(ancestor['id'],{}).get('classes'):
                        break
                    ancestor = resolve(ancestor.get('reply_to_message_id'))
                prior = [q for q in questions if q["sender_id"] == row["sender_id"] and
                         0 <= seconds(row,q) <= (4*3600 if '这个呢' in current['body'] else 300)]
                if ancestor or prior:
                    chosen_prior = ancestor or prior[-1]
                    current["clues"]["followup_to_message_id"] = chosen_prior["id"]
                    current["clues"]["followup_basis"] = "structured_quote_thread" if ancestor else "same_sender_explicit_followup_within_window"
            questions.append(row)
            continue
        if row['id'] in detail_of:
            original = by_id[detail_of[row['id']]]
            analyses[original['id']]['responses'].append({'row':row,
                'assessment':LogisticsResponseAssessment('补充信息',False,'none',None,0.95),
                'basis':'requested_detail_reply','latency':int(seconds(row,original)), 'clues':current['clues']})
            continue
        assessment = current["assessment"]
        affirmative_target = None
        if assessment.kind == "一般回复":
            previous = messages[messages.index(row)-1] if messages.index(row)>0 else None
            if (current['body']=='是的' and previous and previous['id'] in analyses and analyses[previous['id']]['classes']
                    and analyses[previous['id']]['clues'].get('delivery_time') and re.search(r'[?？]',analyses[previous['id']]['body'])
                    and previous['sender_id'] != row['sender_id'] and 0<=seconds(row,previous)<=60):
                affirmative_target=previous
                assessment=LogisticsResponseAssessment('处理方案',True,'solved',current['body'],0.96)
                current['clues']['confirmed_delivery_time']=analyses[previous['id']]['clues']['delivery_time']
            else:
                continue
        eligible = []
        now = datetime.fromisoformat(row["sent_at"])
        for question in questions:
            latency = int((now - datetime.fromisoformat(question["sent_at"])).total_seconds())
            strong_reference = row.get("reply_to_message_id") or current["quote"]
            if latency >= 0 and (latency <= 4 * 3600 or strong_reference) and question["sender_id"] != row["sender_id"]:
                eligible.append(question)
        ref = row.get("reply_to_message_id")
        original_reference = resolve(ref)
        redirected = bool(original_reference and original_reference['id'] in detail_of)
        if redirected:
            ref = detail_of[original_reference['id']]
        quoted = current["quote"]
        chosen = []
        basis = ""
        if affirmative_target:
            chosen=[affirmative_target]
            basis='adjacent_explicit_delivery_confirmation'
        elif ref:
            chosen = [q for q in eligible if ref in (q["id"], q["message_id"], q["server_id"])]
            basis = "quoted_detail_reply_to_original_request" if redirected else "explicit_reply_reference"
        elif quoted:
            chosen = [
                q
                for q in eligible
                if len(_compact(analyses[q["id"]]["body"])) >= 5
                and (
                    _compact(analyses[q["id"]]["body"]) in _compact(quoted)
                    or _compact(quoted) in _compact(analyses[q["id"]]["body"])
                )
            ]
            author = re.match(r"^([^\n:：]{1,80})[:：]", quoted)
            if author and any(author.group(1).strip() in _sender_names(q) for q in eligible):
                chosen = [q for q in chosen if author.group(1).strip() in _sender_names(q)]
            basis = "quoted_text_match"
        else:
            chosen = [q for q in eligible if analyses[q["id"]]["clues"].get("waybill_no")
                      and analyses[q["id"]]["clues"]["waybill_no"] in current["body"]]
            basis = "waybill_clue_match"
            if not chosen and current["semantic_reply_to"]:
                chosen = [q for q in eligible if q["id"] == current["semantic_reply_to"]]
                basis = "semantic_context"
            if not chosen:
                chosen = [q for q in eligible if not any(r["assessment"].is_solution for r in analyses[q["id"]]["responses"])]
                basis = "single_open_issue"
                mentions = set(re.findall(r"@([^\s@，,。:：!！]+)", current["body"])) | set(current['mentioned_sender_ids'])
                if mentions:
                    chosen = [q for q in chosen if mentions & _sender_names(q)]
                    basis = "exact_mention"
                if assessment.kind == "即时响应":
                    chosen = [q for q in chosen if (now - datetime.fromisoformat(q["sent_at"])).total_seconds() <= 300]
                    chosen = [q for q in chosen if not (q.get('sender_corp_id') and row.get('sender_corp_id')
                              and q['sender_corp_id'] == row['sender_corp_id'])]
                    if len(chosen) > 1 and not mentions:
                        latest = max(q['sent_at'] for q in chosen)
                        chosen = [q for q in chosen if q['sent_at'] == latest]
                        basis = 'nearest_open_question_within_5m'
                        for q in chosen:
                            analyses[q['id']]['needs_review'] = True
        if len(chosen) != 1:
            for q in chosen:
                analyses[q["id"]]["needs_review"] = True
            continue
        question = chosen[0]
        response = {
            "row": row, "assessment": assessment, "basis": basis,
            "latency": int((now - datetime.fromisoformat(question["sent_at"])).total_seconds()),
            "clues": current["clues"],
        }
        if all(response_for_category(response, c.category).kind == "一般回复" for c in analyses[question["id"]]["classes"]):
            continue
        analyses[question["id"]]["responses"].append(response)
        # A price with a delivery commitment can answer a later time-only
        # branch while quoting the original shipment. Preserve both questions.
        if assessment.is_solution and current['clues'].get('delivery_time'):
            for followup in eligible:
                follow = analyses[followup['id']]
                if (followup['id']==question['id'] or follow['clues'].get('followup_to_message_id')!=question['id']
                        or not re.search(r'时效|几天|几号|哪天',follow['body'])
                        or any(follow['clues'].get(k) for k in ('packages','weight','volume','dimensions'))):
                    continue
                locations=follow['clues'].get('location_mentions') or []
                if locations and not any(option.get('delivery_time') and any(location in (option.get('destination') or '') for location in locations)
                                         for option in current['clues'].get('quote_options') or []):
                    continue
                follow['responses'].append({**response,'latency':int(seconds(row,followup)),
                                           'basis':'quoted_request_answers_delivery_followup'})
    return analyses


def response_for_category(response: dict[str, Any], category: str):
    assessment = response["assessment"]
    if assessment.kind == "处理方案" and response["basis"] == "single_open_issue":
        text = response["row"]["text"]
        rule = next((rule for rule in RULES if rule.category == category), None)
        if rule and not any(word in text for word in rule.keywords):
            return replace(assessment, kind="一般回复", is_solution=False, status_contribution="none", solution_text=None)
    if assessment.kind == "报价方案" and category not in {"询价报价", "费用对账"}:
        if category == "时效与轨迹" and response["clues"].get("delivery_time"):
            return assessment
        return replace(assessment, is_solution=False, status_contribution="none")
    return assessment
