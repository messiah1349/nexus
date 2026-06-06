"""Smoke tests for nexus.config.loaders. No DB, no LLM."""

from __future__ import annotations

import pytest

from nexus.config import (
    list_available_domains,
    load_domain_yaml,
    load_prompt,
    render_prompt,
)


def test_list_available_domains_finds_language_learning() -> None:
    domains = list_available_domains()
    assert "language_learning" in domains


def test_load_domain_yaml_returns_dict() -> None:
    data = load_domain_yaml("language_learning")
    assert data["domain"] == "language_learning"
    assert "summary" in data


def test_load_domain_yaml_raises_for_unknown() -> None:
    with pytest.raises(ValueError, match="unknown domain"):
        load_domain_yaml("nope")


def test_load_prompt_returns_text() -> None:
    text = load_prompt("architect_system")
    assert "onboarding coach" in text
    assert "$domain" in text  # placeholders are present in the raw text


def test_load_prompt_raises_for_unknown() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt("does_not_exist")


def test_render_prompt_substitutes_placeholders() -> None:
    rendered = render_prompt(
        "architect_system",
        domain="fitness",
        default_config_json='{"placeholder": "value"}',
        existing_projects_detail="(none)",
    )
    assert "Domain: fitness" in rendered
    assert '"placeholder": "value"' in rendered
    assert "$domain" not in rendered  # placeholders fully substituted
    # Literal braces in the prompt body survive unescaped (the whole point
    # of switching from str.format to string.Template).
    assert "<<<PROPOSAL>>>" in rendered
    assert '{"project_name"' in rendered
