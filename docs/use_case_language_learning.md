# Use Case Trace — Language Learning, Days 0–2 (v1 model)

A walk-through of Maria's first interactions with a `language_learning` project under the **v1 design**: plan-driven, session-bounded, no per-turn tool calls. The goal is to validate that the schema in `docs/schema.md` actually carries this flow.

Notation:

- `repo.fn(...)` — call into `nexus/db/repository.py`
- `db.WRITE <table> { ... }` — row inserted/updated
- `llm.chat(...)` — provider-agnostic LLM call
- `[ctx: plans|summaries|session_messages]` — which slice of the context was loaded

---

## Day 0 — Architect onboarding (one-time)

Maria sends `/architect` in Telegram. Bot picks her domain (`language_learning`) and runs the interview.

The architect interview is itself a session with `kind='architect'`. Messages persist as the interview proceeds; nothing else writes to the DB during the interview turns. At the end of the interview:

1. The architect LLM emits a **structured proposal** containing:
   - A `DomainConfig` (validated against `nexus.domains.base.DomainConfig`).
   - One or more `Plan`s — typically a `yearly` and a `weekly`, sometimes a `level_check` if Maria wasn't sure of her starting point.
2. `nexus/architect/persist.py` validates the proposal and writes:
   - `db.WRITE projects.config = { domain: language_learning, profile: { language: spanish, proficiency_target: B2, daily_minutes_target: 20 }, sessions: { idle_timeout_minutes: 30 }, summary: { prompt_style: language_learning, allow_plan_revision: true } }`
   - `db.WRITE plans { id=P_year, horizon=yearly, name="Spanish B2 by Dec 2026", items=[12 monthly milestones], status=active }`
   - `db.WRITE plans { id=P_week, horizon=weekly, name="Week of 2026-05-18", items=[{seq:1, title:"Cooking verbs", status:pending}, {seq:2, title:"Conditional tense intro", status:pending}, {seq:3, title:"Restaurant vocabulary", status:pending}], status=active }`
3. The architect session ends. Its summary is the plan itself — no separate `summaries` row is required in v1.

**End state:** `projects.config` populated, two active plans for Maria's Spanish project.

---

## Day 1, Monday 18:00 — first lesson

### M1. Maria → "Hola! Ready for today's lesson"

