---
name: superpos-hosted-agents
description: Manage Cloud-hosted agents — list, show, status, logs, deployments, and the start/stop/restart/redeploy/scale/rollback/delete lifecycle, plus the preset catalogue. Use when operating platform-managed agent containers on the Cloud edition. Cloud-only; provisioning (create/update) is deferred to PKV-4.
---

# Superpos Hosted Agents

Hosted agents are **platform-managed** agent containers that the Cloud
edition deploys and runs on your behalf (on NoVPS). This module is the
agent-side CLI for their **lifecycle**: inspect status, stream logs,
review deployment history, and drive start / stop / restart / redeploy /
scale / rollback / delete.

> **Cloud-only.** Every command here 404s on a self-hosted (CE)
> deployment — the `App\Cloud` controllers that back these routes are
> stripped from CE builds. If you get a 404 on a known-good id, you are
> almost certainly on CE.

## When to use it

- "Is hosted agent X running?" → `status`
- "Show me the last hour of logs for X" → `logs`
- "X is wedged, bounce it" → `restart` (in-place) or `redeploy` (re-apply spec)
- "Scale X to 3 replicas" → `scale`
- "That last deploy broke X, roll back" → `deployments` then `rollback`
- "Tear down X" → `delete`

If you need to **create** a hosted agent, this module cannot do it yet —
see [Not covered](#not-covered).

## Tools

All commands are on PATH inside the container. They print JSON to stdout
that you can `jq` over. Action verbs (`start`, `stop`, etc.) return a
`202`-style envelope whose `meta.queued_job` names the background job the
server enqueued — the operation is asynchronous; poll `status` to watch
it settle.

### Coverage

| Subcommand    | Route                                                  | Permission         |
| ------------- | ------------------------------------------------------ | ------------------ |
| `list`        | `GET …/hosted-agents`                                  | `hosted-agents.read`   |
| `show`        | `GET …/hosted-agents/{id}`                             | `hosted-agents.read`   |
| `status`      | `GET …/hosted-agents/{id}/status`                      | `hosted-agents.read`   |
| `logs`        | `GET …/hosted-agents/{id}/logs`                        | `hosted-agents.read`   |
| `deployments` | `GET …/hosted-agents/{id}/deployments`                 | `hosted-agents.read`   |
| `start`       | `POST …/hosted-agents/{id}/start`                      | `hosted-agents.manage` + credit |
| `stop`        | `POST …/hosted-agents/{id}/stop`                       | `hosted-agents.manage` |
| `restart`     | `POST …/hosted-agents/{id}/restart`                    | `hosted-agents.manage` + credit |
| `redeploy`    | `POST …/hosted-agents/{id}/redeploy`                   | `hosted-agents.manage` + credit |
| `scale`       | `POST …/hosted-agents/{id}/scale`                      | `hosted-agents.manage` + credit |
| `rollback`    | `POST …/hosted-agents/{id}/deployments/{dep}/rollback` | `hosted-agents.manage` + credit |
| `delete`      | `DELETE …/hosted-agents/{id}`                          | `hosted-agents.manage` |
| `presets`     | `GET /hosted-agent-presets` (org-scoped)               | `hosted-agents.read`   |

All routes are hive-prefixed (`/api/v1/hives/{hive}/hosted-agents/…`)
**except** `presets`, which is org-scoped. The hive is resolved from
`SUPERPOS_HIVE_ID` automatically — you never pass it.

"+ credit" means the server also runs `check-credit-balance`: the call
fails if the org has no remaining credit.

### `superpos-hosted-agents list`

```bash
superpos-hosted-agents list
superpos-hosted-agents list --page 2 --per-page 50
```

Returns the full envelope (`{"data": [...], "meta": {...}}`) — paginate
via `meta.pagination`.

### `superpos-hosted-agents show <id>`

```bash
superpos-hosted-agents show 01HXYZ...
```

### `superpos-hosted-agents status <id>`

```bash
superpos-hosted-agents status 01HXYZ...
```

For a non-terminal agent the server adds a fresh remote probe
(`novps_status`) and a `checked_at` timestamp. Terminal rows return the
cached status without a round-trip.

### `superpos-hosted-agents logs <id>`

```bash
superpos-hosted-agents logs 01HXYZ... \
  --start 2026-06-22T10:00:00Z \
  --end   2026-06-22T11:00:00Z \
  --limit 200 --direction backward
```

`--start` / `--end` are ISO-8601 and **required by the server**: the
window must be ≤ 24h, within the last 30 days, and not in the future.
`--limit` caps at 1000 (default 500). `--direction` is `forward` or
`backward` (default `backward`). `--search` and `--pod` filter the
upstream stream. (SSE streaming is not exposed by the CLI.)

### `superpos-hosted-agents deployments <id>`

```bash
superpos-hosted-agents deployments 01HXYZ...
```

Deployment history, newest first, paginated. Each row carries `status`,
`image_tag`, `resolved_image`, `triggered_by`, and timing — read this
before a `rollback` to pick a `success` target.

### Lifecycle: `start` / `stop` / `restart` / `redeploy`

```bash
superpos-hosted-agents start    01HXYZ...
superpos-hosted-agents stop     01HXYZ...
superpos-hosted-agents restart  01HXYZ...   # bounce container, same spec
superpos-hosted-agents redeploy 01HXYZ...   # re-apply full app spec
```

`restart` redeploys the existing container spec; `redeploy` re-applies
the full deployment spec. The server enforces a state machine — e.g.
`start` is only valid from `stopped`/`error` — and returns `409` with an
`invalid_transition` code otherwise.

### `superpos-hosted-agents scale <id>`

```bash
superpos-hosted-agents scale 01HXYZ... --size m --count 3
```

`--size` is one of `xs`, `s`, `m`, `l`; `--count` is a positive integer
(server-capped). Both are required (matches the server's `{ replicas:
{ size, count } }` payload).

### `superpos-hosted-agents rollback <id>`

```bash
superpos-hosted-agents rollback 01HXYZ... --deployment-id 01HDEP...
```

Roll back to a prior deployment. The target **must** have status
`success` — the server `409`s otherwise. Pull a candidate id from
`deployments` first.

### `superpos-hosted-agents delete <id>`

```bash
superpos-hosted-agents delete 01HXYZ...
```

Flips the agent to `deleting` and enqueues the destroy job. Asynchronous
— confirm with `status` / `list`.

### `superpos-hosted-agents presets`

```bash
superpos-hosted-agents presets
```

The operator-sanitized preset catalogue (the same projection that powers
the dashboard's create wizard). Org-scoped, not hive-prefixed.

## Not covered

- **create / update (provisioning).** Standing up a new hosted agent is
  intentionally **not** in this module. Provisioning is coupled to the
  provider-key vault and is delivered by **PKV-4 (provision-by-key)**, so
  the create/update verbs land there rather than duplicating the
  reference-not-value secret handling here.
- **SSE log streaming.** The `logs` command uses the JSON-proxy path;
  the server's `Accept: text/event-stream` mode is not wired through the
  CLI.
- **State transitions are enforced server-side.** The CLI does not
  pre-validate the lifecycle state machine — an illegal transition (e.g.
  `start` on a running agent) returns a `409` envelope you should read
  from the JSON output.

## Permission note

Hosted-agent routes are gated by the `hosted-agents.read` / `.write` /
`.manage` permission family. Whether `hosted-agents:read` is granted to
agents **by default** is decided by companion issue #189 — until that
lands, a human must grant the permission explicitly or every command
here `403`s. `manage` (for the write/lifecycle verbs) is never a default.

## Requirements

- `SUPERPOS_*` env vars (already set in the container)
- Cloud edition (CE returns 404)
- `hosted-agents.read` for `list` / `show` / `status` / `logs` /
  `deployments` / `presets`
- `hosted-agents.manage` for `start` / `stop` / `restart` / `redeploy` /
  `scale` / `rollback` / `delete`
- A positive org credit balance for `start` / `restart` / `redeploy` /
  `scale` / `rollback`
