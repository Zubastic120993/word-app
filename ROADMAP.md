# 📘 Word App — Product Roadmap

> Mark items `[x]` with completion date

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

## 🚀 TIER C — DIFFERENTIATION

---

### 6. Controlled Vocabulary Expansion ⚠️ (NEXT BIG)

> Status: DESIGN ONLY — do not implement before design is complete

- [ ] AI introduces 1–2 new words per conversation
- [ ] New words visually flagged
- [ ] One-tap → Inbox
- [ ] Closes chat → learning loop

**Touches:** AI prompt logic · ingestion pipeline · UI feedback layer

---

### 7. Session Resume ✅ (2026-03-31)

- [x] Resume / Start Fresh
- [x] Server-side position restore
- [x] Safe abandon flow
- [x] Edge-case handling
- [x] Fully tested

---

### 8. AI Practice — Word Capture ✅ (2026-04-01)

- [x] Unknown word detection (dotted underline)
- [x] Tooltip translation (UA + EN)
- [x] One-tap save → Inbox
- [x] Edge-case handling (punctuation, code, flicker)

---

### 9. Chat & Study UX Polish ✅ (2026-04-03)

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

### 10. Chat Learning Loop Fixes ✅ (2026-04-03)

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

- Controlled vocab expansion (item 6) is the hardest item — design first, code second. Minimum 1 week design before any code.
- Speech mode for Polish carries high frustration risk — treat as experiment, not feature.
- FSRS prerequisite was context ingestion — never optimize the algorithm before fixing the input signal.