1. Telegram client looks up `users.settings.active_project_per_chat[chat_id]` → project P.
2. `specialist.session.open_or_resume_session(P)`:
   - Query: any session for P with `status='active'`? No.
   - `db.WRITE sessions { id=S1, project_id=P, plan_id=P_week, plan_item_index=NULL, kind=lesson, status=active, started_at=Mon 18:00 }`
   - **Note:** `plan_item_index` is null at session creation — the LLM picks the focus at its first turn (CLAUDE.md decision #9, "coach-style autonomy"). The summarizer writes the index back at session end, recording what was *actually* worked on.
3. `repo.add_message(project_id=P, session_id=S1, role=user, content="Hola! ...")`
   - `db.WRITE messages { id=M1, session_id=S1, role=user, content="Hola! ..." }`
4. **`specialist.context.build(S1)`** — called *once* per session, result cached for the rest of the session:
   - **[ctx: plans]** `repo.get_active_plans(P)` → `[P_year, P_week]`. Yearly gives strategic context; weekly gives this-week's items.
   - **[ctx: summaries]** `repo.recent_summaries(P, scope=session, limit=5)` → `[]` (none yet).
   - **[ctx: session_messages]** `repo.list_messages_for_session(S1)` → `[M1]`.
5. System prompt is built from (a) `prompts.base_language_learning`, (b) compact rendering of both plans with all items + statuses (no "this is the current item" highlighting — the LLM decides), (c) the empty summary list. The system prompt instructs the LLM to **pick a focus, state it, and check in with the user** at the start of each session.
6. `llm.chat(system=..., history=[M1])` — no tools available.
7. LLM response: "¡Hola! Based on the plan, I'd like to start with **cooking verbs** today — Item 1 of this week. We'll learn three and practice. Want to go with that, or something else?"
8. `repo.add_message(project_id=P, session_id=S1, role=assistant, content="...", meta={model:"claude-...", input_tokens:..., output_tokens:...})`
   - `db.WRITE messages { id=M2, session_id=S1, role=assistant, ... }`

### M2. Maria → "Yes, let's go"

1. Persist user message → `db.WRITE messages { id=M3, session_id=S1 }`.
2. Context is already cached for this session. Only the new turn is appended.
3. `llm.chat(system=cached, history=[M1, M2, M3])`.
4. LLM teaches: "Three useful cooking verbs: **picar** (to chop), **hervir** (to boil), **freír** (to fry). Examples..."
5. Persist assistant message → `db.WRITE messages { id=M4, session_id=S1 }`.

**Notable:** the agent does not create a `vocab_word` entity, log a `practice_session` event, or call any other tool. The vocab lives in the message text and will be captured by the session summary.

### M3. Maria → "Quiz me — translate these three for me"

Persist M5; LLM responds with a quiz; persist M6. Pure conversation.

### M4. Maria → "picar = to chop, hervir = to boil, freír = to fry"

Persist M7; LLM grades inline ("Perfect — all three correct! Mastery feels easy."); persist M8.

In v1 the *fact* that Maria got these right is encoded only in the message stream and will be summarized at session end. No row is written to `events`.

### M5. Maria → "Gotta go, see you tomorrow"

Persist M9; LLM says goodbye; persist M10.

Maria closes Telegram. No explicit `/end` was sent.

### Session end — idle timeout fires at 18:30

A small in-process job (`nexus/workers/timeout.py`) runs every minute and finds sessions where `status='active' AND now() - max(messages.occurred_at) > idle_timeout_minutes`. S1 matches.

**`specialist.summarizer.end_session(S1, reason="timeout")`:**

1. `repo.list_messages_for_session(S1)` → all 10 messages.
2. `llm.chat(system=prompts.summarize_language_learning, history=[transcript])` → structured response:
   ```
   {
     "content": "Maria's first lesson. Learned three Spanish cooking verbs:
                 picar, hervir, freír. Quizzed and got all three correct
                 on first try, no apparent difficulty. Engagement was high.",
     "focus_tags": ["vocabulary", "cooking"],
     "plan_item_index_addressed": 0,
     "plan_item_update": { "plan_id": "P_week", "item_index": 0, "status": "completed" },
     "plan_revision": null
   }
   ```
3. `db.WRITE summaries { id=Summary1, project_id=P, session_id=S1, scope=session, content="Maria's first lesson...", focus_tags=["vocabulary","cooking"], period_start=Mon 18:00, period_end=Mon 18:11 }`
4. `db.WRITE sessions UPDATE id=S1 SET ended_at=Mon 18:11, end_reason="timeout", status="completed", summary_id=Summary1, plan_item_index=0`
5. `repo.patch_plan_item(plan_id=P_week, item_index=0, patch={status:"completed"})`:
   - Loads plan, applies JSONB patch to `items[0].status`, writes back. `db.WRITE plans UPDATE id=P_week SET items=..., updated_at=now()`.
6. No plan revision — `plan_revision` was null.

**End state after Day 1:** S1 closed, one session summary, the weekly plan's first item marked completed.

---

## Day 2, Tuesday 09:30 — return

### M1 (Day 2). Maria → "Buenos días!"

1. `specialist.session.open_or_resume_session(P)`:
   - Any session for P with `status='active'`? No (S1 closed).
   - `db.WRITE sessions { id=S2, project_id=P, plan_id=P_week, plan_item_index=NULL, kind=lesson, started_at=Tue 09:30 }`
2. `repo.add_message(...)` → `db.WRITE messages { id=N1, session_id=S2 }`.
3. **`context.build(S2)`**:
   - **[ctx: plans]** `[P_year, P_week]` — items[0] now shows `completed`; items[1] (conditional tense intro) is the obvious next pending item, but no field designates it as "the current target" — the LLM picks.
   - **[ctx: summaries]** `[Summary1]` (yesterday's session summary).
   - **[ctx: session_messages]** `[N1]`.
4. `llm.chat(system=..., history=[N1])`.
5. LLM response, using yesterday's summary: "¡Buenos días! Yesterday you nailed *picar / hervir / freír* — nice work. Plan has us starting the **conditional tense** next. Want to dive in, or anything else first?"
6. Persist assistant message.

**Trace what this turn proved:**
- Cross-day continuity works because Summary1 is loaded at session start.
- The LLM (not a rule) picks the focus from the plan + summary context and tells Maria. She can override (and does — see M2 below).

### M2 (Day 2). Maria → "Actually, can we revisit yesterday's verbs first? I'm not 100% sure I remember them."

This is an **interruption / plan deviation**. The agent has two reasonable behaviors:

- (A) Just accommodate; the session summary at the end will note that conditional tense did *not* happen and that yesterday's verbs were revisited.
- (B) Accommodate *and* update the plan immediately (defer the conditional tense item to tomorrow).

V1 picks (A) — no mid-session plan writes. The summarizer at session end will pick this up. Specifically:

1. Persist user message.
2. `llm.chat(...)` — agent says "Of course. Let me quiz you. Translate: picar, hervir, freír."
3. Persist assistant message.
4. Continues for a few turns of review.

### Session end — Maria sends `/end`

Explicit end via Telegram command.

**`specialist.summarizer.end_session(S2, reason="explicit")`:**

1. Load session messages.
2. LLM summarize call:
   ```
   {
     "content": "Maria asked to revisit yesterday's cooking verbs before
                 starting the planned conditional-tense intro. Reviewed all
                 three with no errors. The conditional-tense item from the
                 weekly plan was not covered this session.",
     "focus_tags": ["vocabulary", "review"],
     "plan_item_index_addressed": 0,
     "plan_item_update": null,
     "plan_revision": {
       "reason": "user-driven schedule shift",
       "new_plan": {
         "horizon": "weekly",
         "name": "Week of 2026-05-18 (rev 2)",
         "items": [
           { "sequence": 1, "title": "Cooking verbs", "status": "completed" },
           { "sequence": 2, "title": "Cooking verbs spaced review", "status": "completed" },
           { "sequence": 3, "title": "Conditional tense intro", "status": "pending" },
           { "sequence": 4, "title": "Restaurant vocabulary", "status": "pending" }
         ]
       }
     }
   }
   ```
3. `db.WRITE summaries { id=Summary2, session_id=S2, ... }`
4. `db.WRITE sessions UPDATE id=S2 SET ended_at, status=completed, summary_id=Summary2, plan_item_index=0`
5. `plan_item_update` is null → no in-place patch.
6. `plan_revision` is non-null → revise (coach-style autonomous — see CLAUDE.md #9; not gated on user confirmation):
   - `db.WRITE plans { id=P_week_v2, ...new items..., status=active, project_id=P, horizon=weekly }`
   - `db.WRITE plans UPDATE id=P_week SET status=superseded, superseded_by=P_week_v2, updated_at=now()`
7. The user is *notified* in the next session — see Day 3 sketch below. They are not asked to accept/reject before the revision lands.

---

## Day 3 (sketch) — plan revision is surfaced to the user

Maria starts another session. Day 3's context-build now sees a fresh detail: the active weekly plan (`P_week_v2`) was created *after* the prior session ended, and `P_week` is superseded. The context-builder annotates this in the system prompt block:

> Weekly plan was revised at end of last session. Previous: `P_week` (cooking verbs → conditional tense → restaurant vocab). Now: `P_week_v2` (cooking verbs + cooking-verb review marked done; conditional tense moved to item 3; restaurant vocab item 4). Reason recorded by summarizer: "user-driven schedule shift".

The system prompt also instructs the LLM: *"If a plan revision happened since the last session, mention it briefly in your first turn so the user knows about it. Treat it as confirming a coaching decision, not asking permission."*

Maria's first turn on Day 3: "Hola!"

LLM's response (paraphrased): "¡Buenos días! Heads up — based on yesterday's session, I adjusted the weekly plan: we counted the cooking-verb review as done and pushed conditional tense to next. So today's plan is **conditional tense intro**. Want to dive in, or change anything?"

If Maria says "actually keep conditional for later, I want to keep practicing verbs" → that's natural-language override. The summarizer at this session's end will write another revision. No special command, no modal "accept/reject" dialog.

---

## What this trace exercises

| Concern | Where it appears |
|---|---|
| Architect produces config + plans | Day 0 |
| Lazy session open on first message | M1 Day 1, M1 Day 2 |
| Continuous message persistence | every turn |
| Context loaded *once* at session start, reused | M1–M5 Day 1 share one cached context |
| Idle-timeout session end | end of Day 1 |
| Explicit `/end` session end | end of Day 2 |
| Summary generation at session end | both days |
| Plan-item status patch (in place) | end of Day 1 |
| Plan revision (supersede + new plan) | end of Day 2 |
| Cross-day continuity via summary | M1 Day 2 |

## What this trace does *not* exercise (deferred to v2)

- Creating `entities` for vocab words, exercises, or any other tracked thing.
- Logging `events` for vocab reviews, workout sets, measurements.
- Semantic search over messages or summaries.
- Mid-session retrieval ("let me check my notes").
- Structural stats ("vocab mastery distribution", "weekly volume").

If, after using v1 for a while, the agent feels "forgetful" or imprecise about specific facts older than the last 5 summaries, that's the signal to prioritize **V2.2 (embeddings)** and **V2.3 (mid-session retrieval)**. If it feels too vague about quantitative progress, prioritize **V2.1 (structured extraction)** + **V2.4 (structural stats)**.

## Open questions surfaced

1. **Plan compaction.** If revisions accumulate (many `superseded` rows), the history is preserved but cluttered. Not a v1 problem; revisit when needed.
2. **Architect re-runs.** What if Maria wants to redo her plan from scratch? `/architect` in Telegram should be safe to re-run — it produces *new* plans and supersedes existing actives. Implementation detail for Phase 2.

## Resolved during design review

- **Plan revision auto-apply vs confirmation** (2026-05-18) — auto-apply, notify next session, natural-language override. Coach-style autonomy per CLAUDE.md decision #9.
- **`plan_item_index` selection** (2026-05-18) — LLM decides at session start from plan + summaries; index is written *post-hoc* by the summarizer at session end (what was actually addressed), not at session creation.
