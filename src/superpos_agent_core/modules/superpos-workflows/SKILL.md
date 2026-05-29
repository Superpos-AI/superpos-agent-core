---
name: superpos-workflows
description: Author and operate Superpos workflows — versioned multi-step agent orchestrations with triggers, fan-in, loops, conditionals, and webhook waits. Use when a request is "do X then Y, branching on Z" or when a recurring multi-step pipeline is needed.
---

# Superpos Workflows

A **workflow** is a versioned, multi-step orchestration that runs as a
DAG of agent calls and control-flow nodes. Each execution is a
**WorkflowRun** and every step the run takes is materialised as a
regular Superpos task — so workflows compose with everything else the
hive already does (tracing, replay, dead-letter, channel linking).

Use this skill when the unit of work isn't a single prompt to one
agent, but a chain that needs branching, fan-in, retries, or a wait for
an external signal.

## When to use it

Decision tree, in order:

- **Single ad-hoc prompt** → just create a task (`superpos-task
  create`). No workflow needed.
- **A prompt that should fire on a schedule (no branching)** → use a
  `TaskSchedule` (`superpos-task schedule`). Workflows are
  overkill for "run this prompt every weekday at 9am".
- **Multi-step orchestration with branching, loops, fan-in, or a wait
  for an external event** → use a workflow.

If the work is one prompt with no follow-up that depends on its output,
do not reach for this skill.

## Mental model

Four objects, in this order:

1. **Workflow** — the template. Has a slug, a `trigger_config`, and a
   list of `steps`. Versioned.
2. **WorkflowVersion** — an immutable frozen copy of a workflow's
   `steps` / `trigger_config` / `settings` at a point in time. Created
   automatically whenever you `update` one of those fields. Sub-agent
   slugs are pinned to concrete IDs at snapshot time, so versions don't
   silently change behaviour when a sub-agent is edited.
3. **WorkflowRun** — a single execution instance of a particular
   `WorkflowVersion`. Carries the trigger payload and a `thread` for
   the per-run conversation context.
4. **Tasks** — one task per executed step. These show up in
   `superpos-issues`-style task tracing as normal tasks (with a
   `workflow_run_id` link).

Run statuses: `running`, `completed`, `failed`, `cancelled`,
`retrying`, `retried`.

## Triggers

Three trigger types, set under `trigger_config.type`.

**`manual`** — only ever started by an explicit `start` call. No extra
fields.

```json
{ "type": "manual" }
```

**`schedule`** — periodic. Requires **either** `cron` **or**
`interval_seconds` (≥ 60).

```json
{
  "type": "schedule",
  "cron": "0 9 * * 1-5",
  "timezone": "America/New_York",
  "overlap_policy": "skip"
}
```

`overlap_policy` is one of `skip` | `allow` | `cancel_previous` and
controls what happens if the previous run is still alive when the next
tick fires.

**`event`** — fan-out from a Superpos event. Matches on `event_type`
and optional payload field filters.

```json
{
  "type": "event",
  "event_type": "pr.opened",
  "field_filters": { "repo": "superpos-app" }
}
```

## Step types

Every step has a unique `key`, a `type`, and (except for terminals) a
`next` pointing at the next step's key. Four step types:

### `agent`

Dispatches a task to a capability, a specific agent, or a sub-agent
definition. The `prompt` is a Handlebars template — see "Template
variables" below.

```json
{
  "key": "summarize_pr",
  "type": "agent",
  "name": "Summarize PR diff",
  "target_capability": "code-review",
  "prompt": "Summarize the diff for PR {{trigger.payload.pr_number}}.",
  "output_schema": [
    { "name": "summary", "type": "string" },
    { "name": "risk_level", "type": "string" }
  ],
  "on_failure": "fallback_step",
  "fallback_step": "notify_human",
  "timeout_seconds": 600,
  "next": "decide_route"
}
```

Pick exactly one targeting field: `target_capability`,
`target_agent_id`, or `sub_agent_definition_slug`. `depends_on_steps`
(an array of step keys) turns the step into a fan-in node — it won't
run until all listed steps complete. `on_failure` is one of
`skip` | `fallback_step` | `fail_workflow`.

### `loop`

Generator → evaluator loop. Used for "draft, critique, redraft" cycles
or anything that should iterate until a quality check passes.

```json
{
  "key": "qa_loop",
  "type": "loop",
  "generator_capability": "code-write",
  "generator_prompt": "Implement the change described in {{trigger.payload.spec}}. {{#if loop.feedback}}Address the prior reviewer feedback: {{loop.feedback}}{{/if}}",
  "evaluator_sub_agent_definition_slug": "qa-reviewer",
  "evaluator_prompt": "Review this implementation:\n{{loop.generator_output}}",
  "max_iterations": 4,
  "exit_condition": "approved",
  "on_max_iterations": "use_last",
  "next": "open_pr"
}
```

`evaluator_capability` is interchangeable with
`evaluator_sub_agent_definition_slug`. `exit_condition` is the name of
the field to read off the evaluator's structured result — see
"Lifecycle & failure handling" for the truthy check. `on_max_iterations`
is `use_last` | `fail`.

### `condition`

Top-to-bottom Handlebars expression match — first `if` that renders
truthy wins, otherwise `default` fires.

```json
{
  "key": "decide_route",
  "type": "condition",
  "conditions": [
    { "if": "{{eq steps.summarize_pr.result.risk_level 'high'}}", "then": "notify_human" },
    { "if": "{{eq steps.summarize_pr.result.risk_level 'medium'}}", "then": "qa_loop" }
  ],
  "default": "open_pr"
}
```

