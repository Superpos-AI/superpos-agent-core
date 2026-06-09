"""Slim-agent core runtime: shared code for every Superpos LLM-backed agent."""

from .config import BaseConfig
from .executor import Executor, ExecutionRequest
from .knowledge import KnowledgeClient, KnowledgeNotFound
from .main import ExecutorFactory, run_agent, setup_logging
from .module_loader import (
    bundled_modules_dir,
    collect_mcp_servers,
    discover_modules,
    generate_modules_doc,
)
from .module_setup import run_setup as run_module_setup
from .module_setup import symlink_module_scripts
from .registry_overlay import (
    ModuleOverlayResult,
    RegistryOverlayResult,
    SkillOverlayResult,
    apply_registry_overlay,
)
from .sub_agent_sync import sync_sub_agents
from .progress_reporter import report_progress
from .recent_tasks import RecentTasksLog, TaskSummary
from .redactor import redact
from .runtime_config import RuntimeConfig
from .session_store import SessionStore
from .superpos_client import GitHubDiscoveryForbidden, SuperposClient
from .superpos_poller import run_superpos_poller
from .telegram_bot import build_telegram_app, run_telegram_bot
from .telegram_gateway import Priority, TelegramGateway
from .telegram_streamer import TelegramStreamer
from .worktree_manager import (
    ensure_worktree,
    infer_branch,
    is_git_repo,
    prune_worktrees,
    slot_key,
    worktree_path,
)

__all__ = [
    "BaseConfig",
    "Executor",
    "ExecutionRequest",
    "ExecutorFactory",
    "GitHubDiscoveryForbidden",
    "KnowledgeClient",
    "KnowledgeNotFound",
    "ModuleOverlayResult",
    "Priority",
    "RecentTasksLog",
    "RegistryOverlayResult",
    "RuntimeConfig",
    "SessionStore",
    "SkillOverlayResult",
    "SuperposClient",
    "TaskSummary",
    "TelegramGateway",
    "TelegramStreamer",
    "apply_registry_overlay",
    "build_telegram_app",
    "bundled_modules_dir",
    "collect_mcp_servers",
    "discover_modules",
    "ensure_worktree",
    "generate_modules_doc",
    "infer_branch",
    "is_git_repo",
    "prune_worktrees",
    "redact",
    "report_progress",
    "run_agent",
    "run_module_setup",
    "run_superpos_poller",
    "run_telegram_bot",
    "setup_logging",
    "slot_key",
    "sync_sub_agents",
    "symlink_module_scripts",
    "worktree_path",
]
