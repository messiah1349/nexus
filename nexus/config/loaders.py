"""Loaders for human-tunable assets shipped under ``nexus/config/``.

Layout:

    nexus/config/
    ├── prompts/<name>.md         # prose with $placeholder slots
    └── domains/<name>.yaml       # structured domain defaults

Python code reads through these helpers rather than touching the file
system directly so the storage layout can evolve without ripple.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from string import Template
from typing import Any

import yaml

_CONFIG_DIR = Path(__file__).parent
_PROMPTS_DIR = _CONFIG_DIR / "prompts"
_DOMAINS_DIR = _CONFIG_DIR / "domains"


# ---------------------------------------------------------------------------
# Prompts (.md files with $placeholder substitution)
# ---------------------------------------------------------------------------


@cache
def load_prompt(name: str) -> str:
    """Return the raw text of ``prompts/<name>.md``."""
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"no prompt named {name!r} (looked at {path})")
    return path.read_text(encoding="utf-8")


def render_prompt(name: str, /, **substitutions: Any) -> str:
    """Load a prompt and substitute ``$placeholder`` slots.

    Uses ``string.Template`` (stdlib) rather than ``str.format`` so prompts
    can contain literal ``{`` / ``}`` (JSON examples, schema snippets) without
    needing to escape every brace.
    """
    template = Template(load_prompt(name))
    return template.substitute(**substitutions)


# ---------------------------------------------------------------------------
# Domain YAML defaults
# ---------------------------------------------------------------------------


@cache
def list_available_domains() -> list[str]:
    return sorted(p.stem for p in _DOMAINS_DIR.glob("*.yaml"))


@cache
def load_domain_yaml(domain: str) -> dict:
    """Return the parsed YAML dict for a domain. Validation is the caller's
    job (in `nexus/domains/registry.py` it's wrapped in `DomainConfig`)."""
    path = _DOMAINS_DIR / f"{domain}.yaml"
    if not path.exists():
        available = ", ".join(list_available_domains()) or "(none)"
        raise ValueError(f"unknown domain {domain!r}. available: {available}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
