from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Mapping


_SUBJECT_SUFFIX = re.compile(r"@\s*(?P<subject>[^@\s\r\n]+)\s*$")


def normalize_corp_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return re.sub(r"[\s_-]+", "", normalized)


@dataclass(frozen=True, slots=True)
class SubjectClassification:
    subject_bucket: str  # 'zhongji' | 'other' | 'unknown'
    raw_subject: str | None
    corp_id: str | None
    corp_name: str | None
    basis: str  # e.g. "corp_id_map", "corp_name_match", "sender_display_suffix", "unknown"
    confidence: float


class WecomSubjectClassifier:
    """Classifies enterprise identity strictly based on sender identity metadata (corp_id, corp_name,

    or display suffix like '张三 @中技物流'), NEVER from message text mentioning companies.
    """

    def __init__(
        self,
        zhongji_aliases: tuple[str, ...] | None = None,
        corp_id_mappings: Mapping[str, str] | None = None,
    ) -> None:
        aliases = zhongji_aliases or ("中技", "中技物流", "深圳市中技物流有限公司", "中技国际物流")
        self._zhongji_aliases = {normalize_corp_name(item) for item in aliases}
        self._corp_id_mappings = dict(corp_id_mappings or {})

    def _is_zhongji_corp_name(self, name: str) -> bool:
        norm = normalize_corp_name(name)
        return norm in self._zhongji_aliases or norm.startswith("中技")

    def classify(
        self,
        sender_id: str | None = None,
        sender_display: str | None = None,
        sender_corp_id: str | None = None,
        sender_corp_name: str | None = None,
    ) -> SubjectClassification:
        # Priority 1: corp_id mapping
        if sender_corp_id and sender_corp_id in self._corp_id_mappings:
            mapped_bucket = self._corp_id_mappings[sender_corp_id]
            return SubjectClassification(
                subject_bucket=mapped_bucket,
                raw_subject=sender_corp_name,
                corp_id=sender_corp_id,
                corp_name=sender_corp_name,
                basis="corp_id_map",
                confidence=1.0,
            )

        # Priority 2: sender_corp_name from user/contact table
        if sender_corp_name and sender_corp_name.strip():
            raw = sender_corp_name.strip()
            bucket = "zhongji" if self._is_zhongji_corp_name(raw) else "other"
            return SubjectClassification(
                subject_bucket=bucket,
                raw_subject=raw,
                corp_id=sender_corp_id,
                corp_name=raw,
                basis="corp_name_match",
                confidence=0.98,
            )

        # Priority 3: sender_display containing "@CompanyName" suffix
        if sender_display:
            match = _SUBJECT_SUFFIX.search(sender_display)
            if match:
                raw_subject = match.group("subject").strip("，,。.;；:：()（）[]【】")
                bucket = "zhongji" if self._is_zhongji_corp_name(raw_subject) else "other"
                return SubjectClassification(
                    subject_bucket=bucket,
                    raw_subject=raw_subject,
                    corp_id=sender_corp_id,
                    corp_name=raw_subject,
                    basis="sender_display_suffix",
                    confidence=0.95,
                )

        # Priority 4: If sender_display exists but has no corp info and no corp_id
        if sender_display or sender_id:
            return SubjectClassification(
                subject_bucket="unknown",
                raw_subject=None,
                corp_id=sender_corp_id,
                corp_name=None,
                basis="unknown",
                confidence=0.30,
            )

        return SubjectClassification(
            subject_bucket="unknown",
            raw_subject=None,
            corp_id=None,
            corp_name=None,
            basis="no_identity_data",
            confidence=0.0,
        )
