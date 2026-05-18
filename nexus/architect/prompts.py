"""Architect interview prompt assembly.

The system-prompt text lives in `nexus/config/prompts/architect_system.md`;
this module just renders it with the domain-specific substitutions. Keeping
the prose out of Python means non-coders (or you on a tired evening) can
tune the wording without touching code.
"""

from __future__ import annotations

import json

from nexus.config import render_prompt
from nexus.domains.base import DomainConfig


def build_architect_prompt(default_config: DomainConfig) -> str:
    return render_prompt(
        "architect_system",
        domain=default_config.domain,
        default_config_json=json.dumps(default_config.model_dump(), indent=2),
    )
