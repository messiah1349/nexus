# Use Case Trace — Language Learning, Days 1 & 2

A walk-through of one user's first two interactions with a `language_learning` project. The point of this document is not to specify behavior in detail — it's to **stress-test the design** by tracing what actually happens at each step. If something can't be answered cleanly with the current schema and modules, that's a gap to fix before Phase 3.

Notation used in traces:

- `repo.fn(...)` — call into `nexus/db/repository.py`
- `db.WRITE <table> { ... }` — row inserted/updated
- `tool.<name>(...)` — tool the LLM called, dispatched in `nexus/specialist/tools.py`
- `[memory: episodic|semantic|structural]` — which tier provided context
- `llm.chat(...)` — provider-agnostic LLM call from `nexus/llm/`

---

## Setup (pre-existing before Day 1)

The Architect interview ran on Day 0 and produced a `DomainConfig`. The DB already contains:

```
users:    Maria (id = U)
projects: Spanish B2 (id = P, user_id = U, domain = "language_learning",
                      config = { episodic.message_window_tokens: 4000,
                                 episodic.event_window_days: 7,
                                 semantic.sources: [messages, summaries],
                                 structural_stats: [vocab_mastery_distribution,
                                                    daily_practice_streak,
                                                    new_words_per_week],
                                 summary_cadence: daily,
                                 entity_types: { vocab_word: {...}, goal: {...} },
                                 event_types:  { vocab_review: {...}, practice_session: {...} } })
entities: 1 row, type=goal, name="reach B2 by Dec", state.target_date=2026-12-01
events:   (none)
messages: (none)
embeddings: (none)
summaries:  (none)
```

For the trace I assume the Telegram client has already resolved the active project for this chat (via `/use Spanish B2` in a prior turn — chat-to-project routing is a Phase 4 concern, see open questions at end).

---

## Day 1 — Monday 18:00 (Maria's first real session)

### M1. Maria → "Hola! Quick session today — teach me 3 new Spanish cooking verbs."

**Specialist loop:**

1. `clients.telegram` receives text → dispatches to `specialist.agent.handle_message(project_id=P, content=...)`.
2. `repo.add_message(project_id=P, role="user", content="Hola! ...")`
   → `db.WRITE messages { id=M1, role=user, content="Hola! ...", occurred_at=18:00 }`
3. **Memory assembly** (`specialist.memory.build_context(P)`):
   - **[episodic]** `repo.recent_messages(P, limit=N)` → `[M1]` (just this one)
   - **[episodic]** `repo.recent_events(P, since=now-7d)` → `[]`
   - **[structural]** runs each stat in `config.structural_stats`:
     - `vocab_mastery_distribution(P)` → `{}` (no vocab_words yet)
     - `daily_practice_streak(P)` → `0`
     - `new_words_per_week(P)` → `0`
   - **[semantic]** skipped — query is too short to meaningfully retrieve, and there's nothing to retrieve from yet.
4. `specialist.prompts.build_system(config, stats)` → system prompt declaring the schema-constrained tools and empty stats block.
5. `llm.chat(messages=[system, M1], tools=[create_entity, update_entity_state, log_event])`
6. LLM responds with text + tool calls:
   - `tool.create_entity(type="vocab_word", name="picar", attributes={translation:"to chop", part_of_speech:"verb", examples:["Pica la cebolla finamente."]})`
   - `tool.create_entity(... "hervir" ...)`
   - `tool.create_entity(... "freír" ...)`
   - `tool.log_event(type="practice_session", payload={topic:"cooking verbs", duration_minutes:0})` *(starts a session; duration filled in later)*
   - text: "¡Claro! Three useful ones: **picar** (to chop), **hervir** (to boil), **freír** (to fry). ..."
