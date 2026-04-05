# Codex Branch Prompt Package

## Branch

- `stabilization/p01-baseline-freeze`

## Goal

- Capture a reproducible execution baseline and clean the task inventory without changing runtime behavior.

## Allowed files

- `docs/current_baseline.md`
- `docs/baseline_requirements.txt`
- `docs/schema_baseline.sql`
- `docs/current_execution_run.md`
- `docs/codex_branch_prompts.md`
- `TODO.md`

## Forbidden edits

- Any files in `app/`
- Any files in `tests/`
- Any migrations
- Any schema changes
- Any formatting-only cleanup outside allowed files
- No import reordering
- No formatting tools
- No renames or moves

## Stop conditions

- Stop if required baseline facts cannot be collected.
- Stop if TODO cleanup would require application-code edits.
- Stop if changes outside the allowed files are required.

## Required checks

- `pytest -q --maxfail=1`
- `pytest -q`

## Diff review checklist

- Only allowed docs modified
- No files in `app/` changed
- No files in `tests/` changed
- No migrations added
- No renames or moves
- No formatting-only churn
