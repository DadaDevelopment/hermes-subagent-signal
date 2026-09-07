"""Model-facing tools for the primitive event layer (events.py)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _resolve_session(args: dict, kw: dict) -> str:
    sid = str(args.get("session_id") or "").strip()
    if sid and sid.lower() not in ("current", "this"):
        return sid
    return str(kw.get("session_id") or "")


def _make_schedule_wakeup(ctx):
    def _handler(args: dict, **kw: Any) -> str:
        from .events import MAX_SECONDS

        seconds = args.get("seconds")
        try:
            seconds = int(seconds)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return "Error: 'seconds' is required (integer, how long to sleep)."
        if seconds < 1 or seconds > MAX_SECONDS:
            return f"Error: 'seconds' must be 1..{MAX_SECONDS}."
        session_id = _resolve_session(args, kw)
        if not session_id:
            return "Error: could not resolve a session_id (call from a session, or pass session_id)."
        note = str(args.get("note") or "").strip()

        store = ctx.events_store
        timer = store.add_timer(session_id, fire_at=time.time() + seconds, note=note)
        import time as _t
        return (
            f"Wakeup scheduled: timer '{timer['timer_id']}' fires in {seconds}s "
            f"(at {_t.strftime('%H:%M:%S', _t.localtime(timer['fire_at']))}).\n"
            "This session will receive a [Scheduled wakeup] turn then. Persistent: "
            "survives restarts; cancel with cancel_pending."
        )

    import time
    return _handler


def _make_sleep_until_event(ctx):
    def _handler(args: dict, **kw: Any) -> str:
        from .events import DEFAULT_WAIT_SECONDS, MAX_SECONDS

        event_type = str(args.get("event_type") or "").strip()
        if not event_type:
            return (
                "Error: 'event_type' is required, e.g. 'subagent.completed', 'deploy.finished', "
                "'file.uploaded'. Fire it later with fire_event."
            )
        timeout_seconds = args.get("timeout_seconds")
        try:
            timeout_seconds = int(timeout_seconds) if timeout_seconds is not None else DEFAULT_WAIT_SECONDS  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return "Error: 'timeout_seconds' must be an integer."
        if timeout_seconds < 1 or timeout_seconds > MAX_SECONDS:
            return f"Error: 'timeout_seconds' must be 1..{MAX_SECONDS}."
        session_id = _resolve_session(args, kw)
        if not session_id:
            return "Error: could not resolve a session_id."
        note = str(args.get("note") or "").strip()

        wait = ctx.events_store.add_wait(session_id, event_type, timeout_seconds, note)
        import time as _t
        return (
            f"Parked until event '{event_type}' (wait '{wait['wait_id']}', "
            f"expires in {timeout_seconds}s at {_t.strftime('%H:%M:%S', _t.localtime(wait['expires_at']))}).\n"
            "Stop working now - do not poll. When ANY session (or an external integration "
            "via fire_event/the /wake webhook) fires that event, this session gets a new "
            "turn with the payload. If nothing fires before expiry you get a "
            "[Wait expired] turn instead. The session consumes zero inference while parked."
        )

    return _handler


def _make_fire_event(ctx):
    def _handler(args: dict, **kw: Any) -> str:
        event_type = str(args.get("event_type") or "").strip()
        if not event_type:
            return "Error: 'event_type' is required."
        payload = str(args.get("payload") or "").strip()[:2000]
        source = str(kw.get("session_id") or "external")
        ctx.events_store.record_event(event_type, payload, source)
        return (
            f"Event '{event_type}' recorded. Any session parked on it via sleep_until_event "
            "is woken on the next scheduler scan (~1s)."
        )

    return _handler


def _make_cancel_pending(ctx):
    def _handler(args: dict, **_: Any) -> str:
        target = str(args.get("target") or "").strip()
        if not target:
            return "Error: 'target' is required (a timer id like tm_..., or an event_type to cancel its waits)."
        return ctx.events_store.cancel(target)

    return _handler


_SCHEMAS: dict[str, dict] = {
    "schedule_wakeup": {
        "type": "function",
        "function": {
            "name": "schedule_wakeup",
            "description": (
                "Schedule a future turn for this (or another) session: after 'seconds' "
                "elapse the session receives a '[Scheduled wakeup]' turn. Durable - "
                "survives gateway restarts; fires even if this conversation ended. Use "
                "for time-based continuation ('check back in 10 minutes', 'retry the "
                "deploy in 30m') instead of sleeping or polling. This is the generic "
                "primitive /loop is a convenience wrapper over."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "integer", "description": "Delay before the wakeup turn (1..86400)."},
                    "note": {"type": "string", "description": "Optional note delivered with the wakeup turn."},
                    "session_id": {"type": "string", "description": "Target session. Omit for the current one."},
                },
                "required": ["seconds"],
            },
        },
    },
    "sleep_until_event": {
        "type": "function",
        "function": {
            "name": "sleep_until_event",
            "description": (
                "Park this session until a named event fires (via fire_event, the "
                "subagent_stop hook, or an external integration). Zero inference while "
                "parked. The generic primitive behind goal-wait semantics - use this "
                "instead of polling loops. ALWAYS stop working after calling it; the "
                "event arrival IS your next turn. A timeout expiry also wakes you "
                "(marked [Wait expired]) so a never-firing event cannot strand the "
                "session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_type": {"type": "string", "description": "Event to wait for, e.g. 'subagent.completed' or a custom 'deploy.finished'."},
                    "timeout_seconds": {"type": "integer", "description": "Give up after this long (default 3600, max 86400). Expiry wakes the session."},
                    "note": {"type": "string", "description": "Optional context delivered with either wake."},
                    "session_id": {"type": "string", "description": "Target session. Omit for the current one."},
                },
                "required": ["event_type"],
            },
        },
    },
    "fire_event": {
        "type": "function",
        "function": {
            "name": "fire_event",
            "description": (
                "Fire a named event, waking every session parked on it via "
                "sleep_until_event. Use to signal completion across sessions (a child "
                "waking its parent, one agent handing work to another) or to inject "
                "external facts into a parked session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_type": {"type": "string", "description": "Event name, e.g. 'deploy.finished'. Must match what a sleeper waits on."},
                    "payload": {"type": "string", "description": "Short payload delivered to the woken session(s)."},
                },
                "required": ["event_type"],
            },
        },
    },
    "cancel_pending": {
        "type": "function",
        "function": {
            "name": "cancel_pending",
            "description": "Cancel a scheduled wakeup (by timer id) or all waits on an event type (by event_type).",
            "parameters": {
                "type": "object",
                "properties": {"target": {"type": "string", "description": "Timer id (tm_...) or event_type."}},
                "required": ["target"],
            },
        },
    },
}


def register_event_tools(ctx) -> None:
    try:
        for name, emoji in (
            ("schedule_wakeup", "\u23f0"), ("sleep_until_event", "\U0001f6d1"),
            ("fire_event", "\U0001f525"), ("cancel_pending", "\U0001f5d1"),
        ):
            ctx.register_tool(
                name=name,
                toolset="subagent-signal",
                schema=_SCHEMAS[name],
                handler=_HANDLERS[name](ctx),
                description=_SCHEMAS[name]["function"]["description"],
                emoji=emoji,
            )
    except Exception:
        logger.warning("subagent-signal: failed to register event tools", exc_info=True)


_HANDLERS: dict[str, Any] = {
    "schedule_wakeup": _make_schedule_wakeup,
    "sleep_until_event": _make_sleep_until_event,
    "fire_event": _make_fire_event,
    "cancel_pending": _make_cancel_pending,
}
