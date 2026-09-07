"""Build isolated, anonymized local chat demos without replacing the current demo."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

from import_management_chat import PROJECT, import_chat, restore_explicit_quotes
from chatlog_assistant.sources.pricing_demo import chat_candidates
from chatlog_assistant.sources.wecom_presentation import PresentationService, _canonical, _write


def build(database: Path, root: Path) -> list[dict]:
    original = json.loads((root / "source-chat.json").read_text(encoding="utf-8"))
    current_hash = original["source_conversation_sha256"]
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        groups = conn.execute(
            "SELECT conversation_id,substr(sent_at,1,10),count(*) FROM messages "
            "WHERE substr(sent_at,1,10) IN (?,date(?,'-1 day')) GROUP BY 1,2 ORDER BY 2,3 DESC",
            (original["message_date"], original["message_date"]),
        ).fetchall()
    samples = []
    aliases = {current_hash: "A"}
    for conversation, day, count in groups:
        digest = hashlib.sha256(conversation.encode()).hexdigest()
        if count < 40 or (digest == current_hash and day == original["message_date"]):
            continue
        key = "chat-" + hashlib.sha256((conversation + day).encode()).hexdigest()[:16]
        target = root / "samples" / key
        with tempfile.TemporaryDirectory(prefix="management-sample-") as tmp:
            staged = Path(tmp) / "source-chat.json"
            import_chat(database, staged, conversation, day)
            snapshot = PresentationService(staged.parent).snapshot()
            if len(snapshot["inquiries"]) < 10:
                continue
            if digest not in aliases:
                aliases[digest] = chr(ord("A") + len(aliases))
            alias = aliases[digest]
            target.mkdir(parents=True, exist_ok=True)
            if not (target / "source-chat.json").exists():
                shutil.copyfile(staged, target / "source-chat.json")
        # Each sample owns its PDF snapshot and price book; existing samples are immutable.
        for path in [root / "active-rate.json", *root.glob("rate-*.pdf")]:
            if path.is_file() and not (target / path.name).exists():
                shutil.copyfile(path, target / path.name)
        source_path = target / "source-chat.json"
        old = source_path.read_bytes()
        enriched = json.loads(old)
        restore_explicit_quotes(enriched)
        if _canonical(enriched) != old:
            _write(target / "source-history" / (hashlib.sha256(old).hexdigest() + ".json"), old)
            _write(source_path, _canonical(enriched))
        snapshot = PresentationService(target).snapshot()
        candidates = chat_candidates(snapshot)
        ambiguous = sum(not c["amount"] for c in candidates)
        readable = sum(bool(m.get("body")) for m in snapshot["source"]["messages"])
        focus = "多航司与价格条件核对" if candidates else "交叉询价与未关联报价"
        samples.append({
            "id": key, "title": f"会话 {alias} · {day} · {focus}",
            "description": f"{readable} 条可读消息 · {len(snapshot['inquiries'])} 笔询价 · "
                           f"{len(candidates)} 条报价候选，其中 {ambiguous} 条价格待明确；完整会话可查看交叉消息和补充说明。",
            "date": day, "readable": readable, "inquiries": len(snapshot["inquiries"]),
            "candidates": len(candidates), "ambiguous": ambiguous,
        })
    _write(root / "samples.json", _canonical({"samples": samples}))
    return samples


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=PROJECT / "data/management-demo-local-source/analysis.db")
    parser.add_argument("--root", type=Path, default=PROJECT / "data/management-demo/presentation")
    args = parser.parse_args()
    print(json.dumps(build(args.database, args.root), ensure_ascii=True))
