from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time

from .wecom_paths import (
    DEFAULT_WECOM_ROOT,
    DEFAULT_LOCAL_DATA_ROOT,
    DEFAULT_ANALYSIS_DB,
    DEFAULT_EXPORTS_DIR,
    account_keyring_path,
)
from .wecom_pipeline import (
    discover_wecom_accounts,
    import_decrypted_directory,
    run_single_capture,
    import_normalized_jsonl,
)
from .wecom_storage import WecomLocalStorage
from .wecom_exporter import export_issues_to_csv, export_issues_to_json
from .wecom_semantic import WecomSemanticAnalyzer
from .wecom_web import serve_wecom
from .wecom_pricing import PriceOperationError, build_price_workbook


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="企业微信本地物流聊天记录分析工具")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # 1. discover
    disc = sub.add_parser("discover", help="发现本机企微账号及数据库与 WAL 状态")
    disc.add_argument("--wecom-root", type=Path, default=DEFAULT_WECOM_ROOT)

    # 2. import-offline
    imp_off = sub.add_parser("import-offline", help="从已解密的数据库目录（如离线参考包）导入消息并执行物流分析")
    imp_off.add_argument("--db-dir", type=Path, required=True, help="已解密数据库所在目录")
    imp_off.add_argument("--account-id", default="offline_reference", help="指定账号标签 (默认 offline_reference)")
    imp_off.add_argument("--analysis-db", type=Path, default=DEFAULT_ANALYSIS_DB, help="分析结果数据库路径")
    imp_off.add_argument("--semantic", action="store_true", help="启用 MiniMax-M3 LLM 增强语义分析与方案评估")
    imp_off.add_argument("--full", action="store_true", help="从源库首条重放，不使用增量游标")
    imp_off.add_argument("--conversation-name", help="仅导入匹配的主群及其嵌套消息，自动从头重放")

    # 3. capture-once
    cap = sub.add_parser("capture-once", help="对指定账号执行单次一致性快照与解密导入")
    cap.add_argument("--account-id", required=True, help="目标账号 ID")
    cap.add_argument("--wecom-root", type=Path, default=DEFAULT_WECOM_ROOT)
    cap.add_argument("--analysis-db", type=Path, default=DEFAULT_ANALYSIS_DB)
    cap.add_argument("--keyring", type=Path, help="指定 DPAPI keyring 路径")
    cap.add_argument("--semantic", action="store_true", help="启用 MiniMax-M3 LLM 增强语义分析")
    cap.add_argument("--full", action="store_true", help="从源库首条重放，不使用增量游标")
    cap.add_argument("--conversation-name", help="匹配主群及其嵌套消息，自动从头重放")

    # 4. watch
    wt = sub.add_parser("watch", help="定时增量采集指定账号企微消息")
    wt.add_argument("--account-id", required=True, help="目标账号 ID")
    wt.add_argument("--interval", type=int, default=60, help="轮询间隔秒数 (默认 60s)")
    wt.add_argument("--wecom-root", type=Path, default=DEFAULT_WECOM_ROOT)
    wt.add_argument("--analysis-db", type=Path, default=DEFAULT_ANALYSIS_DB)
    wt.add_argument("--semantic", action="store_true", help="启用 MiniMax-M3 LLM 增强语义分析")

    # 5. rebuild
    reb = sub.add_parser("rebuild", help="对已有消息重新运行分析分类与回复评估")
    reb.add_argument("--analysis-db", type=Path, default=DEFAULT_ANALYSIS_DB)
    reb.add_argument("--semantic", action="store_true", help="启用 MiniMax-M3 LLM 增强语义分析")
    reb.add_argument("--account-id")

    normalized = sub.add_parser("import-jsonl", help="导入规范化消息或展开后的合并转发记录")
    normalized.add_argument("path", type=Path)
    normalized.add_argument("--account-id", required=True)
    normalized.add_argument("--analysis-db", type=Path, default=DEFAULT_ANALYSIS_DB)
    normalized.add_argument("--semantic", action="store_true")

    # 6. query
    qry = sub.add_parser("query", help="查询物流问题、回复方案及证据")
    qry.add_argument("--analysis-db", type=Path, default=DEFAULT_ANALYSIS_DB)
    qry.add_argument("--subject", choices=["zhongji", "other", "unknown"], help="按发送者主体筛选")
    qry.add_argument("--category", help="按物流问题类别筛选")
    qry.add_argument("--status", choices=["unreplied", "acknowledged", "solved", "in_progress"], help="按状态筛选")
    qry.add_argument("--account-id", help="按账号 ID 筛选")
    qry.add_argument("--limit", type=int, default=50)

    # 6. summary
    sum_p = sub.add_parser("summary", help="统计物流问题、类别分布及状态概览")
    sum_p.add_argument("--analysis-db", type=Path, default=DEFAULT_ANALYSIS_DB)
    sum_p.add_argument("--subject", choices=["zhongji", "other", "unknown"])
    sum_p.add_argument("--account-id")

    report_p = sub.add_parser("report", help="导出按提问去重的完整时序、指标与全部消息证据")
    report_p.add_argument("--analysis-db", type=Path, default=DEFAULT_ANALYSIS_DB)
    report_p.add_argument("--account-id")
    report_p.add_argument("--output", type=Path, default=DEFAULT_EXPORTS_DIR / "report")

    # 7. export
    exp = sub.add_parser("export", help="导出物流问题与回复证据链到 CSV / JSON")
    exp.add_argument("--analysis-db", type=Path, default=DEFAULT_ANALYSIS_DB)
    exp.add_argument("--output", type=Path, help="输出文件路径 (默认输出到 data/wecom-local/exports)")
    exp.add_argument("--format", choices=["csv", "json"], default="csv")
    exp.add_argument("--anonymize", action="store_true", help="脱敏敏感信息（手机号、身份证、姓名）")
    exp.add_argument("--subject", choices=["zhongji", "other", "unknown"])
    exp.add_argument("--category")
    exp.add_argument("--status")
    exp.add_argument("--account-id")

    for command in (reb, normalized, qry, sum_p, exp, report_p):
        command.add_argument("--conversation-name", help="模糊匹配主群名称并包含嵌套消息，例如：中技AI cosplay")
    for command in (reb, qry, sum_p, exp, report_p):
        command.add_argument("--conversation-id", help="匹配主会话 ID 及其嵌套消息；同名群可配合账号区分")

    # 8. serve
    srv = sub.add_parser("serve", help="启动本地 Web 交互查询界面 (仅监听 127.0.0.1)")
    srv.add_argument("--analysis-db", type=Path, default=DEFAULT_ANALYSIS_DB)
    srv.add_argument("--source-analysis-db", type=Path, action="append", default=[],
                     help="空运流程 B 可只读发现的规范化本机来源库；可重复指定，不接受网页传入路径")
    srv.set_defaults(include_demo_fixtures=True)
    srv.add_argument("--include-demo-fixtures", dest="include_demo_fixtures", action="store_true",
                     help="显示明确标注的合成演示数据（默认开启；不会标记为真实本地群聊）")
    srv.add_argument("--no-demo-fixtures", dest="include_demo_fixtures", action="store_false",
                     help="隐藏合成演示数据，仅显示配置的本机规范化来源")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8766)

    price = sub.add_parser("prices", help="读取最新价格或待审核价格候选")
    price.add_argument("--analysis-db", type=Path, default=DEFAULT_ANALYSIS_DB)
    price.add_argument("--view", choices=["current", "pending", "all"], default="current")
    price.add_argument("--account-id")
    price.add_argument("--source-database")
    price.add_argument("--conversation-id")
    price.add_argument("--conversation-name")
    price.add_argument("--company")
    price.add_argument("--route")
    price.add_argument("--keyword")
    price.add_argument("--limit", type=int, default=500)

    price_export = sub.add_parser("prices-export", help="导出当前筛选范围的全部价格到 .xlsx")
    price_export.add_argument("--analysis-db", type=Path, default=DEFAULT_ANALYSIS_DB)
    price_export.add_argument("--output", type=Path, required=True)
    price_export.add_argument("--view", choices=["current", "pending", "all"], default="current")
    price_export.add_argument("--account-id")
    price_export.add_argument("--source-database")
    price_export.add_argument("--conversation-id")
    price_export.add_argument("--conversation-name")
    price_export.add_argument("--company")
    price_export.add_argument("--route")
    price_export.add_argument("--keyword")

    template = sub.add_parser("prices-template", help="下载价格维护 .xlsx 填写模板")
    template.add_argument("--output", type=Path, required=True)

    price_config = sub.add_parser("prices-config", help="配置价格责任人及独立价格审核模型开关")
    price_config.add_argument("--analysis-db", type=Path, default=DEFAULT_ANALYSIS_DB)
    price_config.add_argument("--reviewer-id", help="本地配置的价格审核负责人 ID")
    price_config.add_argument("--reviewer-name", help="本地配置的价格审核负责人显示名")
    price_config.add_argument("--role", help="责任岗位名称")
    price_config.add_argument("--price-llm", choices=["on", "off"], help="显式开启或关闭价格审核 LLM；默认不变")
    price_config.add_argument("--model", help="价格审核模型标识（仅配置，不自动外发）")
    price_config.add_argument("--model-version", help="价格审核模型版本标识")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    scope = {key: getattr(args, key, None) for key in ("account_id", "conversation_id", "conversation_name")}

    if args.subcommand == "report":
        from .wecom_report import write_report
        storage = WecomLocalStorage(args.analysis_db)
        storage.initialize()
        report = storage.get_report(**scope)
        print(json.dumps({"files": write_report(report, args.output), "coverage": report["coverage"]}, ensure_ascii=False, indent=2))
        return 0

    if args.subcommand == "import-jsonl":
        result = import_normalized_jsonl(args.path, account_id=args.account_id,
            analysis_db_path=args.analysis_db, conversation_name=args.conversation_name, use_semantic=args.semantic)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.subcommand == "discover":
        accounts = discover_wecom_accounts(args.wecom_root)
        print(json.dumps(accounts, ensure_ascii=False, indent=2))
        return 0

    if args.subcommand == "import-offline":
        res = import_decrypted_directory(
            db_dir=args.db_dir,
            account_id=args.account_id,
            analysis_db_path=args.analysis_db,
            use_semantic=getattr(args, "semantic", False),
            full_replay=args.full,
            conversation_name=args.conversation_name,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if args.subcommand == "capture-once":
        res = run_single_capture(
            account_id=args.account_id,
            wecom_root=args.wecom_root,
            analysis_db_path=args.analysis_db,
            keyring_path=args.keyring,
            use_semantic=getattr(args, "semantic", False),
            full_replay=args.full,
            conversation_name=args.conversation_name,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("success") else 1

    if args.subcommand == "watch":
        print(f"Starting increment watch for account {args.account_id} every {args.interval}s (semantic={args.semantic})...")
        while True:
            try:
                res = run_single_capture(
                    account_id=args.account_id,
                    wecom_root=args.wecom_root,
                    analysis_db_path=args.analysis_db,
                    use_semantic=getattr(args, "semantic", False),
                )
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{ts}] Poll result: {res.get('new_messages', 0)} new messages, success={res.get('success')}", flush=True)
            except Exception as exc:
                print(f"Poll error: {exc}", file=sys.stderr, flush=True)
            time.sleep(max(5, args.interval))

    if args.subcommand == "rebuild":
        storage = WecomLocalStorage(args.analysis_db)
        storage.initialize()
        analyzer = WecomSemanticAnalyzer() if args.semantic else None
        res = storage.rebuild_analysis(semantic_analyzer=analyzer, **scope)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if args.subcommand == "summary":
        storage = WecomLocalStorage(args.analysis_db)
        storage.initialize()
        res = storage.get_summary(subject=args.subject, **scope)
        sys.stdout.buffer.write(json.dumps(res, ensure_ascii=False, indent=2).encode("utf-8"))
        sys.stdout.buffer.write(b"\n")
        return 0

    if args.subcommand == "query":
        storage = WecomLocalStorage(args.analysis_db)
        storage.initialize()
        items = storage.list_issues(
            **scope,
            subject=args.subject,
            category=args.category,
            status=args.status,
            limit=args.limit,
        )
        sys.stdout.buffer.write(json.dumps({"total": len(items), "items": items}, ensure_ascii=False, indent=2).encode("utf-8"))
        sys.stdout.buffer.write(b"\n")
        return 0

    if args.subcommand == "export":
        storage = WecomLocalStorage(args.analysis_db)
        storage.initialize()
        items = storage.list_issues(
            **scope,
            subject=args.subject,
            category=args.category,
            status=args.status,
            limit=10000,
        )
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if args.output:
            out_p = args.output
        else:
            DEFAULT_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
            ext = "json" if args.format == "json" else "csv"
            out_p = DEFAULT_EXPORTS_DIR / f"wecom_logistics_{ts}.{ext}"

        if args.format == "json":
            export_issues_to_json(items, out_p, anonymize=args.anonymize)
        else:
            export_issues_to_csv(items, out_p, anonymize=args.anonymize)
        print(f"Exported {len(items)} issues to {out_p}")
        return 0

    if args.subcommand == "serve":
        storage = WecomLocalStorage(args.analysis_db)
        source_dbs = args.source_analysis_db or [args.analysis_db]
        serve_wecom(storage, host=args.host, port=args.port, source_analysis_dbs=source_dbs,
                    include_demo_fixtures=args.include_demo_fixtures)
        return 0

    if args.subcommand in {"prices", "prices-export"}:
        storage = WecomLocalStorage(args.analysis_db)
        storage.initialize()
        service = storage.price_maintenance()
        kwargs = {
            "account_id": args.account_id, "source_database": args.source_database,
            "conversation_id": args.conversation_id, "conversation_name": args.conversation_name,
            "company": args.company, "route": args.route, "keyword": args.keyword,
            "view": args.view,
        }
        if args.subcommand == "prices":
            result = service.list_prices(**kwargs, limit=args.limit)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        items = []
        cursor = None
        while True:
            page = service.list_prices(**kwargs, cursor=cursor, limit=500)
            items.extend(page["items"])
            cursor = page.get("next_cursor")
            if not cursor:
                break
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(build_price_workbook(items, view=args.view, scope=kwargs))
        print(json.dumps({"output": str(args.output), "view": args.view, "items": len(items)}, ensure_ascii=False, indent=2))
        return 0

    if args.subcommand == "prices-template":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(build_price_workbook(template=True))
        print(json.dumps({"output": str(args.output), "template_version": "1.0"}, ensure_ascii=False, indent=2))
        return 0

    if args.subcommand == "prices-config":
        storage = WecomLocalStorage(args.analysis_db)
        storage.initialize()
        service = storage.price_maintenance()
        current = service.get_settings()
        if any(value is not None for value in (args.reviewer_id, args.reviewer_name, args.role)):
            reviewer_id = args.reviewer_id if args.reviewer_id is not None else current.get("reviewer_id")
            reviewer_name = args.reviewer_name if args.reviewer_name is not None else current.get("reviewer_name")
            if not reviewer_id or not reviewer_name:
                raise PriceOperationError("reviewer_required", "配置价格审核负责人必须同时提供 reviewer-id 和 reviewer-name")
            service.configure_responsibility(reviewer_id, reviewer_name, role=args.role or current["responsibility_role"])
        if args.price_llm is not None or args.model is not None or args.model_version is not None:
            service.configure_llm(args.price_llm == "on" if args.price_llm is not None else current["price_review_llm_enabled"],
                                  model=args.model if args.model is not None else current.get("model"),
                                  model_version=args.model_version if args.model_version is not None else current.get("model_version"))
        print(json.dumps(service.get_settings(), ensure_ascii=False, indent=2))
        return 0

    return 0
