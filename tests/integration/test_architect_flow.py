from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from nexus.architect import extract_proposal, persist_architect_output
from nexus.architect.interview import ArchitectInterview, ProposalParseError
from nexus.db import repository as repo
from nexus.domains.base import (
    ArchitectProposal,
    DomainConfig,
    PlanItem,
    PlanProposal,
    Profile,
    SummaryConfig,
)
from nexus.llm import ChatMessage, LLMClient


class _ScriptedLLM(LLMClient):
    """LLM stub for architect tests — returns a pre-scripted sequence of
    assistant messages, one per chat() call. Lets us drive the interview
    without hitting Anthropic.
    """

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    async def chat(
        self,
        *,
        messages,
        system=None,
        max_tokens=4096,
        model=None,
    ) -> ChatMessage:
        self.calls.append((system or "", list(messages)))
        if not self._replies:
            raise AssertionError("scripted LLM ran out of replies")
        return ChatMessage(role="assistant", content=self._replies.pop(0))


def _sample_proposal() -> ArchitectProposal:
    return ArchitectProposal(
        project_name="Spanish B2",
        config=DomainConfig(
            domain="language_learning",
            profile=Profile.model_validate(
                {
                    "language": "spanish",
                    "proficiency_target": "B2",
                    "daily_minutes_target": 20,
                }
            ),
            summary=SummaryConfig(prompt_style="language_learning"),
            plan_horizons=["yearly", "weekly"],
        ),
        plans=[
            PlanProposal(
                name="Spanish 2026",
                horizon="yearly",
                items=[
                    PlanItem(sequence=1, title="reach B2 by Dec"),
                ],
                target_date="2026-12-01",
            ),
            PlanProposal(
                name="Week of 2026-05-18",
                horizon="weekly",
                items=[
                    PlanItem(sequence=1, title="cooking verbs"),
                    PlanItem(sequence=2, title="conditional intro"),
                ],
            ),
        ],
    )


def test_build_architect_prompt_includes_existing_projects() -> None:
    from nexus.architect import ExistingProjectStub
    from nexus.architect.prompts import build_architect_prompt
    from nexus.domains.registry import load_domain_default

    cfg = load_domain_default("language_learning")

    empty = build_architect_prompt(cfg)
    assert "(none)" in empty

    with_stubs = build_architect_prompt(
        cfg,
        existing_projects=[
            ExistingProjectStub(
                id="00000000-0000-0000-0000-000000000001",
                name="Spanish",
                profile={"language": "spanish", "proficiency_target": "B2"},
            ),
        ],
    )
    assert "Spanish" in with_stubs
    assert "B2" in with_stubs
    assert "USE_EXISTING" in with_stubs
    # The "Path A" rule must be present so the LLM knows it can route to use-existing.
    assert "semantic match found" in with_stubs


def test_extract_use_existing_happy_path() -> None:
    from nexus.architect import extract_use_existing

    text = (
        'Sounds like the existing one.\n\n'
        '<<<USE_EXISTING>>>\n{"project_id": "00000000-0000-0000-0000-000000000001"}\n'
        '<<<END_USE_EXISTING>>>\n\nLet me bind that.'
    )
    decision = extract_use_existing(text)
    assert decision is not None
    assert decision.project_id == "00000000-0000-0000-0000-000000000001"


def test_extract_use_existing_returns_none_when_absent() -> None:
    from nexus.architect import extract_use_existing

    assert extract_use_existing("plain conversation, no marker") is None


def test_extract_use_existing_raises_on_bad_json() -> None:
    import pytest

    from nexus.architect.interview import (
        UseExistingParseError,
        extract_use_existing,
    )

    with pytest.raises(UseExistingParseError):
        extract_use_existing("<<<USE_EXISTING>>>not-json<<<END_USE_EXISTING>>>")


