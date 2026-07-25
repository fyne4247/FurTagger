"""Structured progress events and observer adapters for FurTag."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@dataclass
class RunEvent:
    """A structured engine event for any frontend (terminal / Qt / tests)."""
    kind: str
    message: str = ""
    track: str = ""          # "hash" | "perceptual" | ""
    phase: str = ""
    index: int = 0
    total: int = 0
    current: str = ""
    nxt: str = ""
    sub: str = ""
    result: str = ""
    source_hits: Dict[str, int] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class RunObserver(Protocol):
    def emit(self, event: RunEvent) -> None: ...


class NullObserver:
    def emit(self, event: RunEvent) -> None:
        return None


class TerminalObserver:
    """Bridge structured events into LiveDisplay / notify() / print()."""

    def __init__(self, display=None) -> None:
        self.display = display

    def emit(self, event: RunEvent) -> None:
        d = self.display
        kind = event.kind
        if kind == "log" or kind == "issue":
            if d is not None:
                d.log(event.message)
            else:
                print(event.message)
            return
        if kind == "print":
            print(event.message)
            return
        if d is None:
            if event.message:
                print(event.message)
            return
        if kind == "begin_phase":
            d.begin_phase(
                event.track, event.phase or event.message, event.total,
                growing=bool(event.extra.get("growing")),
                interval=float(event.extra.get("interval") or 0.0))
        elif kind == "grow":
            d.grow(event.track, int(event.extra.get("by") or 1))
        elif kind == "freeze_total":
            d.freeze_total(event.track)
        elif kind == "start_file":
            d.start_file(event.track, event.index, event.current,
                         event.nxt or None)
        elif kind == "status":
            d.status(event.track, event.sub or event.message)
        elif kind == "finish_file":
            d.finish_file(event.track, event.result or event.message)
        elif kind == "close_display":
            d.close()
