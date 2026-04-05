# AI Practice Page — Improvement Plan

> Two-phase plan. Phase 1 = layout cleanup (prerequisite). Phase 2 = word capture feature (new functionality). Both target `chat.html` and are scoped to the Free Chat page only.

---

## Context

The AI Practice page currently has **split identity**: a navigation hub at the top (3 cards) plus an execution surface below (chat). This violates the rule of 1 page = 1 primary action and creates decision friction before the user can do anything.

Additionally, the full learning loop has a gap: AI conversations already introduce unknown vocabulary naturally, but there is no way to capture a word from the chat into the Inbox for structured learning.

**Phase 1** eliminates the friction. **Phase 2** closes the loop.

---

## What already exists (don't rebuild)

| Piece | File | Notes |
|---|---|---|
| `POST /api/units/inbox` | `app/routers/upload.py:875` | Creates inbox entry, deduplicates via `normalized_text`, returns 400 if duplicate |
| `POST /api/ai/chat/translate` | `app/routers/ai.py` | On-demand translation — reuse for tooltip |
| `normalize_text()` | `app/models/learning_unit.py:25` | NFC + lowercase + strip — defines canonical form |
| `addMessage()` | `app/templates/chat.html:698` | Renders all AI messages — annotation goes here |
| `knownTokens` set | (new, client-side) | Built from new endpoint on page load |

---

## Phase 1 — Layout Cleanup

**Goal:** Chat is the dominant element. Controls are one compact line. No cards, no sections, no preamble.

### What to remove

- `<section>` "Contextual Practice" block (lines 6–23 in chat.html) — the cloze card links away to `/study`, the two "coming soon" cards are dead weight
- `<hr>` divider (line 25)
- `<h1>Free Chat</h1>` and the `UNRESTRICTED` badge — replaced by compact header
- `<p class="text-muted">` description line
- `#theme-progress-card` div — replaced by inline status line in header

### What to change

**1. Compact header — orientation anchor + status in one line**

Replace the `<h1>` + badge + description block with:
```html
<div class="chat-header">
    <span class="chat-header-title">AI Practice</span>
    <span id="chat-status-line" class="chat-header-status hidden"></span>
</div>
```

The status line lives here (not as a separate card below). When no theme is active it stays hidden.
Format when theme active: `Travel · 3/8 words · Checkpoint: 4/5`

**2. Controls: vertical stack → one compact line**

Current (3 separate rows):
```
Theme: [dropdown]
Practice scenario: [dropdown]
[ ] Enable corrections
```

Replace with a single `<div class="chat-controls">` containing all three inline:
```
Theme [dropdown]  ·  Scenario [dropdown]  ·  [checkbox] Corrections
```

CSS: `display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; padding: 0.4rem 0; font-size: 0.9rem;`

**3. Prompt chips: move below chat, just above input**

Currently sandwiched between messages area and controls. Move inside `.chat-container`, directly above `.chat-input`. No structural change to the chips themselves.

**4. JS: rename `updateThemeProgressCard()` → `updateChatStatusLine()`**

Update to write to `#chat-status-line` element instead of the removed card. Same data, different render target.

### File changes — Phase 1

| File | Change |
|---|---|
| `app/templates/chat.html` | Remove top section, hr. Replace h1/badge/description with compact header. Collapse controls. Replace progress card with header status. Move chips. Rename JS function. |
| `app/static/style.css` | Add `.chat-header`, `.chat-controls` styles. Remove or repurpose `.scenario-selector` and `.correction-toggle` styles. |

---

## Phase 2 — Word Capture from AI Chat

**Goal:** Unknown Polish words in AI messages get a subtle dotted underline. Click → tooltip shows translation + "Save word" button. One tap saves to Inbox for FSRS scheduling.

### Backend: 1 new endpoint

**`GET /api/vocabulary/known-tokens`**

Returns all normalized Polish word tokens the user has been introduced to (appeared in at least one session).

