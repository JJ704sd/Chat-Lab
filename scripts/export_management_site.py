"""Export only curated demo material into a private, browser-local Sites checkout."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from chatlog_assistant.sources.pricing_demo import PricingDemo, chat_candidates, pdf_candidates
from chatlog_assistant.sources.presentation_samples import sample_catalog, sample_root
from chatlog_assistant.sources.wecom_presentation import _canonical, _write


def clean(value):
    if isinstance(value, list):
        return [clean(item) for item in value]
    if not isinstance(value, dict):
        return value
    private = {'source_message_id', 'reply_to_message_id', 'source_record_sha256',
               'message_id_basis', 'source_conversation_sha256', 'verification', 'imported_at'}
    result = {k: clean(v) for k, v in value.items() if k not in private}
    if value.get('reply_body') is not None:
        result['body'] = value['reply_body']
    return result


def export(root: Path, destination: Path) -> dict:
    public = destination / 'public'
    (public / 'assets').mkdir(parents=True, exist_ok=True)
    static = PROJECT / 'src/chatlog_assistant/static'
    for name in ('management.js', 'management.css', 'management-prefill.js', 'pdf-reader.js', 'management-hosted.js', 'sinotech-logo.png'):
        shutil.copyfile(static / name, public / 'assets' / name)
    html = (static / 'management.html').read_text(encoding='utf-8')
    html = html.replace('<script src="/assets/management-prefill.js"', '<script src="/assets/management-hosted.js" defer></script>\n<script src="/assets/management-prefill.js"')
    html = html.replace('<title>中技 · 智能运价管理</title>', '<title>中技 · 私有运价演示</title><meta name="description" content="精选聊天与预置 PDF 价表的私有交互演示，审核结果保存在当前浏览器。">')
    _write(public / 'index.html', html.encode('utf-8'))
    bundle = {'samples': sample_catalog(root), 'snapshots': {}, 'pages': {}}
    for entry in bundle['samples']:
        pricing = PricingDemo(sample_root(root, entry['id']))
        data = pricing.snapshot()
        materials = data['materials']
        if not materials.get('curation', {}).get('valid'):
            raise ValueError('Export requires a current curated source for every visible sample')
        candidates = pdf_candidates(materials['rate_card']) + [c for c in chat_candidates(materials)
                      if c['id'] in materials['curation']['candidate_ids']]
        data.update(run_id=None, candidates=[{**c, 'status':'pending', 'stale':False,
                    'revision':materials['revision'], 'version':0} for c in candidates], prices=[], decisions=[])
        bundle['snapshots'][entry['id']] = clean(data)
        sha = materials['rate_card']['source_sha256']
        if sha not in bundle['pages']:
            first = pricing.sources.pdf_page(sha, 1)
            bundle['pages'][sha] = list(range(1, first['page_count'] + 1))
            for page in bundle['pages'][sha]:
                _write(public / 'pages' / f'{sha}-{page}.json', _canonical(first if page == 1 else pricing.sources.pdf_page(sha, page)))
    _write(public / 'demo.json', _canonical(bundle))
    _write(destination / 'package.json', _canonical({'name':'zj-management-private-demo','private':True,
        'scripts':{'build':'node build.cjs'}}))
    _write(destination / 'build.cjs', b"const fs=require('node:fs');fs.cpSync('public','dist',{recursive:true});\n")
    _write(destination / '.gitignore', b'dist/\nnode_modules/\n*.tar.gz\n')
    hosting = destination / '.openai/hosting.json'
    config = json.loads(hosting.read_text(encoding='utf-8-sig')) if hosting.is_file() else {}
    config['static'] = {'directory':'dist'}
    _write(hosting, _canonical(config))
    return {'samples':len(bundle['samples']), 'chat_candidates':sum(
        c['lane']=='chat' for s in bundle['snapshots'].values() for c in s['candidates']),
        'pdf_pages':sum(map(len,bundle['pages'].values()))}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=PROJECT / 'data/management-demo/presentation')
    parser.add_argument('--destination', type=Path, default=PROJECT / 'outputs/sites-management')
    args = parser.parse_args()
    print(json.dumps(export(args.root, args.destination)))
