# AI Practice — Implementation Roadmap

> Track execution progress here. Full design spec: `docs/ai_practice_improvement_plan.md`.

---

## Execution Strategy

**Two separate commits. Never implement both phases in one go.**

Phase 1 is a prerequisite — it changes the DOM structure that Phase 2 builds on. If Phase 2 breaks, Phase 1 stays clean and shippable.

| Step | Scope | Verify before moving on |
|---|---|---|
| 1 | Phase 1 — full layout cleanup | Page loads, chat works, controls visible, no regressions |
| 2 | Phase 2a — backend endpoint only | `curl /api/vocabulary/known-tokens` returns token list |
| 3 | Phase 2b — `loadKnownTokens()` only, no annotation | Tokens in memory, `knownTokensLoaded = true`, no console errors |
| 4 | Phase 2c — `annotateUnknownWords()`, no tooltip | Dotted underlines appear on AI messages only, punctuation preserved, user messages untouched |
| 5 | Phase 2d — tooltip + translate, no save | Correct positioning on desktop + mobile, translate works, failure state "Translation unavailable" shown |
| 6 | Phase 2e — save word | DB insert confirmed, tooltip shows "✓ Saved", all same-word spans in message get `.captured` |

**Rollback:** each step is independently revertable. If step 4 breaks annotation, steps 2–3 (endpoint + loading) are unaffected.

**Commits:**
- `refactor(chat): compress layout — remove hub section, inline controls` ← Phase 1
- `feat(chat): word capture — unknown word detection, translate tooltip, save to inbox` ← Phase 2 (all steps together once verified)

---

## Phase 1 — Layout Cleanup

**Goal:** 1 page = 1 primary action. Chat dominant, no preamble, controls compressed.

### HTML (`app/templates/chat.html`)
- [x] Remove `<section aria-labelledby="contextual-practice-heading">` block and its contents
- [x] Remove `<hr>` divider between section and chat
- [x] Replace `<h1>Free Chat</h1>` + UNRESTRICTED badge + description `<p>` with compact `<div class="chat-header">`
- [x] Remove `#theme-progress-card` div
- [x] Add `<span id="chat-status-line">` inside `.chat-header`
- [x] Collapse Theme / Scenario / Corrections into single `<div class="chat-controls">`
- [x] Move `#prompt-chips` div to just above `.chat-input` (inside `.chat-container`)

### JS (`app/templates/chat.html`)
- [x] Remove `const themeProgressCard = document.getElementById("theme-progress-card")` variable declaration (element is removed)
- [x] Rename `updateThemeProgressCard()` → `updateChatStatusLine()`, update to write to `#chat-status-line`

### CSS (`app/static/style.css`)
- [x] Add `.chat-header` styles (flex row, space-between)
- [x] Add `.chat-header-title` and `.chat-header-status` styles
- [x] Add `.chat-controls` styles (flex row, gap, font-size 0.9rem, flex-wrap)
- [x] Remove or replace `.scenario-selector` and `.correction-toggle` styles

**Commit:** `refactor(chat): compress layout — remove hub section, inline controls`

---

## Phase 2 — Word Capture

**Goal:** Unknown words in AI messages get dotted underline → click → translate → Save word → Inbox.

### Backend (`app/routers/upload.py`)
- [x] Add `GET /api/vocabulary/known-tokens` endpoint
  - Query: `learning_units JOIN learning_progress WHERE introduced_at IS NOT NULL`
  - Returns: `{ "tokens": ["słowo", ...] }`

### JS (`app/templates/chat.html`)
- [x] Add `const DEBUG_WORD_CAPTURE = false;` — toggle for annotation debug logging
- [x] Add `knownTokens = new Set()` and `knownTokensLoaded = false` state vars
- [x] Add `let activeTooltip = null` — required by flicker guard and single-tooltip enforcement
- [x] Add `normalizeToken(t)` — NFC + lowercase + trim (mirrors Python `normalize_text()`)
- [x] Add `loadKnownTokens()` — fetch endpoint, populate set, re-annotate existing messages on load
- [x] Call `loadKnownTokens()` in `DOMContentLoaded`
- [x] Add `translationCache = new Map()`
- [x] Add `annotateUnknownWords(contentEl)`:
  - Idempotent guard: `if (contentEl.dataset.annotated === "true") return;` then set it to `"true"` — prevents double-processing if called twice on same element
  - `matchAll(/\p{L}+/gu)` with index slicing — preserves original spacing verbatim
  - `if (DEBUG_WORD_CAPTURE) console.log("Unknown word:", token)`
- [x] Call `annotateUnknownWords()` inside `addMessage()` — **only when `role === 'assistant'`** (explicit guard, not implicit — prevents user/system messages from being annotated)
- [x] Add tooltip click handler on `.unknown-word`
  - Flicker guard: same word fast-clicked returns early
  - Translate via cache → API → failure fallback ("Translation unavailable")
  - Tooltip positioned below span, clamped X + Y (flip above on bottom overflow)
  - "Save word" → `POST /api/units/inbox` → mark all `[data-word]` instances `.captured`
  - Dismiss on outside click or Escape

### CSS (`app/static/style.css`)
- [x] Add `position: relative` to `.chat-messages`
- [x] Add `.unknown-word` (dotted purple underline, pointer cursor)
- [x] Add `.unknown-word.captured` (solid green underline, default cursor)
- [x] Add `.word-tooltip` (absolute, shadow, border-radius, z-index 100)

**Commit:** `feat(chat): word capture — unknown word detection, translate tooltip, save to inbox`

---

## Done ✅

### AI Practice — Word Capture (2026-04-01)
- [x] Layout cleanup — chat-first UI, inline controls, compact header with status line
- [x] Known tokens endpoint (`GET /api/vocabulary/known-tokens`)
- [x] Unknown word detection — Unicode-safe tokenization, index slicing, punctuation preserved
- [x] Tooltip with dual translation (Ukrainian first, English second) + failure handling
- [x] Translation cache — no redundant API calls per session
- [x] Save word → Inbox integration (`POST /api/units/inbox`)
- [x] Multi-instance capture sync — all same-word spans marked `.captured` on save
- [x] Full edge-case handling: punctuation, code blocks, timing race, viewport clamping, flicker guard
