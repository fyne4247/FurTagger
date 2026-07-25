"""Pending human-review queue for uncertain perceptual matches."""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

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
        self._lock = threading.Lock()

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
        except Exception:
            pass

    def save(self) -> None:
        with self._lock:
            payload = {
                "version": 1,
                "items": [it.to_dict() for it in self.items.values()],
            }
            try:
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                tmp.replace(self.path)
            except OSError:
                pass

    def add(self, item: PendingReview) -> PendingReview:
        with self._lock:
            # Replace existing entry for same path
            for eid, existing in list(self.items.items()):
                if existing.path == item.path:
                    del self.items[eid]
            self.items[item.id] = item
        self.save()
        return item

    def remove(self, item_id: str) -> Optional[PendingReview]:
        with self._lock:
            item = self.items.pop(item_id, None)
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
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass
