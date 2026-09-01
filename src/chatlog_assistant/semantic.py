from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request
from typing import Any

from .classifier import (
    RESPONSE_KINDS,
    SEMANTIC_CATEGORIES,
    needs_semantic,
)
from .models import Classification, ResponseAssessment


DEFAULT_API_BASE = "https://api.minimaxi.com/v1"
DEFAULT_MODEL = "MiniMax-M3"
BATCH_SIZE = 12
TEXT_LIMIT = 360

_ISSUE_SYSTEM = """你是物流与企业微信群聊的语义分类器。只输出 JSON，不要 Markdown。
判断每条消息是否在提出需要处理的问题。类别必须选自：
""" + "、".join(SEMANTIC_CATEGORIES) + """
输出格式：
{"items":[{"id":"字符串","is_issue":true,"categories":["询价报价"],"confidence":0.86,"summary":"不超过40字的问题摘要"}]}
规则：
- 寒暄、已办结的结果通报、纯报价回复：is_issue=false，categories=[]
- 一条消息最多 3 个类别
- 拿不准时用「其他物流问题」
- 不要编造未出现的事实
"""

_RESPONSE_SYSTEM = """你是客服回复质量评估器。只输出 JSON，不要 Markdown。
kind 必须选自：""" + "、".join(RESPONSE_KINDS) + """
输出格式：
{"items":[{"id":"字符串","kind":"处理方案","is_solution":true,"confidence":0.9}]}
规则：
- 即时响应：好的/马上/收到，没有具体信息
- 报价方案：给出价格或费用方案
- 处理方案：已安排、改走、给出可执行办法
- 状态更新：处理中、已联系、预计时间
- 一般回复：无法归入以上
"""


class SemanticConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SemanticSettings:
    api_key: str
    api_base: str = DEFAULT_API_BASE
    model: str = DEFAULT_MODEL
    group_id: str = ""
    timeout: float = 60

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls) -> SemanticSettings:
        load_dotenv()
        if os.environ.get("CHATLAB_SEMANTIC", "1").strip().casefold() in {"0", "false", "no", "off"}:
            api_key = ""
        else:
            api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
        return cls(
            api_key=api_key,
            api_base=os.environ.get("MINIMAX_API_BASE", DEFAULT_API_BASE).rstrip("/"),
            model=os.environ.get("MINIMAX_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            group_id=os.environ.get("MINIMAX_GROUP_ID", "").strip(),
            timeout=float(os.environ.get("MINIMAX_TIMEOUT", "60")),
        )


def load_dotenv() -> None:
    candidates = [Path.cwd() / ".env"]
    try:
        candidates.append(Path(__file__).resolve().parents[2] / ".env")
    except IndexError:
        pass
    for path in candidates:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = value
        break


def parse_json_object(text: str) -> dict[str, Any]:
    compact = text.strip()
    if compact.startswith("```"):
        compact = re.sub(r"^```(?:json)?\s*", "", compact)
        compact = re.sub(r"\s*```$", "", compact)
    start = compact.find("{")
    end = compact.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model output is not JSON object")
    payload = json.loads(compact[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("model output is not a JSON object")
    return payload


def _clip(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= TEXT_LIMIT:
        return compact
    return compact[: TEXT_LIMIT - 1] + "…"


class MiniMaxClient:
    def __init__(self, settings: SemanticSettings) -> None:
        self.settings = settings

    def complete(self, system: str, user: str) -> str:
        url = f"{self.settings.api_base}/chat/completions"
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "max_completion_tokens": 2048,
            "thinking": {"type": "disabled"},
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }
        if self.settings.group_id:
            headers["MiniMax-Group-Id"] = self.settings.group_id
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise SemanticConfigError(f"MiniMax HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise SemanticConfigError(f"MiniMax 网络错误: {exc.reason}") from None
        choices = body.get("choices") if isinstance(body, dict) else None
        if not choices:
            raise SemanticConfigError("MiniMax 返回缺少 choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "") if isinstance(part, dict) else str(part) for part in content
            )
        if not isinstance(content, str) or not content.strip():
            raise SemanticConfigError("MiniMax 返回空内容")
        return content


class SemanticAnalyzer:
    def __init__(self, settings: SemanticSettings | None = None, client: MiniMaxClient | None = None) -> None:
        self.settings = settings or SemanticSettings.from_env()
        self.client = client or (MiniMaxClient(self.settings) if self.settings.enabled else None)
        self.calls = 0
        self.classified = 0

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled and self.client is not None)

    def classify_many(self, items: list[tuple[str, str]]) -> dict[str, list[Classification]]:
        results: dict[str, list[Classification]] = {}
        if not self.enabled or not items:
            return results
        total = len(items)
        for offset in range(0, total, BATCH_SIZE):
            chunk = items[offset : offset + BATCH_SIZE]
            payload = [{"id": key, "text": _clip(text)} for key, text in chunk]
            user = "请分类下列消息：\n" + json.dumps(payload, ensure_ascii=False)
            try:
                parsed = parse_json_object(self.client.complete(_ISSUE_SYSTEM, user))
                self.calls += 1
            except (SemanticConfigError, ValueError, json.JSONDecodeError, TypeError):
                continue
            for item in parsed.get("items") or []:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("id") or "")
                if not key:
                    continue
                if not item.get("is_issue"):
                    results[key] = []
                    continue
                categories = item.get("categories") or []
                confidence = float(item.get("confidence") or 0.8)
                summary = str(item.get("summary") or "").strip()
                matches: list[Classification] = []
                for category in categories[:3]:
                    name = str(category)
                    if name not in SEMANTIC_CATEGORIES:
                        name = "其他物流问题"
                    evidence = ("minimax-m3",) + ((summary,) if summary else ())
                    matches.append(Classification(name, min(0.99, max(0.5, confidence)), evidence))
                results[key] = matches
                self.classified += 1
            print(
                f"semantic\t{self.settings.model}\t{min(offset + BATCH_SIZE, total)}/{total}",
                file=sys.stderr,
                flush=True,
            )
        return results

    def assess_many(self, items: list[tuple[str, str]]) -> dict[str, ResponseAssessment]:
        results: dict[str, ResponseAssessment] = {}
        if not self.enabled or not items:
            return results
        for offset in range(0, len(items), BATCH_SIZE):
            chunk = items[offset : offset + BATCH_SIZE]
            payload = [{"id": key, "text": _clip(text)} for key, text in chunk]
            user = "请评估下列回复：\n" + json.dumps(payload, ensure_ascii=False)
            try:
                parsed = parse_json_object(self.client.complete(_RESPONSE_SYSTEM, user))
                self.calls += 1
            except (SemanticConfigError, ValueError, json.JSONDecodeError, TypeError):
                continue
            for item in parsed.get("items") or []:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("id") or "")
                kind = str(item.get("kind") or "一般回复")
                if kind not in RESPONSE_KINDS:
                    kind = "一般回复"
                results[key] = ResponseAssessment(
                    kind,
                    bool(item.get("is_solution")) and kind in {"报价方案", "处理方案"},
                    min(0.99, max(0.5, float(item.get("confidence") or 0.75))),
                )
        return results


def merge_classifications(
    text: str,
    keyword_hits: list[Classification],
    semantic_hits: list[Classification] | None,
) -> list[Classification]:
    if semantic_hits is None:
        return keyword_hits
    if not needs_semantic(text, keyword_hits):
        return keyword_hits
    if semantic_hits:
        return semantic_hits
    return keyword_hits


def merge_assessment(
    keyword: ResponseAssessment,
    semantic: ResponseAssessment | None,
) -> ResponseAssessment:
    if semantic is None or keyword.kind != "一般回复":
        return keyword
    return semantic
