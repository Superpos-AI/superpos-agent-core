"""Mutable runtime overrides for model + reasoning effort.

`BaseConfig` is boot-time and seeded from env.  This holder owns the two
user-tunable knobs that can change while the agent is running (via `/model`
and `/effort` Telegram commands) and persists them to a JSON file on the
home volume so the choice survives container restarts.

Per-agent packages subclass to expose their own ``KNOWN_MODELS`` and
``EFFORT_LEVELS`` for the `/model list` / `/effort` commands.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)


class RuntimeConfig:
    """Per-agent runtime knobs.  Subclass to override KNOWN_MODELS/EFFORT_LEVELS."""

    KNOWN_MODELS: tuple[str, ...] = ()
    EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high")
    MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._\-/:]*$", re.IGNORECASE)

    def __init__(
        self,
        model: str,
        effort: str,
        path: str,
    ) -> None:
        self.model = model
        self.effort = effort
        self._path = path

    @classmethod
    def load(
        cls,
        *,
        default_model: str,
        default_effort: str,
        home_dir: str,
        filename: str = "runtime_config.json",
    ) -> "RuntimeConfig":
        path = os.path.join(home_dir, filename)
        rc = cls(model=default_model, effort=default_effort, path=path)
        if os.path.exists(path):
            try:
                data = json.loads(Path(path).read_text())
                if isinstance(data.get("model"), str):
                    rc.model = data["model"]
                if isinstance(data.get("effort"), str):
                    rc.effort = data["effort"]
                log.info(
                    "RuntimeConfig loaded from %s (model=%s, effort=%s)",
                    path, rc.model, rc.effort,
                )
            except (OSError, json.JSONDecodeError) as e:
                log.warning("runtime_config.json unreadable (%s) — using env defaults", e)
        return rc

    def _save(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        Path(self._path).write_text(
            json.dumps({"model": self.model, "effort": self.effort})
        )

    def set_model(self, model: str) -> None:
        if not self.MODEL_RE.match(model):
            raise ValueError(f"Not a valid model id: {model!r}")
        self.model = model
        self._save()

    def set_effort(self, effort: str) -> None:
        if effort not in self.EFFORT_LEVELS:
            raise ValueError(
                f"Effort must be one of {', '.join(self.EFFORT_LEVELS)} — got {effort!r}"
            )
        self.effort = effort
        self._save()
