"""Load and validate domain configs from `nexus/config/domains/*.yaml`.

The registry is a thin wrapper over `nexus.config.loaders` that adds pydantic
validation via `DomainConfig`. Code that just needs the raw YAML can call
`load_domain_yaml` directly; code that needs a typed config goes through
`load_domain_default` here.
"""

from __future__ import annotations

from functools import cache

from nexus.config.loaders import list_available_domains, load_domain_yaml
from nexus.domains.base import DomainConfig

__all__ = ["list_available_domains", "load_domain_default"]


@cache
def load_domain_default(domain: str) -> DomainConfig:
    """Load the domain YAML and validate as a `DomainConfig`."""
    return DomainConfig.model_validate(load_domain_yaml(domain))
