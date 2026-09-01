from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .storage import Storage


class DashboardHandler(BaseHTTPRequestHandler):
    storage: Storage
    dashboard_path: Path
    workspace: str
    label: str

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_bytes(self.dashboard_path.read_bytes(), "text/html; charset=utf-8")
            return

        query = parse_qs(parsed.query)
        subject = query.get("subject", [None])[0] or None
        if parsed.path == "/api/meta":
            self._send_json({"workspace": self.workspace, "label": self.label})
            return
        if parsed.path == "/api/summary":
            self._send_json(self.storage.summary(subject))
            return
        if parsed.path == "/api/issues":
            category = query.get("category", [None])[0] or None
            self._send_json({"items": self.storage.list_issues(subject, category)})
            return
        if parsed.path == "/api/health":
            self._send_json({"status": "ok", "workspace": self.workspace})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, value: object) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._send_bytes(data, "application/json; charset=utf-8")

    def _send_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def serve(
    storage: Storage,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    workspace: str = "wechat",
    label: str = "个微",
) -> None:
    storage.initialize()
    dashboard_path = Path(__file__).with_name("static") / "index.html"
    handler = type(
        "ConfiguredDashboardHandler",
        (DashboardHandler,),
        {
            "storage": storage,
            "dashboard_path": dashboard_path,
            "workspace": workspace,
            "label": label,
        },
    )
    server = ThreadingHTTPServer((host, port), handler)
    print(f"dashboard\t{label}\thttp://{host}:{port}\t{storage.path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

