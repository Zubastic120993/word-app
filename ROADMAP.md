# 📘 Word App — Product Roadmap

> Mark items `[x]` with completion date

## 📋 COMPLETION RULE

After every step:
1. Mark checkbox `[x] (YYYY-MM-DD)`
2. Run affected tests — confirm no regressions
3. If tests pass → `git commit` with message describing what was done
4. If tests fail → fix before moving to next checkbox

> Never commit a broken step. Never skip the date.

---

## ✅ FOUNDATION — DONE

- [x] Cloze mode
- [x] Context ingestion (initial)
- [x] Session transparency (selection stats)

---

## 🏠 HOME REDESIGN — DONE (2026-03-30)

- [x] Removed duplication ("Recommended today")
- [x] Merged Focus + Status (streak inline)
- [x] One-banner rule (no stacking)
- [x] Quick Add repositioned
- [x] Single primary card (due → weak → lesson)
- [x] Secondary actions collapsed
- [x] Forecast moved to Progress

---

## 🔥 TIER A — HIGH ROI (COMPLETED)

### 1. Context Auto-Extraction ✅ (2026-03-31)

- [x] Word-boundary matching fixed
- [x] Sentence splitting improved
- [x] Backfill API (`POST /api/admin/backfill-context`)
- [x] Batch AI context generation
- [x] Backfill UI (repeat until remaining=0)
- [x] Full test coverage

---

### 2. Quick Add → Inbox ✅ (2026-03-30)

- [x] One-click add (word + translation)
- [x] Dedicated Inbox vocabulary
- [x] Move / delete actions
- [x] Fully tested

---

### 3. Habit Engine + Backlog Visibility ✅ (2026-03-30)

- [x] Daily streak tracking
- [x] Streak break feedback
- [x] Backlog warning banner
- [x] No gamification noise

---

## 🧠 TIER B — CORE SYSTEM (COMPLETED)

### 4. FSRS Integration ✅ (2026-03-31)

- [x] FSRS parameter mapping
- [x] FSRS service integration
- [x] Hybrid model (recall = FSRS, passive = SRS-lite)
- [x] Backfill + safe migration
- [x] DB consistency fixes

---

### 5. SRS Forecast Chart ✅ (2026-03-30)

- [x] 14-day review projection
- [x] Load visibility
- [x] Prevents overload collapse

---

## 🚀 TIER C — DIFFERENTIATION (COMPLETED)

### 6. Session Resume ✅ (2026-03-31)

- [x] Resume / Start Fresh
- [x] Server-side position restore
- [x] Safe abandon flow
- [x] Edge-case handling
- [x] Fully tested

---

### 7. AI Practice — Word Capture ✅ (2026-04-01)

- [x] Unknown word detection (dotted underline)
- [x] Tooltip translation (UA + EN)
- [x] One-tap save → Inbox
- [x] Edge-case handling (punctuation, code, flicker)

---

### 8. Chat & Study UX Polish ✅ (2026-04-03)

#### Core

- [x] Session vocab highlighting
- [x] Restore parity (refresh-safe)
- [x] Retry success signal
- [x] Smart chips (rotation + fingerprint)
- [x] Completion next-step bridge
- [x] Home explanation layer ("Recommended: …")
- [x] Mid-session adaptive feedback (streak thresholds)

#### Polish Layer

- [x] Short-word guard (< 3 chars)
- [x] Hover tooltip on vocab words (data-translation, CSS ::after)
- [x] Grammar categories: Spelling / Case / Verb / Gender / Tense / Preposition / Structure

---

### 9. Chat Learning Loop Fixes ✅ (2026-04-03)

#### UX

- [x] Correction-first flow — assistant suppressed when corrections present
- [x] `renderCorrectionBlock()` — standalone, no "Assistant" bubble wrapper
- [x] Clean retry loop — no duplicate assistant messages

#### Grammar Engine

- [x] Relaxed mode — minor spelling caught (`kilke → kilka`), diacritics enforced
- [x] Structural errors — `nie nigdy` always triggers correction
- [x] Ignore list — capitalization and trailing punctuation not flagged
- [x] Prompt balance — no overcorrection on correct input

