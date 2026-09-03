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
from urllib.parse import parse_qs, urlparse, unquote

from .wecom_storage import WecomLocalStorage
from .wecom_report import display_safe_value
from .wecom_exporter import export_issues_to_csv, export_issues_to_json, mask_sensitive_text
from .wecom_pricing import (
    PriceMaintenance,
    PriceOperationError,
    build_price_workbook,
)


class WecomDashboardHandler(BaseHTTPRequestHandler):
    storage: WecomLocalStorage
    dashboard_bytes: bytes = b""
    csrf_token: str = secrets.token_urlsafe(24)

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
        except (ValueError, TypeError) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
        except Exception:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "服务器处理失败，请查看本地日志或刷新后重试")

    def _send_attachment(self, data: bytes, content_type: str, filename: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, content_type: str, *, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(data)


def serve_wecom(
    storage: WecomLocalStorage,
    host: str = "127.0.0.1",
    port: int = 8766,
) -> None:
    storage.initialize()

    # Locate static/wecom.html correctly relative to package root
    dashboard_path = Path(__file__).resolve().parents[1] / "static" / "wecom.html"
    if not dashboard_path.is_file():
        # Fallback search
        candidate = Path(__file__).resolve().parent / "static" / "wecom.html"
        if candidate.is_file():
            dashboard_path = candidate

    dashboard_bytes = dashboard_path.read_bytes() if dashboard_path.is_file() else b"<h1>Dashboard HTML not found</h1>"

    handler = type(
        "ConfiguredWecomDashboardHandler",
        (WecomDashboardHandler,),
        {
            "storage": storage,
            "dashboard_bytes": dashboard_bytes,
            "csrf_token": secrets.token_urlsafe(24),
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
