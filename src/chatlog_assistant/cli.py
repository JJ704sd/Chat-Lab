from __future__ import annotations

import argparse
import json
from pathlib import Path
import threading

from .pipeline import import_archive_inbox, import_archive_jsonl, import_jsonl
from .semantic import SemanticConfigError
from .sources.discovery import discover_sources
from .sources.live import (
    default_keyring,
    import_live_sources,
    import_live_workspaces,
    probe_and_save_wechat,
    probe_and_save_wecom,
    public_probe,
)
from .sources.monitor import observe_once, watch
from .sources.wechat_nt import probe_wechat
from .sources.wecom import probe_wecom
from .storage import Storage
from .web import serve
from .workspaces import (
    DEFAULT_PORTS,
    PLATFORM_LABELS,
    PLATFORMS,
    archive_inbox_dir,
    database_path,
    migrate_legacy_wechat_keyring,
    userid_display_map_path,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地物流聊天记录分析助手")
    parser.add_argument("--database", type=Path, help="覆盖默认工作库路径")
    parser.add_argument("--source", choices=PLATFORMS, help="个微/企微工作区（数据与密钥分开放置）")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="初始化本地工作库")

    import_parser = subparsers.add_parser("import-jsonl", help="导入规范化 JSONL")
    import_parser.add_argument("path", type=Path)
    import_parser.add_argument("--semantic", action="store_true", help="导入后用 MiniMax-M3 做语义分类")

    archive_parser = subparsers.add_parser("import-archive", help="导入官方会话存档 JSONL（原始解密对象、统一消息或规范化行）")
    archive_parser.add_argument("path", type=Path, nargs="?", help="JSONL 文件；与 --inbox 二选一")
    archive_parser.add_argument("--inbox", action="store_true", help="导入 data/wecom/inbox 中采集器写出的 JSONL")
    archive_parser.add_argument("--interval", type=int, default=0, help=">0 时按秒循环扫描 inbox")
    archive_parser.add_argument("--display-map", type=Path, help="userid → 显示名 JSON，用于 @中技 主体识别")
    archive_parser.add_argument("--semantic", action="store_true", help="导入后用 MiniMax-M3 做语义分类")

    discover_parser = subparsers.add_parser("discover", help="发现本机企微/个微消息库")
    discover_parser.add_argument("--documents", type=Path)

    watch_parser = subparsers.add_parser("watch", help="监控加密消息库的变化并增量导入")
    watch_parser.add_argument("--documents", type=Path)
    watch_parser.add_argument("--interval", type=int, default=600)
    watch_parser.add_argument("--once", action="store_true")

    probe_parser = subparsers.add_parser("probe-wechat", help="授权后定向探测个微加密库")
    probe_parser.add_argument("--documents", type=Path)
    probe_parser.add_argument("--pid", type=int)
    probe_parser.add_argument("--save", action="store_true", help="将通过双重验证的密钥写入 DPAPI 存储")
    probe_parser.add_argument("--capture", action="store_true", help="只读扫描失败时用调试器硬件断点捕获登录密钥")
    probe_parser.add_argument("--restart", action="store_true", help="结束并重启微信，在扫码登录时捕获")
    probe_parser.add_argument("--timeout", type=int, default=180, help="登录捕获等待秒数")

    wecom_parser = subparsers.add_parser("probe-wecom", help="授权后按账号探测企微加密库")
    wecom_parser.add_argument("--documents", type=Path)
    wecom_parser.add_argument("--pid", type=int)
    wecom_parser.add_argument("--save", action="store_true", help="将通过双重验证的密钥写入 DPAPI 存储")
    wecom_parser.add_argument("--capture", action="store_true", help="只读扫描失败时用调试器硬件断点按账号捕获")
    wecom_parser.add_argument("--restart", action="store_true", help="结束并重启企微，在登录时捕获")
    wecom_parser.add_argument("--timeout", type=int, default=180, help="登录捕获等待秒数")

    live_parser = subparsers.add_parser("import-live", help="使用 DPAPI 密钥增量导入本机消息")
    live_parser.add_argument("--documents", type=Path)
    live_parser.add_argument("--semantic", action="store_true", help="导入后用 MiniMax-M3 做语义分类")

    analyze_parser = subparsers.add_parser("analyze", help="重建问题分类（关键词或 MiniMax-M3 语义识别）")
    analyze_parser.add_argument("--semantic", action="store_true", help="调用国内 MiniMax-M3 对模糊类别做语义识别")

    serve_parser = subparsers.add_parser("serve", help="启动本地查询页面")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int)

    wecom_local_parser = subparsers.add_parser("wecom-local", help="企业微信本地分析闭环子命令")
    wecom_local_parser.add_argument("args", nargs=argparse.REMAINDER, help="wecom-local 子命令参数")
    return parser


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _bind_workspace(args: argparse.Namespace, implied: str | None = None) -> tuple[Storage, object, str]:
    source = args.source or implied
    if args.database is not None:
        database = args.database
    elif source:
        database = database_path(source)
    else:
        raise ValueError("需要 --source wechat|wecom 或 --database")
    if source == "wechat" or (implied == "wechat" and args.database is None):
        migrate_legacy_wechat_keyring()
    storage = Storage(database)
    return storage, default_keyring(database), source or "custom"


