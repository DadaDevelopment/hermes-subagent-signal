"""
subagent-signal plugin for Hermes Agent.

Closes the two gaps a live audit of /opt/hermes found in the existing
orchestration stack (delegate_task, goals.py's wait_on_pid/wait_on_session,
gateway/wake.py's session self-post) - NOT a reimplementation of any of it:

1. Child -> parent live progress (subagent_checkpoint / read_subagent_checkpoints).
   delegate_task already relays child progress events upward for DISPLAY
   (tools/delegate_tool_progress.py's _ChildProgressRelay), but there was no
   tool a model could call to explicitly checkpoint, and no tool to read
   checkpoints back. Pull-based, in-process (see checkpoints.py).

2. A generic external-event wakeup (create_wakeup_hook / list_wakeup_hooks /
   revoke_wakeup_hook). Hermes already resumes any session via a self-POST
   to /v1/chat/completions with X-Hermes-Session-Id (gateway/wake.py) -
   verified live on this instance - but that path was only reachable from
   inside gateway/cron code. This wraps it in a signed, TTL'd webhook any
   external system can POST to (see wakeup_hooks.py).

Zero core edits - everything goes through ctx.register_tool() plus a small
local HTTP server (127.0.0.1-only, same pattern as the share-artifact
plugin), fronted by whatever reverse proxy already serves this instance.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["register"]


def register(ctx) -> None:
    from .checkpoint_tools import register_checkpoint_tools
    from .wakeup_tools import register_wakeup_tools, start_wakeup_server

    register_checkpoint_tools(ctx)
    register_wakeup_tools(ctx)
    start_wakeup_server(ctx)
