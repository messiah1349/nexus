You are summarizing a language-learning session that has just ended. The
user's transcript follows. Produce a structured JSON summary inside the
marker block.

# Active plans (for plan_id references)

$active_plans_with_ids

# Transcript

$transcript

# Your task

Emit your output as EXACTLY this marker block, on its own lines, with
valid JSON inside:

<<<SUMMARY>>>
{"content": "...", "focus_tags": [...], "plan_item_index_addressed": 0, "plan_item_update": null, "plan_revision": null}
<<<END_SUMMARY>>>

Fields:

- `content` (required, string): 2–5 sentence prose summary of what happened.
  Include specific vocabulary words / grammar points / exercises if any
  were covered. Mention engagement level and any struggles.

- `focus_tags` (required, array of strings): short keyword tags pulled from
  the session content. Examples: "vocabulary", "cooking", "review",
  "conditional_tense", "listening".

- `plan_item_index_addressed` (optional, int): which item in the plan the
  session actually addressed (0-indexed). Null if the session ranged
  across items or none of them.

- `plan_item_update` (optional, object | null): if a single plan item's
  status should change as a result of this session, emit:
  `{"plan_id": "<uuid>", "item_index": <int>, "status": "completed"}`.
  Statuses: pending | in_progress | completed | skipped. Set to null if
  no item status changes.

- `plan_revision` (optional, object | null): if the session warrants
  superseding a plan with a new one (e.g. user shifted scope, or the
  current plan no longer reflects reality), emit:
  ```
  {
    "plan_id": "<uuid of plan being superseded>",
    "reason": "short explanation",
    "new_plan": {
      "name": "...",
      "horizon": "weekly",
      "items": [...]
    }
  }
  ```
  Otherwise null.

# Rules

- Output ONLY the marker block. No text before or after.
- Apply coach-style autonomy: if a plan revision is warranted, emit it
  yourself — do not ask the user. The next session will surface the
  change to them.
- Be conservative with revisions. A single deviation does NOT justify
  a full plan revision; only revise when the *future* plan is now wrong.
- If nothing of substance happened (very short session, off-topic, etc.),
  still emit a summary — just keep `content` brief.
