"""Architect interview — multi-turn conversation that ends with an
`ArchitectProposal` extracted from the LLM's final turn.

The interview runs entirely in memory; nothing persists to the DB until
`nexus.architect.persist.persist_architect_output` is called with the
extracted proposal.
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from nexus.architect.prompts import ExistingProjectStub, build_architect_prompt
from nexus.domains.base import ArchitectProposal, DomainConfig, UseExistingDecision
from nexus.domains.registry import load_domain_default
from nexus.llm import ChatMessage, LLMClient, get_llm_client

_PROPOSAL_RE = re.compile(
    r"<<<PROPOSAL>>>\s*(.*?)\s*<<<END_PROPOSAL>>>", re.DOTALL
)
_USE_EXISTING_RE = re.compile(
    r"<<<USE_EXISTING>>>\s*(.*?)\s*<<<END_USE_EXISTING>>>", re.DOTALL
)


class ProposalParseError(Exception):
    """Raised when a <<<PROPOSAL>>> block exists but isn't valid JSON / schema."""


class UseExistingParseError(Exception):
    """Raised when a <<<USE_EXISTING>>> block exists but isn't valid."""


def extract_proposal(text: str) -> ArchitectProposal | None:
    """Find a `<<<PROPOSAL>>>...<<<END_PROPOSAL>>>` block in `text` and return
    the validated `ArchitectProposal`. Returns None if no block is present.

    Raises `ProposalParseError` if a block is present but malformed — caller
    can use this to ask the LLM to retry.
    """
    match = _PROPOSAL_RE.search(text)
    if match is None:
        return None
    raw = match.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProposalParseError(f"proposal block is not valid JSON: {exc}") from exc
    try:
        return ArchitectProposal.model_validate(data)
    except ValidationError as exc:
        raise ProposalParseError(f"proposal block failed schema validation: {exc}") from exc


def extract_use_existing(text: str) -> UseExistingDecision | None:
    """Find a `<<<USE_EXISTING>>>...<<<END_USE_EXISTING>>>` block in `text`
    and return the validated decision. Returns None if no block is present.

    Raises `UseExistingParseError` if a block is present but malformed.
    """
    match = _USE_EXISTING_RE.search(text)
    if match is None:
        return None
    raw = match.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UseExistingParseError(
            f"use-existing block is not valid JSON: {exc}"
        ) from exc
    try:
        return UseExistingDecision.model_validate(data)
    except ValidationError as exc:
        raise UseExistingParseError(
            f"use-existing block failed schema validation: {exc}"
        ) from exc


class ArchitectInterview:
    """Drives the architect interview turn by turn.

    Typical usage:

        interview = ArchitectInterview(domain="language_learning")
        greeting = await interview.kick_off()
        print(greeting)

        while not interview.done:
            user_input = input("> ")
            reply, done = await interview.turn(user_input)
            print(reply)

        proposal = interview.proposal  # ArchitectProposal, ready to persist
    """

    MAX_FIX_ATTEMPTS = 2

    def __init__(
        self,
        *,
        domain: str,
        llm: LLMClient | None = None,
        default_config: DomainConfig | None = None,
        existing_projects: list[ExistingProjectStub] | None = None,
    ) -> None:
        self.domain = domain
        self.default_config = default_config or load_domain_default(domain)
        self.system_prompt = build_architect_prompt(
            self.default_config,
            existing_projects=existing_projects,
        )
        self.llm = llm or get_llm_client()
        self.history: list[ChatMessage] = []
        self.proposal: ArchitectProposal | None = None
        # Set when the architect detects the user wants an existing project.
        # Mutually exclusive with `proposal`; checked in `done`.
        self.use_existing_project_id: str | None = None

    @property
    def done(self) -> bool:
        return (
            self.proposal is not None or self.use_existing_project_id is not None
        )

    async def kick_off(self) -> str:
        """First turn — LLM greets and asks initial questions."""
        opener = ChatMessage(
            role="user",
            content="Please begin the interview.",
        )
        self.history.append(opener)
        reply = await self.llm.chat(system=self.system_prompt, messages=self.history)
        self.history.append(reply)
        return reply.content

    async def turn(self, user_input: str) -> tuple[str, bool]:
        """Push a user turn through the LLM. Returns ``(assistant_text, done)``.

        If the LLM emits a `<<<PROPOSAL>>>` block that parses cleanly, ``done``
        is True and ``self.proposal`` is populated. If the block is malformed,
        we ask the LLM to fix it (up to ``MAX_FIX_ATTEMPTS`` extra calls
        without consuming a user turn) before giving up and returning False —
        leaving the user to drive the conversation further.
        """
        self.history.append(ChatMessage(role="user", content=user_input))
        return await self._drive_one_assistant_turn()

    async def _drive_one_assistant_turn(self) -> tuple[str, bool]:
        last_assistant: ChatMessage | None = None
        for attempt in range(self.MAX_FIX_ATTEMPTS + 1):
            reply = await self.llm.chat(
                system=self.system_prompt, messages=self.history
            )
            self.history.append(reply)
            last_assistant = reply

            # The USE_EXISTING marker wins if both appear; the prompt forbids
            # emitting both, so this is mainly defensive.
            try:
                use_existing = extract_use_existing(reply.content)
            except UseExistingParseError as exc:
                if attempt < self.MAX_FIX_ATTEMPTS:
                    self.history.append(
                        ChatMessage(
                            role="user",
                            content=(
                                f"The <<<USE_EXISTING>>> block didn't parse: {exc}\n"
                                "Send it again with corrected JSON."
                            ),
                        )
                    )
                    continue
                return reply.content, False

            if use_existing is not None:
                self.use_existing_project_id = use_existing.project_id
                return reply.content, True

            try:
                proposal = extract_proposal(reply.content)
            except ProposalParseError as exc:
                if attempt >= self.MAX_FIX_ATTEMPTS:
                    return reply.content, False
                self.history.append(
                    ChatMessage(
                        role="user",
                        content=(
                            f"The <<<PROPOSAL>>> block didn't parse: {exc}\n"
                            "Please send the entire proposal block again, "
                            "with corrected JSON. Keep the rest of your message brief."
                        ),
                    )
                )
                continue

            if proposal is not None:
                self.proposal = proposal
                return reply.content, True
            return reply.content, False

        assert last_assistant is not None  # for type-checker
        return last_assistant.content, False