#### Critical Bug Fixes

- [x] `raw_user_message` — grammar corrector used tip-appended string; length guard silently suppressed corrections
- [x] `marked.parse(html: true)` — default `html: false` in v15 escaped annotation spans to visible text
- [x] Explanation quality — full-form requirement, no identical-string comparisons, `Structure:` category added

#### System

- [x] Chat fingerprint gate — new vocab session discards old chat history
- [x] Deterministic highlighting — fresh render === restored render (Test 17 confirmed)

---

## 🔒 TIER D — SYSTEM STABILITY (IN PROGRESS)

### 10. Chat State Persistence 🔴 (NOW)

> Only real architectural risk. In-memory singleton loses all state on restart.

#### Step 1 — Implementation

- [x] Migration: `20260405_000001_add_chat_state.py` (`down_revision = "20260404_000001"`) (2026-04-06)
- [x] Model: `app/models/chat_state.py` — single-row table, `id = 1` always (2026-04-06)
- [x] `app/models/__init__.py` — add `ChatState` import (2026-04-06)
- [x] `FreeChatService`: add `_state_loaded`, `load_from_db()`, `save_to_db()` (2026-04-06)
- [x] `respond()`: lazy load on first call, save before `return` (not in `finally`) (2026-04-06)
- [x] `session_vocab` reset block: `_state_loaded = True` + `save_to_db()` immediately after clear (2026-04-06)
- [x] `clear_history(db)`: wipe DB row + `_state_loaded = False` (2026-04-06)
- [x] Router `clear_chat_history()`: add `db = Depends(get_db)`, pass to `clear_history(db)` (2026-04-06)

**State persisted:** conversation · explained\_bases · user\_produced · assistant\_exposed · session\_vocab\_list · session\_vocab\_active · theme\_user\_messages · current\_theme · checkpoint\_done

**State NOT persisted (intentional):** `_recently_used_words` (ephemeral) · `_theme_tracker` sets (safe to reset) · `_vocab_context` (cache)

**Touches:** `free_chat.py` · `ai.py` · `models/` · `alembic/versions/`

#### Step 2 — Observability (immediately after)

- [x] `load_from_db()` log: `history · explained · covered/vocab · vocab_active` (2026-04-06)
- [x] `save_to_db()` log: same format (2026-04-06)

#### Step 3 — Validation (2–3 days real use, no code)

[~] Validation in progress — implementation complete, real usage testing ongoing (started 2026-04-06)

- [ ] Restart mid-conversation → AI resumes correctly
- [ ] Restart mid-explanation → `💡` hint not repeated for same word
- [ ] Restart mid-checkpoint → `checkpoint_done` preserved
- [ ] `session_vocab` arrival → overwrites DB cleanly (no merge)
- [ ] `/chat/clear` → memory + DB both wiped
- [ ] Restart immediately after sending (before AI reply) → no partial state saved

> **Hard stop after validation. No code until all six scenarios confirmed.**

---

### 11. Cleanup 🟡 (after item 10 validated)

> One fix → test → next. Do not batch.

- [ ] Remove cloze debug block (`study.html` lines 4025–4081)
- [ ] Remove ungated `console.log` calls (`study.html` lines 69, 1576, 2317, 2344, 2350, 4624, 4628)
- [ ] Merge duplicate Levenshtein (`session_service.py` lines 830 + 864) — verify both callers (984, 1363) before deleting

---

### 12. FSRS Phase 3 Verification 🟡 (after item 11)

> Phase 3 code already live. 100% backfill coverage (3,975/3,975 rows). Just validate.

- [ ] Run full recall session → zero `apply_fsrs_scheduling failed` warnings
- [ ] Query `next_review_at` for a unit before and after answering — confirm value updated
- [ ] FSRS active for all recall answers, SRS-lite only for passive

---

## 🌱 TIER E — EXPANSION (NEXT BIG)

