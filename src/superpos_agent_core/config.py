"""Base configuration shared across every slim agent.

Each per-agent package defines its own subclass adding LLM-specific fields
(model id, API key env var name, reasoning effort, …).  Superpos and Telegram
fields are universal and live here.

Fields are seeded from env at startup and may be mutated at runtime when the
Superpos ``/agents/me`` endpoint returns server-authoritative values for
``hive_id``, ``capabilities``, ``permissions``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class BaseConfig:
    """Universal slim-agent config.  Subclass to add per-agent fields."""

    # ── Superpos ──────────────────────────────────────────────────────
    superpos_base_url: str = ""
    superpos_hive_id: str = ""
    superpos_agent_id: str = ""
    superpos_api_token: str = ""
    superpos_refresh_token: str = ""
    superpos_capabilities: list[str] = field(default_factory=list)
    superpos_permissions: list[str] = field(default_factory=list)
    superpos_poll_interval: int = 5

    # Knowledge retrieval injection: before dispatching a Superpos task, search
    # the hive knowledge store for entries relevant to the task and prepend them
    # to the prompt so the agent always starts with the hive's memory in context
    # (instead of only seeing it if it happens to call the knowledge CLI).
    superpos_knowledge_inject: bool = True
    superpos_knowledge_inject_limit: int = 5

    # ── Telegram ──────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_allowed_users: list[int] = field(default_factory=list)
    telegram_chat_id: str = ""
    # Forum topic (message_thread_id) this agent is bound to ("" = unbound).
    # When set, the agent only handles group messages posted in this topic
    # (DMs still work) and sends its proactive notifications — Superpos
    # task streams, disk alerts, permission warnings — into it, so several
    # agents can share one forum group with a topic each.
    telegram_topic_id: str = ""
    # Migration bridge for topic-scoped /new and /stop.  When an executor
    # has NOT yet switched its session/task keys from ``req.chat_id`` to
    # ``req.chat_key``, its work in a forum topic is stored under the bare
    # ``str(chat_id)`` instead of the composite ``chat:thread`` key, so a
    # topic-scoped clear/cancel would silently miss it.  Set this to True
    # for such un-migrated executors to also address the bare chat_id key.
    # Leave False (default) on migrated executors: there the bare key is the
    # legitimate General/plain-chat session, and addressing it from a topic
    # command would clear/cancel unrelated work in that same chat.
    telegram_legacy_session_keys: bool = False

    # ── Executor (LLM-agnostic) ───────────────────────────────────────
    executor_kind: str = "generic"  # subclasses set this: "claude", "codex", "gemini", …
    executor_working_dir: str = "/workspace"
    executor_worktree_isolation: bool = False
    executor_max_parallel: int = 3
    executor_max_turns: int = 30

    # Filesystem layout — where this agent stores per-LLM session/config state.
    # Defaults to /home/agent/.<executor_kind> at runtime in __post_init__.
    home_dir: str = ""

    # Voice transcription (optional — only used if Telegram receives voice notes).
    # Whisper API is the default; OpenAI API key may already be set for other
    # reasons (Codex), but Claude/Gemini agents can set this independently.
    voice_transcribe_api_key: str = ""

    # Module discovery root.  Agents that don't ship modules can leave blank.
    modules_dir: str = ""

    def __post_init__(self) -> None:
        if not self.home_dir:
            home = os.environ.get("HOME", "/home/agent")
            self.home_dir = os.path.join(home, f".{self.executor_kind}")
        if not self.modules_dir:
            self.modules_dir = os.path.join(
                self.executor_working_dir, f".{self.executor_kind}", "modules"
            )

    # ── Base env loader.  Subclasses call super().from_env() and extend. ──

    @classmethod
    def _base_env_kwargs(cls) -> dict:
        """Pull universal fields from env vars.  Subclasses extend this."""
        allowed = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
        caps = os.environ.get("SUPERPOS_CAPABILITIES", "")
        working_dir = os.environ.get("EXECUTOR_WORKING_DIR") or os.environ.get(
            "WORKING_DIR", "/workspace"
        )

        isolation_env = os.environ.get("EXECUTOR_WORKTREE_ISOLATION") or os.environ.get(
            "WORKTREE_ISOLATION"
        )
        if isolation_env is not None:
            worktree_isolation = isolation_env.lower() not in ("0", "false", "no")
        else:
            # Auto-enable when the working directory is a git repo
            worktree_isolation = os.path.isdir(os.path.join(working_dir, ".git"))

        return dict(
            superpos_base_url=os.environ.get("SUPERPOS_BASE_URL", ""),
            superpos_hive_id=os.environ.get("SUPERPOS_HIVE_ID", ""),
            superpos_agent_id=os.environ.get("SUPERPOS_AGENT_ID", ""),
            superpos_api_token=os.environ.get("SUPERPOS_API_TOKEN", ""),
            superpos_refresh_token=os.environ.get("SUPERPOS_REFRESH_TOKEN", ""),
            superpos_capabilities=[c.strip() for c in caps.split(",") if c.strip()],
            superpos_poll_interval=int(os.environ.get("SUPERPOS_POLL_INTERVAL", "5")),
            superpos_knowledge_inject=os.environ.get(
                "SUPERPOS_KNOWLEDGE_INJECT", "true"
            ).lower()
            not in ("0", "false", "no"),
            superpos_knowledge_inject_limit=int(
                os.environ.get("SUPERPOS_KNOWLEDGE_INJECT_LIMIT", "5")
            ),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_allowed_users=[
                int(u.strip()) for u in allowed.split(",") if u.strip()
            ],
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
            telegram_topic_id=os.environ.get("TELEGRAM_TOPIC_ID", ""),
            telegram_legacy_session_keys=os.environ.get(
                "TELEGRAM_LEGACY_SESSION_KEYS", "false"
            ).lower()
            in ("1", "true", "yes"),
            executor_working_dir=working_dir,
            executor_worktree_isolation=worktree_isolation,
            executor_max_parallel=int(os.environ.get("EXECUTOR_MAX_PARALLEL", "3")),
            executor_max_turns=int(os.environ.get("EXECUTOR_MAX_TURNS", "30")),
            voice_transcribe_api_key=os.environ.get("VOICE_TRANSCRIBE_API_KEY", "")
            or os.environ.get("OPENAI_API_KEY", ""),
        )

    @classmethod
    def from_env(cls) -> "BaseConfig":
        return cls(**cls._base_env_kwargs())

    # ── Properties ────────────────────────────────────────────────────

    @property
    def superpos_enabled(self) -> bool:
        return bool(
            self.superpos_base_url
            and self.superpos_hive_id
            and self.superpos_agent_id
            and self.superpos_api_token
        )

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)

    @property
    def telegram_thread_id(self) -> int | None:
        """``telegram_topic_id`` as the int the Bot API expects, or None.

        A non-numeric value is treated as unbound (with the misconfig
        logged) rather than crashing the agent at startup.
        """
        if not self.telegram_topic_id:
            return None
        try:
            return int(self.telegram_topic_id)
        except ValueError:
            log.warning(
                "TELEGRAM_TOPIC_ID=%r is not numeric — ignoring topic binding",
                self.telegram_topic_id,
            )
            return None

    def has_permission(self, permission: str) -> bool:
        """Check whether the agent has a given permission.

        Matches exact, ``category:*`` wildcards, and the ``admin:*``
        superwildcard.  If permissions are empty (unknown — /me failed
        and env doesn't carry them), returns True so the agent tries the
        call; the server will reject if it truly lacks the right.
        """
        if not self.superpos_permissions:
            return True
        if permission in self.superpos_permissions:
            return True
        if "admin:*" in self.superpos_permissions:
            return True
        if ":" in permission:
            category = permission.split(":", 1)[0]
            if f"{category}:*" in self.superpos_permissions:
                return True
        return False