def _serve_workspace(args: argparse.Namespace, source: str, port: int) -> None:
    storage, _keyring, _name = _bind_workspace(args, source)
    serve(
        storage,
        args.host,
        port,
        workspace=source,
        label=PLATFORM_LABELS.get(source, source),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "init":
        targets = (args.source,) if args.source else PLATFORMS
        if args.database is not None:
            Storage(args.database).initialize()
            print(f"initialized\t{args.database}")
            return 0
        for source in targets:
            path = database_path(source)
            Storage(path).initialize()
            if source == "wechat":
                migrate_legacy_wechat_keyring()
            print(f"initialized\t{source}\t{path}")
        return 0
    if args.command == "import-jsonl":
        storage, _keyring, source = _bind_workspace(args, args.source or "wechat")
        try:
            count = import_jsonl(storage, args.path, semantic=True if args.semantic else None)
        except SemanticConfigError as exc:
            print(str(exc))
            return 2
        print(f"imported\t{count}\t{storage.path}")
        return 0
    if args.command == "import-archive":
        storage, _keyring, source = _bind_workspace(args, args.source or "wecom")
        display_map = args.display_map or userid_display_map_path("wecom")
        semantic = True if args.semantic else None
        try:
            if args.inbox:
                inbox = args.path if args.path and args.path.is_dir() else archive_inbox_dir("wecom")
                payload = import_archive_inbox(
                    storage,
                    inbox,
                    display_map=display_map,
                    semantic=semantic,
                )
                if args.interval and args.interval > 0:
                    import time

                    print(json.dumps(payload, ensure_ascii=False))
                    while True:
                        time.sleep(max(5, args.interval))
                        payload = import_archive_inbox(
                            storage,
                            inbox,
                            display_map=display_map,
                            semantic=semantic,
                        )
                        print(json.dumps(payload, ensure_ascii=False), flush=True)
                    return 0
                print(json.dumps(payload, ensure_ascii=False))
                return 0
            if args.path is None:
                print("需要 JSONL 路径，或使用 --inbox")
                return 2
            count = import_archive_jsonl(
                storage,
                args.path,
                display_map=display_map,
                semantic=semantic,
            )
        except SemanticConfigError as exc:
            print(str(exc))
            return 2
        print(f"imported\t{count}\t{storage.path}")
        return 0
    if args.command == "discover":
        _print_json([item.as_dict() for item in discover_sources(args.documents)])
        return 0
    if args.command == "watch":
        all_sources = discover_sources(args.documents)
        if args.database is not None:
            storage, keyring, _source = _bind_workspace(args, args.source)
            selected = [item for item in all_sources if args.source is None or item.platform == args.source]
            if args.once:
                events = observe_once(storage, selected)
                imported = import_live_sources(storage, keyring, args.documents, selected)
                _print_json({
                    "changed": [
                        {"source_key": event.source_key, "path": event.path.name, "size": event.size}
                        for event in events
                    ],
                    "import": imported,
                })
                return 0
            watch(storage, selected, args.interval, keyring=keyring, documents=args.documents)
            return 0
        if args.once:
            _print_json(import_live_workspaces(args.documents, all_sources, platform=args.source))
            return 0
        if args.source:
            storage, keyring, _source = _bind_workspace(args, args.source)
            selected = [item for item in all_sources if item.platform == args.source]
            watch(storage, selected, args.interval, keyring=keyring, documents=args.documents)
            return 0
        print("持续 watch 请指定 --source wechat 或 --source wecom")
        return 2
    if args.command == "probe-wechat":
        storage, keyring, _source = _bind_workspace(args, "wechat")
        storage.initialize()
        result = (
            probe_and_save_wechat(
                keyring,
                args.documents,
                args.pid,
                capture=args.capture,
                restart=args.restart,
                timeout=args.timeout,
            )
            if args.save or args.capture or args.restart
            else public_probe(probe_wechat(args.documents, args.pid))
        )
        result["workspace"] = str(storage.path)
        _print_json(result)
        return 0 if result.get("success") else 1
    if args.command == "probe-wecom":
        storage, keyring, _source = _bind_workspace(args, "wecom")
        storage.initialize()
        result = (
            probe_and_save_wecom(
                keyring,
                args.documents,
                args.pid,
                capture=args.capture,
                restart=args.restart,
                timeout=args.timeout,
            )
            if args.save or args.capture or args.restart
            else public_probe(probe_wecom(args.documents, args.pid))
        )
        result["workspace"] = str(storage.path)
        _print_json(result)
        return 0 if result.get("success") else 1
    if args.command == "import-live":
        semantic = True if args.semantic else None
        try:
            if args.database is not None:
                storage, keyring, _source = _bind_workspace(args, args.source)
                selected = discover_sources(args.documents)
                if args.source:
                    selected = [item for item in selected if item.platform == args.source]
                _print_json(
                    import_live_sources(
                        storage,
                        keyring,
                        args.documents,
                        selected,
                        platform=args.source,
                        semantic=semantic,
                    )
                )
                return 0
            _print_json(import_live_workspaces(args.documents, platform=args.source, semantic=semantic))
            return 0
        except SemanticConfigError as exc:
            print(str(exc))
            return 2
    if args.command == "analyze":
        semantic = True if args.semantic else None
        try:
            if args.database is not None:
                storage, _keyring, _source = _bind_workspace(args, args.source)
                storage.initialize()
                _print_json(storage.rebuild_analysis(semantic=semantic))
                return 0
            targets = (args.source,) if args.source else PLATFORMS
            payload = {}
            for source in targets:
                if source == "wechat":
                    migrate_legacy_wechat_keyring()
                storage = Storage(database_path(source))
                storage.initialize()
                payload[source] = storage.rebuild_analysis(semantic=semantic)
            _print_json(payload)
            return 0
        except SemanticConfigError as exc:
            print(str(exc))
            return 2
    if args.command == "serve":
        if args.source or args.database is not None:
            source = args.source or "wechat"
            port = args.port or DEFAULT_PORTS.get(source, 8765)
            _serve_workspace(args, source, port)
            return 0
        threads = []
        for source in PLATFORMS:
            port = DEFAULT_PORTS[source]
            thread = threading.Thread(
                target=_serve_workspace,
                args=(args, source, port),
                name=f"serve-{source}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)
        try:
            threads[0].join()
        except KeyboardInterrupt:
            return 0
        return 0
    if args.command == "wecom-local":
        from .sources.wecom_cli import main as wecom_cli_main
        return wecom_cli_main(args.args)
    return 2
