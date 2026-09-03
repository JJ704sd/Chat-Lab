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
    # The bucket is an existing reporting dimension.  It is deliberately kept
    # separate from the resolved company name and its evidence state.
    company_status: str = "unknown"  # known | unknown | conflict
    candidate_corp_names: tuple[str, ...] = ()


def split_sender_display(value: str | None) -> tuple[str, str | None]:
    """Return the person portion and an explicit display-name company suffix."""
    text = str(value or "").strip()
    match = _SUBJECT_SUFFIX.search(text)
    if not match:
        return text, None
    company = match.group("subject").strip("，,。.;；:：()（）[]【】") or None
    person = text[:match.start()].strip() or text
    return person, company


def sender_label(
    sender_name: str | None,
    corp_name: str | None = None,
    *,
    company_status: str | None = None,
) -> str:
    """Format every sender as ``姓名 @公司`` without leaking an unverified suffix."""
    person, display_company = split_sender_display(sender_name)
    status = company_status or ("known" if corp_name else "unknown")
    if status == "conflict":
        company = "公司待确认"
    elif corp_name and str(corp_name).strip():
        company = str(corp_name).strip()
    elif display_company and status == "known":
        company = display_company
    else:
        company = "公司未知"
    return f"{person or '未知人员'} @{company}"


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
        display_person, display_corp = split_sender_display(sender_display)
        metadata_corp = sender_corp_name.strip() if sender_corp_name and sender_corp_name.strip() else None

        # A contact table value and a display suffix are independent identity
        # claims.  Do not silently pick one when they disagree.
        if metadata_corp and display_corp and normalize_corp_name(metadata_corp) != normalize_corp_name(display_corp):
            return SubjectClassification(
                subject_bucket="unknown",
                raw_subject=None,
                corp_id=sender_corp_id,
                corp_name=None,
                basis="identity_conflict",
                confidence=0.0,
                company_status="conflict",
                candidate_corp_names=(metadata_corp, display_corp),
            )

        # Priority 1: corp_id mapping
        if sender_corp_id and sender_corp_id in self._corp_id_mappings:
            mapped_bucket = self._corp_id_mappings[sender_corp_id]
            return SubjectClassification(
                subject_bucket=mapped_bucket,
                raw_subject=metadata_corp or display_corp,
                corp_id=sender_corp_id,
                corp_name=metadata_corp or display_corp,
                basis="corp_id_map",
                confidence=1.0,
                company_status="known" if (metadata_corp or display_corp) else "unknown",
            )

        # Priority 2: sender_corp_name from user/contact table
        if metadata_corp:
            raw = metadata_corp
            bucket = "zhongji" if self._is_zhongji_corp_name(raw) else "other"
            return SubjectClassification(
                subject_bucket=bucket,
                raw_subject=raw,
                corp_id=sender_corp_id,
                corp_name=raw,
                basis="corp_name_match",
                confidence=0.98,
                company_status="known",
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
                    company_status="known",
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
                company_status="unknown",
            )

        return SubjectClassification(
            subject_bucket="unknown",
            raw_subject=None,
            corp_id=None,
            corp_name=None,
            basis="no_identity_data",
            confidence=0.0,
            company_status="unknown",
        )
