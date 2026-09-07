"""
Durable event/wakeup primitives: schedule_wakeup, sleep_until_event,
fire_event. Primary layer - /loop and /goal's wait barriers are sibling
consumers of the same wake mechanism (the api_server session self-post),
not the other way around.

Design:
- Records (timers, waits, fired events) live in the plugin's durable JSON
  state (ctx.state): shared across ALL processes loading this plugin
  (gateway, dashboard, CLI one-shots) through the profile-scoped
  plugin-data dir with per-write file locking.
- Every process runs a lightweight scheduler thread scanning due timers,
  expired waits and undelivered events. Exactly-once delivery across
  processes is by O_EXCL claim files under data_dir/claims: the process
  that atomically creates the claim performs the self-post; losers skip.
- Delivery reuses the verified session-wake contract: POST
  /v1/chat/completions with X-Hermes-Session-Id (same path
  gateway/wake.py uses). Works on any SessionDB session id regardless of
  which platform created it; the continuation runs in the gateway's
  api_server platform (reply lands in desktop/API surfaces, not the
  origin chat - documented caveat for Telegram-origin sessions).
- subagent_stop hook auto-fires the "subagent.completed" event type, so
  sleep_until_event("subagent.completed") parks a parent until its
  delegation batch finishes (child -> parent completion wake).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

MAX_SECONDS = 24 * 3600
DEFAULT_WAIT_SECONDS = 3600
_SCAN_INTERVAL = 1.0
_PRUNE_KEEP = 100


class EventStore:
    """Durable records + claim-file exactly-once fire for the event layer."""

    def __init__(self, state, data_dir, post_wake):
        self._state = state
        self._claims_dir = data_dir / "claims"
        self._claims_dir.mkdir(parents=True, exist_ok=True)
        self._post_wake = post_wake

    def _get(self, key: str, default):
        return self._state.get(key, default)

    def _set(self, key: str, value):
        self._state.set(key, value)

    # -- timers ---------------------------------------------------------

    def add_timer(self, session_id: str, fire_at: float, note: str) -> dict:
        timer_id = f"tm_{time.time_ns():x}"[-14:]
        timers = self._get("timers", {})
        timers[timer_id] = {
            "session_id": session_id, "fire_at": fire_at, "note": note,
            "status": "scheduled", "created_at": time.time(),
        }
        self._set("timers", timers)
        return {"timer_id": timer_id, "fire_at": fire_at}

    def cancel(self, target: str) -> str:
        timers = self._get("timers", {})
        if target in timers and timers[target]["status"] == "scheduled":
            timers[target]["status"] = "cancelled"
            self._set("timers", timers)
            return f"Cancelled timer '{target}'."
        waits = self._get("waits", {})
        removed = []
        for wid, w in list(waits.items()):
            if w["session_id"] and (w.get("event_type") == target or wid == target):
                removed.append(wid)
                del waits[wid]
        if removed:
            self._set("waits", waits)
            return f"Cancelled {len(removed)} wait(s) on '{target}'."
        return f"Nothing found for '{target}' (timer ids look like tm_..., waits cancel by event_type)."

    # -- waits ----------------------------------------------------------

    def add_wait(self, session_id: str, event_type: str, timeout_seconds: int, note: str) -> dict:
        wait_id = f"w_{time.time_ns():x}"[-14:]
        waits = self._get("waits", {})
        waits[wait_id] = {
            "session_id": session_id, "event_type": event_type, "note": note,
            "expires_at": time.time() + max(1, timeout_seconds), "created_at": time.time(),
        }
        self._set("waits", waits)
        return {"wait_id": wait_id, "event_type": event_type, "expires_at": waits[wait_id]["expires_at"]}

    def remove_wait(self, wait_id: str) -> None:
        waits = self._get("waits", {})
        if wait_id in waits:
            del waits[wait_id]
            self._set("waits", waits)

    # -- events ---------------------------------------------------------

    def record_event(self, event_type: str, payload: str, source: str) -> str:
        event_id = f"e_{time.time_ns():x}"[-14:]
        events = self._get("events", {})
        events[event_id] = {
            "event_type": event_type, "payload": payload, "source": source,
            "created_at": time.time(), "status": "pending",
        }
        self._set("events", events)
        return event_id

    def mark_event_delivered(self, event_id: str) -> None:
        events = self._get("events", {})
        if event_id in events:
            events[event_id]["status"] = "delivered"
            self._set("events", events)

    def _sessions_for_event(self, event_type: str) -> list[dict]:
        waits = self._get("waits", {})
        out = []
        for wid, w in list(waits.items()):
            if w.get("event_type") == event_type:
                out.append({"wait_id": wid, "session_id": w["session_id"], "note": w.get("note", "")})
        return out

    # -- claim-file exactly-once -----------------------------------------

    def _claim(self, *parts: str) -> bool:
        digest = hashlib.sha256(":".join(parts).encode()).hexdigest()[:32]
        path = self._claims_dir / digest
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(time.time()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            return False
        except OSError:
            return True

    # -- the scan loop body ----------------------------------------------

    def scan_once(self) -> int:
        """Fire everything due. Returns the number of wake posts performed."""
        fired = 0
        now = time.time()

        timers = self._get("timers", {})
        for tid, t in list(timers.items()):
            if t["status"] != "scheduled" or t["fire_at"] > now:
                continue
            if not self._claim("timer", tid):
                continue
            t["status"] = "fired"
            timers[tid] = t
            self._set("timers", timers)
            text = "[Scheduled wakeup]" + (f" {t['note']}" if t.get("note") else "")
            self._post_wake(session_id=t["session_id"], text=text)
            fired += 1

        events = self._get("events", {})
        for eid, e in list(events.items()):
            if e["status"] != "pending":
                continue
            for w in self._sessions_for_event(e["event_type"]):
                if not self._claim("event", eid, w["session_id"]):
                    continue
                self.remove_wait(w["wait_id"])
                text = f"[Event: {e['event_type']}]" + (f" {e['payload']}" if e.get("payload") else "")
                self._post_wake(session_id=w["session_id"], text=text)
                fired += 1
            e["status"] = "delivered"
            events[eid] = e
            self._set("events", events)

        waits = self._get("waits", {})
        for wid, w in list(waits.items()):
            if w["expires_at"] > now:
                continue
            if not self._claim("wait-expiry", wid):
                continue
            self.remove_wait(wid)
            text = (
                f"[Wait expired: {w.get('event_type', '?')}]" +
                (f" {w['note']}" if w.get("note") else " Nothing fired before the timeout.")
            )
            self._post_wake(session_id=w["session_id"], text=text)
            fired += 1

        if fired:
            self._prune()
        return fired

    def _prune(self) -> None:
        for key in ("timers", "events"):
            records = self._get(key, {})
            if len(records) > _PRUNE_KEEP:
                keep = sorted(records.items(), key=lambda kv: kv[1].get("created_at", 0))[-_PRUNE_KEEP:]
                self._set(key, dict(keep))


class EventScheduler:
    """One daemon scan thread per process. Idempotent start; claims make
    concurrent scans across processes safe."""

    def __init__(self, store: EventStore):
        self._store = store
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        def _loop():
            while not self._stop.wait(_SCAN_INTERVAL):
                try:
                    self._store.scan_once()
                except Exception:
                    logger.debug("event scan failed", exc_info=True)

        self._thread = threading.Thread(target=_loop, name="subagent-signal-events", daemon=True)
        self._thread.start()
        logger.info("subagent-signal: event scheduler running")

    def stop(self) -> None:
        self._stop.set()
