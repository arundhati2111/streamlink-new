"""Storage backends. CSV now; a DB backend can implement the same interface later."""
from __future__ import annotations

import csv
import os
from abc import ABC, abstractmethod
from typing import Iterable

from .models import CSV_FIELDS, StationResult


class Storage(ABC):
    """Persist StationResult rows. Implementations must be idempotent on dedup_key."""

    @abstractmethod
    def save(self, result: StationResult) -> bool:
        """Persist one result. Returns True if newly written, False if a duplicate."""

    def save_many(self, results: Iterable[StationResult]) -> int:
        written = 0
        for r in results:
            if self.save(r):
                written += 1
        return written

    def close(self) -> None:  # optional hook for backends that hold resources
        pass


class CsvStorage(Storage):
    """Append rows to a CSV file, skipping duplicates (platform + stream_url).

    Existing rows are loaded on open so reruns don't create duplicates.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._seen: set[tuple[str, str]] = set()
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._ensure_header_and_load()

    def _ensure_header_and_load(self) -> None:
        if not os.path.exists(self.path):
            with open(self.path, "w", newline="", encoding="utf-8") as fh:
                csv.DictWriter(fh, fieldnames=CSV_FIELDS).writeheader()
            return
        # File exists: load existing dedup keys so we don't re-append.
        with open(self.path, "r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                self._seen.add((row.get("platform", ""), row.get("stream_url", "")))

    def save(self, result: StationResult) -> bool:
        if not result.stream_url:
            return False
        key = result.dedup_key()
        if key in self._seen:
            return False
        with open(self.path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            writer.writerow(result.to_row())
        self._seen.add(key)
        return True
