from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Iterable

from ..storage import Storage
from .discovery import SourceCandidate


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    source_key: str
    path: Path
    size: int
    modified_ns: int


def observe_once(storage: Storage, sources: Iterable[SourceCandidate]) -> list[ChangeEvent]:
    storage.initialize()
    events: list[ChangeEvent] = []
    for source in sources:
        for path in source.watch_paths:
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            event_key = f"{source.key}:{path.name}"
            changed = storage.record_source_event(event_key, str(path), stat.st_size, stat.st_mtime_ns)
            if changed:
                events.append(ChangeEvent(source.key, path, stat.st_size, stat.st_mtime_ns))
    return events


def watch(
    storage: Storage,
    sources: Iterable[SourceCandidate],
    interval_seconds: int = 600,
    *,
    keyring=None,
    documents=None,
) -> None:
    source_list = list(sources)
    while True:
        events = observe_once(storage, source_list)
        for event in events:
            print(f"changed\t{event.source_key}\t{event.path.name}\t{event.size}", flush=True)
        if events and keyring is not None:
            from .live import import_live_sources
            result = import_live_sources(storage, keyring, documents, source_list)
            print(f"imported\t{result['imported']}", flush=True)
        time.sleep(max(1, interval_seconds))

