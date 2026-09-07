"""
subagent-signal plugin for Hermes Agent.

Primitive layer first: schedule_wakeup / sleep_until_event / fire_event /
cancel_pending are DURABLE, cross-process event primitives. /loop (native
schedule-wakeups UX) and /goal's wait barriers are sibling consumers of the
same underlying wake mechanism (the api_server session self-post) - this
plugin exposes the mechanism itself as first-class tools instead of
duplicating any of it.

Plus the two audit gaps closed earlier (kept unchanged):
1. Child -> parent live progress: subagent_checkpoint / read_subagent_checkpoints.
2. External-event webhook wake: create_wakeup_hook / list_wakeup_hooks /
   revoke_wakeup_hook (signed, TTL'd, wraps the existing self-post).

An auto-fire wiring rides on the native `subagent_stop` lifecycle hook:
when a delegation batch finishes, the plugin records a
`subagent.completed` event, so a parent parked via
sleep_until_event("subagent.completed") wakes automatically. Zero core
edits - everything goes through ctx.register_tool / ctx.register_hook /
ctx.state.

Storage: plugin durable state (shared across gateway/dashboard/CLI
processes) + O_EXCL claim files for exactly-once wake delivery across
those processes.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["register"]


def _resolve_api_port(ctx) -> int:
    try:
        return int(ctx.get_config("api_server_port", 8642) or 8642)
    except (TypeError, ValueError):
        return 8642


def _make_post_wake(ctx):
    def _post_wake(*, session_id: str, text: str) -> None:
        import threading
        from .wakeup_hooks import _self_post_wake

        api_key = ""
        try:
            from .wakeup_tools import _api_key
            api_key = _api_key()
        except Exception:
            logger.debug("event wake: no API key resolvable", exc_info=True)
        if not api_key or not session_id:
            logger.warning("event wake skipped (no api key or session id)")
            return
        threading.Thread(
            target=_self_post_wake, name="subagent-signal-wake", daemon=True,
            kwargs={"session_id": session_id, "text": text, "host": "127.0.0.1",
                    "port": _resolve_api_port(ctx), "api_key": api_key},
        ).start()

    return _post_wake


def _register_subagent_stop_hook(ctx) -> None:
    """Fire the `subagent.completed` event when any delegation batch in this
    process finishes, so sleep_until_event('subagent.completed') parents wake."""

    def _on_subagent_stop(**kwargs) -> None:
        try:
            parent_sid = str(kwargs.get("parent_session_id") or "")
            if not parent_sid:
                return
            status = str(kwargs.get("child_status") or "")
            summary = str(kwargs.get("child_summary") or "")[:500]
            ctx.events_store.record_event(
                "subagent.completed",
                f"delegation finished (status={status or 'unknown'}): {summary}",
                source=parent_sid,
            )
        except Exception:
            logger.debug("subagent_stop event recording failed", exc_info=True)

    try:
        ctx.register_hook("subagent_stop", _on_subagent_stop)
    except Exception:
        logger.warning("subagent-signal: could not register subagent_stop hook", exc_info=True)


def register(ctx) -> None:
    from .checkpoint_tools import register_checkpoint_tools
    from .event_tools import register_event_tools
    from .events import EventScheduler, EventStore
    from .wakeup_hooks import WakeupHookServer
    from .wakeup_tools import register_wakeup_tools, start_wakeup_server, _settings as _wakeup_settings

    register_checkpoint_tools(ctx)
    register_event_tools(ctx)
    register_wakeup_tools(ctx)

    try:
        ctx.events_store = EventStore(ctx.state, ctx.state.data_dir, _make_post_wake(ctx))
    except Exception:
        logger.warning("subagent-signal: failed to build event store", exc_info=True)
        ctx.events_store = None

    if ctx.events_store is not None:
        try:
            scheduler = EventScheduler(ctx.events_store)
            scheduler.start()
            ctx.events_scheduler = scheduler
        except Exception:
            logger.warning("subagent-signal: failed to start event scheduler", exc_info=True)
        _register_subagent_stop_hook(ctx)

    start_wakeup_server(ctx)
