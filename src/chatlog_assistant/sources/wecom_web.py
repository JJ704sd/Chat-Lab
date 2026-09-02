from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlparse

from .wecom_storage import WecomLocalStorage
from .wecom_exporter import export_issues_to_csv, export_issues_to_json, mask_sensitive_text


class WecomDashboardHandler(BaseHTTPRequestHandler):
    storage: WecomLocalStorage
    dashboard_bytes: bytes = b""

    protocol_version = "HTTP/1.1"

    def address_string(self) -> str:
        # Crucial for Windows: avoid blocking socket.getfqdn DNS reverse lookup
        return self.client_address[0]

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

        query = parse_qs(parsed.query)
        account_id = query.get("account_id", [None])[0] or None
        subject = query.get("subject", [None])[0] or None
        category = query.get("category", [None])[0] or None
        status = query.get("status", [None])[0] or None

        if parsed.path == "/api/wecom/summary":
            summary = self.storage.get_summary(
                account_id=account_id,
                subject=subject,
                category=category,
                status=status,
            )
            self._send_json(summary)
            return

        if parsed.path == "/api/wecom/issues":
            items = self.storage.list_issues(
                account_id=account_id,
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
                account_id=account_id,
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

    def _send_json(self, value: object) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._send_bytes(data, "application/json; charset=utf-8")

    def _send_attachment(self, data: bytes, content_type: str, filename: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
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
        },
    )
    server = ThreadingHTTPServer((host, port), handler)
    print(f"dashboard\twecom-local\thttp://{host}:{port}\t{storage.path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
