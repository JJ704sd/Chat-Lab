from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import json
from pathlib import Path
import re
import secrets
import sys
import tempfile
import uuid
from urllib.parse import parse_qs, urlparse, unquote

from .wecom_storage import WecomLocalStorage
from .wecom_report import display_safe_value
from .wecom_exporter import export_issues_to_csv, export_issues_to_json, mask_sensitive_text
from .wecom_pricing import (
    PriceMaintenance,
    PriceOperationError,
    build_price_workbook,
)
from .wecom_airfreight import AirfreightOperationError, AirfreightService


class WecomDashboardHandler(BaseHTTPRequestHandler):
    storage: WecomLocalStorage
    dashboard_bytes: bytes = b""
    legacy_dashboard_bytes: bytes = b""
    csrf_token: str = secrets.token_urlsafe(24)
    airfreight_source_paths: tuple[Path, ...] = ()
    # This handler serves the local demonstration page.  Its synthetic source
    # is explicitly labeled in every API/UI view and remains independent from
    # configured real local sources.
    airfreight_include_demo_fixtures: bool = True

    protocol_version = "HTTP/1.1"

    def address_string(self) -> str:
        # Crucial for Windows: avoid blocking socket.getfqdn DNS reverse lookup
        return self.client_address[0]

    def handle(self) -> None:  # noqa: A003
        # A browser or urllib client may close a keep-alive connection while
        # the stdlib handler is waiting for its next request.  Treat that as
        # a normal local-client disconnect instead of printing a traceback.
        try:
            super().handle()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        # Suppress noisy standard logging for higher performance
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path == "/legacy/pickup":
            self._send_bytes(self.legacy_dashboard_bytes, "text/html; charset=utf-8")
            return

        if parsed.path in ("/", "/wecom", "/index.html"):
            self._send_bytes(self.dashboard_bytes, "text/html; charset=utf-8")
            return

        if parsed.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if parsed.path == "/api/wecom/csrf":
            self._send_json({"csrf_token": self.csrf_token})
            return

        query = parse_qs(parsed.query)

        air_path = self._airfreight_path(parsed.path)
        if air_path is not None:
            try:
                self._handle_airfreight_get(air_path, query)
            except AirfreightOperationError as exc:
                self._send_error(exc.http_status, exc.error_code, exc.message, details=exc.details)
            except (ValueError, TypeError):
                self._send_error(HTTPStatus.BAD_REQUEST, "invalid_airfreight_request", "空运查询参数无效")
            return

        account_id = query.get("account_id", [None])[0] or None
        source_database = query.get("source_database", [None])[0] or None
        subject = query.get("subject", [None])[0] or None
        category = query.get("category", [None])[0] or None
        status = query.get("status", [None])[0] or None
        conversation_scope = {key: query.get(key, [None])[0] or None
                              for key in ("conversation_id", "conversation_name")}

        if parsed.path == "/api/wecom/config":
            self._send_json(self.storage.price_maintenance().get_settings())
            return

        if parsed.path == "/api/wecom/prices":
            try:
                prices = self.storage.price_maintenance().list_prices(
                    account_id=account_id,
                    source_database=source_database,
                    conversation_id=conversation_scope["conversation_id"],
                    conversation_name=conversation_scope["conversation_name"],
                    company=query.get("company", [None])[0] or None,
                    route=query.get("route", [None])[0] or None,
                    keyword=query.get("keyword", [None])[0] or None,
                    status=query.get("status", [None])[0] or None,
                    view=query.get("view", ["current"])[0] or "current",
                    cursor=query.get("cursor", [None])[0] or None,
                    limit=int(query.get("limit", [100])[0]),
                )
            except (ValueError, TypeError) as exc:
                self._send_error(HTTPStatus.BAD_REQUEST, "invalid_limit", "limit 必须是整数")
                return
            except PriceOperationError as exc:
                self._send_error(exc.http_status, exc.error_code, exc.message, field_errors=exc.field_errors, details=exc.details)
                return
            self._send_json(prices)
            return

        if parsed.path == "/api/wecom/prices-export":
            try:
                service = self.storage.price_maintenance()
                view = query.get("view", ["current"])[0] or "current"
                template = query.get("template", ["0"])[0].lower() in ("1", "true", "yes")
                if template:
                    items = []
                else:
                    items = []
                    cursor = None
                    while True:
                        page = service.list_prices(
                            account_id=account_id,
                            source_database=source_database,
                            conversation_id=conversation_scope["conversation_id"],
                            conversation_name=conversation_scope["conversation_name"],
                            company=query.get("company", [None])[0] or None,
                            route=query.get("route", [None])[0] or None,
                            keyword=query.get("keyword", [None])[0] or None,
                            status=query.get("status", [None])[0] or None,
                            view=view, cursor=cursor, limit=500,
                        )
                        items.extend(page["items"])
                        cursor = page.get("next_cursor")
                        if not cursor:
                            break
                data = build_price_workbook(items, template=template, view=view,
                                           scope={"account_id": account_id,
                                                 "source_database": query.get("source_database", [None])[0] or None,
                                                 "conversation_id": conversation_scope["conversation_id"],
                                                 "conversation_name": conversation_scope["conversation_name"]})
                filename = "wecom_prices_template.xlsx" if template else "wecom_prices.xlsx"
                self._send_attachment(data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename)
            except PriceOperationError as exc:
                self._send_error(exc.http_status, exc.error_code, exc.message, field_errors=exc.field_errors, details=exc.details)
            except (ValueError, TypeError):
                self._send_error(HTTPStatus.BAD_REQUEST, "invalid_price_export", "价格导出筛选参数无效")
            return

        if parsed.path in ("/api/wecom/price-detail", "/api/wecom/price-export"):
            try:
                item = self.storage.price_maintenance().get_price_item(
                    record_id=query.get("record_id", [None])[0], candidate_id=query.get("candidate_id", [None])[0],
                    account_id=account_id, source_database=source_database, **conversation_scope)
                if item is None:
                    self._send_error(HTTPStatus.NOT_FOUND, "not_found", "当前范围内找不到所选报价")
                elif parsed.path.endswith("price-export"):
                    from .wecom_pricing import build_single_price_workbook
                    self._send_attachment(build_single_price_workbook(item),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "pickup_fleet_quote.xlsx")
                else:
                    self._send_json(item)
            except PriceOperationError as exc:
                self._send_error(exc.http_status, exc.error_code, exc.message, field_errors=exc.field_errors)
            return

        if parsed.path == "/api/wecom/messages":
            try:
                result = self.storage.list_messages(
                    account_id=account_id,
                    source_database=source_database,
                    conversation_id=conversation_scope["conversation_id"],
                    conversation_name=conversation_scope["conversation_name"],
                    author_id=query.get("author_id", [None])[0] or None,
                    company=query.get("company", [None])[0] or None,
                    keyword=query.get("keyword", [None])[0] or None,
                    message_type=query.get("message_type", [None])[0] or None,
                    start_at=query.get("start_at", [None])[0] or None,
                    end_at=query.get("end_at", [None])[0] or None,
                    cursor=query.get("cursor", [None])[0] or None,
                    limit=int(query.get("limit", [100])[0]),
                    include_text=query.get("include_text", ["0"])[0].lower() in ("1", "true", "yes"),
                )
            except (ValueError, TypeError):
                self._send_error(HTTPStatus.BAD_REQUEST, "invalid_limit", "limit 或游标无效")
                return
            self._send_json(result)
            return

        if parsed.path.startswith("/api/wecom/messages/") and parsed.path.endswith(("/evidence", "/forwarded")):
            from urllib.parse import unquote
            suffix = "/forwarded" if parsed.path.endswith("/forwarded") else "/evidence"
            message_id = unquote(parsed.path[len("/api/wecom/messages/"):-len(suffix)].strip("/"))
            try:
                scope = dict(account_id=account_id, source_database=source_database, **conversation_scope)
                evidence = (self.storage.get_forwarded_messages(message_id, **scope) if suffix == "/forwarded"
                            else self.storage.get_message_evidence(message_id, context_limit=int(query.get("context_limit", [20])[0]), **scope))
            except (ValueError, TypeError):
                self._send_error(HTTPStatus.BAD_REQUEST, "invalid_context_limit", "上下文数量必须是整数")
                return
            if evidence is None:
                self._send_error(HTTPStatus.NOT_FOUND, "not_found", "当前账号／会话范围内找不到该消息")
            else:
                self._send_json(evidence)
            return

        if parsed.path == "/api/wecom/companies":
            self._send_json(self.storage.list_companies(
                account_id=account_id,
                source_database=query.get("source_database", [None])[0] or None,
                conversation_id=conversation_scope["conversation_id"],
            ))
            return

        if parsed.path in ("/api/wecom/report", "/api/wecom/report-export"):
            if parsed.path.endswith("report-export"):
                # Exports intentionally keep the complete, backwards-compatible
                # evidence payload even when the page uses the index view.
                report = self.storage.get_report(**conversation_scope, account_id=account_id,
                                                 source_database=source_database,
                                                 subject=subject, category=category, status=status)
                if query.get("format", ["json"])[0] == "csv":
                    from .wecom_report import write_report
                    with tempfile.TemporaryDirectory() as directory:
                        write_report(report, directory)
                        data = (Path(directory) / "events.csv").read_bytes()
                    self._send_attachment(data, "text/csv; charset=utf-8-sig", "wecom_events.csv")
                else:
                    self._send_attachment(json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"),
                                          "application/json; charset=utf-8", "wecom_report.json")
            else:
                view = query.get("view", [None])[0] or None
                include_messages = query.get("include_messages", [None])[0]
                if include_messages is not None and include_messages.lower() in ("0", "false", "no"):
                    view = "index"
                report = self.storage.get_report(**conversation_scope, account_id=account_id,
                                                 source_database=source_database,
                                                 subject=subject, category=category, status=status,
                                                 view=view)
                self._send_json(report)
            return

        if parsed.path == "/api/wecom/report-detail":
            message_id = query.get("message_id", [None])[0] or None
            if not message_id:
                self.send_error(HTTPStatus.BAD_REQUEST, "缺少 message_id")
                return
            detail = self.storage.get_report_detail(
                message_id=message_id,
                **conversation_scope,
                account_id=account_id,
                source_database=source_database,
                subject=subject,
                category=category,
                status=status,
            )
            if detail is None:
                self.send_error(HTTPStatus.NOT_FOUND, "当前筛选下找不到该业务轮次")
                return
            self._send_json(detail)
            return

        if parsed.path == "/api/wecom/summary":
            summary = self.storage.get_summary(
                **conversation_scope,
                account_id=account_id,
                source_database=source_database,
                subject=subject,
                category=category,
                status=status,
            )
            self._send_json(summary)
            return

        if parsed.path == "/api/wecom/conversations":
            convs = self.storage.list_conversations(account_id=account_id)
            self._send_json({"items": convs, "total": len(convs)})
            return

        if parsed.path == "/api/wecom/issues":
            items = self.storage.list_issues(
                **conversation_scope,
                account_id=account_id,
                source_database=source_database,
                subject=subject,
                category=category,
                status=status,
                limit=500,
            )
            self._send_json({"items": items, "total": len(items)})
            return

        if parsed.path == "/api/wecom/export":
            fmt = query.get("format", ["csv"])[0].lower()
            anonymize = query.get("anonymize", ["0"])[0] in ("1", "true")
            items = self.storage.list_issues(
                **conversation_scope,
                account_id=account_id,
                source_database=source_database,
                subject=subject,
                category=category,
                status=status,
                limit=5000,
            )
            if fmt == "json":
                with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
                    tmp_p = tf.name
                export_issues_to_json(items, tmp_p, anonymize=anonymize)
                data = Path(tmp_p).read_bytes()
                try:
                    Path(tmp_p).unlink()
                except Exception:
                    pass
                self._send_attachment(data, "application/json; charset=utf-8", "wecom_logistics_issues.json")
                return
            else:
                with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tf:
                    tmp_p = tf.name
                export_issues_to_csv(items, tmp_p, anonymize=anonymize)
                data = Path(tmp_p).read_bytes()
                try:
                    Path(tmp_p).unlink()
                except Exception:
                    pass
                self._send_attachment(data, "text/csv; charset=utf-8-sig", "wecom_logistics_issues.csv")
                return

        if parsed.path == "/api/health":
            self._send_json({"status": "ok", "service": "wecom-local"})
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    @staticmethod
    def _airfreight_path(path: str) -> str | None:
        """Accept the new first-class route and a namespaced compatibility alias."""
        if path.startswith("/api/airfreight/"):
            return path
        if path.startswith("/api/wecom/airfreight/"):
            return "/api/airfreight/" + path[len("/api/wecom/airfreight/"):]
        return None

    def _handle_airfreight_get(self, path: str, query: dict[str, list[str]]) -> None:
        # Storage initialization happens once at server startup.  GET handlers
        # must remain read-only: discovering sources, metadata, previews and
        # evidence cannot create demo rows or trigger analysis.
        service = AirfreightService(
            self.storage, initialize=False, source_db_paths=self.airfreight_source_paths or (self.storage.path,),
            include_demo_fixtures=self.airfreight_include_demo_fixtures,
        )
        artifact_match = re.fullmatch(r"/api/airfreight/artifacts/([^/]+)/(preview|download)", path)
        if artifact_match:
            artifact = service.get_artifact_bytes(unquote(artifact_match.group(1)))
            if artifact is None:
                raise AirfreightOperationError("artifact_unavailable", "附件原件不可得或不存在", http_status=404)
            data, mime, name = artifact
            if artifact_match.group(2) == "download":
                self._send_attachment(data, mime, name)
            else:
                self._send_bytes(data, mime)
            return
        if path == "/api/airfreight/meta":
            self._send_json({
                "business_line": "airfreight", "title": "企微信息分析助手", "subtitle": "空运业务信息分析",
                "demo_data": True, "default_search": "中技AI cosplay", "flows": {"A": "每日索价与发布最新价卡", "B": "本地群聊导入与多选项报价"},
                "steps": {"A": ["开始每日索价演示", "模拟供应商回复", "接收多文件回复", "解析文件", "查看字段证据", "跨文件查重", "人工审核", "发布最新价卡", "查看发布结果"], "B": ["选择本地群聊数据", "选择账号、目标群和时间", "预览完整性", "确认导入并分析", "查看聊天证据", "解析询价与多票截图", "处理待确认项", "计算计费重", "匹配最新价卡", "生成内部测算", "人工确认报价", "生成报价预览"]},
                "legacy_entry": {"path": "/legacy/pickup", "label": "历史揽收分析（只读）"}, "settings": service.settings(),
            })
            return
        if path == "/api/airfreight/demo":
            self._send_json(service.demo_state(query.get("flow", [None])[0]))
            return
        if path == "/api/airfreight/settings":
            self._send_json(service.settings())
            return
        if path == "/api/airfreight/sources":
            self._send_json(service.list_chat_sources())
            return
        if path == "/api/airfreight/chat/preview":
            scope = {key: query.get(key, [None])[0] for key in ("source_key", "account_id", "source_snapshot", "conversation_id", "conversation_name", "target_root_message_id", "start_at", "end_at")}
            self._send_json(service.preview_chat(scope))
            return
        chat_match = re.fullmatch(r"/api/airfreight/chat/import/([^/]+)(?:/(evidence|messages))?", path)
        if chat_match:
            import_id = unquote(chat_match.group(1))
            self._send_json(service.chat_evidence(import_id) if chat_match.group(2) == "evidence" else service.get_chat_import(import_id))
            return
        if path == "/api/airfreight/batches":
            self._send_json(service.list_batches(limit=int(query.get("limit", [50])[0])))
            return
        batch_match = re.fullmatch(r"/api/airfreight/batches/([^/]+)(?:/(parse|evidence))?", path)
        if batch_match:
            self._send_json(service.get_batch(unquote(batch_match.group(1))))
            return
        if path == "/api/airfreight/rate-cards":
            self._send_json(service.list_rate_cards(view=query.get("view", ["current"])[0] or "current", batch_id=query.get("batch_id", [None])[0], destination=query.get("destination", [None])[0]))
            return
        rate_sheet_match = re.fullmatch(r"/api/airfreight/rate-cards/([^/]+)/published-sheet(\.pdf)?", path)
        if rate_sheet_match:
            version_id = unquote(rate_sheet_match.group(1))
            if rate_sheet_match.group(2):
                data, filename = service.published_rate_card_pdf(version_id)
                self._send_attachment(data, "application/pdf", filename)
            else:
                self._send_json(service.published_rate_card_sheet(version_id))
            return
        version_match = re.fullmatch(r"/api/airfreight/rate-cards/([^/]+)(?:/(evidence|rates))?", path)
        if version_match:
            version_id = unquote(version_match.group(1))
            if version_match.group(2) == "evidence":
                self._send_json({"version_id": version_id, "items": service.field_evidence(entity_type="rate_card_version", entity_id=version_id)})
            else:
                items = [item for item in service.list_rate_cards(view="all")["items"] if item["version_id"] == version_id]
                self._send_json(items[0] if items else {"version_id": version_id, "rates": []})
            return
        if path == "/api/airfreight/conflicts":
            self._send_json({"items": service.list_conflicts(query.get("batch_id", [None])[0]), "total": len(service.list_conflicts(query.get("batch_id", [None])[0]))})
            return
        if path == "/api/airfreight/quotes":
            self._send_json(service.list_quotes(import_id=query.get("import_id", [None])[0]))
            return
        quote_pdf_match = re.fullmatch(r"/api/airfreight/quote-preview/([^/]+)/quotation\.pdf", path)
        if quote_pdf_match:
            data, filename = service.quote_pdf(unquote(quote_pdf_match.group(1)))
            self._send_attachment(data, "application/pdf", filename)
            return
        quote_match = re.fullmatch(r"/api/airfreight/quotes/([^/]+)(?:/(weight|match|calculation|evidence))?", path)
        if quote_match:
            quote_id = unquote(quote_match.group(1))
            action = quote_match.group(2)
            if action == "weight":
                self._send_json(service.calculate_chargeable_weight(quote_id))
            elif action == "match":
                self._send_json(service.match_rates(quote_id))
            elif action == "calculation":
                self._send_json(service.generate_internal_calculation(quote_id))
            elif action == "evidence":
                detail = service.get_quote_detail(quote_id)
                self._send_json({"quote_request_id": quote_id, "items": [service.field_evidence(entity_type="package_group", entity_id=group["package_group_id"]) for group in detail["package_groups"]]})
            else:
                self._send_json(service.get_quote_detail(quote_id))
            return
        if path == "/api/airfreight/quote-preview":
            self._send_json(service.quote_preview(import_id=query.get("import_id", [None])[0]))
            return
        if path == "/api/airfreight/health":
            self._send_json({"status": "ok", "business_line": "airfreight", "external_model": False})
            return
        raise AirfreightOperationError("not_found", "空运接口不存在", http_status=404)

    def _send_json(self, value: object, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(display_safe_value(value), ensure_ascii=False).encode("utf-8")
        self._send_bytes(data, "application/json; charset=utf-8", status=status)

    def _send_error(self, status: int, error_code: str, message: str, *, field_errors: list[dict] | None = None,
                    details: dict | None = None) -> None:
        payload = {"error_code": error_code, "message": message}
        if field_errors:
            payload["field_errors"] = field_errors
        if details:
            payload.update(details)
        self._send_json(payload, status=status)

    def _write_request_allowed(self) -> bool:
        host_header = (self.headers.get("Host") or "").split(":", 1)[0].strip("[]").lower()
        allowed_hosts = {"127.0.0.1", "localhost", "::1", (self.server.server_address[0] or "").strip("[]").lower()}
        if host_header not in allowed_hosts:
            self._send_error(HTTPStatus.FORBIDDEN, "write_origin_forbidden", "写请求来源不在本机允许范围")
            return False
        origin = self.headers.get("Origin")
        if not origin:
            self._send_error(HTTPStatus.FORBIDDEN, "write_origin_required", "写请求必须包含同源 Origin")
            return False
        origin_url = urlparse(origin)
        origin_host = (origin_url.hostname or "").lower()
        origin_port = origin_url.port
        server_port = int(self.server.server_address[1])
        if origin_url.scheme not in {"http", "https"} or origin_host not in allowed_hosts or (origin_port not in (None, server_port)):
            self._send_error(HTTPStatus.FORBIDDEN, "write_origin_forbidden", "写请求 Origin 不符合本地同源要求")
            return False
        token = self.headers.get("X-CSRF-Token")
        if not token or not secrets.compare_digest(token, self.csrf_token):
            self._send_error(HTTPStatus.FORBIDDEN, "csrf_required", "写请求缺少有效的防 CSRF 标记")
            return False
        return True

    def _read_body(self, max_bytes: int = 10 * 1024 * 1024) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise PriceOperationError("invalid_content_length", "Content-Length 无效", http_status=400) from exc
        if length < 0 or length > max_bytes:
            raise PriceOperationError("file_too_large", "请求体超过大小限制", http_status=413)
        return self.rfile.read(length)

    @staticmethod
    def _parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], bytes | None]:
        match = re.search(r"boundary=\"?([^\";]+)", content_type, re.I)
        if not match:
            raise PriceOperationError("invalid_multipart", "multipart 请求缺少 boundary", http_status=400)
        boundary = b"--" + match.group(1).encode()
        fields: dict[str, str] = {}
        file_data = None
        for part in body.split(boundary)[1:]:
            part = part.strip(b"\r\n-")
            if not part or b"\r\n\r\n" not in part:
                continue
            header_bytes, content = part.split(b"\r\n\r\n", 1)
            headers = header_bytes.decode("utf-8", "ignore")
            content = content.rstrip(b"\r\n")
            name_match = re.search(r'name="([^"]+)"', headers, re.I)
            if not name_match:
                continue
            name = name_match.group(1)
            if "filename=" in headers.lower() or name in {"file", "workbook"}:
                file_data = content
            else:
                fields[name] = content.decode("utf-8", "ignore")
        return fields, file_data

    def _request_json(self, body: bytes) -> dict:
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PriceOperationError("invalid_json", "请求 JSON 格式无效", http_status=400) from exc
        if not isinstance(value, dict):
            raise PriceOperationError("invalid_json", "请求 JSON 必须是对象", http_status=400)
        return value

    def _upload_request(self, body: bytes) -> tuple[dict, bytes]:
        content_type = self.headers.get("Content-Type", "").lower()
        if content_type.startswith("multipart/form-data"):
            fields, file_data = self._parse_multipart(body, self.headers.get("Content-Type", ""))
            if file_data is None:
                raise PriceOperationError("file_required", "请求缺少 .xlsx 文件", http_status=400)
            value = {}
            for key in ("source_scope", "scope"):
                if fields.get(key):
                    try:
                        value["source_scope"] = json.loads(fields[key])
                    except json.JSONDecodeError as exc:
                        raise PriceOperationError("invalid_json", "source_scope JSON 格式无效", http_status=400) from exc
            if fields.get("request_id"):
                value["request_id"] = fields["request_id"]
            return value, file_data
        if "json" in content_type or not content_type:
            value = self._request_json(body)
            encoded = value.pop("file_base64", None)
            if encoded is None:
                raise PriceOperationError("file_required", "请求缺少 file_base64", http_status=400)
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise PriceOperationError("invalid_file_encoding", "file_base64 无效", http_status=400) from exc
            return value, data
        return {}, body

    def do_POST(self) -> None:  # noqa: N802
        if not self._write_request_allowed():
            return
        parsed = urlparse(self.path)
        # Browser headers are ByteStrings; percent-encoded Unicode names are
        # decoded before checking the exact configured review identity.
        actor_id = unquote(self.headers.get("X-Operator-Id", "")) or None
        actor_name = unquote(self.headers.get("X-Operator-Name", "")) or None
        try:
            body = self._read_body()
            air_path = self._airfreight_path(parsed.path)
            if air_path is not None:
                self._handle_airfreight_post(air_path, body, actor_id=actor_id, actor_name=actor_name)
                return
            if parsed.path == "/api/wecom/price-candidates":
                value = self._request_json(body)
                # Browser writes are always human-originated candidates.  A
                # caller cannot promote a request by putting source_kind,
                # actor_type or auto_approve in JSON; chat/system candidates
                # are created only by the trusted analysis write boundary.
                source_kind = "manual"
                actor_type = "human"
                scope = value.get("source_scope")
                if not isinstance(scope, dict) or not all(scope.get(key) for key in ("account_id", "source_database", "conversation_id")):
                    raise PriceOperationError("source_scope_required", "网页候选必须绑定账号、来源库和业务会话范围", http_status=422)
                result = self.storage.price_maintenance().submit_candidate(
                    value, source_kind=source_kind, source_scope=scope,
                    field_sources=value.get("field_sources"), idempotency_key=value.get("idempotency_key") or self.headers.get("Idempotency-Key"),
                    base_version=value.get("base_version"), actor_type=actor_type,
                    actor_id=actor_id, actor_name=actor_name,
                )
                self._send_json(result, status=HTTPStatus.CREATED)
                return
            candidate_match = re.fullmatch(r"/api/wecom/price-candidates/([^/]+)/review", parsed.path)
            if candidate_match:
                value = self._request_json(body)
                result = self.storage.price_maintenance().review_candidate(
                    candidate_match.group(1), action=value.get("action", ""),
                    actor_id=actor_id, actor_name=actor_name,
                    reason=value.get("reason", ""), expected_version=value.get("expected_version", value.get("base_version")),
                    correction=value.get("correction"), idempotency_key=value.get("idempotency_key") or self.headers.get("Idempotency-Key"),
                    account_id=value.get("account_id"), source_database=value.get("source_database"),
                    conversation_id=value.get("conversation_id"),
                )
                self._send_json(result)
                return
            deactivate_match = re.fullmatch(r"/api/wecom/prices/([^/]+)/deactivate", parsed.path)
            if deactivate_match:
                value = self._request_json(body)
                if not value.get("account_id") or not value.get("source_database"):
                    raise PriceOperationError("source_scope_required", "停用必须绑定账号和来源库范围", http_status=422)
                result = self.storage.price_maintenance().deactivate(
                    deactivate_match.group(1), actor_id=actor_id, actor_name=actor_name,
                    reason=value.get("reason", ""), expected_version=value.get("base_version", value.get("expected_version")),
                    idempotency_key=value.get("idempotency_key") or self.headers.get("Idempotency-Key"),
                    account_id=value.get("account_id"), source_database=value.get("source_database"), conversation_id=value.get("conversation_id"),
                )
                self._send_json(result)
                return
            if parsed.path == "/api/wecom/prices-import/preview":
                value, data = self._upload_request(body)
                scope = value.get("source_scope") or value.get("scope") or {}
                if not isinstance(scope, dict) or not all(scope.get(key) for key in ("account_id", "source_database", "conversation_id")):
                    raise PriceOperationError("source_scope_required", "Excel 预览必须绑定账号、来源库和业务会话范围", http_status=422)
                result = self.storage.price_maintenance().preview_import(data, source_scope=scope, request_id=value.get("request_id") or self.headers.get("Idempotency-Key"))
                self._send_json(result, status=HTTPStatus.CREATED)
                return
            confirm_match = re.fullmatch(r"/api/wecom/prices-import/confirm", parsed.path)
            if confirm_match:
                value = self._request_json(body)
                encoded = value.get("file_base64")
                data = None
                if encoded:
                    try:
                        data = base64.b64decode(encoded, validate=True)
                    except (ValueError, TypeError) as exc:
                        raise PriceOperationError("invalid_file_encoding", "file_base64 无效", http_status=400) from exc
                selected = value.get("selected_rows") or value.get("rows") or []
                try:
                    selected = [int(item) for item in selected]
                except (TypeError, ValueError) as exc:
                    raise PriceOperationError("invalid_rows", "selected_rows 必须是整数列表", http_status=400) from exc
                result = self.storage.price_maintenance().confirm_import(
                    value.get("preview_id", ""), selected_rows=selected,
                    actor_id=actor_id, actor_name=actor_name, data=data,
                    idempotency_key=value.get("idempotency_key") or self.headers.get("Idempotency-Key"),
                    account_id=value.get("account_id"),
                )
                self._send_json(result)
                return
            if parsed.path == "/api/wecom/company-corrections":
                value = self._request_json(body)
                role = value.get('business_role')
                if role is not None and role not in ('', 'supplier', 'unknown'):
                    raise PriceOperationError('invalid_business_role', '业务角色无效')
                if role and not str(value.get('basis') or '').strip():
                    raise PriceOperationError('business_role_basis_required', '确认业务身份需要填写核验依据')
                common = dict(account_id=value.get("account_id", ""), source_database=value.get("source_database", ""),
                              conversation_id=value.get("conversation_id"), message_ids=[str(v) for v in value.get("message_ids", [])],
                              sender_ids=[str(v) for v in value.get("sender_ids", [])], normalized_company_id=value.get("normalized_company_id", ""),
                              normalized_company_name=value.get("normalized_company_name", ""))
                service = self.storage.price_maintenance()
                if value.get("mode", "submit") == "preview":
                    result = service.preview_company_correction(**common)
                else:
                    result = service.apply_company_correction(
                        **common, original_corp_id=value.get("original_corp_id"), original_corp_name=value.get("original_corp_name"),
                        basis=value.get("basis", "manual_correction"), actor_id=actor_id,
                        actor_name=actor_name, alias=value.get("alias"),
                    )
                    if role and result.get('status') == 'applied':
                        result['role_confirmation'] = self.storage.confirm_business_role(
                            account_id=common['account_id'], source_database=common['source_database'],
                            conversation_id=common['conversation_id'], company_id=common['normalized_company_id'],
                            company_name=common['normalized_company_name'], business_role=role,
                            basis=value.get('basis', ''), actor_id=actor_id,
                            actor_name=actor_name)
                self._send_json(result, status=HTTPStatus.CREATED)
                return
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "接口不存在")
        except PriceOperationError as exc:
            self._send_error(exc.http_status, exc.error_code, exc.message, field_errors=exc.field_errors, details=exc.details)
        except AirfreightOperationError as exc:
            self._send_error(exc.http_status, exc.error_code, exc.message, details=exc.details)
        except (ValueError, TypeError) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
        except Exception:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "服务器处理失败，请查看本地日志或刷新后重试")

    def _handle_airfreight_post(self, path: str, body: bytes, *, actor_id: str | None, actor_name: str | None) -> None:
        service = AirfreightService(
            self.storage, source_db_paths=self.airfreight_source_paths or (self.storage.path,),
            include_demo_fixtures=self.airfreight_include_demo_fixtures,
        )
        value = self._request_json(body) if body else {}
        if path == "/api/airfreight/demo/reset":
            self._send_json(service.reset_demo())
            return
        if path == "/api/airfreight/demo/reviewer":
            # The browser cannot nominate an arbitrary reviewer.  This action
            # only enables the named, deterministic local demo identity.
            self._send_json(service.configure_demo_reviewer(), status=HTTPStatus.CREATED)
            return
        if path == "/api/airfreight/demo/step":
            # Browser writes must use the server-owned state machine.  The
            # legacy unversioned ``flow + step`` shape is intentionally not
            # accepted here because it allowed a stale page to forge progress.
            self._send_json(service.transition_demo(
                value.get("flow", "A"), value.get("action", ""),
                state_version=value.get("state_version"), payload=value.get("payload") or {"step": value.get("step"), "scope": value.get("scope"), "confirm": value.get("confirm"), "mode": value.get("mode"), "reason": value.get("reason")},
                actor_id=actor_id, actor_name=actor_name,
                idempotency_key=value.get("idempotency_key") or self.headers.get("Idempotency-Key"),
            ))
            return
        if path == "/api/airfreight/batches":
            files = value.get("files")
            if value.get("demo") or files is None:
                result = service.ensure_demo_batch()
            else:
                if not isinstance(files, list):
                    raise AirfreightOperationError("invalid_files", "files 必须是数组", http_status=400)
                result = service.create_batch(files=files, batch_kind=value.get("batch_kind", "manual_rate_card"), source_scope=value.get("source_scope") or {}, request_id=value.get("request_id") or self.headers.get("Idempotency-Key"))
            self._send_json(result, status=HTTPStatus.CREATED)
            return
        batch_parse = re.fullmatch(r"/api/airfreight/batches/([^/]+)/parse", path)
        if batch_parse:
            self._send_json(service.parse_batch(unquote(batch_parse.group(1))))
            return
        if path == "/api/airfreight/chat/import":
            self._send_json(service.confirm_chat_import(value, actor_id=actor_id, actor_name=actor_name), status=HTTPStatus.CREATED)
            return
        if path == "/api/airfreight/quotes/parse":
            self._send_json(service.parse_quotes(value.get("import_id", "")), status=HTTPStatus.CREATED)
            return
        correction_match = re.fullmatch(r"/api/airfreight/quotes/([^/]+)/corrections", path)
        if correction_match:
            self._send_json(service.correct_quote_field(unquote(correction_match.group(1)), entity_type=value.get("entity_type", "package_group"), entity_id=value.get("entity_id", ""), field_name=value.get("field_name", ""), corrected_value=value.get("corrected_value"), actor_id=actor_id, actor_name=actor_name, reason=value.get("reason", ""), idempotency_key=value.get("idempotency_key") or self.headers.get("Idempotency-Key") or ("correction-" + uuid.uuid4().hex)))
            return
        quote_action = re.fullmatch(r"/api/airfreight/quotes/([^/]+)/(calculate|match|internal|confirm)", path)
        if quote_action:
            quote_id = unquote(quote_action.group(1))
            action = quote_action.group(2)
            if action == "calculate":
                self._send_json(service.calculate_chargeable_weight(quote_id))
            elif action == "match":
                self._send_json(service.match_rates(quote_id))
            elif action == "internal":
                self._send_json(service.generate_internal_calculation(quote_id))
            else:
                self._send_json(service.confirm_quote(quote_id, actor_id=actor_id, actor_name=actor_name, reason=value.get("reason", ""), idempotency_key=value.get("idempotency_key") or self.headers.get("Idempotency-Key") or ("confirm-quote-" + quote_id), sales_adjustment_per_kg=value.get("sales_adjustment_per_kg")))
            return
        review_match = re.fullmatch(r"/api/airfreight/rate-cards/([^/]+)/review", path)
        if review_match:
            self._send_json(service.publish_rate_card(unquote(review_match.group(1)), actor_id=actor_id, actor_name=actor_name, reason=value.get("reason", ""), idempotency_key=value.get("idempotency_key") or self.headers.get("Idempotency-Key") or ("publish-" + unquote(review_match.group(1)))))
            return
        resolve_match = re.fullmatch(r"/api/airfreight/conflicts/([^/]+)/resolve", path)
        if resolve_match:
            self._send_json(service.resolve_conflict(unquote(resolve_match.group(1)), rate_id=value.get("rate_id"), corrected_amount=value.get("corrected_amount"), actor_id=actor_id, actor_name=actor_name, reason=value.get("reason", ""), idempotency_key=value.get("idempotency_key") or self.headers.get("Idempotency-Key") or ("resolve-" + unquote(resolve_match.group(1)))))
            return
        internal_match = re.fullmatch(r"/api/airfreight/internal-rules/([^/]+)/confirm", path)
        if internal_match:
            self._send_json(service.confirm_internal_rule(unquote(internal_match.group(1)), actor_id=actor_id, actor_name=actor_name, reason=value.get("reason", ""), idempotency_key=value.get("idempotency_key") or self.headers.get("Idempotency-Key") or ("confirm-rule-" + unquote(internal_match.group(1)))))
            return
        weight_match = re.fullmatch(r"/api/airfreight/rate-cards/([^/]+)/weight-rules/confirm", path)
        if weight_match:
            self._send_json(service.confirm_weight_rules(
                unquote(weight_match.group(1)), boundaries=value.get("boundaries") or {}, actor_id=actor_id,
                actor_name=actor_name, reason=value.get("reason", ""),
                idempotency_key=value.get("idempotency_key") or self.headers.get("Idempotency-Key") or ("weight-rules-" + unquote(weight_match.group(1))),
            ))
            return
        raise AirfreightOperationError("not_found", "空运接口不存在", http_status=404)

    def _send_attachment(self, data: bytes, content_type: str, filename: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, content_type: str, *, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(data)


def serve_wecom(
    storage: WecomLocalStorage,
    host: str = "127.0.0.1",
    port: int = 8766,
    source_analysis_dbs: tuple[Path, ...] | list[Path] | None = None,
    include_demo_fixtures: bool = True,
) -> None:
    storage.initialize()
    source_paths = tuple(Path(path).resolve() for path in (source_analysis_dbs or [storage.path]))

    # Airfreight is the only default business line.  The old pickup page is
    # still retained behind /legacy/pickup for read-only compatibility.
    dashboard_path = Path(__file__).resolve().parents[1] / "static" / "airfreight.html"
    if not dashboard_path.is_file():
        # Fallback search
        candidate = Path(__file__).resolve().parent / "static" / "airfreight.html"
        if candidate.is_file():
            dashboard_path = candidate

    dashboard_bytes = dashboard_path.read_bytes() if dashboard_path.is_file() else b"<h1>Dashboard HTML not found</h1>"
    legacy_path = Path(__file__).resolve().parents[1] / "static" / "wecom.html"
    legacy_dashboard_bytes = legacy_path.read_bytes() if legacy_path.is_file() else b"<h1>Legacy dashboard not found</h1>"

    handler = type(
        "ConfiguredWecomDashboardHandler",
        (WecomDashboardHandler,),
        {
            "storage": storage,
            "dashboard_bytes": dashboard_bytes,
            "legacy_dashboard_bytes": legacy_dashboard_bytes,
            "csrf_token": secrets.token_urlsafe(24),
            "airfreight_source_paths": source_paths,
            "airfreight_include_demo_fixtures": bool(include_demo_fixtures),
        },
    )
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        server = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        print(f"Error: 端口 {port} 已被占用，请先终止旧进程或指定新端口（例如 --port 8767）: {exc}", file=sys.stderr)
        return

    print(f"dashboard\twecom-local\thttp://{host}:{port}\t{storage.path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
