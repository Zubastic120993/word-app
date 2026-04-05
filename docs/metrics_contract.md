# Metrics Contract

## Overview

This document defines the canonical semantics for the learning metrics used by the application:

- `due_words`
- `weak_words`
- `mastered_words`
- `learning_streak`
- `study_streak`

The goal is to freeze meaning before implementation refactors. Definitions below reflect current code behavior for `due_words`, `weak_words`, `learning_streak`, and `study_streak`, plus the branch decision for canonical `mastered_words`.

All metric names in this document describe logical metrics, not specific response field names. Current APIs may expose them as fields such as `due_words_count`, `weak_words_count`, `learning_streak_days`, or `study_streak_days`.

## Metric Definitions

### due_words

#### Definition

Count of introduced learning units whose scheduled review time is due now or overdue.

#### Source tables or fields used

- `learning_progress`
- `learning_progress.introduced_at`
- `learning_progress.next_review_at`

#### Exact counting rule

Count one row for each `learning_progress` record where all of the following are true:

- `introduced_at IS NOT NULL`
- `next_review_at IS NOT NULL`
- `next_review_at <= now`

The count is based on `learning_progress` rows, not on theme membership, vocabulary grouping, or session history.

#### Edge case handling

- `NULL next_review_at`: do not count as due.
- Missing progress rows: do not count, because the metric is computed from `learning_progress`.
- Missing theme or vocabulary mapping: no effect; this metric does not join through theme or vocabulary mappings.
- `stability < 0`: no effect on counting. `stability_score` is not consulted by this metric.
- `stability > 1`: no effect on counting. `stability_score` is not consulted by this metric.

#### Example scenarios

- A word has `introduced_at` set and `next_review_at` 10 minutes in the past: count it.
- A word has `introduced_at` set and `next_review_at` 2 days in the future: do not count it.
- A word has `introduced_at` set but `next_review_at = NULL`: do not count it.
- A `learning_unit` exists with no `learning_progress` row: do not count it.

### weak_words

#### Definition

Count of introduced learning units whose confidence is below 0.5.

#### Source tables or fields used

- `learning_progress`
- `learning_progress.introduced_at`
- `learning_progress.confidence_score`

#### Exact counting rule

Count one row for each `learning_progress` record where all of the following are true:

- `introduced_at IS NOT NULL`
- `confidence_score < 0.5`

No other conditions apply. In particular, due state, recall result, blocked state, and theme membership are not part of the canonical metric.

#### Edge case handling

- `NULL next_review_at`: no effect; `next_review_at` is not consulted.
- Missing progress rows: do not count, because the metric is computed from `learning_progress`.
- Missing theme or vocabulary mapping: no effect; this metric is global and mapping-independent.
- `stability < 0`: no effect on counting. `stability_score` is not consulted by this metric.
- `stability > 1`: no effect on counting. `stability_score` is not consulted by this metric.

#### Example scenarios

- A word has `introduced_at` set and `confidence_score = 0.49`: count it.
- A word has `introduced_at` set and `confidence_score = 0.50`: do not count it.
- A word has `confidence_score = 0.20` but `introduced_at = NULL`: do not count it.
- A word has `confidence_score = 0.10` and `next_review_at = NULL`: count it if `introduced_at IS NOT NULL`.

### mastered_words

#### Definition

Count of learning units that satisfy the strict SRS mastery rule.

#### Source tables or fields used

- `learning_progress`
- `learning_progress.introduced_at`
- `learning_progress.last_recall_result`
- `learning_progress.confidence_score`
- `learning_progress.next_review_at`

#### Exact counting rule

Count one row for each `learning_progress` record where all of the following are true:

- `introduced_at IS NOT NULL`
- `last_recall_result == CORRECT`
- `confidence_score >= 0.85`
- `next_review_at IS NOT NULL`
- `next_review_at > now`

This is the canonical mastery definition for the branch, even if some current UI aggregates still display a looser `confidence_score >= 0.8` percentage.

#### Edge case handling

- `NULL next_review_at`: do not count as mastered.
- Missing progress rows: do not count as mastered.
- Missing theme or vocabulary mapping: no effect; mastery is determined from progress state only.
- `stability < 0`: no direct effect on counting. `stability_score` is not a mastery criterion.
- `stability > 1`: no direct effect on counting. `stability_score` is not a mastery criterion.

#### Example scenarios

- A word has `introduced_at` set, `last_recall_result = CORRECT`, `confidence_score = 0.85`, and `next_review_at` tomorrow: count it.
- The same word with `next_review_at` one minute in the past: do not count it.
- The same word with `last_recall_result = FAILED`: do not count it.
- The same word with `confidence_score = 0.84`: do not count it.
- A row has `confidence_score = 0.95` but `introduced_at = NULL`: do not count it.

### learning_streak

#### Definition

Number of consecutive local calendar days, ending today, on which the learner has at least one learning-progress row whose most recent recall outcome is correct and whose `last_seen` falls on that day.

