"""
create_wakeup_hook / list_wakeup_hooks / revoke_wakeup_hook - a generic
external-event wakeup for a Hermes session, wrapping the EXISTING self-post
wake contract instead of building a new one.

The primitive this rides on: gateway/wake.py's `_self_post_chat_completion`
already resumes ANY session by POSTing to the local API server with
`X-Hermes-Session-Id: <sid>` - verified live on this instance (see the
plugin's README/skill for the curl proof). That's the real "wake this
session" mechanism Hermes already has; it was only reachable from inside
gateway/cron code, never from an external system (a deploy webhook, a CI
callback, a custom monitoring alert).

This module is the thin, signed wrapper that makes it reachable: a hook is
an HMAC-signed (id, session_id) pair with a TTL. POSTing to its URL runs the
existing self-post against the caller's chosen session - no new delivery
path, no new "session" concept, no event bus.

Storage: this plugin's durable JSON state (ctx.state) - hooks are small and
few, no need for a database.

Serving: a stdlib ThreadingHTTPServer bound to 127.0.0.1 (same pattern as
the share-artifact plugin's store.py). Binding loopback-only by design;
public exposure needs the same one-line reverse-proxy addition documented
in the SKILL.md, done deliberately by the operator, not automatically.
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_HOOK_ID_RE = re.compile(r"^wh_[0-9a-f]{12}$")
_SIGN_RE = re.compile(r"^[0-9a-f]{16,64}$")
_URL_RE = re.compile(r"^/wake/(wh_[0-9a-f]{12})/([0-9a-f]{16,64})/?$")

DEFAULT_MAX_TTL_SECONDS = 24 * 3600
DEFAULT_TTL_SECONDS = 3600


class HookError(RuntimeError):
    pass


class WakeupHookStore:
    """Owns hook bookkeeping ({hook_id: {session_id, expires_at, note}}) via a
    plugin-state-backed dict; HMAC secret persisted the same way."""

    def __init__(self, state):
        self._state = state

    def _secret(self) -> str:
        import secrets as _secrets
        secret = self._state.get("hmac_secret", "")
        if not secret:
            secret = _secrets.token_hex(32)
            self._state.set("hmac_secret", secret)
        return secret

    def _sign(self, hook_id: str) -> str:
        return hmac.new(self._secret().encode(), hook_id.encode(), hashlib.sha256).hexdigest()[:24]

    def verify(self, hook_id: str, sign: str) -> bool:
        return hmac.compare_digest(self._sign(hook_id), sign)

    def create(self, *, session_id: str, ttl_seconds: int, note: str = "") -> dict:
        if not session_id:
            raise HookError("session_id is required")
        ttl_seconds = max(60, min(int(ttl_seconds or DEFAULT_TTL_SECONDS), DEFAULT_MAX_TTL_SECONDS))
        hook_id = f"wh_{uuid.uuid4().hex[:12]}"
        hooks = self._state.get("hooks", {})
        hooks[hook_id] = {
            "session_id": session_id,
            "created_at": time.time(),
            "expires_at": time.time() + ttl_seconds,
            "note": note,
            "fire_count": 0,
        }
        self._state.set("hooks", hooks)
        return {"hook_id": hook_id, "sign": self._sign(hook_id), "expires_at": hooks[hook_id]["expires_at"]}

    def list(self) -> list[dict]:
        hooks = self._state.get("hooks", {})
        now = time.time()
        return [
            {"hook_id": hid, **{k: v for k, v in h.items() if k != "session_id"}, "expired": h["expires_at"] < now,
             "session_id_tail": str(h.get("session_id", ""))[-12:]}
            for hid, h in hooks.items()
        ]

    def revoke(self, hook_id: str) -> bool:
        hooks = self._state.get("hooks", {})
        if hook_id not in hooks:
            return False
        del hooks[hook_id]
        self._state.set("hooks", hooks)
        return True

    def resolve(self, hook_id: str, sign: str) -> Optional[str]:
        """session_id for a valid, unexpired, correctly-signed hook, else None."""
        if not self.verify(hook_id, sign):
            return None
        hooks = self._state.get("hooks", {})
        entry = hooks.get(hook_id)
        if entry is None or entry["expires_at"] < time.time():
            return None
        entry["fire_count"] = entry.get("fire_count", 0) + 1
        entry["last_fired_at"] = time.time()
        hooks[hook_id] = entry
        self._state.set("hooks", hooks)
        return str(entry["session_id"])


def _self_post_wake(*, session_id: str, text: str, host: str, port: int, api_key: str, timeout: float = 600.0) -> None:
    """Fire-and-forget resume of an existing session via the EXISTING
    /v1/chat/completions self-post contract (gateway/wake.py's approach,
    reimplemented minimally here to avoid importing gateway internals from
    a plugin). Runs on its own thread; logs, never raises into the caller."""
    import urllib.request
    import urllib.error

    url = f"http://{host}:{port}/v1/chat/completions"
    payload = json.dumps({
        "model": "hermes-agent",
        "messages": [{"role": "user", "content": text}],
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Hermes-Session-Id": session_id,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            logger.info("wakeup-hook: wake delivered for session %s (HTTP %s)", session_id, resp.status)
    except urllib.error.HTTPError as e:
        logger.warning("wakeup-hook: wake self-post failed for session %s: HTTP %s", session_id, e.code)
    except Exception as e:
        logger.warning("wakeup-hook: wake self-post failed for session %s: %s", session_id, e)


class _Handler(http.server.BaseHTTPRequestHandler):
    store: WakeupHookStore = None  # type: ignore[assignment]
    event_store: Any = None  # EventStore; None disables the event_type branch
    api_host: str = "127.0.0.1"
    api_port: int = 8642
    get_api_key: Any = None  # callable() -> str, resolved lazily (secret may rotate)
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logger.debug("wakeup-hook http: " + fmt, *args)

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        m = _URL_RE.match(self.path.split("?", 1)[0])
        if not m:
            self._send(404, b'{"error":"not found"}')
            return
        hook_id, sign = m.group(1), m.group(2)
        session_id = self.store.resolve(hook_id, sign)
        if session_id is None:
            self._send(403, b'{"error":"invalid, expired, or unknown hook"}')
            return

        length = int(self.headers.get("Content-Length") or 0)
        body_raw = self.rfile.read(length) if length else b""
        default_text = "[External event] A wakeup hook fired for this session."
        text = default_text
        event_type = ""
        event_payload = ""
        if body_raw:
            try:
                parsed = json.loads(body_raw)
                if isinstance(parsed, dict):
                    if isinstance(parsed.get("event_type"), str) and parsed["event_type"].strip():
                        event_type = parsed["event_type"].strip()
                        event_payload = str(parsed.get("payload") or "").strip()[:2000]
                    elif isinstance(parsed.get("text"), str) and parsed["text"].strip():
                        text = f"[External event] {parsed['text'].strip()}"
            except (ValueError, TypeError):
                pass

        # event_type branch: fire an event for ALL sleepers (needs the event store).
        if event_type:
            if self.event_store is None:
                self._send(503, b'{"error":"event layer not available on this instance"}')
                return
            self.event_store.record_event(event_type, event_payload, source=f"webhook:{hook_id}")
            threading.Thread(
                target=self.event_store.scan_once, name="wakeup-hook-event-scan", daemon=True,
            ).start()
            self._send(202, json.dumps({"status": "accepted", "event_type": event_type}).encode())
            return

        api_key = self.get_api_key() if callable(self.get_api_key) else ""
        if not api_key:
            self._send(500, b'{"error":"no API_SERVER_KEY configured on this instance"}')
            return

        threading.Thread(
            target=_self_post_wake, name="wakeup-hook-fire", daemon=True,
            kwargs={"session_id": session_id, "text": text, "host": self.api_host, "port": self.api_port, "api_key": api_key},
        ).start()
        self._send(202, json.dumps({"status": "accepted", "session_id_tail": session_id[-12:]}).encode())


class WakeupHookServer:
    """Owns the daemon ThreadingHTTPServer lifecycle. Idempotent start/stop."""

    def __init__(self, store: WakeupHookStore, *, host: str = "127.0.0.1", port: int, api_host: str, api_port: int, get_api_key, event_store: Any = None):
        self.store, self.host, self.port = store, host, port
        self.api_host, self.api_port, self.get_api_key = api_host, api_port, get_api_key
        self.event_store = event_store
        self._httpd: Optional[http.server.ThreadingHTTPServer] = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> bool:
        if self._httpd is not None:
            return True
        handler = type("_BoundHandler", (_Handler,), {
            "store": self.store, "api_host": self.api_host, "api_port": self.api_port,
            "get_api_key": staticmethod(self.get_api_key),
            "event_store": self.event_store,
        })
        try:
            httpd = http.server.ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as e:
            logger.warning("wakeup-hook: could not bind %s:%s (%s)", self.host, self.port, e)
            return False
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, name="wakeup-hook-http", daemon=True).start()
        self._httpd = httpd
        logger.info("wakeup-hook: serving on %s:%s", self.host, self.port)
        return True

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