### 13. Controlled Vocabulary Expansion ⚠️ (AFTER STABILITY — design first)

> Status: DESIGN ONLY — do not implement before item 10 is validated and design is complete

- [ ] AI introduces 1–2 new words per conversation
- [ ] New words visually flagged
- [ ] One-tap → Inbox
- [ ] Closes chat → learning loop

**Touches:** AI prompt logic · ingestion pipeline · UI feedback layer

---

## 🔬 TIER F — LANGUAGE INTELLIGENCE (CONDITIONAL)

> Each item unlocks only if real usage in item 10 validation reveals the need.

---

### 14. Polish Morphology (spaCy) ⚪ (if needed)

> Trigger: real use shows inflected forms being missed (e.g., `czytałem` not tracked as `czytać`). Do NOT trigger on "theoretically incomplete."

- [ ] `pip install spacy && python -m spacy download pl_core_news_md`
- [ ] Replace `_guess_base_form()` (4 suffix rules) with `nlp(token)[0].lemma_`
- [ ] Update `VocabularyValidator._is_variant_of_allowed()` with lemma comparison

**Touches:** `free_chat.py` · `vocabulary_validator.py`

---

### 15. Checkpoint Persistence ⚪ (when data is meaningful)

> Trigger: after enough real use that checkpoint history is worth reviewing.

- [ ] `record_event()` with `event_type="checkpoint_result"` when `checkpoint_done` transitions to `True`
- [ ] Payload: `score · total · theme · timestamp · attempt (user_messages count) · vocab_items`
- [ ] Surface in analytics dashboard as timeline

> `attempt` field (message count at checkpoint) enables learning curve analysis. Add from the start.

**Touches:** `free_chat.py` · `analytics_dashboard.html`

---

### 16. `session_service.py` Refactor ⚪ (when touching it)

> 5,666 lines. Do NOT refactor proactively. Only when a new feature requires touching this file.

- [ ] `SelectionService` — pool building, sampling, balancing (lines 1386–2159)
- [ ] `AnswerEvaluationService` — Levenshtein, `evaluate_answer`, `RecallResult` (lines 830–1012)
- [ ] `ProgressUpdateService` — confidence smoothing, stability, FSRS bridge (lines 5430–5661)

---

### 17. Index Optimization ⚪ (when count/speed warrants)

> Trigger: session creation feels slow, or unit count exceeds ~10k. Current: 9,325 units.

- [ ] `CREATE INDEX idx_review_confidence ON learning_progress(next_review_at, confidence_score)`

---

## 🧪 EXPERIMENTAL

### 🎤 Speech Mode

- [ ] Prototype only — validate Polish accuracy first
- [ ] Measure frustration vs value
- [ ] Do not promote without validation

---

## 🟡 OPTIONAL (NEXT SMALL WINS)

### 📱 PWA ← RECOMMENDED NEXT

- [ ] manifest.json
- [ ] Service worker
- [ ] Installable on mobile / iPad

👉 High perceived value, low risk, one session

---

### 📊 Advanced Analytics

- [ ] Forgetting curves
- [ ] Learning velocity graphs

---

## 🔁 SYSTEM FLYWHEEL

```
Capture → Retention → Return → Discovery → Capture
```

Every new feature must fit one of these four stages. If it doesn't, it's scope creep.

---

## 📌 NOTES

- Controlled vocab expansion (item 13) is the hardest remaining item — design first, code second. Minimum 1 week design before any code.
- Speech mode for Polish carries high frustration risk — treat as experiment, not feature.
- FSRS prerequisite was context ingestion — never optimize the algorithm before fixing the input signal.
- Chat state persistence (item 10) is the only current architectural risk — singleton in-memory state lost on restart. All other issues are minor or already resolved.
- FSRS is already active (not "coming") — Phase 3 code live, 100% backfill, just needs verification run (item 12).
- Tier F items (14–17) are pull-based — no item starts without real usage revealing the need. Do not implement speculatively.
- No item starts until the previous is validated by real use. Hard stop after item 10 validation before writing any new code.
