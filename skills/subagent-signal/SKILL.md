---
name: subagent-signal
description: "Use when a subagent should report progress mid-run, or a session should wake on an external event (webhook/CI/monitoring) instead of polling."
version: 0.1.0
author: DadaDevelopment
license: MIT
metadata:
  hermes:
    tags: [delegation, subagents, orchestration, webhooks, events]
---

# Subagent Signal

Two small tool sets that close real gaps found by auditing Hermes's
existing orchestration primitives (`delegate_task`, `/goal`'s
`wait_on_pid`/`wait_on_session`, the gateway's session-wake self-post) -
this plugin does NOT reimplement any of that; it only adds what was
missing on top.

## Part 1: child -> parent progress checkpoints

`delegate_task` already gives you: spawn, `action=list` (status of running
children), `action=steer` (parent -> child), `action=stop`. What's missing:
a way for a LONG-RUNNING child to proactively report "still alive, here's
where I am" without waiting for its final result, and a way for the parent
(or a human, or a `/goal` judge) to read those notes.

- `subagent_checkpoint(text)` - call this FROM INSIDE a delegated subagent
  to report a short progress note. Use it sparingly (a few times per task
  at real milestones), not on every tool call.
- `read_subagent_checkpoints(subagent_id)` - call this as the PARENT to see
  what a running (or finished) child has reported. Pair with
  `delegate_task(action="list")` to first get the `subagent_id`.

This is pull-based on purpose: a running parent turn cannot safely have
new content spliced into a live generation, so checkpoints sit in memory
until something chooses to read them - same park+poll shape `/goal`
already uses for `wait_on_pid`/`wait_on_session`.

### When to use

- A subagent's task is long (minutes+) and you want visibility before it
  finishes, e.g. "found 40/120 items so far".
- You're orchestrating several subagents and want to check on one that's
  taking a while without stopping it.

### When NOT to use

- Short tasks (seconds) - just wait for the result.
- As a substitute for the final summary - checkpoints are progress notes,
  not the deliverable.

## Part 2: external-event wakeup hooks

Hermes can already resume ANY session by POSTing to its local API server
with `X-Hermes-Session-Id: <sid>` (verified live: two calls to the same
session id round-tripped a remembered fact through `/v1/chat/completions`).
That path was only reachable from inside gateway/cron code. This wraps it
in a signed, TTL'd webhook any external system can POST to.

- `create_wakeup_hook(session_id?, ttl_seconds?, note?)` - creates a
  signed URL. POSTing to it (optionally with `{"text": "..."}`) wakes the
  target session with that text as the next turn. Omit `session_id` (or
  pass `"current"`) to target the session you're calling from.
- `list_wakeup_hooks()` - see hooks you've created (status, fire count).
- `revoke_wakeup_hook(hook_id)` - invalidate a hook's URL immediately.

### When to use

- You're about to go idle waiting on something OUTSIDE this Hermes
  process entirely that you don't control the polling of: a CI pipeline's
  completion webhook, a deploy system's callback, a third-party
  monitoring alert, a partner API's async notification.
- The generic version of `/goal`'s `wait_on_pid`/`wait_on_session`, which
  only cover processes THIS Hermes instance spawned.

### When NOT to use

- Something you spawned yourself with `terminal()` - use
  `notify_on_complete=True` or `watch_patterns` instead (native, no hook
  needed).
- A subagent you delegated - use `delegate_task(action="list")` plus
  Part 1's checkpoints instead.

### Example

    User: "Deploy this and let me know when the pipeline finishes"
    -> create_wakeup_hook(note="deploy pipeline callback")
       -> URL: https://harness.example.com/wake/wh_.../abc123.../
    -> configure the CI/deploy system to POST that URL on completion
       (with {"text": "pipeline finished, status: success"})
    -> this session goes idle; POSTing to the URL wakes it with that text

## One-time reverse-proxy setup (operator, not the agent)

The wakeup-hook server binds `127.0.0.1:<port>` (default 8792) by design -
loopback only. To receive hooks from OUTSIDE the box, add one route to
whatever already terminates public TLS for this Hermes instance:

    handle /wake/* {
        reverse_proxy 127.0.0.1:8792
    }

(Caddy example; nginx/traefik equivalent works the same way.) Reload the
proxy. Checkpoints (Part 1) need no proxy route - they're in-process only.

## Config (optional; all have sane defaults)

    plugins:
      entries:
        subagent-signal:
          settings:
            wakeup_port: 8792
            api_server_port: 8642
            base_url: "https://harness.example.com"

`base_url` defaults to `HERMES_DASHBOARD_PUBLIC_URL`. `create_wakeup_hook`
requires a usable `API_SERVER_KEY` on this instance (the same key the
built-in `/v1/chat/completions` API server uses) - without one, hook
creation still succeeds but firing a hook fails until one is configured.