#### Source tables or fields used

- `learning_progress`
- `learning_progress.last_recall_result`
- `learning_progress.times_correct`
- `learning_progress.last_seen`

#### Exact counting rule

1. Compute the local start of today using the application server's local calendar day.
2. Determine whether there is at least one `learning_progress` row where:
   - `last_recall_result == CORRECT`
   - `times_correct >= 1`
   - `last_seen IS NOT NULL`
   - `last_seen >= start_of_today`
3. If no such row exists, `learning_streak = 0`.
4. Otherwise, collect distinct `last_seen.date()` values from rows matching:
   - `last_recall_result == CORRECT`
   - `times_correct >= 1`
   - `last_seen IS NOT NULL`
   - `last_seen >= start_of_today - 365 days`
5. Starting from today, count consecutive dates moving backward while each date is present in that set.

The metric is driven by the current `learning_progress` state, especially the current `last_seen` and current `last_recall_result`, not by a historical event log.

#### Edge case handling

- `NULL next_review_at`: no effect; scheduling is not consulted.
- Missing progress rows: contribute nothing.
- Missing theme or vocabulary mapping: no effect; streak is computed globally from `learning_progress`.
- `stability < 0`: no effect on counting. `stability_score` is not consulted.
- `stability > 1`: no effect on counting. `stability_score` is not consulted.

#### Example scenarios

- There is at least one qualifying correct recall today, yesterday, and two days ago: `learning_streak = 3`.
- There was a qualifying correct recall yesterday and two days ago, but none today: `learning_streak = 0`.
- A word was answered correctly in the past, but its current `last_recall_result` is now `FAILED`: it does not contribute a qualifying day.
- Multiple qualifying rows on the same day still count as one day.

### study_streak

#### Definition

Number of consecutive calendar dates with at least one completed study session, where the streak may start from today or yesterday.

#### Source tables or fields used

- `learning_sessions`
- `learning_sessions.completed`
- `learning_sessions.completed_at`

#### Exact counting rule

1. Query all sessions where:
   - `completed == TRUE`
   - `completed_at IS NOT NULL`
2. Extract `completed_at.date()` values.
3. Sort unique dates descending.
4. If there are no completed-session dates, `study_streak = 0`.
5. Let `today` be the current date from the same clock used by the history summary code.
6. If the most recent completed-session date is older than yesterday, `study_streak = 0`.
7. Otherwise, initialize the streak to 1 and continue counting while each subsequent unique date is exactly one day earlier than the previous unique date.

The streak counts dates with completed sessions only. It does not inspect answers, confidence, recall outcomes, or learning progress rows.

#### Edge case handling

- `NULL next_review_at`: no effect; this metric does not use scheduling data.
- Missing progress rows: no effect; this metric is session-based.
- Missing theme or vocabulary mapping: no effect; sessions are counted globally.
- `stability < 0`: no effect on counting. `stability_score` is not consulted.
- `stability > 1`: no effect on counting. `stability_score` is not consulted.

#### Example scenarios

- Completed sessions exist today, yesterday, and two days ago: `study_streak = 3`.
- Completed sessions exist yesterday and two days ago, but not today: `study_streak = 2`.
- The most recent completed session was three days ago: `study_streak = 0`.
- Two completed sessions on the same day count as one streak day.

## Edge Case Handling

The following rules apply consistently across the canonical metrics:

- `NULL next_review_at` only matters for metrics that consult scheduling. It excludes rows from `due_words` and `mastered_words`, and has no effect on `weak_words`, `learning_streak`, or `study_streak`.
- Missing progress rows never count toward `due_words`, `weak_words`, `mastered_words`, or `learning_streak`, because those metrics read from `learning_progress`.
- Missing theme or vocabulary mapping never changes these metrics. They are defined as global learning-state metrics, not as theme-filtered metrics.
- Out-of-range `stability_score` values do not directly change any metric defined here. None of these metrics uses `stability_score` as a counting predicate.

## Consistency Guarantees

- `due_words` and `weak_words` are row counts over `learning_progress` limited to introduced units.
- `mastered_words` is the strict SRS mastery count defined in this document.
- `learning_streak` is a current-state streak derived from `learning_progress.last_seen` and current recall state.
- `study_streak` is a completed-session streak derived from `learning_sessions.completed_at`.
- These metrics are independent of theme mapping, vocabulary grouping, and `stability_score` values.
- A refactor may change query structure or performance characteristics, but it must not change the logical predicates above.

## Notes for Future Refactor

- Preserve metric meaning even if field names, helper functions, or query composition change.
- If existing UI aggregates or dashboards currently expose different mastery behavior, align implementation to this contract rather than changing this contract to match looser legacy displays.
- Be explicit about time basis during refactors. `learning_streak` currently uses local calendar days, while due/mastery comparisons use `now` against `next_review_at`.
- Do not add theme-specific filtering to these metrics unless the metric name and contract are changed accordingly.