7. Tool dispatcher executes each:
   - `repo.upsert_entity(P, "vocab_word", "picar", attributes=..., state={mastery_level:0})`
     → `db.WRITE entities { id=E_picar, ..., state.mastery_level=0 }` ×3 for the three verbs
   - `repo.add_event(P, type="practice_session", payload={topic:"cooking verbs"}, source="agent")`
     → `db.WRITE events { id=Ev_session, ... }`
8. `repo.add_message(P, role="assistant", content="¡Claro! ...", meta={tool_calls:[...], model:"claude-...", input_tokens:..., output_tokens:...})`
   → `db.WRITE messages { id=M2_asst, role=assistant, ... }`
9. Telegram client sends the assistant text back to Maria.

**End state:** 3 vocab_word entities created, 1 practice_session event, 2 messages logged.

---

### M2. Maria → "Got it. Quiz me on those three."

1. `repo.add_message(role="user", content="Got it. ...")` → `db.WRITE messages { id=M3 }`
2. **Memory assembly:**
   - **[episodic]** `recent_messages(P, limit=N)` → `[M1_user, M2_asst, M3_user]`
   - **[episodic]** `recent_events(P)` → `[Ev_session]`
   - **[structural]** `vocab_mastery_distribution(P)` → `{0: 3}` (3 words at mastery 0)
3. The system prompt's "current state" block now shows the three new vocab_words. The agent doesn't need to re-look-them-up — they're already in the structural snapshot, plus episodic recall covers them.
4. LLM call returns text-only: "Translate these for me: 1) picar 2) hervir 3) freír"
5. `repo.add_message(role="assistant", ...)` → `db.WRITE messages { id=M4_asst }`

**No tool calls.** This is the "free conversation" case — not every turn writes structured data.

---

### M3. Maria → "picar = to chop, hervir = to boil, freír = to fry"

1. `repo.add_message(role="user", ...)` → `db.WRITE messages { id=M5 }`
2. Memory assembly same shape as M2.
3. LLM grades the answers and emits tool calls:
   - `tool.log_event(type="vocab_review", payload={result:"easy", latency_ms:null}, entity_names=["picar"])`
   - same for "hervir" and "freír"
   - `tool.update_entity_state(entity_id=E_picar, state_patch={mastery_level:1, last_reviewed_at:"2026-05-11T18:03:00Z"})`
   - same for the other two
   - text: "Perfect — all three correct! ..."
4. Tool dispatcher resolves `entity_names → entity_ids` (lookup by `(P, "vocab_word", name)` via `repo.get_entity_by_name`), then:
   - For each review: `repo.add_event(P, type="vocab_review", payload={result:"easy"}, entity_ids=[E_X])`
     → 3 rows in `events`, 3 rows in `event_entities`
   - For each state update: load entity, merge state, flush
     → `entities.state` and `entities.updated_at` change for all three
5. `repo.add_message(role="assistant", ...)`

**Notable:** the `updates_state` rule in the DomainConfig (`vocab_review.updates_state.mastery_level: spaced_repetition_v1`) could compute the new mastery deterministically instead of trusting the LLM to set it. **Open question** at end.

---

### M4. Maria → "Perfect, see you tomorrow."

1. `repo.add_message` for the user turn.
2. Memory assembly identical pattern.
3. LLM response: "¡Hasta mañana! Try to use one of these verbs in conversation today. 👋" — no tool calls.
4. `repo.add_message` for assistant.

End of session.

---

## Overnight (Day 1 → Day 2)

Two background workers run between sessions. Neither is in the request path.

### Embedder (`nexus/workers/embedder.py`, runs every few minutes)

Watches new rows in `messages`, `summaries`, `entities` and chunks/embeds them.

For each new message and entity created today:
1. `llm.embeddings.embed(content)` → 1024-d vector.
2. `db.WRITE embeddings { project_id=P, source_table="messages", source_id=M_X, content=chunk_text, embedding=vec }`

Total new rows in `embeddings`: ~8 (4 user + 4 assistant messages chunked at 1 chunk each, plus 3 entities embedded by their attribute description).

