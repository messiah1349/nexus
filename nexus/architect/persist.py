"""Persist a validated `ArchitectProposal` into project + plan rows.

All writes happen in a single transaction. Caller supplies the AsyncSession.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from nexus.db import repository as repo
from nexus.db.models import Plan, Project
from nexus.domains.base import ArchitectProposal


async def persist_architect_output(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    domain: str,
    proposal: ArchitectProposal,
) -> tuple[Project, list[Plan]]:
    """Create the project + all proposed plans under the given user.

    Returns ``(project, plans)``. Raises if the user doesn't exist.
    """
    user = await repo.get_user(session, user_id)
    if user is None:
        raise ValueError(f"no user with id {user_id}")

    project = await repo.create_project(
        session,
        user_id=user_id,
        name=proposal.project_name,
        domain=domain,
        config=proposal.config.model_dump(),
    )

    plans: list[Plan] = []
    for plan_proposal in proposal.plans:
        target_date = (
            date.fromisoformat(plan_proposal.target_date)
            if plan_proposal.target_date
            else None
        )
        plan = await repo.create_plan(
            session,
            project_id=project.id,
            name=plan_proposal.name,
            description=plan_proposal.description,
            horizon=plan_proposal.horizon,
            items=[item.model_dump() for item in plan_proposal.items],
            attributes=plan_proposal.attributes,
            target_date=target_date,
        )
        plans.append(plan)

    return project, plans
