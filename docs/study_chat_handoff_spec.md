# Study → Chat Handoff — Implementation Spec

> Feature: after a study session, user can continue practicing the same vocabulary in AI chat.
> No schema changes. No new endpoints. Stateless via sessionStorage.

---

## Decisions

| Question | Decision | Rationale |
|---|---|---|
| Trigger | Button on session-end screen only | Mid-session breaks focus |
| Button label | "Practice these words in AI chat →" | Clarity > brevity |
| Vocab passing | sessionStorage | No extra API call, no race condition, auto-clears on tab close |
| Vocab selection | All units in session, capped at 20, session order | Natural distribution works best for AI |
| AI context | System prompt prepend, injected once | No new endpoints, no schema changes |
| UI | Banner (entry) + status line (persistent) | Banner = signal on load; status line = context visible throughout |
| Lifetime | Until: page reload without sessionStorage, "Clear chat" click, or new session loaded | Explicit reset points only |

---

## Source data — confirmed field names

`study.html` exposes a global `units` array (line 442):
```javascript
let units = {{ session.units | tojson if session else '[]' }};
```

Each item in `units` has:
- `unit.text` — the Polish word/phrase
- `unit.translation` — English translation
- `answered` — boolean, true when answered in this session
- `is_correct` — boolean

Build vocab from answered units only (user actually saw them):
```javascript
const vocab = units
    .filter(u => u.answered)
    .slice(0, 20)
    .map(u => ({
        word: u.unit.text,
        translation: u.unit.translation || ""
    }));
```

Slice before storing — never store more than 20.

---

## Button placement — confirmed

The end screen renders into `#completion-buttons` via `showCompletionButtons()` (line 4084).
Add the handoff button inside that function, after existing buttons, only if `vocab.length > 0`:

```javascript
if (vocab.length > 0) {
    const btn = document.createElement("button");
    btn.className = "btn-secondary";
    btn.textContent = "Practice these words in AI chat →";
    btn.addEventListener("click", () => {
        sessionStorage.setItem("chat_vocab", JSON.stringify(vocab));
        setTimeout(() => { window.location.href = "/chat"; }, 50);
    });
    completionButtonsEl.appendChild(btn);
}
```

50ms delay — ensures sessionStorage write completes before navigation (avoids rare race on slow devices).

---

## sessionStorage contract

Key: `chat_vocab`

Value: JSON array, max 20 items:
```json
[
  { "word": "podróż", "translation": "journey" },
  { "word": "lotnisko", "translation": "airport" }
]
```

Written by: `study.html` on button click, sliced to 20 before write.
Read + consumed by: `chat.html` on `DOMContentLoaded` — immediately removed after reading.
Cleared by: also cleared on "Clear chat".

---

## Chat page — reading and consuming

```javascript
const raw = sessionStorage.getItem("chat_vocab");
sessionStorage.removeItem("chat_vocab"); // consume immediately — no stale persistence

let sessionVocab = [];
try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed) && parsed.length > 0) {
        sessionVocab = parsed; // already capped at 20 by writer
    }
} catch (e) {
    sessionVocab = []; // parse failure → treat as no vocab
}
// From here: if sessionVocab.length === 0 → no banner, no injection, normal chat
```

---

## System prompt injection

Injected once, guarded by flag:

```javascript
let sessionPromptInjected = false;
```

On first message send (before POST to `/api/ai/chat`):
```javascript
if (!sessionPromptInjected && sessionVocab.length > 0) {
    const wordList = sessionVocab.map(v => `${v.word} (${v.translation})`).join(", ");
    const injection = `The user has just studied the following Polish words:\n\n${wordList}\n\nUse these words naturally in the conversation.\nPrioritize them where appropriate.\nDo not force usage unnaturally.\nAvoid introducing new vocabulary unless necessary.\n\n`;
    systemPrompt = injection + systemPrompt;
    sessionPromptInjected = true;
}
```

Guard prevents: duplicate injection on retries, corrupted prompt stack.

---

## Clear chat

When user clears/resets conversation:
```javascript
sessionVocab = [];
sessionPromptInjected = false; // also reset injection flag
// hide banner if still visible
// revert status line to theme status or hide
```

---

## UI spec

### Banner

```html
<div id="session-vocab-banner" class="session-vocab-banner hidden">
    Session loaded: <strong id="session-vocab-count"></strong> words from your study session
    <button id="session-vocab-dismiss" class="banner-dismiss">×</button>
</div>
```

Placement: inside `.chat-container`, above `.chat-messages`.

Behavior:
- Shown on load if `sessionVocab.length > 0`
- Dismiss button hides it immediately
- Auto-hides after 8s **only if user has not started typing** — if typing has started, skip auto-hide

```javascript
let bannerDismissTimer = null;
if (sessionVocab.length > 0) {
    showBanner(sessionVocab.length);
    bannerDismissTimer = setTimeout(() => {
        if (!userHasTyped) hideBanner();
    }, 8000);
}
// On first keydown in textarea: clearTimeout(bannerDismissTimer)
```

### Status line — priority rule

Reuse `#chat-status-line`. Explicit priority:

```javascript
if (themeActive) {
    // show theme status (existing behavior)
} else if (sessionVocab.length > 0) {
    statusLine.textContent = `AI practice · ${sessionVocab.length} words`;
    statusLine.classList.remove("hidden");
} else {
    statusLine.classList.add("hidden");
}
```

Theme always wins. Vocab status shows only when no theme is active.

---

## File changes

| File | Change |
|---|---|
| `app/templates/study.html` | Add button in `showCompletionButtons()`, write sessionStorage on click |
| `app/templates/chat.html` | Read + consume sessionStorage; `sessionVocab` state; `sessionPromptInjected` guard; banner + status line; clear on reset |
| `app/static/style.css` | `.session-vocab-banner` styles |

No changes to: any Python file, `app/routers/`, DB schema.

---

## Edge cases

| Case | Handling |
|---|---|
| Session has 0 answered units | `vocab.length === 0` → no button rendered |
| sessionStorage empty / missing | Parse guard returns `[]` → normal chat, no banner |
| JSON parse failure | `catch` → `sessionVocab = []` → normal chat |
| User opens chat directly (no handoff) | `sessionStorage.getItem` returns `null` → normal chat |
| User reopens tab | sessionStorage is tab-scoped — gone on new tab |
| Clear chat | Reset `sessionVocab = []` and `sessionPromptInjected = false` |
| Theme active + vocab loaded | Theme status wins in status line; banner still shows on entry |
| `translation` field empty | `u.unit.translation || ""` — stored as empty string, shown without parens in prompt if empty |
