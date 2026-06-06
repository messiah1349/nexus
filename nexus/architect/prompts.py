"""Architect interview prompt assembly.

The system-prompt text lives in `nexus/config/prompts/architect_system.md`;
this module just renders it with the domain-specific substitutions. Keeping
the prose out of Python means non-coders (or you on a tired evening) can
tune the wording without touching code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from nexus.config import render_prompt
from nexus.domains.base import DomainConfig


@dataclass(frozen=True)
class ExistingProjectStub:
    """Lightweight view of an existing project, passed to the architect so
    it can do semantic similarity checks mid-interview.

    `id` is a string (UUID-stringified) so the LLM can copy it verbatim
    into a USE_EXISTING marker without any UUID-formatting concerns.
    """

    id: str
    name: str
    profile: dict[str, Any]


def _render_existing_projects_detail(
    projects: list[ExistingProjectStub] | None,
) -> str:
    if not projects:
        return "(none)"
    arr = [
        {"id": p.id, "name": p.name, "profile": p.profile} for p in projects
    ]
    return json.dumps(arr, indent=2)


def build_architect_prompt(
    default_config: DomainConfig,
    *,
    existing_projects: list[ExistingProjectStub] | None = None,
) -> str:
    return render_prompt(
        "architect_system",
        domain=default_config.domain,
        default_config_json=json.dumps(default_config.model_dump(), indent=2),
        existing_projects_detail=_render_existing_projects_detail(existing_projects),
    )
