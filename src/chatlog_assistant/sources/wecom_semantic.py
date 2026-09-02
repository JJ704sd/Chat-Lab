from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
import urllib.error
import urllib.request

from .wecom_classifier import (
    LOGISTICS_CATEGORIES,
    LogisticsIssueClassification,
    LogisticsResponseAssessment,
    extract_business_clues,
)
from .wecom_exporter import mask_sensitive_text

DEFAULT_API_BASE = "https://api.minimaxi.com/v1"
DEFAULT_MODEL = "MiniMax-M3"
BATCH_SIZE = 12
TEXT_LIMIT = 380

_WECOM_ISSUE_SYSTEM = """你是专业跨境物流与企业微信群聊业务语义分类器。只输出 JSON，不要输出 Markdown 或任何额外说明。
请判断每条消息是否包含需要处理的物流问题、业务咨询、操作指令或系统/AI问题。
类别必须选自以下 10 个标准分类：
""" + "、".join(LOGISTICS_CATEGORIES) + """

输出 JSON 格式：
{
  "items": [
    {
      "id": "字符串ID",
      "is_issue": true,
      "categories": ["询价报价"],
      "confidence": 0.92,
      "summary": "不超过40字的问题核心摘要"
    }
  ]
}

分类与判断规则：
1. 询价报价：客户/群成员询问单价、总费用、运价、算成本、询价。
2. 提货安排：上门提货、拉货、装车时间及地址。
3. 运输方式与线路：咨询空运/海运/铁运、直飞/转运、航班船期、港口口岸。
4. 时效与轨迹：查物流进度、跟踪单号、查到哪了、问几天到达。
5. 入仓预约：订舱、交仓、进仓单、预约入仓时间。
6. 单证报关：报关单、清关、海关查验、商检、提单发票资料、违禁品/禁限运咨询。
7. 派送签收：末端配送、签收确认、收货人信息。
8. 费用对账：账单对账、付款开票、水单确认、退款。
9. 延误/丢损/异常：货物破损、少件、丢失、延误排查、索赔理赔、系统报错/接口调用失败。
10. 其他待分类问题：无法明确归入上述 9 类但确有待办/请求事项（如会议预约、通用请示）。
11. 纯日常寒暄、系统自动通知、已办结的单纯通报、纯报价答复：is_issue=false，categories=[]。
12. 拿不准时可归入「其他待分类问题」，绝不编造未出现的事实。一条消息最多输出 2 个分类。
"""

_WECOM_RESPONSE_SYSTEM = """你是物流客服回复质量与解决方案评估器。只输出 JSON，不要输出 Markdown。
评估每条回复属于哪种类型以及是否给出了实质解决方案。
kind 必须选自：即时响应、报价方案、处理方案、状态更新、一般回复

输出 JSON 格式：
{
  "items": [
    {
      "id": "字符串ID",
      "kind": "处理方案",
      "is_solution": true,
      "status_contribution": "solved",
      "solution_text": "提炼出的方案核心内容",
      "confidence": 0.90
    }
  ]
}

判断规则：
1. 即时响应：仅表示收到，如“好的/马上/收到/OK/在看了/正在看”，无具体方案（kind="即时响应", is_solution=false, status_contribution="acknowledged"）。
2. 报价方案：给出具体运费单价、总价测算、阶梯报价（kind="报价方案", is_solution=true, status_contribution="solved"）。
3. 处理方案：已安排提货/已放行/改派渠道/给出明确可执行的解决办法或单号（kind="处理方案", is_solution=true, status_contribution="solved"）。
4. 状态更新：处理中/已联系航司或仓库/预计何时出结果（kind="状态更新", is_solution=false, status_contribution="in_progress"）。
5. 一般回复：无法归入以上的普通交流或反问（kind="一般回复", is_solution=false, status_contribution="none"）。
"""


class WecomSemanticError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WecomSemanticSettings:
    api_key: str
    api_base: str = DEFAULT_API_BASE
    model: str = DEFAULT_MODEL
    group_id: str = ""
    timeout: float = 60.0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.api_key.strip())

    @classmethod
    def from_env(cls) -> WecomSemanticSettings:
        _load_dotenv()
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


def _load_dotenv() -> None:
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[3] / ".env",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'").strip('"')
                if key and key not in os.environ:
                    os.environ[key] = value
        except Exception:
            pass
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


