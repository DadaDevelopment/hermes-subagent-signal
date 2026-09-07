# hermes-subagent-signal

Hermes Agent plugin closing two gaps found by auditing the existing
orchestration stack (`delegate_task`, `/goal`'s wait barriers, the
gateway's session self-post wake) - not a reimplementation of any of it.

## What it does

**Child -> parent progress** (in-process, pull-based):
- `subagent_checkpoint(text)` - called from inside a delegated subagent to
  report progress without ending the task.
- `read_subagent_checkpoints(subagent_id)` - called by the parent to read
  what a child has reported so far.

**External-event wakeup hooks** (signed webhook, wraps the existing
session self-post):
- `create_wakeup_hook(session_id?, ttl_seconds?, note?)` - signed URL;
  POSTing to it wakes the target session with a new turn.
- `list_wakeup_hooks()` / `revoke_wakeup_hook(hook_id)`.

See `skills/subagent-signal/SKILL.md` for full usage and when (not) to
reach for each tool.

## Install

```
hermes plugins install DadaDevelopment/hermes-subagent-signal
hermes plugins enable subagent-signal
```

### One-time reverse-proxy route (operator, only if you want EXTERNAL webhooks)

```caddyfile
handle /wake/* {
    reverse_proxy 127.0.0.1:8792
}
```

Checkpoints work with zero proxy setup (in-process). Wakeup hooks work
locally (loopback) with zero setup too; the proxy route is only needed if
an external system outside this box must be able to POST to a hook.

## Configure (optional)

```yaml
plugins:
  entries:
    subagent-signal:
      settings:
        wakeup_port: 8792
        api_server_port: 8642
        base_url: "https://harness.example.com"
```

## Why these two, and not a full event bus

An audit of `/opt/hermes` (goals.py, delegate_tool.py, gateway/run_goals.py,
gateway/run_notifications.py, gateway/wake.py, tools/process_registry.py)
found that persistent goals, turn budgets, subgoals, quality gates,
background-process wait barriers (`wait_on_pid`/`wait_on_session` with
`watch_patterns` mid-run triggers), `/loop` scheduled wakeups, and
delegate_task's spawn/list/steer/stop/background/group semantics are ALL
already implemented natively. Building a parallel "event bus" abstraction
on top would duplicate working infrastructure. The two real gaps were:
child processes can't proactively signal the parent mid-run (only pulled
via progress relay for display), and there was no way for a system OUTSIDE
this Hermes process to wake a session (only inside gateway/cron code).
This plugin closes exactly those two, using the existing primitives
(`ctx.register_tool`, the local API server's session self-post) rather
than inventing new ones.

## License

MIT
