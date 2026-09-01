from __future__ import annotations

from pathlib import Path
from typing import Iterable, Protocol

from ..models import Message
from .discovery import SourceCandidate


class AuthorizationRequired(RuntimeError):
    pass


class AuthorizedExtractor(Protocol):
    """Implemented only after the operator confirms lawful local access."""

    def extract(self, source: SourceCandidate, since_cursor: str | None) -> tuple[Iterable[Message], str]: ...


def extract_encrypted_source(
    source: SourceCandidate,
    extractor: AuthorizedExtractor | None,
    since_cursor: str | None = None,
) -> tuple[Iterable[Message], str]:
    if extractor is None:
        raise AuthorizationRequired(
            f"{source.key} is encrypted; an explicitly authorized local extractor is required"
        )
    if not Path(source.database_path).is_file():
        raise FileNotFoundError(source.database_path)
    return extractor.extract(source, since_cursor)