def test_extract_proposal_happy_path() -> None:
    text = (
        "Sure, here is the plan:\n\n<<<PROPOSAL>>>\n"
        f"{_sample_proposal().model_dump_json()}\n"
        "<<<END_PROPOSAL>>>\n\nSaved!"
    )
    proposal = extract_proposal(text)
    assert proposal is not None
    assert proposal.project_name == "Spanish B2"
    assert proposal.plans[1].horizon == "weekly"


def test_extract_proposal_returns_none_when_no_marker() -> None:
    assert extract_proposal("just chatting") is None


def test_extract_proposal_raises_on_bad_json() -> None:
    import pytest

    with pytest.raises(ProposalParseError):
        extract_proposal("<<<PROPOSAL>>>not json<<<END_PROPOSAL>>>")


def test_extract_proposal_raises_on_schema_violation() -> None:
    import pytest

    with pytest.raises(ProposalParseError):
        extract_proposal('<<<PROPOSAL>>>{"project_name":"x"}<<<END_PROPOSAL>>>')


async def test_interview_extracts_proposal_on_done_turn() -> None:
    llm = _ScriptedLLM(
        replies=[
            "Welcome! What's your target language?",
            # Second LLM reply contains the proposal block
            (
                "Got it. Here's a plan.\n\n<<<PROPOSAL>>>\n"
                f"{_sample_proposal().model_dump_json()}\n"
                "<<<END_PROPOSAL>>>"
            ),
        ]
    )
    interview = ArchitectInterview(domain="language_learning", llm=llm)
    opener = await interview.kick_off()
    assert "target language" in opener
    assert not interview.done

    reply, done = await interview.turn("Spanish, aiming for B2 by December")
    assert done is True
    assert interview.proposal is not None
    assert interview.proposal.project_name == "Spanish B2"


async def test_interview_retries_on_bad_proposal_block() -> None:
    bad_block = "<<<PROPOSAL>>>not-json<<<END_PROPOSAL>>>"
    good_block = (
        "<<<PROPOSAL>>>\n"
        f"{_sample_proposal().model_dump_json()}\n"
        "<<<END_PROPOSAL>>>"
    )
    llm = _ScriptedLLM(
        replies=[
            "Welcome!",          # kick_off
            bad_block,           # first attempt — malformed
            good_block,          # retry — valid
        ]
    )
    interview = ArchitectInterview(domain="language_learning", llm=llm)
    await interview.kick_off()
    reply, done = await interview.turn("ok")
    assert done is True
    assert interview.proposal is not None
    # The retry consumed one extra LLM call without an extra user turn:
    # 1 for kick_off, 1 for the user turn, 1 for the retry.
    assert len(llm.calls) == 3


async def test_persist_architect_output_creates_project_and_plans(
    session: AsyncSession,
) -> None:
    user = await repo.create_user(session, display_name="Maria")
    proposal = _sample_proposal()

    project, plans = await persist_architect_output(
        session,
        user_id=user.id,
        domain="language_learning",
        proposal=proposal,
    )

    assert project.user_id == user.id
    assert project.name == "Spanish B2"
    assert project.domain == "language_learning"
    assert project.config["profile"]["language"] == "spanish"

    horizons = {p.horizon for p in plans}
    assert horizons == {"yearly", "weekly"}

    yearly = next(p for p in plans if p.horizon == "yearly")
    assert yearly.target_date.isoformat() == "2026-12-01"
    assert len(yearly.items) == 1

    weekly = next(p for p in plans if p.horizon == "weekly")
    assert len(weekly.items) == 2
    assert weekly.items[0]["title"] == "cooking verbs"


async def test_persist_architect_output_rejects_unknown_user(
    session: AsyncSession,
) -> None:
    import uuid as _uuid

    import pytest

    with pytest.raises(ValueError, match="no user"):
        await persist_architect_output(
            session,
            user_id=_uuid.uuid4(),
            domain="language_learning",
            proposal=_sample_proposal(),
        )
