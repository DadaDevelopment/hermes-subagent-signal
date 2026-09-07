"""
In-process checkpoint registry for child->parent progress (subagent_checkpoint
/ read_subagent_checkpoints). Pull-based by design: a running parent turn
cannot safely have new content injected mid-generation, so a child writes
checkpoints and the parent (or a goal judge, or a human polling the UI)
reads them whenever it chooses - same pattern goal.py already uses for
wait_on_pid/wait_on_session (park + poll), not a push into a live turn.

Keyed by subagent_id (the same id delegate_task hands back from spawn/list),
scoped to this process - subagents run as threads in the same interpreter as
their parent (tools/delegate_tool.py), so no IPC is needed.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

_LOCK = threading.Lock()
_CHECKPOINTS: Dict[str, List[Dict[str, Any]]] = {}
_MAX_PER_SUBAGENT = 50
_MAX_TEXT_CHARS = 2000


def add_checkpoint(subagent_id: str, text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS] + " ...[truncated]"
    entry = {"ts": time.time(), "text": text}
    with _LOCK:
        bucket = _CHECKPOINTS.setdefault(subagent_id, [])
        bucket.append(entry)
        if len(bucket) > _MAX_PER_SUBAGENT:
            del bucket[: len(bucket) - _MAX_PER_SUBAGENT]
    return entry


def read_checkpoints(subagent_id: str, *, since_ts: Optional[float] = None) -> List[Dict[str, Any]]:
    with _LOCK:
        bucket = list(_CHECKPOINTS.get(subagent_id, []))
    if since_ts is not None:
        bucket = [c for c in bucket if c["ts"] > since_ts]
    return bucket


def clear_checkpoints(subagent_id: str) -> None:
    with _LOCK:
        _CHECKPOINTS.pop(subagent_id, None)
