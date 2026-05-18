# Nexus — Schema Design (Draft v0.2)

## Status

This document describes the **schema as it exists in code** after Phase 1 (migration 0001) plus the **plans/sessions extension** landing in Phase 2 (migration 0002, not yet applied at time of writing).

Tables are tagged with their v1 status:

- **[v1]** — actively written to in v1 (users, projects, plans, sessions, messages, summaries).
- **[v2-reserved]** — created in v1 migrations but not written to until v2 features land (entities, events, event_entities, embeddings).

The schema is fixed across domains regardless of which tables a domain actually exercises. Adding a new domain never changes DDL.

## Design principles

1. **Fixed core schema, flexible attributes.** No per-domain DDL. Domain variation lives in JSONB on `projects.config`, `plans.items[]`, `plans.attributes`, etc.
2. **Multi-tenant from day one.** Every row carries `project_id`; `project_id` carries `user_id`.
3. **Plan-driven.** Each project has one or more plans (yearly / weekly / level-check). Plans frame what the specialist works on.
4. **Session-bounded.** Each user interaction is a `session` with a start and end. The session is the unit that produces a summary and updates plan progress.
5. **No mid-session writes beyond messages.** During a session, only the `messages` table grows (one row per turn). Summary + plan updates happen *at session end*, not per-turn.
6. **No deletes by default.** `archived_at` columns and `status` enums; never hard-delete in the v1 path.

## ER overview (v1 path bold)

```
users ──< projects ──< plans
                   ──< sessions ──< messages
                                ──< summaries (1:1 via sessions.summary_id)
                   ──< entities       [v2-reserved]
                   ──< events         [v2-reserved] ──< event_entities >── entities
                   ──< embeddings     [v2-reserved]
```

## Tables — v1 active

### `users` [v1]

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| telegram_id | bigint unique nullable | |
| email | text unique nullable | |
| display_name | text | |
| settings | jsonb not null default '{}' | `active_project_per_chat: { telegram_chat_id: project_id }` etc. |
| created_at | timestamptz default now() | |

### `projects` [v1]

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| user_id | uuid fk users(id) | cascade delete |
| name | text | |
| domain | text | `language_learning`, `fitness`, ... |
| config | jsonb | `DomainConfig` — see "Domain config" |
| created_at, archived_at | timestamptz | |

Index: `(user_id)` partial `WHERE archived_at IS NULL`.

### `plans` [v1]

A project has one or more plans, each with a `horizon` (`yearly`, `monthly`, `weekly`, `goal`, `level_check`, ...). Plans are revised by superseding rather than mutating in place — old plans stay around with `status='superseded'` and a pointer to the new plan.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| project_id | uuid fk projects(id) | cascade |
| name | text | "Spanish B2 — yearly", "Week of 2026-05-18" |
| description | text nullable | |
| horizon | text not null | `yearly` / `monthly` / `weekly` / `goal` / `level_check` |
| status | text not null default 'active' | `active` / `completed` / `superseded` / `archived` |
| items | jsonb not null default '[]' | `[{sequence, title, description, status, due_date, attributes}, ...]` |
| attributes | jsonb not null default '{}' | `{target_level: "B2", schedule: "3x/week"}` etc. |
| target_date | date nullable | |
| superseded_by | uuid fk plans(id) nullable | self-reference, set when this plan is replaced |
| created_at, updated_at, archived_at | timestamptz | |

Indexes: `(project_id, status, horizon)`, `(project_id, horizon) WHERE status='active'`.

**Why items as JSONB rather than a separate `plan_items` table:** items are almost always read together with the plan; querying across plans for "which items are due this week" can be done with JSON-path operators when needed; not worth splitting until that query pattern actually appears.

**Why `superseded` instead of update-in-place:** plan revisions are decision artifacts — we want to be able to ask "why did the plan change?" later. Keeping the prior row preserves that history.

### `sessions` [v1]

A bounded interaction. The session opens lazily on the first message after no active session (or via explicit `/start_lesson`); it closes on explicit `/end_lesson` OR an idle timeout (default 30 min). One summary per session.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| project_id | uuid fk projects(id) | cascade |
| plan_id | uuid fk plans(id) nullable | the plan this session works against; null for architect sessions |
| plan_item_index | int nullable | which item in `plan.items[]` was planned |
| kind | text not null default 'lesson' | `lesson` / `architect` / `plan_review` |
| started_at | timestamptz default now() | |
| ended_at | timestamptz nullable | |
| end_reason | text nullable | `explicit` / `timeout` / `project_archived` |
| status | text not null default 'active' | `active` / `completed` / `abandoned` |
| summary_id | uuid fk summaries(id) nullable | set after summarizer runs |
| attributes | jsonb default '{}' | `{planned_topic, actual_topic, deviation}` |
| created_at | timestamptz default now() | |