### Daily summarizer (`nexus/workers/scheduler.py` + `nexus/specialist/summarizer.py`)

For every project whose `config.summary_cadence == "daily"`, runs at local midnight:

1. `repo.recent_messages(P, since=day_start)` + `repo.recent_events(P, since=day_start)` + `repo.list_active_entities(P, type="vocab_word", since=day_start)`
2. `llm.chat([...summarize prompt..., context...])` → summary text + focus_tags
3. `db.WRITE summaries { project_id=P, scope="daily", period_start=Mon 00:00, period_end=Mon 23:59, content="Maria learned 3 cooking verbs (picar, hervir, freír), aced the first quiz at mastery 1.", focus_tags=["vocabulary","cooking"] }`
4. The new summary row is also embedded (next embedder pass).

---

## Day 2 — Tuesday 09:30 (Maria returns)

### M5. Maria → "Buenos días! What did we cover yesterday?"

1. `repo.add_message(role="user", ...)`
2. **Memory assembly** — *all three tiers fire here*:
   - **[episodic]** `recent_messages(P, limit=N)` → yesterday's messages still inside the 4000-token window, included verbatim.
   - **[episodic]** `recent_events(P, since=now-7d)` → `[Ev_session, 3×vocab_review]` from yesterday.
   - **[structural]** `vocab_mastery_distribution(P)` → `{1: 3}` ; `daily_practice_streak(P)` → `1` ; `new_words_per_week(P)` → `3`.
   - **[semantic]** `repo.semantic_search(P, query="What did we cover yesterday?", k=5, sources=[messages,summaries])` → top hit is yesterday's daily summary (cosine similarity to "cooking verbs / picar / hervir / freír"), then a couple of message chunks.
3. The assembled prompt now contains: episodic transcript + stats block + 1–3 semantic snippets (mostly the summary).
4. LLM responds: "Buenos días! Yesterday you learned three Spanish cooking verbs — picar, hervir, freír — and got all three right on the first quiz. You're at mastery level 1 on each. ..."
   - No tool calls.
5. `repo.add_message(role="assistant", ...)`

**Tier interplay:** for "what did we do yesterday," episodic alone would have answered. Semantic is doing redundant work here — that's fine; it's cheap and reinforces. **Stronger test of semantic comes in M7 below.**

---

### M6. Maria → "What's my current vocab list look like?"

