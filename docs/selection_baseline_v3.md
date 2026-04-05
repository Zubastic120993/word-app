# STAGE 1 — Reality Freeze (Exact Production Behavior)

## Objective

This document freezes the current production behavior of session selection as implemented today.

It is descriptive only. No selector behavior is modified by this document.

Production selector logic remains unchanged:

- No production selector logic changes
- No debug hooks
- No refactor

---

## due_only (Production Path)

The production `due_only` path is the early-return branch inside `create_session()`.

Behavior:

- `create_session()` enters a dedicated early-return branch when `due_only=True`
- That branch runs before curriculum lesson-window construction
- Base query filters to units joined through `LearningProgress`
- Base query requires `introduced_at IS NOT NULL`
- Base query requires `next_review_at <= now`
- Base query is ordered by `next_review_at ASC`
- Lesson filtering is ignored in this production path
- Daily cap is enforced before final selection
- Empty due pool raises `InsufficientUnitsError`
- `_select_units_weighted_random()` is not used in this production path

Selection split:

- If `available_due_count <= session_size`, the ordered base query is used directly with `LIMIT session_size`
- If `available_due_count > session_size`, the branch materializes the full due pool and calls `_select_balanced_units()`

Ordering clarification:

- Base ordering is `next_review_at ASC`
- Tie ordering for identical `next_review_at` values is database-dependent because no secondary sort key is applied
- If `_select_balanced_units()` runs, the balancing/interleaving stage may alter the final session ordering relative to the base query order

Balancing helper clarification:

- `_select_balanced_units()` is a helper capability, not the default selector for all production paths
- In production, it is used here only when the due pool is larger than the chosen `session_size`
- `_select_balanced_units()` includes an intra-session reinforcement insertion stage
- That reinforcement stage may insert duplicate units into the final session

Daily-cap clarification:

- Daily quota is checked before `session_size` is finalized
- If quota is exhausted and `override_daily_cap=False`, the branch returns the daily-cap response instead of creating a session
- Otherwise the final `session_size` is capped by available due units, the hard session cap, and optionally the remaining daily quota

---

## weak_only

The `weak_only` path is implemented inside `_select_units_weighted_random()` and uses weighted sampling without replacement.

Behavior:

- Uses `_select_units_weighted_random()`
- Uses `_select_units_weighted_random()`'s `weak_only` branch
- Uses `_weighted_random_sample()`
- `_weighted_random_sample()` performs weighted sampling without replacement
- Does not use `_select_balanced_units()`
- No reinforcement insertion
- No duplicate insertion

### Weak Pool Construction

Initial pool = weak units only.

Construction priority:

- Weak units are fetched first
- If weak units are insufficient, review candidates are appended next
- If weak plus review are still insufficient, new candidates are appended last

### Behavior Split

#### Case A — Enough Weak Units

- All selected IDs come from the weak pool
- No padding occurs
- No duplicates occur

#### Case B — Insufficient Weak Units

- Weak pool is exhausted first
- Review candidates are appended next
- New candidates are appended last
- Final selection is still sampled without replacement
- No duplicates occur

Note:

Verification here concerns pool-construction priority, not final sampled order. The weighted sampler may output the selected IDs in a different order than the append order used to build the candidate pool.

---

## normal

The normal production path also goes through `_select_units_weighted_random()`, but not through its `weak_only` or `due_only` special branches.

Behavior:

- Due items are considered first
- Due-item selection is capped at 70% of session size
- Remaining slots are filled by weighted sampling from bucket pools
- Does not use `_select_balanced_units()`
- Does not use `_compute_reinforcement_depth()`
- Does not use `_compute_gain_adjustment()`
- No duplicate insertion
- No reinforcement stage

Selection structure:

- Due-first selection happens through `_get_due_units_weighted()` plus `_weighted_random_sample()`
- Remaining slots are filled from new, weak, and review buckets
- Final fill, if needed, comes from the combined remaining eligible units across those buckets
- Sampling is without replacement because `_weighted_random_sample()` excludes already selected IDs

---

## Invariants Frozen by This Document

- Production selector behavior is unchanged
- Helper capability is separate from production path
- Weak pool-priority invariant is defined
- `next_review_at ASC` base ordering is clarified
- Sampling vs. eligibility is clarified