Indexes: `(project_id, started_at desc)`, `(project_id) WHERE status='active'`.

**Active-session uniqueness:** we *don't* enforce one-active-session-per-project at the DB level. Phone + laptop could in principle have parallel sessions; the application resolves which session a given chat goes to via `users.settings`.

### `messages` [v1]

Conversation turns. **Continuous insert per turn**, even though the agent does no other writes during a session.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| project_id | uuid fk projects(id) | cascade |
| session_id | uuid fk sessions(id) nullable | added in 0002 migration |
| role | text not null | `user` / `assistant` / `system` |
| content | text nullable | |
| metadata | jsonb default '{}' | model used, tokens, etc. (mapped to Python attr `meta`) |
| occurred_at | timestamptz default now() | |

Indexes: `(project_id, occurred_at desc)`, `(session_id, occurred_at)`.

### `summaries` [v1]

One row per session in v1 (`scope='session'`). Other scopes (`daily`, `weekly`, `topical`) reserved for v2.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| project_id | uuid fk projects(id) | cascade |
| session_id | uuid fk sessions(id) nullable | added in 0002 |
| scope | text not null | `session` in v1; future: `daily` / `weekly` / `topical` |
| period_start, period_end | timestamptz nullable | for v2 scopes; for `session` use session.started_at/ended_at |
| content | text not null | the summary text |
| focus_tags | jsonb not null default '[]' | array of strings the summarizer pulled out |
| created_at | timestamptz default now() | |

Indexes: `(project_id, scope, period_end desc)`, `(session_id)`.

**Symmetry with sessions:** `sessions.summary_id` ↔ `summaries.session_id` — both directions present so queries from either side are one-hop. We accept the small denormalization risk; the summarizer writes both atomically.

## Tables — v2-reserved (created by migration 0001, idle in v1)

These tables exist so future schema changes are additive only.

### `entities` [v2-reserved]

Tracked things with state — vocab words, exercises, habits. In v1 the specialist does *not* upsert into this table; v2 structured extraction will. Schema unchanged from Phase 1: `(project_id, type, name)` unique, `attributes` and `state` JSONB, GIN indexes on both.

### `events` + `event_entities` [v2-reserved]

Append-only log of typed things-that-happened (vocab reviews, workout sets, measurements). The repo helper `add_event` exists from Phase 1 for test reasons but no production code path writes to it in v1.

### `embeddings` [v2-reserved]

pgvector(1024) + HNSW + cosine. In v1 we deliberately do not embed messages or summaries — semantic recall is a v2 feature once the v1 conversation loop has been tested and proven insufficient on its own.

## Memory model (v1)

At session start the agent's context is built once:

1. **Active plan(s)** — fetched from `plans` where `project_id = ? AND status = 'active'`. Usually 1–3 rows (yearly + weekly + maybe goal).
2. **Last K session summaries** — `summaries` where `project_id = ? AND scope = 'session'` ordered by `created_at desc` limited to K (default 5).
3. **This session's messages so far** — `messages` where `session_id = ?`. On session open this is the user's opening turn; it grows as the session progresses.

Everything above is concatenated into the system prompt + chat history. The LLM responds based only on this; it does not query the DB mid-turn. Mid-session retrieval is **v2**.

At session end the **summarizer** runs:

1. Load all messages in the session.
2. Call LLM with a summarize prompt: produce `(content, focus_tags, plan_item_index_addressed?, plan_item_status_update?, plan_revision?)`.
3. Write `summaries` row with `scope='session'`, `session_id`.
4. Update `sessions.ended_at`, `status='completed'`, `summary_id`. If `plan_item_index_addressed` is present, also set `sessions.plan_item_index` to that value (this is the only writer of `plan_item_index` — it's *not* set at session creation).
5. If `plan_item_status_update` is present, mutate the relevant `plans.items[i].status` in place (it's a simple JSONB patch).
6. If `plan_revision` is present, mark the current plan `status='superseded'`, create a new plan with `superseded_by` pointer.

**Plan-revision behavior** (resolution of v0.2 open question #2): the summarizer applies revisions itself (coach-style autonomy, see CLAUDE.md decision #9). The user is *notified* at the next session start — the agent's opening turn surfaces "the plan changed because X; here's what's new". User can push back in natural language; pushback creates another plan revision in the opposite direction. No special command, no modal "accept / reject" prompt.

**Session-focus selection** (resolution of v0.2 open question #2 alt): `sessions.plan_item_index` is *null* at session creation. The LLM, given the active plans + last K summaries in the system prompt, decides at the start of each session which item to work on and tells the user in its opening turn. User can redirect; the summarizer at session end records what was actually worked on by setting `plan_item_index`. We never store the LLM's session-start *intent* — only the post-hoc *truth* the summarizer extracts.

## Domain config (`projects.config`)

Compared to v0.1 this is simpler — no `entity_types` / `event_types` schemas, because v1 doesn't write to those tables.

```yaml
domain: language_learning
schema_version: 2
profile:
  language: spanish
  proficiency_target: B2
  daily_minutes_target: 20
focus_tags: [vocabulary, grammar, conversation, listening]

sessions:
  idle_timeout_minutes: 30
  expected_duration_minutes: 20

summary:
  prompt_style: language_learning  # which named summarizer prompt to use
  allow_plan_revision: true        # summarizer can supersede plans

plan_horizons: [yearly, weekly, level_check]  # which horizons the architect should produce
```

```yaml
domain: fitness
schema_version: 2
profile:
  units: metric
  experience_level: intermediate
focus_tags: [strength, hypertrophy, conditioning]

sessions:
  idle_timeout_minutes: 60   # workouts run longer than language lessons
  expected_duration_minutes: 60

summary:
  prompt_style: fitness
  allow_plan_revision: true

plan_horizons: [yearly, weekly, level_check]
```

The pydantic schema for `DomainConfig` lives in `nexus/domains/base.py` (Phase 2).

## Worked example: language learning, v1 path

| Concept | Where it lives in v1 |
|---|---|
| "Maria wants to reach B2 by Dec" | `plans` row, horizon=`yearly`, attributes.target_level=`B2`, target_date=2026-12-01 |
| "This week we're doing cooking verbs" | `plans` row, horizon=`weekly`, items=[{sequence:1, title:"cooking verbs", status:"in_progress"}, ...] |
| "Maria starts a lesson at 18:00" | `sessions` row created, status=`active`, plan_id=weekly plan |
| "Each turn of the lesson" | `messages` rows, session_id set |
| "Lesson wrapped, here's what happened" | `summaries` row, scope=`session`, session_id set; `plans.items[0].status` flips to `completed` |
| "Plan changed because Maria struggled" | new `plans` row (revised); old plan `status='superseded'`, `superseded_by` set |

Things that **do not** live anywhere in v1 (deferred to v2):

| Concept | v2 destination |
|---|---|
| "the word *aprender* as a tracked entity" | `entities`, type=`vocab_word` |
| "Maria reviewed *aprender* and got it right" | `events`, type=`vocab_review`, joined to entity |
| "What's my vocab mastery distribution?" | structural stat function over `events` |
| "Three weeks ago you said something about Spanish kitchens" | `embeddings` semantic search |

## Open questions / decisions to revisit

1. **Active-session collision.** What happens if two messages arrive into "the same project" from different chats while one session is active? For MVP single-chat-per-project, doesn't arise. Revisit when (if) web client lands.
2. **`messages.metadata` size.** If model responses include large tool-call traces (in v2), `metadata` can grow. Cap or split. Not a v1 problem.
3. **Embedding dimension.** Pinned at 1024 in migration 0001. Locked in for v2.
4. **What "domain" config keys does the architect *itself* need vs the specialist?** Phase 2 will refine. Current draft has both reading the same `DomainConfig`; that may split.

## Resolved decisions

- **Plan revision authorship.** Summarizer applies revisions; agent surfaces them at next session start; user override is natural-language. See "Plan-revision behavior" above and CLAUDE.md decision #9.
- **Session-focus selection.** LLM chooses focus at session start, tells the user, can be overridden. `sessions.plan_item_index` is written *only at session end* by the summarizer (post-hoc, what was actually done). See "Session-focus selection" above.
