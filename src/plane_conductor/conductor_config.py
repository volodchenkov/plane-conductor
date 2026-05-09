"""Workspace config — one self-contained YAML per workspace.

Layout (nginx-vhost style):

    /etc/plane-conductor/conductor.d/
        qsale.yaml      # full self-contained workspace (creds + workflow)
        aist.yaml

Each file describes ONE workspace: Plane creds, project, initiator, agent
working dir, prompts dir, plus the agents / labels / states for that
workspace's workflow. Files are gitignored on the host (they hold secrets).

The top-level `Settings` (see `config.py`) keeps only host-wide runtime
concerns (port, log dir, capacity, timeouts) — nothing workspace-specific.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
        # Trim and reject whitespace inside — a typo like "dev " would otherwise
        # turn into an invalid invite email and a nickname that never matches
        # mentions. Fail fast at config load.
        nickname = self.nickname.strip().lower()
        if not nickname or any(ch.isspace() for ch in nickname):
            raise ValueError("agent nickname must be a non-empty single token")
        object.__setattr__(self, "nickname", nickname)
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


def _default_pipeline_labels() -> list[LabelDef]:
    """Pipeline-routing labels created in every new workspace by default.

    `pipeline:doc-only` triggers the short doc pipeline (see
    claude-sdlc-agents `plane-api.md` §6.13b): initiator triggers a coder
    directly, no SPEC / ARCH_REVIEW / tests / final REVIEW.
    """
    return [
        LabelDef(
            name="pipeline:doc-only",
            color="#7c8db5",
            description=(
                "Short pipeline: documentation-only. Initiator triggers a "
                "coder directly; no SPEC/ARCH_REVIEW/tests/REVIEW."
            ),
        ),
    ]


class LabelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifacts: list[LabelDef] = Field(default_factory=list)
    roles: list[LabelDef] = Field(default_factory=list)
    pipelines: list[LabelDef] = Field(default_factory=_default_pipeline_labels)


class WorkspaceConfig(BaseModel):
    """Self-contained per-workspace config. One YAML file per workspace."""

    model_config = ConfigDict(extra="forbid")

    # --- identity / Plane connection ---------------------------------------
    workspace_slug: str = Field(
        ...,
        description="Workspace slug (lowercase). Path segment of the webhook URL: /<slug>/webhook.",
    )
    plane_base_url: str = Field(..., description="Plane base URL, e.g. https://plane.example.io")
    plane_api_key: str = Field(..., description="Plane API token (workspace-scoped)")
    project_id: UUID = Field(..., description="Project UUID inside the workspace.")
    initiator_uuid: UUID = Field(
        ...,
        description="Human initiator UUID — ignored as a target so we don't trigger you as an agent.",
    )

    # --- webhook -----------------------------------------------------------
    webhook_secret: str = Field(
        ..., description="HMAC secret for inbound webhook verification (per workspace)."
    )
    webhook_signature_header: str = Field(
        default="X-Plane-Signature", description="Header Plane uses to send the signature."
    )

    # --- agent invocation --------------------------------------------------
    email_domain: str = Field(
        ..., description="Bot email domain. setup invites <nickname>@<email_domain>."
    )
    prompts_dir: Path = Field(..., description="Directory holding agent prompt files (<role>.md).")
    agent_working_dir: Path | None = Field(
        default=None, description="Working dir for spawned claude. Defaults to cwd."
    )

    # --- workflow ----------------------------------------------------------
    agents: list[AgentDef]
    labels: LabelsConfig = Field(default_factory=LabelsConfig)
    states: list[StateDef] = Field(default_factory=list)

    # --- behaviour ---------------------------------------------------------
    announce_spawn: bool = Field(
        default=True,
        description=(
            "When true, post a 'Picking up @nick…' comment on spawn and update it on exit. "
            "Gives instant feedback in Plane even before the agent itself produces output."
        ),
    )
    allowed_nicknames: list[str] = Field(
        default_factory=list,
        description="Allow-list of nicknames. Empty = allow all configured agents.",
    )

    # --- validators / helpers ---------------------------------------------

    @field_validator("workspace_slug", mode="before")
    @classmethod
    def _validate_slug(cls, v: object) -> str:
        # Slug becomes the URL path segment (`/<slug>/webhook`) and the prefix
        # of every log/sentinel filename. Reject anything that is not safe in
        # both contexts: lowercase a-z/0-9/-, must start alphanumeric, max 63.
        if not isinstance(v, str):
            raise ValueError("workspace_slug must be a string")
        s = v.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", s):
            raise ValueError(
                "workspace_slug must be 1-63 chars, lowercase a-z/0-9/-, starting with a-z or 0-9"
            )
        return s

    @field_validator("webhook_secret", mode="after")
    @classmethod
    def _validate_webhook_secret(cls, v: str) -> str:
        # The HMAC secret is the only thing standing between an attacker who
        # knows the URL and a remote agent spawn. Reject:
        #   - the shipped placeholder string
        #   - anything below 32 chars
        #   - obviously-not-random "secrets" like 64 zeros / 64 fs
        secret = v.strip()
        if secret == "replace-me-with-openssl-rand-hex-32":
            raise ValueError(
                "webhook_secret is still the example placeholder; replace it "
                "(generate a fresh secret with `openssl rand -hex 32`)"
            )
        if len(secret) < 32:
            raise ValueError(
                "webhook_secret must be at least 32 characters (use "
                "`openssl rand -hex 32` to generate a strong one)"
            )
        if re.fullmatch(r"([0-9a-fA-F])\1{31,}", secret):
            raise ValueError(
                "webhook_secret looks like a monorepeat hex string "
                "(e.g. all zeros) — replace with `openssl rand -hex 32`"
            )
        return secret

    @model_validator(mode="after")
    def _normalize(self) -> WorkspaceConfig:
        object.__setattr__(self, "plane_base_url", self.plane_base_url.rstrip("/"))
        object.__setattr__(
            self,
            "allowed_nicknames",
            [n.lower().strip() for n in self.allowed_nicknames if n and n.strip()],
        )
        return self

    @model_validator(mode="after")
    def _ensure_unique_nicknames(self) -> WorkspaceConfig:
        seen: set[str] = set()
        for a in self.agents:
            if a.nickname in seen:
                raise ValueError(f"duplicate nickname in agents: {a.nickname!r}")
            seen.add(a.nickname)
        return self

    def agents_by_nickname(self) -> dict[str, AgentDef]:
        return {a.nickname: a for a in self.agents}

    def all_labels(self) -> list[LabelDef]:
        return [*self.labels.artifacts, *self.labels.roles, *self.labels.pipelines]

    def all_label_names(self) -> list[str]:
        return [lbl.name for lbl in self.all_labels()]

    @property
    def allowed_nicknames_set(self) -> frozenset[str]:
        return frozenset(self.allowed_nicknames)


def load_workspace(path: Path) -> WorkspaceConfig:
    """Load + validate one workspace YAML."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level must be a mapping, got {type(raw).__name__}")
    return WorkspaceConfig.model_validate(raw)


