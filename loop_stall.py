"""Stalled /loop recovery: a watchdog that fires loop ticks whose owner died.

Why this exists (live incident, 2026-09-13): a /loop set from a DESKTOP
session has ``route: {}`` in its persisted state, so the gateway's idle
loop-wakeup watcher skips it (``run_goals.py``: "CLI / TUI-owned loop — their
own schedulers drive it"). Its real driver is the session-owner process's
per-session poller (session_notifications.py). When that process is killed
without the session finalizing (gateway SIGKILL, container restart), the
persisted loop stays ``active`` with ``next_due_at`` in the past and nobody
scanning — the loop silently stalls until the session is reopened.

This watchdog closes that gap with the plugin's existing wake contract:
scan SessionDB ``loop:*`` state rows (read-only), find ACTIVE loops overdue
beyond a stall margin (the owner's own poller fires within ~5s of due, so
overdue-by-minutes means the owner is gone), self-post a recovery wake via
the api_server session self-post. The gateway's post-turn loop-completion
hook then reschedules the tick as usual; ``awaiting_response`` guards double
fire across this watchdog and a returning owner.
"""

from __future__ import annotations

import logging
import time
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_STALL_MARGIN_SECONDS = 20 * 60
_SCAN_EVERY_SECONDS = 60.0


def stalled_loops(margin_seconds: int) -> List[dict]:
    """ACTIVE loops overdue by more than *margin_seconds*: [{session_id, state}]."""
    try:
        from hermes_cli.loops import list_active_loops
        active = list_active_loops()
    except Exception:
        logger.debug("loop-stall scan: list_active_loops unavailable", exc_info=True)
        return []
    now = time.time()
    out = []
    for session_id, state in active:
        due = float(getattr(state, "next_due_at", 0) or 0)
        if not due or now - due < margin_seconds:
            continue
        if getattr(state, "awaiting_response", False):
            continue
        out.append({
            "session_id": session_id,
            "ticks_fired": getattr(state, "ticks_fired", 0),
            "overdue_seconds": int(now - due),
            "interval_seconds": getattr(state, "interval_seconds", 0),
        })
    return out


class LoopStallWatcher:
    """Periodically scans for stalled loops and wakes their session once."""

    def __init__(self, event_store, *, stall_margin_seconds: int = DEFAULT_STALL_MARGIN_SECONDS,
                 scan_every_seconds: float = _SCAN_EVERY_SECONDS):
        self._store = event_store
        self._margin = max(60, int(stall_margin_seconds))
        self._every = max(5.0, float(scan_every_seconds))
        self._stop: Optional[Any] = None
        self._thread = None
        self._last_scan = 0.0

    def start_own_thread(self) -> None:
        import threading

        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()

        def _loop():
            while not self._stop.wait(self._every):
                try:
                    self.scan_once()
                except Exception:
                    logger.debug("loop-stall scan failed", exc_info=True)

        self._thread = threading.Thread(target=_loop, name="subagent-signal-loop-stall", daemon=True)
        self._thread.start()
        logger.info("subagent-signal: loop-stall watchdog running (margin %ss)", self._margin)

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()

    def scan_once(self) -> int:
        now = time.monotonic()
        if now - self._last_scan < self._every:
            return 0
        self._last_scan = now
        victims = stalled_loops(self._margin)
        fired = 0
        for v in victims:
            claim_key = f"loop-stall:{v['session_id']}:{v['overdue_seconds'] // self._margin}"
            if not self._store._claim(claim_key, "watchdog"):
                continue
            overdue_h = v["overdue_seconds"] / 3600.0
            text = (
                f"[Loop recovery] This session's /loop stalled: its driver process died while the "
                f"loop was waiting (tick #{v['ticks_fired'] + 1} was due ~{overdue_h:.1f}h ago and no "
                f"owner polled it). You are the recovery wake. Continue the loop's recurring task now; "
                f"the normal wakeup cadence resumes after this turn. If the recurring task is finished, "
                f"end your reply with LOOP_COMPLETE on its own line."
            )
            try:
                self._store._post_wake(session_id=v["session_id"], text=text)
                fired += 1
                logger.warning(
                    "loop-stall watchdog: recovered session %s (overdue %.1fh, ticks %s)",
                    v["session_id"], overdue_h, v["ticks_fired"],
                )
            except Exception:
                logger.debug("loop-stall wake failed for %s", v["session_id"], exc_info=True)
        return fired
