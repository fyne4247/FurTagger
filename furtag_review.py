"""Pending human-review queue for uncertain perceptual matches."""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from furtag_settings import atomic_write_text

REVIEW_FILE = ".furtag_review.json"


@dataclass
class PendingReview:
    """One file awaiting human approval of a perceptual candidate."""
    id: str
    path: str                 # absolute path
    relpath: str
    size: int
    mtime: float
    md5: Optional[str] = None
    source: str = "fluffle"   # producer
    match_class: str = ""     # exact / tossUp / alternative / unlikely
    platform: str = ""
    location: str = ""        # candidate URL
    post_id: str = ""
    md5_from_url: str = ""
    fluffle_tags: List[str] = field(default_factory=list)
    fluffle_urls: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    # Extra opaque payload for future SauceNAO etc.
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, **kwargs) -> "PendingReview":
        kwargs.setdefault("id", uuid.uuid4().hex[:12])
        return cls(**kwargs)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PendingReview":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore
        filtered = {k: v for k, v in data.items() if k in known}
        if "id" not in filtered:
            filtered["id"] = uuid.uuid4().hex[:12]
        return cls(**filtered)


class ReviewQueue:
    """Persisted per-scan-root queue of PendingReview items."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / REVIEW_FILE
        self.items: Dict[str, PendingReview] = {}
        # path → id, so replacing the entry for a path is O(1) rather than a
        # linear scan on every add.
        self._by_path: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._dirty = False

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
            records = data.get("items") if isinstance(data, dict) else data
            if not isinstance(records, list):
                return
            for rec in records:
                if isinstance(rec, dict):
                    item = PendingReview.from_dict(rec)
                    self.items[item.id] = item
                    self._by_path[item.path] = item.id
        except Exception:
            pass

    def save(self) -> None:
        """Persist only when something actually changed.

        Callers checkpoint this on a fixed cadence, so without the dirty guard a
        scan with no pending reviews rewrites an empty queue file repeatedly.
        """
        with self._lock:
            if not self._dirty:
                return
            payload = {
                "version": 1,
                "items": [it.to_dict() for it in self.items.values()],
            }
            try:
                atomic_write_text(
                    self.path, json.dumps(payload, ensure_ascii=False, indent=2))
                self._dirty = False
            except OSError:
                pass

    def add(self, item: PendingReview) -> PendingReview:
        with self._lock:
            # Replace existing entry for same path
            prior_id = self._by_path.get(item.path)
            if prior_id is not None:
                self.items.pop(prior_id, None)
            self.items[item.id] = item
            self._by_path[item.path] = item.id
            self._dirty = True
        self.save()
        return item

    def remove(self, item_id: str) -> Optional[PendingReview]:
        with self._lock:
            item = self.items.pop(item_id, None)
            if item is not None:
                if self._by_path.get(item.path) == item_id:
                    del self._by_path[item.path]
                self._dirty = True
        if item is not None:
            self.save()
        return item

    def get(self, item_id: str) -> Optional[PendingReview]:
        return self.items.get(item_id)

    def list_items(self) -> List[PendingReview]:
        return sorted(self.items.values(), key=lambda i: i.created_at)

    def __len__(self) -> int:
        return len(self.items)

    def clear(self) -> None:
        with self._lock:
            self.items.clear()
            self._by_path.clear()
            self._dirty = False
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass
