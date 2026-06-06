"""DomainConfig — pydantic v2 schema for a project's per-domain configuration.

The architect produces an instance of this; the specialist reads it. Values are
persisted as JSONB into ``projects.config``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Profile(BaseModel):
    """Domain-specific profile fields. Examples:

    - language_learning: ``language``, ``proficiency_target``, ``daily_minutes_target``
    - fitness: ``units``, ``experience_level``

    Extras are allowed so each domain can carry its own keys without a schema
    change here.
    """

    model_config = ConfigDict(extra="allow")


class SessionDefaults(BaseModel):
    idle_timeout_minutes: int = 30
    expected_duration_minutes: int = 20


class SummaryConfig(BaseModel):
    prompt_style: str
    allow_plan_revision: bool = True


class DomainConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str
    schema_version: int = 2
    profile: Profile = Field(default_factory=Profile)
    focus_tags: list[str] = Field(default_factory=list)
    sessions: SessionDefaults = Field(default_factory=SessionDefaults)
    summary: SummaryConfig
    plan_horizons: list[str] = Field(default_factory=lambda: ["yearly", "weekly"])


# ---------------------------------------------------------------------------
# Plan proposal — what the architect emits per plan it wants to create.
# Persistence layer maps these onto Plan rows.
# ---------------------------------------------------------------------------


class PlanItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    sequence: int
    title: str
    description: str | None = None
    status: str = "pending"  # 'pending' | 'in_progress' | 'completed' | 'skipped'
    due_date: str | None = None  # ISO date string


class PlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    horizon: str  # 'yearly' | 'monthly' | 'weekly' | 'goal' | 'level_check'
    items: list[PlanItem] = Field(default_factory=list)
    attributes: dict = Field(default_factory=dict)
    target_date: str | None = None  # ISO date


class ArchitectProposal(BaseModel):
    """Top-level structured output of the architect interview."""

    model_config = ConfigDict(extra="forbid")

    project_name: str
    config: DomainConfig
    plans: list[PlanProposal]


class UseExistingDecision(BaseModel):
    """Emitted by the architect when the user confirms continuing an
    existing project instead of creating a new one (mid-interview
    semantic-match path)."""

    model_config = ConfigDict(extra="forbid")

    project_id: str  # UUID as string; parsed at the boundary


# ---------------------------------------------------------------------------
# Summarizer output — emitted by the specialist's summarize prompt at session
# end. Drives `summaries` row creation, plan-item patches, and plan revisions.
# ---------------------------------------------------------------------------


class PlanItemUpdate(BaseModel):
    """In-place patch to a single item on an existing plan."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str  # UUID as string; parsed at the persistence boundary
    item_index: int
    status: str  # 'pending' | 'in_progress' | 'completed' | 'skipped'


class PlanRevision(BaseModel):
    """Replace an existing plan with a new one (supersede chain)."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str  # the plan being superseded
    reason: str
    new_plan: PlanProposal


class SessionSummary(BaseModel):
    """Structured output of the summarize prompt — one row in `summaries`
    plus optional plan mutations."""

    model_config = ConfigDict(extra="forbid")

    content: str
    focus_tags: list[str] = Field(default_factory=list)
    plan_item_index_addressed: int | None = None
    plan_item_update: PlanItemUpdate | None = None
    plan_revision: PlanRevision | None = None
