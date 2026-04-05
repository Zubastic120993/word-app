# Current Baseline

## Repository state

- Branch status: `## stabilization/p01-baseline-freeze`
- Commit hash: `4cf37bb84c5ffa2c49a527fb47690b7d7c0b544c`
- Remotes: none configured in `git remote -v`

## Runtime environment

- Python version: `Python 3.12.6`
- `pytest -q --maxfail=1`: `524 passed, 8 warnings in 7.78s`
- `pytest -q`: `524 passed, 8 warnings in 7.80s`
- Warnings summary:
  - 1 `PendingDeprecationWarning` from `starlette/formparsers.py:12` about `python_multipart`
  - 7 `DeprecationWarning`s from `starlette/templating.py:161` about `TemplateResponse` argument order

## Database state

- Database file path: `/Users/vladymyrzub/Desktop/word_app/data/vocabulary.db`
- Database file size: `7704576` bytes (`7.3M` via `ls -lh`)
- Table list: `alembic_version`

## Explicit row counts

- `vocabularies`: unavailable, table missing from configured database
- `practice_events`: unavailable, table missing from configured database
- `session_units`: unavailable, table missing from configured database

## Known open gaps from roadmap

- Verify the Mac firewall allows inbound TCP 8000.
- In non-debug mode, resolve iPad identity from a cookie or HTTP header instead of a query parameter.
- Add a viewport meta tag for iPad Safari.
- Add iPad-specific CSS media queries and touch-target adjustments.
- Increase study answer button targets to at least 44x44 pt.
- Ensure recall input font size is at least 16 px to avoid Safari auto-zoom.
- Validate virtual-keyboard behavior so the study card remains visible.
- Disable hover-dependent interactions that do not translate to touch.
- Ensure `WORD_APP_HOST` can override the bind address.
