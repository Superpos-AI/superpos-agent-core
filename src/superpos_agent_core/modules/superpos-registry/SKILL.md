---
name: superpos-registry
description: Author Superpos registry items (skills, subagents, modules, dynamic_workflows) on the server. Use when a task asks you to create, update, publish, or remove a registry item — a reusable skill, subagent definition, CLI module, or dynamic workflow — so it can be attached to agents/hives.
---

# Superpos Registry Authoring

The **registry** is the hive's store of reusable, attachable capabilities.
Each item has a **kind**, a stable **slug**, a **name**, and a kind-specific
**payload** (the body, stored as a versioned revision). Items are later
*attached* to agents/hives/tasks to take effect.

Use this skill when a task asks you to **author** a registry item — create a
new skill/subagent/module/dynamic_workflow, edit one, or tombstone one. If the
work is just reading the resolved capability set, you don't need this skill;
the runtime already syncs resolved items down.

## Kinds

`--kind` is one of:

- `skill` — a reusable instruction skill. Payload: `{frontmatter: {name, description}, instructions: <SKILL.md text>, files: []}`.
- `subagent` — a subagent definition.
- `module` — a CLI module. Payload: `{manifest, files, install: {steps}, skill, source}`.
- `dynamic_workflow` — a multi-step workflow (payload validated server-side).

## Tools

`superpos-registry` is on PATH inside the container. It prints JSON to stdout
that you can `jq` over; errors go to stderr.

```
superpos-registry list   --kind <kind> [--include-inactive] [--include-deleted]
superpos-registry show   --kind <kind> --slug <slug>
superpos-registry create --kind <kind> --slug <slug> --name <name>
                          (--payload '<json>' | --payload-file <path>)
                          [--body <md> | --body-file <path>]
                          [--description <text>] [--private] [--owner-agent-id <id>]
                          [--message <revision-message>]
superpos-registry update --kind <kind> --slug <slug>
                          [--name <name>] [--description <text>]
                          [--payload '<json>' | --payload-file <path>]
                          [--body <md> | --body-file <path>]
                          [--is-active | --draft] [--private | --hive]
                          [--message <revision-message>]
superpos-registry delete --kind <kind> --slug <slug>
```

### Payload vs body

- `--payload` / `--payload-file` set the full kind-specific JSON payload.
- `--body` / `--body-file` are a convenience that populate `payload.instructions`
  from Markdown — handy for skills/subagents whose body is SKILL.md text. They
  layer on top of `--payload` (instructions wins).

`create` requires a payload (via `--payload(-file)` and/or `--body(-file)`).
`update` only sends the fields you pass; supplying a new payload records a fresh
revision.

### Visibility & active state

- New items are **hive**-visible by default; pass `--private` to make them
  owner-only. A private create resolves the owner from `SUPERPOS_AGENT_ID`
  (or an explicit `--owner-agent-id`); it errors if neither is set.
- On update, `--private` / `--hive` change visibility and `--is-active` /
  `--draft` toggle whether the item is live or a draft.

## Examples

Create a skill from a Markdown file:

```bash
superpos-registry create --kind skill --slug deep-dive \
  --name "Deep Dive" \
  --payload '{"frontmatter": {"name": "Deep Dive", "description": "Thorough analysis"}, "files": []}' \
  --body-file ./SKILL.md \
  --message "initial version"
```

Publish a draft (flip active):

```bash
superpos-registry update --kind skill --slug deep-dive --is-active
```

Show / list:

```bash
superpos-registry show --kind module --slug superpos-github
superpos-registry list --kind skill --include-inactive | jq '.[].slug'
```

Tombstone:

```bash
superpos-registry delete --kind dynamic_workflow --slug nightly-report
```

## API contract

Maps onto the agent-callable registry endpoints (superpos-app
`RegistryApiController`, under `auth:sanctum-agent`). The hive is derived from
the agent token, so paths carry no hive id:

- `GET    /api/v1/registry/{kind}`         — list
- `POST   /api/v1/registry/{kind}`         — create (slug in body)
- `GET    /api/v1/registry/{kind}/{slug}`  — show
- `PATCH  /api/v1/registry/{kind}/{slug}`  — update
- `DELETE /api/v1/registry/{kind}/{slug}`  — soft-delete

Create body fields: `slug`, `name`, `payload` (required); `description`,
`visibility` (`hive`|`private`), `owner_agent_id`, `message` (optional).
Update accepts any of `name`, `description`, `payload`, `is_active`,
`visibility`, `message`.

> Note: authoring is gated server-side by a `registry:write` permission
> (companion issue #187). If a write returns 403, the authoring agent has not
> been granted that permission yet.
