"""
subagent_checkpoint / read_subagent_checkpoints - the missing child->parent
progress channel for delegate_task subagents.

Existing primitive this rides on: every subagent's terminal/tool task_id IS
its subagent_id (tools/delegate_tool_child_run.py: `self.child_task_id =
self.subagent_id`, and child.run_conversation(task_id=self.child_task_id)).
The normal tool-dispatch path (model_tools._run_tool_execution ->
registry.dispatch) already passes task_id/session_id into every handler's
kwargs - so a checkpoint tool running INSIDE a spawned child can identify
itself for free, no parent_agent plumbing required.

Pull-based on purpose: a running parent turn cannot safely have new content
spliced into a live generation. The parent (or a /goal judge, a human
polling delegate_task(action="list"), or the desktop UI) reads checkpoints
whenever it chooses - the exact same park+poll shape hermes_cli.goals'
wait_on_pid/wait_on_session already uses, just generalized to "what has my
child told me so far" instead of "has this process/pid exited".
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _subagent_id_from_kwargs(kw: dict) -> str:
    """The calling child's own subagent_id, or ''. Children get task_id ==
    subagent_id (see module docstring); a bare 'sa-' prefix check keeps this
    from misfiring if a non-delegated caller (rare direct registry use)
    passes some other task_id shape."""
    task_id = str(kw.get("task_id") or "")
    return task_id if task_id.startswith("sa-") else ""


def _make_subagent_checkpoint():
    def _handler(args: dict, **kw: Any) -> str:
        from .checkpoints import add_checkpoint

        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return "Error: 'text' is required (a short progress note)."
        subagent_id = _subagent_id_from_kwargs(kw)
        if not subagent_id:
            return (
                "Error: subagent_checkpoint can only be called from inside a delegate_task "
                "subagent (no subagent_id resolvable for this call)."
            )
        add_checkpoint(subagent_id, text.strip())
        return "Checkpoint recorded. The parent can read it any time via read_subagent_checkpoints."

    return _handler


def _make_read_subagent_checkpoints():
    def _handler(args: dict, **kw: Any) -> str:
        from .checkpoints import read_checkpoints

        subagent_id = str(args.get("subagent_id") or "").strip()
        if not subagent_id:
            return "Error: 'subagent_id' is required (from delegate_task's spawn response or action='list')."
        since_ts = args.get("since_ts")
        try:
            since = float(since_ts) if since_ts is not None else None
        except (TypeError, ValueError):
            since = None
        entries = read_checkpoints(subagent_id, since_ts=since)
        if not entries:
            return f"No checkpoints yet for subagent '{subagent_id}'."
        lines = [f"Checkpoints for '{subagent_id}' ({len(entries)}):"]
        for e in entries:
            lines.append(f"  [{e['ts']:.0f}] {e['text']}")
        return "\n".join(lines)

    return _handler


_SCHEMAS: dict[str, dict] = {
    "subagent_checkpoint": {
        "type": "function",
        "function": {
            "name": "subagent_checkpoint",
            "description": (
                "Report a short progress note while you (a delegated subagent) are still "
                "working, WITHOUT ending your task. Use this for a long-running delegation "
                "so the parent (or a /goal judge watching you) can see you're alive and "
                "what stage you're at, instead of only finding out when you finish. Call it "
                "sparingly - a few times per task at real milestones, not every tool call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Short progress note (e.g. 'finished data collection, starting analysis')."},
                },
                "required": ["text"],
            },
        },
    },
    "read_subagent_checkpoints": {
        "type": "function",
        "function": {
            "name": "read_subagent_checkpoints",
            "description": (
                "Read progress checkpoints a running (or finished) subagent has reported via "
                "subagent_checkpoint. Use this to check on a long-running delegated task "
                "without waiting for its final result - e.g. after delegate_task(action='list') "
                "shows a child still running."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subagent_id": {"type": "string", "description": "The subagent id from delegate_task's spawn response or action='list'."},
                    "since_ts": {"type": "number", "description": "Optional: only checkpoints reported after this unix timestamp."},
                },
                "required": ["subagent_id"],
            },
        },
    },
}


def register_checkpoint_tools(ctx) -> None:
    try:
        ctx.register_tool(
            name="subagent_checkpoint",
            toolset="subagent-signal",
            schema=_SCHEMAS["subagent_checkpoint"],
            handler=_make_subagent_checkpoint(),
            description=_SCHEMAS["subagent_checkpoint"]["function"]["description"],
            emoji="\U0001f4cd",
        )
        ctx.register_tool(
            name="read_subagent_checkpoints",
            toolset="subagent-signal",
            schema=_SCHEMAS["read_subagent_checkpoints"],
            handler=_make_read_subagent_checkpoints(),
            description=_SCHEMAS["read_subagent_checkpoints"]["function"]["description"],
            emoji="\U0001f4cb",
        )
    except Exception:
        logger.warning("subagent-signal: failed to register checkpoint tools", exc_info=True)
