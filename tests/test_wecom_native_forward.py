"""Synthetic fixtures for the native layout observed in the verified local DB."""
import sqlite3
import unittest
from contextlib import closing
import tempfile
from pathlib import Path

from chatlog_assistant.sources.wecom_parser import WecomLocalParser


def varint(value):
    data = bytearray()
    while value > 127:
        data.append((value & 127) | 128)
        value >>= 7
    return bytes(data + bytes([value]))


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode('utf-8')
    return varint((number << 3) | 2) + varint(len(value)) + value


def text_payload(text):
    return field(1, field(1, 0) + field(2, field(1, text)))


def node(sender, stamp, name, company, text, quote=None):
    result = (field(1, sender) + field(2, stamp) + field(4, 0) + field(5, 2)
              + field(10, 901) + field(11, name) + field(13, 123) + field(14, company)
              + field(101, text_payload(text)))
    if quote is not None:
        result += field(17, field(1002, field(1, quote)))
    return result


def envelope(*nodes):
    return b''.join(field(1, item) for item in nodes) + field(2, '群聊') + field(5, 2)


class NativeForwardTests(unittest.TestCase):
    def test_native_quote_omits_leading_mention_without_losing_identity(self):
        original=node(222,1788138143,'测试服务','其他物流','@测试客户\n尺寸有吗')
        compact=node(222,1788138143,'测试服务','其他物流','尺寸有吗')
        reply=node(111,1788138181,'测试客户','中技物流','135*135*76',quote=compact)
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.execute('CREATE TABLE message_table(message_id INTEGER,sender_id INTEGER,conversation_id TEXT,content_type INTEGER,send_time INTEGER,content BLOB)')
            conn.execute("INSERT INTO message_table VALUES(7,333,'g',4,1788182081,?)",(envelope(original,reply),))
            records,_,_=WecomLocalParser().parse_databases('fixture',conn)
        q=next(r for r in records if r.sender_id=='222')
        r=next(r for r in records if r.sender_id=='111')
        self.assertEqual(r.reply_to_message_id,q.id)

    def test_adjacent_route_and_spec_are_one_request_with_separate_evidence(self):
        items=[{'message_id':'q','sender_id':'c','sender_display':'测试客户 @中技','sent_at':'2026-08-31T09:00:00+08:00','content':'惠州提货送广州'},
               {'message_id':'spec','sender_id':'c','sender_display':'测试客户 @中技','sent_at':'2026-08-31T09:00:03+08:00','content':'8PLT/2200KG/9CBM'},
               {'message_id':'r','sender_id':'s','sender_display':'测试服务 @其他','sent_at':'2026-08-31T09:00:30+08:00','content':'广州580当天到','reply_to_message_id':'q'}]
        r=self.analyze_fixture(items)
        self.assertEqual(r['metrics']['question_count'],1)
        self.assertEqual(r['metrics']['solved_count'],1)
        self.assertIsNone(r['events'][0]['clues']['weight'])
        self.assertEqual(r['events'][0]['responses'][0]['clues']['weight'],'2200KG')

    def test_native_nesting_and_truncated_tail_are_not_silently_complete(self):
        from chatlog_assistant.sources.wecom_protobuf import decode_forwarded_content
        payload = envelope(node(111, 1788138143, '测试客户', '中技物流', '询价'))
        for _ in range(12):
            card = (field(1, 222) + field(2, 1788138181) + field(4, 4)
                    + field(11, '测试转发者') + field(101, payload))
            payload = envelope(card)
        result = decode_forwarded_content(payload)
        children = result.messages
        for _ in range(12):
            children = children[0]['forwarded_messages']
        self.assertEqual(children[0]['content'], '询价')
        self.assertFalse(result.gaps)
        broken = decode_forwarded_content(payload + b'\x0a\x80')
        self.assertTrue(broken.gaps)
        self.assertEqual(len(broken.messages), 1)

    def test_a_bare_quote_does_not_cover_every_requested_route(self):
        items=[{'message_id':'q','sender_id':'c','sender_display':'测试客户 @中技','sent_at':'2026-08-31T09:00:00+08:00','content':'1PLT/200KG/1CBM 广州送上海、北京'},
               {'message_id':'r','sender_id':'s','sender_display':'测试服务 @其他','sent_at':'2026-08-31T09:00:30+08:00','content':'620','reply_to_message_id':'q'}]
        r=self.analyze_fixture(items)
        self.assertEqual(r['metrics']['solved_count'],1)
        self.assertGreater(len(r['events'][0]['route_coverage']['requested_routes']),1)
        self.assertEqual(r['events'][0]['route_coverage']['routes_with_attributed_solution'],[])
        self.assertEqual(sum(len(route['quotes']) for route in r['routes']),1)

    def analyze_fixture(self, items):
        from chatlog_assistant.sources.wecom_parser import records_from_message_tree
        from chatlog_assistant.sources.wecom_storage import WecomLocalStorage
        records, _ = records_from_message_tree(items, account_id='fixture', source_database='fixture',
            conversation_id='fixture', conversation_name='合成测试', source_reference='fixture')
        with tempfile.TemporaryDirectory() as folder:
            storage = WecomLocalStorage(Path(folder) / 'test.db')
            storage.initialize()
            storage.upsert_messages(records)
            storage.rebuild_analysis()
            return storage.get_report()

    def test_requested_dimensions_are_evidence_for_original_inquiry(self):
        def msg(key, sender, seconds, body, reply=None):
            return {'message_id':key, 'sender_id':sender, 'sender_display': '测试客户 @中技' if sender=='customer' else '测试服务 @其他',
                    'sent_at':f'2026-08-31T09:00:{seconds:02d}+08:00', 'content':body, 'reply_to_message_id':reply}
        r = self.analyze_fixture([msg('q','customer',0,'8plts/2294kg/9.84cbm'),
                                  msg('clarify','service',10,'有尺寸吗','q'),
                                  msg('detail','customer',20,'113 cm X 99 cm X 110cm'),
                                  msg('quote','service',50,'广州580当天到深圳550当天到','detail')])
        self.assertEqual(r['metrics']['question_count'],1)
        self.assertEqual(r['metrics']['solved_count'],1)
        self.assertEqual(r['events'][0]['solution_seconds'],50)
        self.assertIsNone(r['events'][0]['clues']['dimensions'])
        self.assertEqual(len(r['messages']),4)

    def test_delivery_followup_is_a_separate_resolved_branch(self):
        items=[{'message_id':'q','sender_id':'c','sender_display':'测试客户 @中技','sent_at':'2026-08-31T09:00:00+08:00','content':'900KG/1PLT/0.8CBM 广州送深圳'},
               {'message_id':'followup','sender_id':'c','sender_display':'测试客户 @中技','sent_at':'2026-08-31T09:00:10+08:00','content':'看看什么时效'},
               {'message_id':'quote','sender_id':'s','sender_display':'测试服务 @其他','sent_at':'2026-08-31T09:00:30+08:00','content':'深圳750当天到','reply_to_message_id':'q'}]
        r=self.analyze_fixture(items)
        self.assertEqual(r['metrics']['question_count'],2)
        self.assertEqual(r['metrics']['solved_count'],2)
        self.assertEqual(r['events'][1]['solution_seconds'],20)

    def test_quote_of_a_followup_matches_its_body_without_old_quote_or_mention(self):
        followup = node(111, 1788138143, '测试客户', '中技物流', '“测试服务：\n上海620”\n------\n@测试服务\n时效')
        followup += field(21, text_payload('时效'))
        compact_quote = node(111, 1788138143, '测试客户', '中技物流', '时效')
        reply = node(222, 1788138181, '测试服务', '其他物流', '“测试客户：\n时效”\n------\n@测试客户\n隔日达', quote=compact_quote)
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.execute('CREATE TABLE message_table(message_id INTEGER,sender_id INTEGER,conversation_id TEXT,content_type INTEGER,send_time INTEGER,content BLOB)')
            conn.execute("INSERT INTO message_table VALUES(7,333,'g',4,1788182081,?)", (envelope(followup, reply),))
            records, _, _ = WecomLocalParser().parse_databases('fixture', conn)
        q = next(r for r in records if r.sender_id == '111')
        r = next(r for r in records if r.sender_id == '222')
        self.assertEqual(r.reply_to_message_id, q.id)
        self.assertIn('上海620', q.text)
        self.assertEqual(q.provenance['unquoted_text'], '时效')

    def test_multi_destination_quotes_and_shipment_requests(self):
        from chatlog_assistant.sources.wecom_classifier import extract_business_clues, classify_logistics_issue, assess_logistics_response
        body = '@测试客户\n上海480当天到大兴2900西安3400'
        self.assertTrue(assess_logistics_response(body).is_solution)
        options = extract_business_clues(body).as_dict()['quote_options']
        self.assertEqual([(q['destination'], q['amount']) for q in options],
                         [('上海', '480'), ('大兴', '2900'), ('西安', '3400')])
        self.assertEqual(options[0]['delivery_time'], '当天到')
        self.assertIsNone(options[1]['delivery_time'])
        self.assertFalse(classify_logistics_issue(body))
        self.assertIn('询价报价', [c.category for c in classify_logistics_issue('52CTNS/900KGS/6.73CBM 江苏省南京市江北新区 送上海浦东')])
        self.assertEqual(extract_business_clues('145*103*135CM‑6').dimension_items, ['145*103*135CM‑6'])
        self.assertFalse(assess_logistics_response('上海 500kg/3cbm').is_solution)
        self.assertIsNone(extract_business_clues('今天提货，几号到呢').destination)
        from chatlog_assistant.sources.wecom_report import region
        self.assertEqual(region('宁波'),'华东')
        self.assertEqual(region('大兴机场'),'华北')

    def test_native_type4_preserves_children_and_exact_quote_identity(self):
        question = node(111, 1788138143, '测试客户', '中技物流', '交上海送南京，请报价')
        reply = node(222, 1788138181, '测试服务', '其他物流',
                     '“测试客户：\n交上海送南京，请报价”\n------\n@测试客户 南京620隔日达', quote=question)
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.execute('CREATE TABLE message_table(message_id INTEGER,sender_id INTEGER,conversation_id TEXT,content_type INTEGER,send_time INTEGER,content BLOB)')
            conn.execute("INSERT INTO message_table VALUES(7,333,'g',4,1788182081,?)", (envelope(question, reply),))
            records, _, _ = WecomLocalParser().parse_databases('fixture', conn)
        self.assertEqual(len(records), 3)
        parent = next(r for r in records if r.message_id == '7')
        q = next(r for r in records if r.sender_id == '111')
        r = next(r for r in records if r.sender_id == '222')
        self.assertEqual(parent.parse_status, 'expanded_forward')
        self.assertEqual(q.parent_id, parent.id)
        self.assertEqual(q.subject_bucket, 'zhongji')
        self.assertEqual(r.subject_bucket, 'other')
        self.assertEqual(r.reply_to_message_id, q.id)
        self.assertEqual(q.sent_at.isoformat(), '2026-08-31T09:02:23+08:00')
        self.assertIsNone(q.provenance['original_message_id'])
        self.assertEqual(q.provenance['message_id_basis'], 'native_payload_position')
        self.assertTrue(q.provenance['native_payload_path'].endswith('.1[0]'))


if __name__ == '__main__':
    unittest.main()
