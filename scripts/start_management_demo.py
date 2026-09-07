"""Start the local dual-lane pricing demo on macOS or Windows (Python 3.11+)."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from chatlog_assistant.sources.wecom_storage import WecomLocalStorage
from chatlog_assistant.sources.wecom_web import serve_wecom


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=PROJECT / "data/management-demo")
    parser.add_argument("--port", type=int, default=8891)
    args = parser.parse_args()
    serve_wecom(WecomLocalStorage(args.data_root / "analysis.db"), host="127.0.0.1",
                port=args.port, include_demo_fixtures=False)
