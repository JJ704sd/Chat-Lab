from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib


@dataclass(frozen=True, slots=True)
class Message:
    source: str
    source_message_id: str
    conversation_id: str
    sent_at: datetime
    sender_display: str
    content: str
    direction: str = "unknown"
    content_type: str = "text"
    raw_ref: str | None = None
    sender_corp_name: str | None = None

    @property
    def id(self) -> str:
        value = f"{self.source}\0{self.source_message_id}".encode("utf-8")
        return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class SubjectMatch:
    raw_subject: str | None
    bucket: str
    confidence: float


@dataclass(frozen=True, slots=True)
class Classification:
    category: str
    confidence: float
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResponseAssessment:
    kind: str
    is_solution: bool
    confidence: float

