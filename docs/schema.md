# Nexus — Schema Design (Draft v0.1)

## Design principles

1. **Fixed core schema, flexible attributes.** The schema doesn't change per domain. Domain variation lives in JSONB attributes and in a per-project config row. This kills the LLM-codegen + Docker-provisioning loop entirely.
2. **Multi-tenant from day one.** Every row carries `project_id`; `project_id` carries `user_id`. No global state.
3. **Time-series friendly.** `events` is the central log. Most domain reasoning ("did I practice yesterday?", "what was my best lift this month?") becomes a query over `events`.
4. **Memory-tier aligned.** Tables map directly to the three memory tiers:
   - **Episodic** → `messages`, `events` (recent windows)
   - **Semantic** → `embeddings` (vector recall over any source)
   - **Structural** → `entities.state` + aggregations over `events` (typed stats)
5. **No deletes by default.** `archived_at` columns. Audit + recoverability matter for a memory system.

## ER overview

```
users ──< projects ──< entities ──< event_entities >── events
                   ──< messages
                   ──< summaries
                   ──< embeddings  (polymorphic ref → messages|summaries|events|entities)
```

## Tables

### `users`
Identity. One human, many projects.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| telegram_id | bigint unique nullable | populated when bot connects |
| email | text unique nullable | |
| display_name | text | |
| settings | jsonb not null default '{}' | preferred LLM, locale, timezone |
| created_at | timestamptz not null default now() | |

### `projects`
A "siloed personal assistant" instance. One per (user, domain instance). `siloed` = `WHERE project_id = ?` everywhere.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| user_id | uuid fk users(id) not null | |
| name | text not null | "Spanish B2", "Strength training" |
| domain | text not null | 'language_learning', 'fitness', 'habit_tracker', ... |
| config | jsonb not null | output of the Architect interview — see Domain Config below |
| created_at | timestamptz not null default now() | |
| archived_at | timestamptz nullable | |

Index: `(user_id) WHERE archived_at IS NULL`.

### `entities`
Anything tracked over time with state: a vocab word, an exercise, a goal, a habit, a recipe, a topic.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| project_id | uuid fk projects(id) not null | |
| type | text not null | domain-defined, e.g. 'vocab_word', 'exercise', 'goal', 'habit' |
| name | text not null | human label; unique per (project_id, type) recommended |
| attributes | jsonb not null default '{}' | descriptive, mostly-stable fields |
| state | jsonb not null default '{}' | mutable counters/levels (mastery, current_1rm, streak) |
| created_at | timestamptz not null default now() | |
| updated_at | timestamptz not null default now() | |
| archived_at | timestamptz nullable | |

Indexes: `(project_id, type)`, GIN on `attributes`, GIN on `state`.