```python
# Definition of "known": introduced_at IS NOT NULL
# Query: SELECT lu.normalized_text FROM learning_units lu
#        JOIN learning_progress lp ON lp.unit_id = lu.id
#        WHERE lp.introduced_at IS NOT NULL
# Response: { "tokens": ["słowo", "czytać", "dom", ...] }
```

Lives in `app/routers/upload.py` (alongside other unit/vocabulary endpoints). No new schema, no migration.

### Frontend: changes to `chat.html`

**1. Load known tokens on page load**

```javascript
let knownTokens = new Set();
let knownTokensLoaded = false;

async function loadKnownTokens() {
    const data = await fetch('/api/vocabulary/known-tokens').then(r => r.json());
    knownTokens = new Set(data.tokens);
    knownTokensLoaded = true;
    // Re-annotate any messages already rendered before tokens loaded
    document.querySelectorAll('.chat-message.assistant .content')
        .forEach(el => annotateUnknownWords(el));
}
// called in DOMContentLoaded
```

**2. Normalize function — must mirror Python `normalize_text()` exactly**

```javascript
function normalizeToken(t) {
    return t.normalize("NFC").toLowerCase().trim();
}
```

**3. Tokenization — Unicode regex, not whitespace split**

Extract tokens using `/\p{L}+/gu` (Unicode letter sequences). This handles Polish diacritics, punctuation attached to words ("słowo,"), dashes ("nie-prawda"), and parentheses correctly. Whitespace split is insufficient.

```javascript
const tokens = text.match(/\p{L}+/gu) || [];
```

**4. Annotate AI message text nodes after render**

In `addMessage()`, for assistant messages only, after inserting into DOM call `annotateUnknownWords(contentEl)`.

Walk text nodes directly (avoids touching HTML structure — preserves markdown links, bold, code spans):

```
For each text node in .content:
  - Guard: skip if node.parentElement already has class 'unknown-word' (prevents re-wrapping)
  - if !knownTokensLoaded → skip entire node
  - Use matchAll(/\p{L}+/gu) WITH indices to iterate over matches
  - Build a DocumentFragment by slicing the original string:
      cursor = 0
      for each match at [start, end]:
          append text(originalString.slice(cursor, start))  ← exact spacing/punctuation preserved
          token = match[0]
          if token.length >= 3 AND not /^\d+$/ AND normalizeToken(token) not in knownTokens:
              append <span class="unknown-word" data-word="NORMALIZED">token</span>
          else:
              append text(token)
          cursor = end
      append text(originalString.slice(cursor))  ← tail
  - Replace original text node with fragment
```

This approach slices the original string between match indices — spacing, punctuation, and non-letter characters are always preserved verbatim. Never reconstruct from token array alone (loses inter-token characters).

Only annotate on `addMessage()` call (one message at a time) + once on `loadKnownTokens()` completion. Never reprocess already-annotated messages.

**5. Translation cache**

```javascript
const translationCache = new Map();
```

Check cache before calling API. Store on first fetch. Prevents redundant API calls when user hovers the same word multiple times.

**6. Click handler → tooltip → capture**

```
click .unknown-word (not already .captured)
  → if activeTooltip.dataset.word === word → return (flicker guard — same word fast-clicked)
  → close any existing tooltip
  → position tooltip below span:
      X: Math.min(spanRect.left, window.innerWidth - tooltipWidth - 10)
      Y: if spanRect.bottom + tooltipHeight > window.innerHeight
             → place above span instead (spanRect.top - tooltipHeight - 4)
         else → spanRect.bottom + 4
  → tooltip needs a positioned ancestor: .chat-messages must have position: relative
  → check translationCache first; if miss → show "Translating..." → POST /api/ai/chat/translate
      → on translate failure → tooltip shows "Translation unavailable" + dismiss button
         (do not show "Save word" — can't save without translation)
  → update tooltip: "[translation]  [Save word]"
  → click "Save word"
      normalized = normalizeToken(word)
      → POST /api/units/inbox { text: word, translation: ... }
      → 201:
          all spans with data-word === normalized → class "captured"  ← mark ALL instances in message
          tooltip shows "✓ Saved", auto-dismiss after 1.5s
      → 400 "already exists": tooltip shows "Already saved", auto-dismiss
  → click outside tooltip or Escape → dismiss
```

