"""Workflow config — agents, labels, states. Loaded from a YAML file.

This is separate from `Settings` (which holds *runtime* config — ports, secrets,
paths). `ConductorConfig` describes *what* the orchestrator should do: which
nicknames map to which prompt roles, what labels the project should have,
whether to publish a "spawning…" comment when an agent starts.

A workflow config file is **mandatory** for `serve` and `setup`. Ship one or
more example configs in `examples/`; users copy + edit + point
`CONDUCTOR_CONFIG=` at it.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentDef(BaseModel):
    """One agent the orchestrator will spawn when its nickname is mentioned."""

    model_config = ConfigDict(extra="forbid")

    nickname: str = Field(
        ..., description="Email local-part. Becomes the @mention name and the --agent flag."
    )
    prompt_role: str = Field(
        ...,
        description="Filename stem in PROMPTS_DIR (e.g. 'system-analyst' → 'system-analyst.md').",
    )
    display_name: str = Field(
        default="",
        description="Used by `plane-conductor setup` for the bot's Plane display name.",
    )

    @model_validator(mode="after")
    def _normalize(self) -> AgentDef:
        # Nicknames are case-insensitive; canonicalise to lower so dedup works.
        object.__setattr__(self, "nickname", self.nickname.lower())
        return self


class LabelDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    color: str | None = None
    description: str = ""


class StateDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    group: str = Field(
        ..., description="One of: backlog | unstarted | started | completed | cancelled."
    )
    color: str = "#cccccc"


class LabelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifacts: list[LabelDef] = Field(default_factory=list)
    roles: list[LabelDef] = Field(default_factory=list)


class ConductorConfig(BaseModel):
    """Top-level workflow config (loaded from YAML)."""

    model_config = ConfigDict(extra="forbid")

    agents: list[AgentDef]
    labels: LabelsConfig = Field(default_factory=LabelsConfig)
    states: list[StateDef] = Field(default_factory=list)

    # Behaviour flags --------------------------------------------------------

    announce_spawn: bool = Field(
        default=True,
        description=(
            "When true, Plane Conductor posts a 'Picking up @nick…' comment to the issue "
            "as soon as an agent is spawned, and updates it to a final status on exit. "
            "Gives instant feedback in Plane even before the agent itself produces output."
        ),
    )

    # Helpers ----------------------------------------------------------------

    @model_validator(mode="after")
    def _ensure_unique_nicknames(self) -> ConductorConfig:
        seen: set[str] = set()
        for a in self.agents:
            if a.nickname in seen:
                raise ValueError(f"duplicate nickname in agents: {a.nickname!r}")
            seen.add(a.nickname)
        return self

    def agents_by_nickname(self) -> dict[str, AgentDef]:
        return {a.nickname: a for a in self.agents}

    def all_labels(self) -> list[LabelDef]:
        return [*self.labels.artifacts, *self.labels.roles]

    def all_label_names(self) -> list[str]:
        return [lbl.name for lbl in self.all_labels()]


def load_config(path: Path) -> ConductorConfig:
    """Load + validate a conductor.yaml. Raises pydantic ValidationError on bad input."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level must be a mapping, got {type(raw).__name__}")
    return ConductorConfig.model_validate(raw)
