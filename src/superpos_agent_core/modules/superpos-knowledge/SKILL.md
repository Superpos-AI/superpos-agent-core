---
name: superpos-knowledge
description: Search, read, and record entries in the Superpos knowledge store. Use when you need facts the user or other agents recorded — recent decisions, deploy notes, known incidents, key conventions — or when you've learned a lasting, non-obvious fact worth persisting for future agents.
---

# Superpos Knowledge

The hive's persistent memory.  Other agents (and the user) write decisions,
incident postmortems, deploy notes, and project conventions here.  Before
guessing or making up details about *this* hive's history or state, search
the knowledge store.

## When to use it

Reach for these tools when the task touches:

- Recent decisions ("why did we pick X?", "what's the status of the Y migration?")
- Project conventions ("how do we name branches in this repo?", "what's our PR template?")
- Hive-specific facts that aren't in the code ("who runs the staging cluster?", "where is X deployed?")
- Incident history ("did this fail before?", "what was the root cause last time?")

If the answer is in the code itself, just read the code — that's cheaper.

## Tools

All commands are on PATH inside the container.  They print JSON to stdout
that you can `jq` over or just read directly.

### `superpos-knowledge search`

The everyday tool — full-text or semantic search.

```bash
superpos-knowledge search "auth migration"
superpos-knowledge search "deploy staging" --limit 5
superpos-knowledge search "how do we test webhooks" --semantic
superpos-knowledge search --scope hive  # browse without a query
```

Flags:
- `--scope` — restrict to a scope (`hive`, `apiary`, `agent:<id>`)
- `--semantic` — pgvector cosine-similarity instead of Postgres FTS
- `--limit N` — top-N results (default 10, server cap 100)

Either a positional query OR `--scope` must be provided; the server
rejects requests with neither.

### `superpos-knowledge get <entry_id>`

Fetch a single entry by ID — use when search results give you an ID and
you want the full body.

```bash
superpos-knowledge get 01HXYZ...
```

### `superpos-knowledge list`

Browse entries with structured filters.  Useful for "find stale entries
that need refreshing":

```bash
superpos-knowledge list --tags prod,critical --limit 20
superpos-knowledge list --stale-days 30 --sort least-read
superpos-knowledge list --key 'deploy.*'    # wildcard supported
```

Flags:
- `--key` — key pattern (`*` is wildcard)
- `--scope` — restrict to a scope
- `--tags` — comma-separated; ALL tags must match
- `--stale-days N` — entries not read in N days
- `--sort least-read` — order by read-count ascending
- `--limit N` — default 50, server cap 100

### `superpos-knowledge graph <entry_id>`

BFS link traversal — find connected entries (decisions that depend on
each other, services that link to incidents, etc.).

```bash
superpos-knowledge graph 01HXYZ... --depth 3
superpos-knowledge graph 01HXYZ... --link-types decides,depends_on
```

Flags:
- `--depth N` — server clamps 1–5 (default 2)
- `--max-nodes N` — server clamps 1–200 (default 50)
- `--link-types` — comma-separated allowlist (e.g. `decides,depends_on`)

### `superpos-knowledge topics` / `superpos-knowledge decisions`

Convenience indexes — what topic clusters exist in this hive, what
decisions have been recorded.  Use to *discover* what's available before
searching.

```bash
superpos-knowledge topics
superpos-knowledge decisions
```

### `superpos-knowledge create` / `update` (recording knowledge)

When you discover a **lasting, non-obvious** fact during a task, record it
so future agents inherit it instead of rediscovering it.

There are two shapes.  Prefer the **typed wiki** shape — it's the current
model.  The legacy key/value shape still works but is **deprecated** (it
prints a deprecation notice) and will be removed.

#### Typed wiki pages (preferred)

A typed page has a **type**, a stable **slug**, a Markdown **body**, and
optional **frontmatter** (a JSON object, validated server-side per type).

```bash
# create a topic page from a Markdown file
superpos-knowledge create \
  --type topic \
  --slug proposal-knowledge-wiki \
  --body-file ./proposal.md \
  --summary "A3 knowledge-wiki redesign: typed pages + sources." \
  --tags proposal,architecture \
  --frontmatter '{"status": "accepted"}'

# create an entity page (entity REQUIRES frontmatter.kind)
superpos-knowledge create \
  --type entity --slug entity:redis-cluster-prod \
  --body "# Redis prod cluster" \
  --frontmatter '{"kind": "service"}'

# update by id (PUT is id-only)
superpos-knowledge update --id 01HXYZ... --summary "refreshed" --body-file ./new.md

# update by slug (resolved to an id via the typed list endpoint;
# --type is required so the lookup knows which list to scan)
superpos-knowledge update --type topic --slug proposal-knowledge-wiki \
  --frontmatter '{"status": "superseded"}'
```