1. `repo.add_message` for user turn.
2. Memory assembly same as M5 — but the agent doesn't need to call any tool because the structural-stats block in the prompt already enumerates entity counts. To list the actual *words*, however, the agent needs more.
3. LLM emits a tool call (one we haven't designed yet — see open questions):
   - `tool.list_entities(type="vocab_word", limit=50)` → would map to a new repo function `list_entities(P, type, ...)`.
   - **Gap:** this isn't in the Phase 1 repo helpers yet. The Specialist will need it; I'll add it in Phase 3.
4. Result returned to LLM, LLM formats: "You have 3 vocab words at mastery 1: picar, hervir, freír."
5. `repo.add_message(role="assistant", ...)`

---

### M7. Maria → "Yesterday you mentioned that thing about Spanish kitchens being smaller — what was the word you used for stovetop?"

This is the **semantic-search hero case**. The detail Maria is asking about is buried in a casual aside in M2 (assistant turn) — not an entity, not an event. Episodic might or might not still cover it depending on token budget; semantic must.

1. `repo.add_message` for user turn.
2. Memory assembly:
   - **[episodic]** assume the M2 assistant turn has already aged out of the 4000-token window because of all the cooking-verb explanation that followed. So episodic does *not* contain the stovetop aside.
   - **[semantic]** `repo.semantic_search(P, query="Spanish kitchens smaller stovetop word", k=5)` → returns the M2_asst chunk that contained "...los fogones tend to be smaller than American stovetops..."
3. LLM has the snippet in context and answers: "I used **fogón** — that's the stovetop. Want me to add it to your vocab list?"
4. If Maria says yes (next turn), `tool.create_entity` would fire. That turn isn't in this trace.

This message is **the proof that semantic memory does something episodic and structural can't**: recall of off-topic content from a prior session that wasn't lifted to a structured entity at the time.

---

## What this trace exercises

| Concern | Where it appears |
|---|---|
| `repo.add_message` (user + assistant) | Every message |
| `repo.upsert_entity` | M1 |
| `repo.add_event` (free-standing) | M1 (practice_session) |
| `repo.add_event` with `entity_ids` | M3 (vocab_review) |
| Entity state mutation | M3 (mastery_level update) |
| Episodic memory | M2, M5 |
| Episodic events memory | M5 |
| Structural stats | M5, M6 |
| Semantic search | M7 |
| Daily summary worker | overnight |
| Embedder worker | overnight |
| Cross-day continuity | M5 onward |
| Free conversation (no tools) | M2, M4 |

Memory tiers cover three failure modes:
- **Episodic** answers "what did we just talk about" — fast, exact, but bounded.
- **Structural** answers "how am I doing" — typed, aggregable, doesn't need recall.
- **Semantic** answers "you said something about X a while back" — the only tier that survives the episodic window for unstructured asides.

---

## Open questions surfaced by this trace

These need decisions before Phase 3 (Specialist v1):

1. **`list_entities` repo helper.** M6 needs it; Phase 1 only added `get_entity_by_name`. Add it as part of Phase 3 setup along with `list_entities_by_attribute_match` for "find my vocab words tagged X" queries.

2. **Who computes new entity state — LLM or named handler?** In M3 the trace shows the LLM choosing `mastery_level: 1`. The DomainConfig field `event_types.vocab_review.updates_state.mastery_level: spaced_repetition_v1` suggests a deterministic handler. Two options:
   - LLM proposes the patch; tool dispatcher overrides with the registered handler if one exists. (Trustworthy — recommend.)
   - LLM is just told the rule and writes the patch directly. (Simpler — fragile.)
   Decide before Phase 3 codes the tool dispatcher.

3. **Chat-to-project routing.** The trace assumed Telegram already knows which `project_id` to send M1 to. The schema doesn't have a "current project per chat" concept yet. Two reasonable places:
   - `users.settings.active_project_per_chat = { telegram_chat_id: project_id }`
   - A new `chat_sessions` table — overkill for now.
   Lean toward `users.settings`; revisit if multi-chat-per-project needs split.

4. **Conversation boundaries.** The trace treats Day 1 and Day 2 as one continuous message stream filtered by recency. If Maria's episodic context becomes confusing across long gaps, we may want explicit conversation boundaries (e.g., a `conversation_id` on `messages` that resets after N hours of silence). Currently deferred per `docs/schema.md` open question #1; M5/M7 don't *require* it.

5. **Multi-tool-turn session handling.** M1 shows the agent emitting four tool calls in one turn. Tool calls happen within a single LLM turn — the dispatcher must run them sequentially, feed results back to the LLM if the LLM expects them, and only then write the final assistant message. SQLAlchemy session lifecycle: keep one `session_scope()` open for the whole turn so all writes are atomic, or commit per tool? Recommend one session per turn (atomic). Decide in Phase 3.

6. **Embedder worker timing.** Trace assumed embeddings are ready by morning of Day 2. If a user comes back 5 minutes later, semantic search may miss recent content. For Phase 5: either embed inline on insert (simpler, slight latency) or accept the worker lag as a known limitation. Recommend inline embedding for messages, async batch for entities/summaries.

7. **`semantic_search` filtering by source.** M7 shows querying with `sources=[messages]`. The repo helper signature should accept that filter from day one — otherwise we'll be retrieving summary text when we want raw messages. Spec: `semantic_search(project_id, query, k, sources: list[str] | None = None)`.

These are concrete, schema-affecting decisions. None of them threaten the Phase 1 work — they all live above the repository layer.
