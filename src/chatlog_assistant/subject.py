from __future__ import annotations

import re
import unicodedata

from .models import SubjectMatch


_SUBJECT_SUFFIX = re.compile(r"(?:^|\s)@(?P<subject>[^@\s]+)\s*$")


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return re.sub(r"[\s_-]+", "", normalized)


class SubjectResolver:
    """Resolve the business subject from the sender label, never from message text."""

    def __init__(self, zhongji_aliases: tuple[str, ...] | None = None) -> None:
        aliases = zhongji_aliases or ("中技", "中技物流")
        self._zhongji_aliases = {_normalize(item) for item in aliases}

    def resolve(self, sender_display: str, sender_corp_name: str | None = None) -> SubjectMatch:
        if sender_corp_name and self._is_zhongji(sender_corp_name):
            return SubjectMatch(raw_subject=sender_corp_name.strip(), bucket="zhongji", confidence=1.0)

        match = _SUBJECT_SUFFIX.search(sender_display)
        if not match:
            return SubjectMatch(raw_subject=None, bucket="other", confidence=0.65)

        raw_subject = match.group("subject").strip("，,。.;；:：()（）[]【】")
        return SubjectMatch(
            raw_subject=raw_subject,
            bucket="zhongji" if self._is_zhongji(raw_subject) else "other",
            confidence=1.0,
        )

    def _is_zhongji(self, value: str) -> bool:
        normalized = _normalize(value)
        return normalized in self._zhongji_aliases or normalized.startswith("中技")

