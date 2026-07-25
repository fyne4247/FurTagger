"""Structured progress events and observer adapters for FurTag.

The engine (`furtag.py`) never touches a frontend directly: every progress
point, status line and warning is emitted as a :class:`RunEvent` to the active
:class:`RunObserver`. Exactly one observer is installed per run, so each event
is rendered exactly once.

Event kinds
-----------
``begin_phase``   start/reset a track. ``track``, ``phase`` (label), ``total``,
                  ``extra={"growing": bool, "interval": float}``.
``sidecar_sync``  resumable sidecar reconciliation progress before the scan
                  tracks. ``index``, ``total``, ``current``, running counters.
``grow``          the track's total gained items: ``extra={"by": int}`` (default 1).
``freeze_total``  the producer feeding this track is done; total is final.
``start_file``    ``track``, ``index`` (1-based), ``current``, ``nxt``.
``status``        sub-status for the current file/track: ``sub`` (or ``message``).
``finish_file``   ``track``, ``result``; ``extra={"pending_review": True}`` when
                  the file was queued for manual review.
``issue``         a warning/error from ``notify()``: ``message``.
``log``           an informational line: ``message`` (treated like ``issue``).
``print``         a line intended for a plain stdout/run-log, never the panel.
``close_display`` tear the live panel down.
"""

from __future__ import annotations

import os
import sys
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
    """Render structured events through a :class:`LiveDisplay` (or plain stdout).

    This is the *only* thing that drives ``LiveDisplay`` — the engine must never
    call display methods itself, or every progress point would render twice.
    With ``display=None`` (headless CLI) it degrades to printing the messages
    that carry human-readable text and silently dropping pure progress ticks.
    """

    def __init__(self, display=None) -> None:
        self.display = display

    def emit(self, event: RunEvent) -> None:
        d = self.display
        kind = event.kind
        if kind == "sidecar_sync":
            if sys.stdout.isatty():
                line = f"  {event.message}"
                try:
                    width = max(
                        20, os.get_terminal_size(sys.stdout.fileno()).columns)
                except OSError:
                    width = 100
                if len(line) >= width:
                    line = line[:max(1, width - 2)] + "…"
                sys.stdout.write("\r" + line + "\x1b[K")
                if event.extra.get("final"):
                    sys.stdout.write("\n")
                sys.stdout.flush()
            elif event.extra.get("checkpoint") or event.extra.get("final"):
                print(event.message)
            return
        if kind in ("log", "issue"):
            # LiveDisplay.log() keeps the panel intact (rolling issue history);
            # without a panel there is nothing to corrupt, so print directly.
            if d is not None:
                d.log(event.message)
            else:
                print(event.message)
            return
        if kind == "print":
            if d is not None:
                d.log(event.message)
            else:
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
            d.finish_file(
                event.track, event.result or event.message,
                source_hits=event.source_hits)
        elif kind == "close_display":
            d.close()
