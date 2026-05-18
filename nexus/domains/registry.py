"""Load and validate domain configs shipped with the codebase.

These YAML files are the *defaults* — the architect uses them as starting
points and tailors per-user instances of `DomainConfig` from them. The
registry doesn't talk to the database.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml

from nexus.domains.base import DomainConfig

_DOMAINS_DIR = Path(__file__).parent


@cache
def list_available_domains() -> list[str]:
    """All domain YAML names (without `.yaml`)."""
    return sorted(p.stem for p in _DOMAINS_DIR.glob("*.yaml"))


@cache
def load_domain_default(domain: str) -> DomainConfig:
    """Load `nexus/domains/<domain>.yaml` and validate as a `DomainConfig`."""
    path = _DOMAINS_DIR / f"{domain}.yaml"
    if not path.exists():
        available = ", ".join(list_available_domains()) or "(none)"
        raise ValueError(
            f"unknown domain '{domain}'. available: {available}"
        )
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return DomainConfig.model_validate(data)
