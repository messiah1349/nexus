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

from nexus.architect.prompts import build_architect_prompt
from nexus.domains.base import ArchitectProposal, DomainConfig
from nexus.domains.registry import load_domain_default
from nexus.llm import ChatMessage, LLMClient, get_llm_client

_PROPOSAL_RE = re.compile(
    r"<<<PROPOSAL>>>\s*(.*?)\s*<<<END_PROPOSAL>>>", re.DOTALL
)


class ProposalParseError(Exception):
    """Raised when a <<<PROPOSAL>>> block exists but isn't valid JSON / schema."""


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
    ) -> None:
        self.domain = domain
        self.default_config = default_config or load_domain_default(domain)
        self.system_prompt = build_architect_prompt(self.default_config)
        self.llm = llm or get_llm_client()
        self.history: list[ChatMessage] = []
        self.proposal: ArchitectProposal | None = None

    @property
    def done(self) -> bool:
        return self.proposal is not None

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
