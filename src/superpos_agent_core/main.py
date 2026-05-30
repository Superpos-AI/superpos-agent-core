"""Generic orchestrator: wires every shared daemon together for one agent run.

Per-agent ``__main__`` modules only need to:

  1. Build their concrete ``BaseConfig`` subclass from env.
  2. Build their concrete ``RuntimeConfig`` subclass.
  3. Hand ``run_agent`` an ``executor_factory`` callable.

``run_agent`` then creates the SuperposClient, fetches /me, fetches persona,
builds the Telegram app + gateway, instantiates the executor, and runs the
whole asyncio.gather() event loop until shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import shutil
import signal
import sys
from typing import Awaitable, Callable

from .config import BaseConfig
from .executor import Executor
from .runtime_config import RuntimeConfig
from .superpos_client import SuperposClient
from .superpos_poller import run_superpos_poller
from .telegram_bot import build_telegram_app, run_telegram_bot
from .telegram_gateway import TelegramGateway
from .worktree_manager import is_git_repo, prune_worktrees

log = logging.getLogger(__name__)

ExecutorFactory = Callable[
    [BaseConfig, RuntimeConfig, SuperposClient | None, TelegramGateway | None, str | None],
    Executor,
]

_REQUIRED_PERMISSIONS = ("tasks:read", "tasks:claim", "tasks:update")
_OPTIONAL_PERMISSIONS = ("tasks:create", "knowledge:write")


def setup_logging(log_dir: str) -> None:
    """Configure root logger with stderr + rotating file output."""
    fmt = "%(asctime)s %(levelname)-8s %(name)s  %(message)s"
    os.makedirs(log_dir, exist_ok=True)

    logging.basicConfig(level=logging.INFO, format=fmt, stream=sys.stderr)

    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "agent.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    file_handler.setFormatter(logging.Formatter(fmt))
    file_handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(file_handler)


async def _warn_missing_permissions(
    gateway: TelegramGateway | None, config: BaseConfig,
) -> None:
    """If /me lacks critical permissions, log and notify Telegram (fire-and-forget)."""
    if not config.superpos_permissions:
        return

    missing_required = [p for p in _REQUIRED_PERMISSIONS if not config.has_permission(p)]
    missing_optional = [p for p in _OPTIONAL_PERMISSIONS if not config.has_permission(p)]

    if missing_required:
        log.error("Agent missing required permissions: %s", missing_required)
    if missing_optional:
        log.warning("Agent missing optional permissions: %s", missing_optional)

    if not missing_required and not missing_optional:
        return
    if not gateway or not config.telegram_chat_id:
        return

    lines = ["⚠️ Agent started with missing permissions:"]
    if missing_required:
        lines.append(f"  • Required (agent will malfunction): {', '.join(missing_required)}")
    if missing_optional:
        lines.append(f"  • Optional (some tasks may fail): {', '.join(missing_optional)}")
    lines.append("")
    lines.append("Grant them in the Superpos dashboard and restart the agent.")

    try:
        await gateway.send_message(config.telegram_chat_id, "\n".join(lines))
    except Exception:
        log.debug("Failed to send missing-permissions warning to Telegram", exc_info=True)


async def _monitor_disk(
    gateway: TelegramGateway,
    config: BaseConfig,
    *,
    interval_seconds: int = 300,
    warn_threshold: float = 0.90,
    clear_threshold: float = 0.85,
) -> None:
    """Poll disk usage on the home volume and alert via Telegram.

    When a full disk truncates session_store.json / per-chat JSONL files,
    the symptom surfaces much later as "agent lost context" — the operator
    sees a nonsense answer without any clue the underlying cause was disk
    pressure.  This task surfaces the warning early.
    """
    alerted = False
    path = config.home_dir
    while True:
        try:
            total, used, free = shutil.disk_usage(path)
            usage = used / total if total else 0.0

            if usage >= warn_threshold and not alerted:
                free_gb = free / (1024 ** 3)
                total_gb = total / (1024 ** 3)
                log.error(
                    "Disk nearly full: %.0f%% used (%.1fGB free of %.1fGB) at %s",
                    usage * 100, free_gb, total_gb, path,
                )
                if config.telegram_chat_id:
                    msg = (
                        f"⚠️ Agent disk at {usage:.0%} "
                        f"({free_gb:.1f}GB free of {total_gb:.1f}GB).\n"
                        f"Session persistence may start failing — "
                        f"free up disk on the host before the agent loses context."
                    )
                    try:
                        await gateway.send_message(config.telegram_chat_id, msg)
                    except Exception:
                        log.debug("Failed to send disk warning", exc_info=True)
                alerted = True
            elif usage < clear_threshold and alerted:
                log.info("Disk usage recovered: %.0f%%", usage * 100)
                if config.telegram_chat_id:
                    try:
                        await gateway.send_message(
                            config.telegram_chat_id,
                            f"✅ Agent disk recovered to {usage:.0%}.",
                        )
                    except Exception:
                        pass
                alerted = False
        except Exception:
            log.debug("Disk check failed", exc_info=True)

        await asyncio.sleep(interval_seconds)


def _shutdown(loop: asyncio.AbstractEventLoop) -> None:
    log.info("Received shutdown signal")
    for task in asyncio.all_tasks(loop):
        task.cancel()


async def run_agent(
    config: BaseConfig,
    runtime: RuntimeConfig,
    *,
    executor_factory: ExecutorFactory,
    log_dir: str | None = None,
    extra_tasks: list[Awaitable] | None = None,
) -> None:
    """Boot a slim agent.

    Sets up logging, optional Superpos integration, optional Telegram bot,
    constructs the agent's concrete Executor via ``executor_factory``, then
    runs all daemons under asyncio.gather() until shutdown.

    ``extra_tasks`` lets per-agent ``__main__`` modules inject additional
    background coroutines (e.g. a custom watchdog) without re-implementing
    the orchestrator.
    """
    if log_dir is None:
        log_dir = os.path.join(config.home_dir, "logs")
    setup_logging(log_dir)

    # Prune orphaned worktrees from prior runs
    if config.executor_worktree_isolation and is_git_repo(config.executor_working_dir):
        try:
            await prune_worktrees(config.executor_working_dir)
        except Exception:
            log.warning("Failed to prune worktrees on startup", exc_info=True)

    # Superpos client (optional)
    superpos: SuperposClient | None = None
    if config.superpos_enabled:
        superpos = SuperposClient(config)
        log.info("Superpos integration enabled (%s)", config.superpos_base_url)
        try:
            await superpos.update_status("online")
            log.info("Agent status set to online")
        except Exception:
            log.warning("Failed to set agent status to online", exc_info=True)

        # Overlay server-authoritative profile (hive_id, capabilities,
        # permissions) on top of env config.  Env stays as the fallback
        # so a /me outage doesn't ground the agent.
        me = await superpos.fetch_me()
        if me:
            if me.get("hive_id"):
                config.superpos_hive_id = me["hive_id"]
            caps = me.get("capabilities")
            if isinstance(caps, list):
                # Treat any list — including `[]` — as authoritative.  An
                # operator clearing every capability in the dashboard is a
                # real config change; requiring `caps` to be truthy would
                # silently fall back to env-derived caps and the poller
                # would keep claiming tasks the operator just revoked.
                config.superpos_capabilities = [str(c) for c in caps]
            perms = me.get("permissions")
            if isinstance(perms, list):
                config.superpos_permissions = [str(p) for p in perms]
            log.info(
                "Agent profile: name=%r hive=%s capabilities=%s permissions=%d",
                me.get("name"), config.superpos_hive_id,
                config.superpos_capabilities, len(config.superpos_permissions),
            )
        else:
            log.warning(
                "Could not load /agents/me — falling back to env-configured "
                "hive_id=%s, capabilities=%s",
                config.superpos_hive_id, config.superpos_capabilities,
            )
    else:
        log.info("Superpos integration disabled (missing config)")

    # Telegram app + centralized gateway (optional)
    bot_app = None
    gateway: TelegramGateway | None = None
    if config.telegram_enabled:
        bot_app = build_telegram_app(config)
        bot = bot_app.bot
        gateway = TelegramGateway(bot)
    else:
        log.info("Telegram disabled (no TELEGRAM_BOT_TOKEN)")

    # Fetch persona at startup
    persona: str | None = None
    if superpos:
        try:
            persona = await superpos.get_persona_assembled()
            if persona:
                log.info("Persona loaded (version from assembled endpoint)")
            else:
                log.info("No persona configured for this agent")
        except Exception:
            log.warning("Could not fetch persona at startup", exc_info=True)

    log.info("Runtime: model=%s, effort=%s", runtime.model, runtime.effort)

    # Per-agent executor (concrete implementation supplied by the factory)
    executor = executor_factory(config, runtime, superpos, gateway, persona)
    log.info(
        "Executor: kind=%s, max_parallel=%d, worktree_isolation=%s",
        config.executor_kind,
        config.executor_max_parallel,
        config.executor_worktree_isolation,
    )

    # Verify auth (executor-specific check, default no-op)
    try:
        await executor.preflight()
    except SystemExit:
        raise
    except Exception:
        log.exception("Executor preflight failed")
        sys.exit(1)

    # Build task list
    tasks: list[Awaitable] = [executor.run()]
    if bot_app and gateway:
        tasks.append(run_telegram_bot(bot_app, executor, config, runtime))
        tasks.append(gateway.run())
        tasks.append(_monitor_disk(gateway, config))
    if superpos:
        tasks.append(run_superpos_poller(superpos, executor, config))
        tasks.append(_warn_missing_permissions(gateway, config))
    if extra_tasks:
        tasks.extend(extra_tasks)

    if len(tasks) == 1:
        log.error("Neither Telegram nor Superpos is configured — nothing to do")
        sys.exit(1)

    # Graceful shutdown on SIGTERM/SIGINT
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: _shutdown(loop))

    # Auto-cleanup stale session data on startup
    if config.telegram_enabled:
        counts = await asyncio.to_thread(executor.cleanup_stale_sessions, 48)
        if counts.get("projects") or counts.get("session_env"):
            freed_mb = counts.get("bytes_freed", 0) / (1024 * 1024)
            log.info(
                "Startup cleanup: removed %d sessions, %d env snapshots (%.1fMB freed)",
                counts.get("projects", 0), counts.get("session_env", 0), freed_mb,
            )

    log.info("Starting %d tasks", len(tasks))
    try:
        await asyncio.gather(*tasks)
    finally:
        if superpos:
            try:
                await superpos.update_status("offline")
                log.info("Agent status set to offline")
            except Exception:
                log.debug("Failed to set agent status to offline (shutdown)")
            await superpos.close()
