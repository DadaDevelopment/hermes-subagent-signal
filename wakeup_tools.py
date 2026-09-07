"""Model-facing tools for the wakeup-hook subsystem (see wakeup_hooks.py for the mechanism)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 8792
_SERVER: "Any" = None


def _settings(ctx) -> dict:
    return {
        "port": int(ctx.get_config("wakeup_port", _DEFAULT_PORT) or _DEFAULT_PORT),
        "base_url": (ctx.get_config("base_url", "") or _dashboard_public_url()).rstrip("/"),
        "api_port": int(ctx.get_config("api_server_port", 8642) or 8642),
    }


def _dashboard_public_url() -> str:
    import os
    return os.getenv("HERMES_DASHBOARD_PUBLIC_URL", "") or "http://127.0.0.1"


def _api_key() -> str:
    try:
        from agent.secret_scope import get_secret
        return get_secret("API_SERVER_KEY", "") or ""
    except Exception:
        logger.debug("wakeup-hook: could not resolve API_SERVER_KEY", exc_info=True)
        return ""


def _get_store(ctx):
    from .wakeup_hooks import WakeupHookStore
    return WakeupHookStore(ctx.state)


def _get_server(ctx):
    global _SERVER
    from .wakeup_hooks import WakeupHookServer

    if _SERVER is not None and _SERVER.running:
        return _SERVER
    cfg = _settings(ctx)
    store = _get_store(ctx)
    _SERVER = WakeupHookServer(
        store, host="127.0.0.1", port=cfg["port"], api_host="127.0.0.1", api_port=cfg["api_port"],
        get_api_key=_api_key,
    )
    _SERVER.start()
    return _SERVER


def _hook_url(ctx, hook_id: str, sign: str) -> str:
    cfg = _settings(ctx)
    return f"{cfg['base_url']}/wake/{hook_id}/{sign}/"


def _resolve_session_id(ctx, requested: str) -> str:
    """Explicit session_id, or the CURRENT session when omitted/'current'."""
    if requested and requested.strip().lower() not in ("", "current", "this"):
        return requested.strip()
    return ""  # filled by the caller from kw["session_id"] when available


def _make_create_wakeup_hook(ctx):
    def _handler(args: dict, **kw: Any) -> str:
        from .wakeup_hooks import HookError

        session_id = _resolve_session_id(ctx, str(args.get("session_id") or ""))
        if not session_id:
            session_id = str(kw.get("session_id") or "")
        if not session_id:
            return "Error: could not resolve a session_id (pass one explicitly, or call this from a session that has one)."

        ttl_seconds = args.get("ttl_seconds")
        note = str(args.get("note") or "").strip()
        _get_server(ctx)  # ensure the local server is up before handing out a URL
        store = _get_store(ctx)
        try:
            hook = store.create(session_id=session_id, ttl_seconds=int(ttl_seconds) if ttl_seconds else 3600, note=note)
        except HookError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.warning("wakeup-hook: create failed", exc_info=True)
            return f"Error: {e}"

        url = _hook_url(ctx, hook["hook_id"], hook["sign"])
        import time as _time
        expires_in = int(hook["expires_at"] - _time.time())
        return (
            f"Wakeup hook created for this session.\n"
            f"URL: {url}\n"
            f"Hook ID: {hook['hook_id']}\n"
            f"Expires in: {expires_in}s\n\n"
            "POST to this URL (optionally with JSON body {\"text\": \"...\"}) from any external "
            "system (CI callback, deploy webhook, monitoring alert, cron) to wake this exact "
            "session with that text as the next turn. The URL itself is the access control - "
            "share it only with systems that should be able to wake this session."
        )

    return _handler


def _make_list_wakeup_hooks(ctx):
    def _handler(args: dict | None = None, **_: Any) -> str:
        store = _get_store(ctx)
        hooks = store.list()
        if not hooks:
            return "No wakeup hooks."
        lines = [f"Wakeup hooks ({len(hooks)}):"]
        for h in hooks:
            status = "expired" if h["expired"] else "active"
            lines.append(
                f"  - {h['hook_id']} ({status}, session ...{h['session_id_tail']}, "
                f"fired {h.get('fire_count', 0)}x, note: {h.get('note') or '(none)'})"
            )
        return "\n".join(lines)

    return _handler


def _make_revoke_wakeup_hook(ctx):
    def _handler(args: dict, **_: Any) -> str:
        hook_id = str(args.get("hook_id") or "").strip()
        if not hook_id:
            return "Error: 'hook_id' is required."
        store = _get_store(ctx)
        if store.revoke(hook_id):
            return f"Revoked wakeup hook '{hook_id}'."
        return f"No wakeup hook found with id '{hook_id}'."

    return _handler


_SCHEMAS: dict[str, dict] = {
    "create_wakeup_hook": {
        "type": "function",
        "function": {
            "name": "create_wakeup_hook",
            "description": (
                "Create a signed webhook URL that, when POSTed to by any external system "
                "(CI pipeline, deploy hook, monitoring alert, custom script), wakes THIS "
                "session with a new turn - the generic external-event equivalent of /goal's "
                "wait_on_pid/wait_on_session, for triggers that live outside this Hermes "
                "process entirely. Use this before going idle/parking on an external event "
                "you don't control (e.g. 'ping me when the deploy webhook fires') instead of "
                "polling in a loop."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session to wake. Omit or pass 'current' to target this session.",
                    },
                    "ttl_seconds": {
                        "type": "integer",
                        "description": f"How long the hook stays valid, in seconds (default 3600, max {24*3600}).",
                    },
                    "note": {"type": "string", "description": "Optional label to recognize this hook later in list_wakeup_hooks."},
                },
                "required": [],
            },
        },
    },
    "list_wakeup_hooks": {
        "type": "function",
        "function": {
            "name": "list_wakeup_hooks",
            "description": "List wakeup hooks created in this profile (id, status, target session, fire count).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "revoke_wakeup_hook": {
        "type": "function",
        "function": {
            "name": "revoke_wakeup_hook",
            "description": "Revoke a wakeup hook by id, immediately invalidating its URL.",
            "parameters": {
                "type": "object",
                "properties": {"hook_id": {"type": "string", "description": "The hook id from create_wakeup_hook or list_wakeup_hooks."}},
                "required": ["hook_id"],
            },
        },
    },
}


def register_wakeup_tools(ctx) -> None:
    try:
        ctx.register_tool(
            name="create_wakeup_hook", toolset="subagent-signal", schema=_SCHEMAS["create_wakeup_hook"],
            handler=_make_create_wakeup_hook(ctx), description=_SCHEMAS["create_wakeup_hook"]["function"]["description"],
            emoji="\U0001f514",
        )
        ctx.register_tool(
            name="list_wakeup_hooks", toolset="subagent-signal", schema=_SCHEMAS["list_wakeup_hooks"],
            handler=_make_list_wakeup_hooks(ctx), description=_SCHEMAS["list_wakeup_hooks"]["function"]["description"],
            emoji="\U0001f4cb",
        )
        ctx.register_tool(
            name="revoke_wakeup_hook", toolset="subagent-signal", schema=_SCHEMAS["revoke_wakeup_hook"],
            handler=_make_revoke_wakeup_hook(ctx), description=_SCHEMAS["revoke_wakeup_hook"]["function"]["description"],
            emoji="\U0001f5d1",
        )
    except Exception:
        logger.warning("subagent-signal: failed to register wakeup-hook tools", exc_info=True)


def start_wakeup_server(ctx) -> None:
    try:
        _get_server(ctx)
    except Exception:
        logger.warning("subagent-signal: failed to start wakeup-hook server", exc_info=True)