def load_workspaces(directory: Path) -> dict[str, WorkspaceConfig]:
    """Scan a directory for `*.yaml` / `*.yml` workspace configs.

    Returns a slug-keyed dict. Validates that:
      - the directory exists and contains at least one workspace file
      - every file's filename stem matches its `workspace_slug` (catches typos)
      - slugs are unique across files
    """
    if not directory.exists():
        raise FileNotFoundError(f"conductor dir not found: {directory}")
    if not directory.is_dir():
        raise NotADirectoryError(f"conductor dir is not a directory: {directory}")

    files = sorted(
        [p for p in directory.iterdir() if p.suffix in {".yaml", ".yml"} and p.is_file()]
    )
    if not files:
        raise FileNotFoundError(f"no *.yaml workspace configs found in {directory}")

    workspaces: dict[str, WorkspaceConfig] = {}
    for path in files:
        ws = load_workspace(path)
        if ws.workspace_slug != path.stem.lower():
            raise ValueError(
                f"{path}: workspace_slug={ws.workspace_slug!r} does not match filename stem {path.stem!r}"
            )
        if ws.workspace_slug in workspaces:
            raise ValueError(f"duplicate workspace_slug {ws.workspace_slug!r} across files")
        workspaces[ws.workspace_slug] = ws
    return workspaces
