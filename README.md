# superpos-agent-core

Shared runtime for the Superpos "slim agent" family — the part that is identical across Claude, Codex, Gemini, Qwen, and any future LLM-CLI-backed agent.

## What lives here

- `Executor` protocol + `ExecutionRequest` — the contract every agent implementation must satisfy.
- `SuperposClient` — REST client for the Superpos backend (tasks, persona, agent lifecycle).
- `run_superpos_poller` — daemon that polls Superpos for tasks and enqueues them on an `Executor`.
- `TelegramGateway` + `TelegramStreamer` + `build_telegram_app` / `run_telegram_bot` — Telegram I/O with backpressure, flood-ban handling, message streaming.
- `WorktreeManager` helpers — per-branch isolation via `git worktree`.
- `SessionStore`, `RecentTasksLog`, `RuntimeConfig` — persistence and runtime knobs.
- `redact()` — strip known secret patterns from outbound text.
- `run_agent()` — orchestrator that wires everything together given a concrete `Executor` factory.

## What does NOT live here

- Per-agent executor implementations (`ClaudeExecutor`, `CodexExecutor`, `GeminiExecutor`, …) — they live in each agent's own repo and depend on this package.
- LLM SDK / CLI installation — each agent's Dockerfile is responsible.
- Per-agent config (model lists, auth env vars) — subclassed from `BaseConfig`.

## Using it

```python
from superpos_agent_core import run_agent, BaseConfig, Executor

class MyAgentConfig(BaseConfig):
    my_api_key: str = ""

class MyExecutor:
    # implements superpos_agent_core.Executor
    ...

if __name__ == "__main__":
    import asyncio
    config = MyAgentConfig.from_env(extra={"my_api_key": "MY_API_KEY"})
    asyncio.run(run_agent(
        config=config,
        executor_factory=lambda cfg, runtime, superpos, gateway, persona:
            MyExecutor(cfg, runtime, superpos, gateway, persona=persona),
    ))
```

See `Slim-Agent-Gemini/` for a complete working example.

## CLI helpers

`superpos_task.py` ships a `superpos-task` console helper agents shell out to
for task/schedule operations. Hive, base URL and token are resolved from the
standard `SUPERPOS_*` environment variables.

- `superpos-task create --prompt …` — create a task (broadcast by default;
  `--self-target` pins it to the calling agent).
- `superpos-task schedule …` — create a one-off / interval / cron schedule.
- `superpos-task schedules` / `delete-schedule --id …` — manage schedules.
- `superpos-task list [filters]` — list tasks in the hive. All filters are
  AND-combined server-side and optional: `--status`, `--type`,
  `--target-agent-id`, `--target-capability`, `--creator-id`,
  `--parent-task-id`, `--created-after`, `--created-before`, `--q`, plus
  `--page` / `--per-page` (max 100). Prints a compact summary by default, or the
  raw `data` list as JSON with `--json`. (Consumes
  `GET /api/v1/hives/{hive}/tasks`, exposed in the SDK as
  `SuperposClient.list_tasks`.)
- `superpos-task show <task-id> [--json]` — show a single task by ID
  (`GET /api/v1/hives/{hive}/tasks/{task}`, SDK `SuperposClient.get_task`).
- `superpos-task memory --content … [--mode append|prepend|replace]` — update
  the active persona's MEMORY document.