class WecomMiniMaxClient:
    def __init__(self, settings: WecomSemanticSettings) -> None:
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
            raise WecomSemanticError(f"MiniMax HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise WecomSemanticError(f"MiniMax 网络错误: {exc.reason}") from None

        choices = body.get("choices") if isinstance(body, dict) else None
        if not choices:
            raise WecomSemanticError("MiniMax 返回缺少 choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "") if isinstance(part, dict) else str(part) for part in content
            )
        if not isinstance(content, str) or not content.strip():
            raise WecomSemanticError("MiniMax 返回空内容")
        return content


class WecomSemanticAnalyzer:
    def __init__(
        self,
        settings: WecomSemanticSettings | None = None,
        client: WecomMiniMaxClient | None = None,
    ) -> None:
        self.settings = settings or WecomSemanticSettings.from_env()
        self.client = client or (WecomMiniMaxClient(self.settings) if self.settings.enabled else None)
        self.calls = 0
        self.classified_count = 0

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled and self.client is not None)

    def classify_issues_batch(
        self,
        items: list[tuple[str, str]],
    ) -> dict[str, tuple[list[LogisticsIssueClassification], str]]:
        """Classify batch of messages into issue categories and summary.

        Returns dict: id -> (list[LogisticsIssueClassification], summary_text)
        """
        results: dict[str, tuple[list[LogisticsIssueClassification], str]] = {}
        if not self.enabled or not items:
            return results

        total = len(items)
        for offset in range(0, total, BATCH_SIZE):
            chunk = items[offset : offset + BATCH_SIZE]
            # Data privacy: mask sensitive phone/id numbers before sending to LLM
            payload = [
                {"id": key, "text": _clip(mask_sensitive_text(text))}
                for key, text in chunk
            ]
            user = "请分析并分类下列群聊消息：\n" + json.dumps(payload, ensure_ascii=False)
            try:
                raw_out = self.client.complete(_WECOM_ISSUE_SYSTEM, user)
                parsed = parse_json_object(raw_out)
                self.calls += 1
            except Exception:
                continue

            for item in parsed.get("items") or []:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("id") or "")
                if not key:
                    continue
                if not item.get("is_issue"):
                    results[key] = ([], "")
                    continue

                categories = item.get("categories") or []
                confidence = float(item.get("confidence") or 0.85)
                summary = str(item.get("summary") or "").strip()
                matches: list[LogisticsIssueClassification] = []

                for cat in categories[:2]:
                    cat_name = str(cat).strip()
                    if cat_name not in LOGISTICS_CATEGORIES:
                        cat_name = "其他待分类问题"
                    matches.append(
                        LogisticsIssueClassification(
                            category=cat_name,
                            confidence=min(0.99, max(0.5, confidence)),
                            evidence=("minimax-m3", summary) if summary else ("minimax-m3",),
                        )
                    )

                results[key] = (matches, summary)
                self.classified_count += 1

        return results

    def assess_responses_batch(
        self,
        items: list[tuple[str, str]],
    ) -> dict[str, LogisticsResponseAssessment]:
        """Assess batch of responses.

        Returns dict: id -> LogisticsResponseAssessment
        """
        results: dict[str, LogisticsResponseAssessment] = {}
        if not self.enabled or not items:
            return results

        total = len(items)
        for offset in range(0, total, BATCH_SIZE):
            chunk = items[offset : offset + BATCH_SIZE]
            payload = [
                {"id": key, "text": _clip(mask_sensitive_text(text))}
                for key, text in chunk
            ]
            user = "请评估下列回复内容与方案：\n" + json.dumps(payload, ensure_ascii=False)
            try:
                raw_out = self.client.complete(_WECOM_RESPONSE_SYSTEM, user)
                parsed = parse_json_object(raw_out)
                self.calls += 1
            except Exception:
                continue

            for item in parsed.get("items") or []:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("id") or "")
                if not key:
                    continue

                kind = str(item.get("kind") or "一般回复")
                is_solution = bool(item.get("is_solution", False))
                status_contrib = str(item.get("status_contribution") or ("solved" if is_solution else "none"))
                solution_text = str(item.get("solution_text") or "").strip() or None
                conf = float(item.get("confidence") or 0.85)

                results[key] = LogisticsResponseAssessment(
                    kind=kind,
                    is_solution=is_solution,
                    status_contribution=status_contrib,
                    solution_text=solution_text,
                    confidence=min(0.99, max(0.5, conf)),
                )

        return results