No page reload. No blocking UI. Only one tooltip open at a time.

**Button label: "Save word" (not "Add to Inbox" or "Learn this word")**
- "Add to Inbox" is accurate but exposes internal implementation detail the user doesn't need to know
- "Learn this word" implies instant learning, which is misleading — the word enters a queue
- "Save word" is honest, short, and action-oriented

### Edge cases

| Case | Handling |
|---|---|
| Token already in `knownTokens` | Not highlighted — no action |
| Word in Inbox, not yet introduced (`introduced_at` NULL) | Highlighted (not in known set) → user clicks → POST returns 400 → "Already saved" |
| Duplicate click on same word | First POST succeeds → span becomes `.captured` → click no-ops |
| Known tokens not loaded yet at render time | Guard: skip annotation entirely — re-annotate all existing messages on load completion |
| Text node already wrapped (re-render guard) | `node.parentElement?.classList.contains('unknown-word')` check — skip |
| Polish inflection (e.g. "słowa" vs "słowo") | Normalized forms differ → highlighted. Correct: inflections are distinct learning objects for an early learner. No stemming. |
| Tokens < 3 chars (prepositions: w, z, na) | Skipped |
| Numbers and pure digits | `/^\d+$/` guard skips them |
| Tooltip off right edge | Clamp X: `Math.min(left, window.innerWidth - tooltipWidth - 10)` |
| Tooltip off bottom edge | Clamp Y: if below viewport → flip above the span |
| Translate API fails or times out | Show "Translation unavailable" + dismiss button — do NOT show "Save word" without a translation |
| Same word appears 2× in one message | On save: query all `[data-word="NORMALIZED"]` in the chat and add `.captured` to all — consistent state |
| Same word translated twice | `translationCache` returns cached value — no API call |
| Fast repeated click on same word | Flicker guard: `if (activeTooltip?.dataset.word === word) return` |

### CSS additions — Phase 2

`.chat-messages` must have `position: relative` — required so `position: absolute` tooltip positions within the message container, not the viewport.

```css
.unknown-word {
    border-bottom: 1px dotted #9c27b0;
    cursor: pointer;
}
.unknown-word.captured {
    border-bottom: 1px solid #4caf50;
    cursor: default;
}
.word-tooltip {
    position: absolute;
    background: #fff;
    border: 1px solid #ddd;
    border-radius: 6px;
    padding: 0.5rem 0.75rem;
    font-size: 0.85rem;
    box-shadow: 0 2px 8px rgba(0,0,0,0.12);
    z-index: 100;
    white-space: nowrap;
}
```

### File changes — Phase 2

| File | Change |
|---|---|
| `app/routers/upload.py` | Add `GET /api/vocabulary/known-tokens` endpoint |
| `app/templates/chat.html` | `loadKnownTokens()`, `normalizeToken()`, `annotateUnknownWords()`, `translationCache`, tooltip click handler |
| `app/static/style.css` | `.unknown-word`, `.unknown-word.captured`, `.word-tooltip` |

---

## Constraints

- Word capture applies to Free Chat only — Study Mode already enforces strict vocabulary, annotation there would be confusing
- Single tokens only — no phrase detection at this stage
- No auto-adding — explicit user action required
- No stemming/lemmatization — Polish morphology is too complex; inflections are valid distinct learning objects

---

## Execution order

1. Phase 1 (layout cleanup) — complete first, it's fast and makes Phase 2 easier to place correctly
2. Phase 2 (word capture) — implement immediately after

---

## Out of scope (later)

- Option B: Study → Chat handoff (load session vocabulary as chat context)
- Option C: Checkpoint scores on dashboard
- Option D: Conversation-driven FSRS review signal (needs design before code)