**Types** (exactly these six): `entity`, `topic`, `trend`, `source_page`,
`log`, `procedure`.

**Gotcha:** `--type entity` **requires** `frontmatter.kind` — the CLI
fails fast if it's missing, and the server returns 422 on the
`frontmatter` field otherwise.  Other types accept empty frontmatter.

Typed flags (create & update):
- `--type` — one of the six types above (required with `--slug` on create)
- `--slug` — stable slug, regex `^[A-Za-z0-9:_\-.]+$`
- `--body` — Markdown body, OR `--body-file <path>` to read it from a file
  (the two are mutually exclusive)
- `--frontmatter '<json>'` — frontmatter as a JSON object (clear error on
  invalid JSON)
- `--summary` — top-level one-line summary (max 500 chars; not folded into
  frontmatter)
- `--title`, `--tags a,b,c`, `--visibility public|private`
  - On **update**, `--visibility` cannot be the only field — the server
    rejects a visibility-only typed update, so it must accompany at least one
    content field (`--body`/`--body-file`/`--frontmatter`/`--title`/
    `--summary`/`--tags`). On create, visibility-with-content is fine.
- create-only: `--scope` (defaults to `SUPERPOS_KNOWLEDGE_FILLIN_SCOPE`
  env or `hive`; org scope needs `knowledge.write_organization`; scope is
  immutable after create)

**Update is id-only.**  `--id` updates directly.  `--slug` is resolved to
an id first by listing pages of `--type` and matching the slug, so
`--type` is required with `--slug`; a slug that matches 0 or >1 pages
errors rather than guessing.  `scope` cannot be changed on update.

#### Legacy key/value (deprecated)

```bash
superpos-knowledge create \
  --key "decisions:retry-backoff" \
  --title "Task retries use capped exponential backoff" \
  --summary "Claim retries cap at 2 attempts, not 3 — see MAX_TASK_CLAIMS." \
  --content "Rule: a task may be re-claimed at most twice before the poller fails it server-side.
Why: three attempts on a stuck task burned ~17 min/cycle in traces; two halves that while still recovering one transient blip.
How to apply: when adding new claim/expire paths, respect MAX_TASK_CLAIMS rather than adding a parallel counter." \
  --tags tasks,reliability --confidence high

superpos-knowledge update 01HXYZ... --content "Rule: …  Why: …  How to apply: …"
```

Legacy flags:
- `--content` (required on create) — the body.  **Use the shape below.**
- `--title`, `--summary`, `--tags a,b,c`, `--confidence high|medium|low`
- `--visibility public|private`, `--ttl <ISO8601>`
- `--value '<json>'` — raw escape hatch; structured flags override its fields
- create: `--key` (required, stable); update: positional `entry_id`

You **cannot mix** typed and legacy flags in one call — the CLI rejects
that up front, mirroring the server's dual-shape XOR.

**KEEP** — worth recording: non-obvious invariants, design rationale with
rejected alternatives, pitfalls with root cause, cross-file mental models.

**SKIP** — do not record: PR/commit restatements, per-task "what I did"
recaps, anything reconstructable from `git log` + current code + feature
docs, or restatements of existing entries (prefer `update` instead).

**Required body shape** for `--content`:

```
Rule: <the invariant, pattern, or decision in one sentence>
Why:  <motivation — name rejected alternatives or failure modes when relevant>
How to apply: <when and where future work should invoke this>
```

Before creating, **search first** — if a near-duplicate exists, `update`
it (or link a `supersedes` relation) rather than writing a fresh entry.

## Tips

- **Search first, ask later.**  If the user asks about something hive-specific,
  search before reasoning from memory.
- **Use `jq` for follow-ups.**  `superpos-knowledge search "x" | jq '.[].key'`
  to just see what keys exist.
- **`get` after `search`.**  Search returns summaries; `get` returns the
  full body including `value`, version history, links.
- **Write sparingly.**  A handful of high-signal entries beats a pile of
  recaps. Every inline write is tagged `metadata.source = "agent_inline"`
  so the Curator can audit and decay it.

## Requirements

- Python 3.10+ (already provided)
- `SUPERPOS_*` env vars (already set in container)
- `knowledge.read` permission to read; `knowledge.write` (or
  `knowledge.write_organization` for org scope) to create/update
