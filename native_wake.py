"""Wake delivery for sessions the api_server refuses to drive.

Since the session-authority admission (gateway/session_api_turn.py), POST /v1/chat/completions with
X-Hermes-Session-Id is refused with 409 permission_denied for any session whose source is not
api_server or bot_room - desktop, tui and cli sessions included. Every wake the plugin self-posted
into such a session was lost while its timer still read "fired" (15 of 15 for one desktop
orchestrator, 2026-10-26..29).

Those sessions are woken through Hermes' own /loop instead: a one-shot loop (times=1) due now, which
the desktop/TUI loop scheduler fires as a normal turn - the same path that delivered 58 ticks to a
desktop session on 2026-10-21. A session that already runs a /loop is not overwritten: that loop
keeps ticking, and the wake is reported as not delivered.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

SELF_POST_SOURCES = frozenset({"api_server", "bot_room"})


def session_source(session_id: str) -> Optional[str]:
    """``sessions.source`` of a session id, or None when unknown/unreadable."""
    try:
        from hermes_state import SessionDB
        db = SessionDB(read_only=True)
        try:
            row = db.get_session(session_id)
        finally:
            db.close()
    except Exception:
        logger.debug("native wake: source lookup failed for %s", session_id, exc_info=True)
        return None
    return (row or {}).get("source")


def needs_native_wake(session_id: str) -> bool:
    """True when the api_server would refuse a self-post into this session."""
    source = session_source(session_id)
    return source is not None and source not in SELF_POST_SOURCES


def arm_one_shot_loop(session_id: str, text: str) -> tuple[bool, str]:
    """Arm a native one-shot /loop that fires ``text`` as the session's next turn.

    Returns (armed, detail). Refuses to replace an active or paused /loop.
    """
    from hermes_cli.loops import LoopManager, save_loop
    mgr = LoopManager(session_id)
    if mgr.has_loop():
        state = mgr.state
        return False, (f"session already runs a /loop ({state.status}, {state.ticks_fired} ticks): "
                       "not overwritten, that loop keeps ticking")
    state = mgr.set(text, interval_seconds=60, times=1)
    state.next_due_at = time.time()
    save_loop(session_id, state)
    return True, "armed native one-shot /loop (due now)"