**Why split `attributes` vs `state`:** attributes barely change (a Spanish word's translation); state changes after almost every event (mastery_level, last_reviewed_at). Separating them keeps update churn out of attribute indexes and makes reasoning easier ("describe the entity" = attributes; "how is it doing" = state).

### `events`
The append-only log. Anything that happened at a time: a workout set, a vocab review, a measurement, a check-in, a tool call.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| project_id | uuid fk projects(id) not null | |
| type | text not null | domain-defined, e.g. 'workout_set', 'vocab_review' |
| occurred_at | timestamptz not null | when it happened (≠ created_at if backfilled) |
| payload | jsonb not null default '{}' | type-specific fields (reps, weight, score) |
| source | text not null default 'agent' | 'agent' / 'manual' / 'import' |
| created_at | timestamptz not null default now() | |

Indexes: `(project_id, occurred_at desc)`, `(project_id, type, occurred_at desc)`, GIN on `payload`.

### `event_entities`
Many-to-many: an event can touch multiple entities, an entity has many events.

| column | type | notes |
|---|---|---|
| event_id | uuid fk events(id) on delete cascade | |
| entity_id | uuid fk entities(id) on delete cascade | |
| role | text not null default 'subject' | reserved for future ('subject', 'related') |

PK: `(event_id, entity_id)`. Index: `(entity_id, event_id)`.

### `messages`
Conversation turns between user and specialist agent. Kept separate from `events` because the structure is richer (role, tool calls) and access patterns differ (always-recent windows + threading).

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| project_id | uuid fk projects(id) not null | |
| role | text not null | 'user' / 'assistant' / 'system' / 'tool' |
| content | text | nullable for tool-only messages |
| metadata | jsonb not null default '{}' | tool calls, tool results, attachments, model used, token counts |
| occurred_at | timestamptz not null default now() | |

Index: `(project_id, occurred_at desc)`.

### `summaries`
Periodic reflections produced by the specialist. Daily/weekly recaps, topical syntheses.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| project_id | uuid fk projects(id) not null | |
| scope | text not null | 'daily' / 'weekly' / 'session' / 'topical' |
| period_start | timestamptz | nullable for topical |
| period_end | timestamptz | nullable for topical |
| content | text not null | |
| focus_tags | jsonb not null default '[]' | array of strings |
| created_at | timestamptz not null default now() | |

Index: `(project_id, scope, period_end desc)`.

### `embeddings`
Polymorphic vector index over any text-bearing source.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| project_id | uuid fk projects(id) not null | |
| source_table | text not null | 'messages' / 'summaries' / 'events' / 'entities' |
| source_id | uuid not null | row in that table |
| chunk_index | int not null default 0 | |
| content | text not null | the chunk text (denormalized for retrieval) |
| embedding | vector(1536) not null | pgvector — dimension matches your embed model |
| metadata | jsonb not null default '{}' | |
| created_at | timestamptz not null default now() | |

Indexes: HNSW on `embedding` (cosine), `(project_id, source_table, source_id)` for invalidation/re-embed.

**Polymorphic ref note:** no FK on `source_id` — you handle invalidation via background jobs / triggers. Keeps the table simple and lets you re-embed without cascading deletes.

## Domain config (lives in `projects.config`)

This is the artifact the Architect produces — a validated JSON document, not generated code.

```yaml
# Example: language_learning
domain: language_learning
schema_version: 1
profile:
  language: spanish
  proficiency_target: B2
  daily_minutes_target: 20
focus_tags: [vocabulary, grammar, conversation, listening]

entity_types:
  vocab_word:
    attributes_schema:
      translation: { type: string, required: true }
      part_of_speech: { type: string }
      examples: { type: array, items: string }
    state_schema:
      mastery_level: { type: integer, min: 0, max: 5, default: 0 }
      last_reviewed_at: { type: datetime, nullable: true }
      next_review_at: { type: datetime, nullable: true }
  grammar_concept:
    attributes_schema:
      explanation: { type: string }
    state_schema:
      comprehension_level: { type: integer, min: 0, max: 5, default: 0 }
  goal:
    attributes_schema: { description: { type: string } }
    state_schema:
      progress: { type: number, min: 0, max: 1, default: 0 }
      target_date: { type: date, nullable: true }

event_types:
  vocab_review:
    payload_schema:
      result: { type: enum, values: [forgot, hard, ok, easy] }
      latency_ms: { type: integer, nullable: true }
    updates_state:
      mastery_level: spaced_repetition_v1
      last_reviewed_at: now
      next_review_at: spaced_repetition_v1
  practice_session:
    payload_schema:
      duration_minutes: { type: integer }
      topic: { type: string }

memory:
  episodic:
    message_window_tokens: 4000
    event_window_days: 7
  semantic:
    sources: [messages, summaries]
    chunk_size_tokens: 400
  structural_stats:
    - vocab_mastery_distribution
    - daily_practice_streak
    - new_words_per_week

summary_cadence: daily
```

```yaml
# Example: fitness
domain: fitness
schema_version: 1
profile:
  units: metric
  experience_level: intermediate
focus_tags: [strength, hypertrophy, conditioning]

entity_types:
  exercise:
    attributes_schema:
      muscle_groups: { type: array, items: string }
      equipment: { type: string }
    state_schema:
      one_rep_max_kg: { type: number, nullable: true }
      best_volume_kg: { type: number, nullable: true }
  goal:
    attributes_schema: { description: { type: string } }
    state_schema:
      target_value: { type: number, nullable: true }
      current_value: { type: number, nullable: true }

event_types:
  workout_set:
    payload_schema:
      reps: { type: integer, required: true }
      weight_kg: { type: number, required: true }
      rpe: { type: number, nullable: true }
    updates_state:
      one_rep_max_kg: epley_estimate
  body_measurement:
    payload_schema:
      weight_kg: { type: number, nullable: true }
      bodyfat_pct: { type: number, nullable: true }

memory:
  episodic:
    message_window_tokens: 4000
    event_window_days: 14
  semantic:
    sources: [messages, summaries]
    chunk_size_tokens: 400
  structural_stats:
    - prs_last_30_days
    - weekly_volume_per_muscle_group
    - bodyweight_trend

summary_cadence: weekly
```

The config has its own pydantic schema (`nexus.domains.base.DomainConfig`). The Architect's job is to produce a config that validates against this schema; the Specialist's job is to execute against it.

## Worked example: language learning maps cleanly

| Concept | Where it lives |
|---|---|
| "the word *aprender*" | `entities` row, type=`vocab_word`, name=`aprender`, attributes={translation: "to learn", ...} |
| "I reviewed it just now and got it right" | `events` row, type=`vocab_review`, payload={result: "ok"}, joined to entity via `event_entities` |
| "my mastery of it is now 3/5" | `entities.state.mastery_level` updated by the event handler |
| "what's my streak?" | aggregation over `events` where type IN (vocab_review, practice_session) |
| "what did we talk about last Tuesday?" | semantic search on `embeddings` filtered by `project_id` |
| "weekly recap" | `summaries` row, scope=`weekly` |

## Worked example: fitness maps cleanly

| Concept | Where it lives |
|---|---|
| "Bench Press" | `entities`, type=`exercise`, attributes.muscle_groups=[chest, triceps] |
| "I just did 5×80kg" | `events`, type=`workout_set`, payload={reps:5, weight_kg:80}, joined to bench-press entity |
| "my estimated 1RM" | `entities.state.one_rep_max_kg` updated by `epley_estimate` rule |
| "weight trend" | aggregation over `events` where type=`body_measurement` |

## Open questions / decisions to revisit

1. **Conversations table?** Currently messages are flat per project. If users want to start fresh "sessions," add `conversation_id`. Defer until a real use case appears.
2. **Soft delete vs hard delete.** Defaulting to soft delete (`archived_at`). Hard-delete is a separate command, not the default.
3. **Embedding model + dimensions.** I've assumed 1536 (OpenAI/Voyage-style). If we go local (e.g., `bge-base`, dim=768), change `vector(1536)` accordingly. Decide before first migration.
4. **Schema-per-project ("real silos") vs `project_id` filtering.** Filtering is simpler and is what's drafted. Schema-per-project gives stronger isolation but complicates pooling and embeddings. Defer unless someone needs it.
5. **State update rules** (`epley_estimate`, `spaced_repetition_v1`): these are named handlers in `nexus.specialist.stats`. Domain config references them by name. Open question: do we want users / the Architect to be able to define new handlers, or only pick from a fixed registry? Recommend: fixed registry for MVP.
6. **Telegram identity.** `users.telegram_id` lets a user be auto-recognized. For email login, defer until web UI exists.