### `webhook_wait`

Pause the run until a matching webhook arrives on a configured route.

```json
{
  "key": "wait_for_signoff",
  "type": "webhook_wait",
  "match": {
    "webhook_route_id": "01HWEBHOOK...",
    "field_filters": {
      "action": "approved",
      "pr_number": "{{trigger.payload.pr_number}}"
    }
  },
  "timeout_seconds": 86400,
  "on_timeout": "fail_workflow",
  "next": "merge"
}
```

Field-filter values are rendered through Handlebars before the match,
so they can interpolate `{{trigger.*}}` and prior step results.

## Template variables in prompts

Inside any Handlebars-rendered field (prompts, condition `if`,
webhook field filters) you have:

- `{{trigger.payload}}` — the JSON payload passed to `start`.
- `{{trigger.input}}` — alias for the same payload, exposed for
  templates that treat it as a "user input" slot.
- `{{steps.<key>.result.<field>}}` — structured-output field from any
  completed prior step.
- `{{loop.feedback}}` — last evaluator result, available inside a
  `loop` step's generator prompt on iterations ≥ 2.
- `{{loop.generator_output}}` — current iteration's generator output,
  available inside the evaluator prompt.

## Authoring end-to-end

Author the two JSON blobs locally, then `create`:

```bash
# Write steps.json and trigger.json locally
superpos-workflows create \
  --name "PR Review" \
  --slug pr-review \
  --trigger-config @trigger.json \
  --steps @steps.json

# Start a run
superpos-workflows start pr-review --payload '{"pr_number": 42}'

# Watch progress
superpos-workflows run-show pr-review <run-id>
```

`--trigger-config`, `--steps`, `--settings`, and `--payload` all accept
either an inline JSON string or `@path/to/file.json`.

## Built-in templates to copy from

The hive ships four seeded workflow templates. Fetch them with
`superpos-workflows list` and copy the `steps` / `trigger_config` into
a new workflow to adapt.

- **`superpos-plan-build-qa`** — plan → build (with QA loop) → exit.
- **`superpos-research-summarize`** — gather sources → structured
  summary.
- **`superpos-bug-triage`** — triage → route by severity → notify.
- **`superpos-pr-review`** — review diff → per-file comments → final
  approval.

Don't reinvent these from scratch when one is close to what you need.

## Versioning

Every edit that touches `steps`, `trigger_config`, or `settings`
snapshots a new immutable `WorkflowVersion`. Edits to cosmetic fields
(`name`, `description`, `is_active`) do not. Sub-agent definition
slugs referenced in a snapshot are pinned to the **then-current** ID,
so a running workflow can't be subverted by editing a sub-agent.

```bash
superpos-workflows versions pr-review
superpos-workflows version pr-review 3
superpos-workflows diff pr-review 2 3
superpos-workflows rollback pr-review 2   # promote v2 to be the new head
```

## Lifecycle & failure handling

**Run statuses**: `running`, `completed`, `failed`, `cancelled`,
`retrying`, `retried`.

**Step states**: `pending`, `running`, `waiting`, `completed`,
`failed`, `skipped`. (`waiting` is the state of a `webhook_wait` step
that's been reached and is parked for its signal.)

Per-step `on_failure` overrides any workflow-level default:

- `skip` — mark failed, move to `next` anyway.
- `fallback_step` — jump to `fallback_step` and continue.
- `fail_workflow` — abort the run with `status=failed`.

For `loop` steps, `exit_condition` reads the named field off the
evaluator's structured result. The loop exits when that field is
truthy *or* equal (case-insensitive) to one of `true`, `approved`,
`done`, `pass`, `ok`. Otherwise the loop runs another iteration up to
`max_iterations`; on exhaustion it either `use_last` (treat the last
generator output as the step's result) or `fail` (fail the step).

`cancel` is best-effort: in-flight step tasks will keep running to
completion, but no further steps are dispatched. `retry` re-runs from
the failed step where possible, otherwise from the start with the
original trigger payload.

## Common pitfalls

Do not:

- Use a slug that doesn't match `^[a-z0-9]+(?:-[a-z0-9]+)*$` — the
  server returns 422 and the message often gets blamed on the wrong
  field.
- Ship a `steps` list with no entry point. At least one step must
  have no incoming edge (nothing else `next`s into it, and it isn't
  in any `depends_on_steps`).
- Reference a `sub_agent_definition_slug` that isn't active in the
  hive at create time — slugs are pinned at snapshot, and the snapshot
  refuses to write.
- Omit both `cron` and `interval_seconds` from a `schedule` trigger.
  One of them is required, with `interval_seconds` ≥ 60.
- Use a non-array `output_schema`. It is a list of
  `{"name": ..., "type": ...}` rows, not a JSON-Schema object.
- Try to `delete` a workflow with active runs — cancel them first
  (`runs --status running` then `cancel ...`).
- Create circular DAG dependencies. Cycles are **not** detected at
  create time; the run will simply hang with steps stuck in `pending`
  waiting on each other.
- Reference a non-existent step key in `next`, `then`, or
  `depends_on_steps`. The executor silently treats it as "no next
  step" / "dependency never resolves" — easy to misdiagnose.

## Requirements

- `SUPERPOS_*` env vars (already set in the container)
- `workflows.read` and/or `workflows.manage` permission on the hive
