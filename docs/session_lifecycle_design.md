# Session Lifecycle Design

## Current Behavior Overview

Study sessions currently begin when the application creates a `learning_sessions` row and associated `session_units` rows. In practice, session creation also acts as session start, because there is no distinct persisted "started" state separate from row creation.

Completion is currently inferred from a mix of fields and runtime behavior. The primary persisted signals are boolean fields such as `locked` and `completed`, plus the optional timestamp `completed_at`. A session may also be treated as effectively finished when all units are answered, even if persisted fields are not fully aligned.

Resume behavior is similarly implicit. A session is considered resumable if it still exists and has not been treated as fully completed by the surrounding logic. Because the lifecycle is inferred from combinations of booleans, timestamps, and answer progress, resume semantics depend on interpretation rather than a single authoritative state.

## Identified Problems

The current model allows ambiguous states. For example, a session can be `locked=True` while also being incomplete, or `completed=True` with a missing `completed_at`, or have all units answered while persisted completion fields do not fully reflect that fact.

There are multiple lifecycle-related signals spread across booleans and timestamps. This creates overlap between row existence, lock state, completion state, and derived progress state.

Completion signals are inconsistent. Different parts of the system may rely on `completed`, `completed_at`, or computed answer counts. That increases the risk of regressions in resume logic, history views, analytics, and export/import behavior.

The current design also does not clearly represent abandonment. A session that is no longer active but never explicitly completed has no clean first-class lifecycle state.

## Proposed Lifecycle Model

Use a single authoritative lifecycle field:

- `created`
- `active`
- `completed`
- `abandoned`

Recommended meaning of each state:

- `created`: session row exists but study interaction has not meaningfully started yet.
- `active`: session has started and may still be resumed.
- `completed`: session was finished intentionally or by answering all required items.
- `abandoned`: session is no longer expected to resume and was not completed.

State transitions should be explicit:

- `created -> active`
- `active -> completed`
- `active -> abandoned`

Optional operational rule:

- If the product does not need a distinct pre-start state, creation may transition immediately to `active`, but the persisted model should still reserve `created` for clarity and future flexibility.

Resume behavior becomes straightforward:

- Only `active` sessions are resumable.
- `completed` sessions are historical only.
- `abandoned` sessions are historical but unfinished.

## Recommended Database Representation

Prefer a status enum column as the single source of truth:

- `status ENUM('created', 'active', 'completed', 'abandoned')`

Recommended timestamps:

- `started_at`
- `completed_at`
- `abandoned_at`

Suggested interpretation:

- `created`: `started_at`, `completed_at`, `abandoned_at` are null
- `active`: `started_at` is set; `completed_at` and `abandoned_at` are null
- `completed`: `started_at` and `completed_at` are set; `abandoned_at` is null
- `abandoned`: `started_at` is typically set; `abandoned_at` is set; `completed_at` is null

This avoids ambiguous combinations of multiple boolean lifecycle flags. Existing progress or summary fields can remain, but they should not define lifecycle state.

Recommended direction:

- deprecate lifecycle booleans such as `completed` and `locked` for state modeling
- keep them temporarily only if needed for migration compatibility
- use `status` as the only authoritative lifecycle field

## Migration Considerations

Current records can be mapped conservatively into the new model.

Suggested mapping:

- `completed=True` or `completed_at IS NOT NULL` -> `status='completed'`
- not completed, with evidence of interaction or persisted session units -> `status='active'`
- newly created records with no meaningful interaction -> `status='created'`

If the product wants explicit abandonment, old records that are incomplete and stale may either:

- remain `active` initially for a safe migration, then be marked `abandoned` by later policy, or
- be migrated to `abandoned` only if there is a clear business rule for inactivity age

Recommended timestamp backfill:

- `started_at` <- existing `created_at`
- `completed_at` <- existing `completed_at`
- `abandoned_at` <- null for migrated records unless abandonment can be determined safely

Recommended rollout approach:

1. Add `status`, `started_at`, and `abandoned_at` as additive fields.
2. Backfill `status` from current booleans and timestamps.
3. Update application reads to rely on `status`.
4. Update writes so lifecycle transitions set only the new authoritative state fields.
5. Remove or ignore legacy lifecycle booleans once downstream code no longer depends on them.
