# Cloze Sentence Audio — Implementation Spec

> Status: READY TO IMPLEMENT — FINAL
> Reviewed: 2026-04-04 — v3, all risks resolved including blob URL tracking and play-start re-enable

---

## Goal

After a user submits a cloze answer, play the full Polish `context_sentence` via TTS.
- Audio button: visible + disabled before answer, enabled + auto-plays after submit (correct or wrong)
- No audio generated until the user actually submits — zero quota waste
- Full isolation: recall / passive / recall_audio modes untouched

---

## Prerequisite check

**`context_sentence` guarantee:** When `exercise_type == "cloze"` is stored on a `SessionUnit`,
it was set only after `get_or_generate_sentence()` returned a non-empty value AND `make_cloze_prompt()`
succeeded (`session_service.py:2144–2150`). Failure path falls back to `exercise_type == "recall"`.

Therefore: `session_unit.unit.context_sentence` is **always non-null** for cloze units.
The 204 fallback in the endpoint is purely defensive for recall-fallback units that may hit it.

**`context_sentence` content:** clean full sentence (no blanks). `cloze_prompt` is the blanked
version stored separately. TTS reads from `context_sentence` — correct.

---

## Phase 1 — DB: `sentence_audio_assets` table

**File:** `app/models/audio.py`

Add after the existing `AudioAsset` class:

```python
class SentenceAudioAsset(Base):
    """
    Content-addressed audio cache for full context sentences (cloze mode TTS).

    Unlike AudioAsset, this is not tied to a specific LearningUnit — the same
    sentence may appear across multiple units. Keyed on audio_hash which is
    computed from (engine, voice, language, normalized_text).
    """
    __tablename__ = "sentence_audio_assets"
    __table_args__ = (
        UniqueConstraint(
            "audio_hash", "engine", "voice", "language",
            name="uq_sentence_audio_hash_engine_voice_lang",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    audio_hash = Column(String, nullable=False, index=True)
    engine = Column(String, nullable=False)
    voice = Column(String, nullable=False)
    language = Column(String, nullable=False)
    file_path = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return f"<SentenceAudioAsset(id={self.id}, hash={self.audio_hash[:8]}, engine={self.engine})>"
```

**File:** `app/models/__init__.py`

Add `SentenceAudioAsset` to imports and `__all__`.

---

## Phase 2 — Migration

**File:** `alembic/versions/20260404_000001_add_sentence_audio_assets.py`

```python
"""add sentence_audio_assets table

Revision ID: 20260404_000001
Revises: 20260331_000002_add_db_instance_id_to_settings
Create Date: 2026-04-04
"""
from alembic import op
import sqlalchemy as sa

revision = "20260404_000001"
down_revision = "20260331_000002_add_db_instance_id_to_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sentence_audio_assets",
        sa.Column("id", sa.Integer, primary_key=True, index=True),
        sa.Column("audio_hash", sa.String, nullable=False, index=True),
        sa.Column("engine", sa.String, nullable=False),
        sa.Column("voice", sa.String, nullable=False),
        sa.Column("language", sa.String, nullable=False),
        sa.Column("file_path", sa.String, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "audio_hash", "engine", "voice", "language",
            name="uq_sentence_audio_hash_engine_voice_lang",
        ),
    )


def downgrade() -> None:
    op.drop_table("sentence_audio_assets")
```

---

## Phase 3 — Schema

**File:** `app/schemas/audio.py`

Add:

```python
class SentenceAudioRequest(BaseModel):
    session_unit_id: int
```

Server resolves the sentence from DB using `session_unit_id` — no raw text on the wire.

---

## Phase 4 — Backend endpoint

**File:** `app/routers/audio.py`

Add **above** the existing `@router.get("/{unit_id}")` route (FastAPI resolves literal paths before
parameterized — if placed after, `"sentence"` is matched as a `unit_id` integer and fails with 422).

New imports needed at top of file:
```python
from app.models import LearningUnit, AudioAsset, SentenceAudioAsset
from app.models.session import SessionUnit
from app.schemas.audio import VoiceOverrideRequest, SentenceAudioRequest
```

Endpoint:

```python
@router.post("/sentence")
def get_sentence_audio(
    request: SentenceAudioRequest,
    db: Session = Depends(get_db),
):
    """
    Generate or serve cached TTS audio for a cloze context sentence.

    Accepts session_unit_id (not raw text) to prevent arbitrary TTS abuse.
    Returns 204 when the unit has no context_sentence (recall-fallback units).
    Returns 403 when TTS is disabled.
    Audio is cached in sentence_audio_assets (content-addressed by hash).
    """
    from app.models.session import SessionUnit

    session_unit = db.query(SessionUnit).filter(
        SessionUnit.id == request.session_unit_id
    ).first()
    if not session_unit:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"SessionUnit {request.session_unit_id} not found")

    context_sentence = session_unit.unit.context_sentence
    if not context_sentence:
        # Recall-fallback unit — no sentence available, silent no-op
        return Response(status_code=204)

    tts_service = get_tts_service_for_source_language(settings.source_language)
    if not tts_service.is_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Audio pronunciation is not enabled")

    # Always hash and generate from the same normalized string
    normalized = normalize_text_for_audio(context_sentence)
    audio_hash = compute_audio_hash(
        engine=tts_service.engine,
        voice=tts_service.voice,
        language=tts_service.language,
        normalized_text=normalized,
    )

    # Cache lookup
    asset = (
        db.query(SentenceAudioAsset)
        .filter_by(
            audio_hash=audio_hash,
            engine=tts_service.engine,
            voice=tts_service.voice,
            language=tts_service.language,
        )
        .first()
    )
    if asset:
        file_path = settings.base_dir / asset.file_path
        if file_path.exists():
            logger.debug(f"[TTS CACHE HIT] sentence hash={audio_hash[:8]}, "
                         f"session_unit={request.session_unit_id}")
            return FileResponse(str(file_path), media_type="audio/mpeg",
                                filename="sentence.mp3")
        # Stale record — file deleted externally; delete record and regenerate
        logger.warning(f"[TTS STALE] sentence hash={audio_hash[:8]}, regenerating")
        db.delete(asset)
        db.flush()  # flush, not commit — keep atomic with the upcoming insert

    # Generate from normalized (same string that was hashed — guaranteed cache consistency)
    logger.info(f"[TTS GENERATED] sentence hash={audio_hash[:8]}, "
                f"session_unit={request.session_unit_id}, engine={tts_service.engine}")
    try:
        audio_bytes = tts_service.generate_audio(normalized)
    except (MurfDisabledError, ElevenLabsDisabledError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Audio pronunciation is not enabled")
    except (MurfInvalidConfigurationError, ElevenLabsInvalidConfigurationError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except AudioGenerationError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"Failed to generate sentence audio: {e}")

    safe_voice = _sanitize_for_filename(tts_service.voice)
    safe_language = _sanitize_for_filename(tts_service.language)
    filename = f"sentence_{audio_hash}_{safe_language}_{safe_voice}.mp3"
    relative_path = f"data/audio/{filename}"
    file_path = settings.audio_dir / filename

    file_path.write_bytes(audio_bytes)

    new_asset = SentenceAudioAsset(
        audio_hash=audio_hash,
        engine=tts_service.engine,
        voice=tts_service.voice,
        language=tts_service.language,
        file_path=relative_path,
    )
    db.add(new_asset)
    db.commit()  # single commit covers both the flush'd delete (if any) and this insert

    logger.info(f"Sentence audio cached: session_unit={request.session_unit_id}, "
                f"hash={audio_hash[:8]}, file={relative_path}")

    return FileResponse(str(file_path), media_type="audio/mpeg", filename="sentence.mp3")
```

**Transaction note:** `db.flush()` on stale delete + `db.commit()` after insert = one atomic
operation. No window where the cache entry is absent.

---

## Phase 5 — Frontend state

In `study.html`, near the `audioBlobCache` declaration, add:

```js
let clozeSentenceBlobCache = new Map();  // session_unit.id → Blob
let currentAudioBlobUrl = null;          // explicitly tracked for safe revocation
```

**Why explicit tracking:** `audioPlayer.src` is normalized by the browser to an absolute URL
(`blob:http://localhost:8000/abc-123`). Revoking the DOM-normalized value has quirks in Safari.
Tracking the original `blobUrl` directly is deterministic across all browsers.

---

## Phase 6 — `updateAudioButtonForCloze()`

Add near `updateAudioButtonForRecall()` (~line 4536):

```js
function updateAudioButtonForCloze() {
    if (!audioEnabled) return;
    const unit = units.find(u => u.position === currentPosition);
    const audioBtn = document.getElementById('audio-btn');
    if (!unit || unit.exercise_type !== 'cloze' || !unit.unit.context_sentence) {
        audioBtn.classList.add('hidden');
        return;
    }
    audioBtn.classList.remove('hidden');
    // Disabled pre-answer (unit.answered === false), enabled post-answer
    audioBtn.disabled = !unit.answered;
    setAudioLoading(false);
}
```

**Wire into `updateAudioButton()`** — add as first branch at line 4341, before `recall_audio` check:

```js
if (sessionMode === 'cloze') {
    updateAudioButtonForCloze();
    return;
}
```

**Wire into `displayUnit()` cloze branch** — at line 3722, replace:
```js
updateAudioButtonForRecall();
```
with:
```js
updateAudioButtonForCloze();
```

---

## Phase 7 — `playClozeSentenceAudio()`

Add after `showAudioError()` (~line 4504):

```js
async function playClozeSentenceAudio() {
    if (audioLoading) return;  // must be first — blocks double-play race

    const unit = units.find(u => u.position === currentPosition);
    if (!unit || !unit.unit.context_sentence) return;

    const cacheKey = unit.id;  // session_unit.id — correct scope for this sentence instance

    // Disable button for the duration of load+play to prevent rapid-click overlap
    const audioBtn = document.getElementById('audio-btn');
    if (audioBtn) audioBtn.disabled = true;

    setAudioLoading(true);

    try {
        let audioBlob;

        if (clozeSentenceBlobCache.has(cacheKey)) {
            audioBlob = clozeSentenceBlobCache.get(cacheKey);
        } else {
            const resp = await fetch('/api/audio/sentence', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_unit_id: unit.id }),
            });
            if (resp.status === 204) {
                // No sentence available for this unit — silent, no error shown
                setAudioLoading(false);
                if (audioBtn) audioBtn.disabled = false;
                return;
            }
            if (!resp.ok) {
                setAudioLoading(false);
                if (audioBtn) audioBtn.disabled = false;
                showAudioError('Failed to load sentence audio');
                return;
            }
            audioBlob = await resp.blob();
            clozeSentenceBlobCache.set(cacheKey, audioBlob);
        }

        // Revoke stale blob URL via explicit tracker (not audioPlayer.src — browser normalizes
        // to absolute URL, which can cause revocation quirks in Safari)
        const audioPlayer = document.getElementById('audio-player');
        if (currentAudioBlobUrl) {
            URL.revokeObjectURL(currentAudioBlobUrl);
            currentAudioBlobUrl = null;
        }

        const blobUrl = URL.createObjectURL(audioBlob);
        currentAudioBlobUrl = blobUrl;  // track for next revocation

        audioPlayer.onended = null;
        audioPlayer.onerror = null;

        audioPlayer.onended = () => {
            URL.revokeObjectURL(blobUrl);
            if (currentAudioBlobUrl === blobUrl) currentAudioBlobUrl = null;
            if (audioBtn) audioBtn.disabled = false;  // re-enable for replay
        };
        audioPlayer.onerror = () => {
            setAudioLoading(false);
            URL.revokeObjectURL(blobUrl);
            if (currentAudioBlobUrl === blobUrl) currentAudioBlobUrl = null;
            if (audioBtn) audioBtn.disabled = false;
            showAudioError('Sentence audio failed');
        };

        audioPlayer.src = blobUrl;
        await audioPlayer.play();
        setAudioLoading(false);

        // Re-enable immediately after playback starts — covers pause/navigate edge case
        // where onended never fires. onended still handles the clean replay flow.
        if (audioBtn) audioBtn.disabled = false;

    } catch (err) {
        setAudioLoading(false);
        if (audioBtn) audioBtn.disabled = false;
        showAudioError('Audio error: ' + err.message);
    }
}
```

**Wire into `playAudio()`** — add after the `audioLoading` guard at line 4378:

```js
if (sessionMode === 'cloze') {
    const audioBtn = document.getElementById('audio-btn');
    if (audioBtn && audioBtn.disabled) return;  // pre-answer guard
    playClozeSentenceAudio();
    return;
}
```

---

## Phase 8 — `showRecallFeedback()` changes

**Auto-play block** — add after `render(); syncControls();` (~line 3993), before the existing
`if (isCorrect)` block:

```js
if (sessionMode === 'cloze') {
    updateAudioButtonForCloze();   // re-show + enable (unit.answered is now true)
    playClozeSentenceAudio();      // always auto-play — wrong answers benefit more
}
```

**Manual advance flag** — change line 3984 from:
```js
StudyState.feedbackRequiresManualAdvance = !isCorrect;
```
to:
```js
StudyState.feedbackRequiresManualAdvance = sessionMode === 'cloze' ? true : !isCorrect;
// Cloze always requires explicit tap — gives time to hear the sentence
```

**Auto-advance guards** — lines 3996 and 4003:

```js
// Line 3996
if (data.session_completed && StudyState.autoAdvance && sessionMode !== 'cloze') {

// Line 4003
} else if (!data.session_completed && StudyState.autoAdvance && sessionMode !== 'cloze') {
```

Both paths excluded for cloze. The session-complete path is also excluded — cloze requires a
manual tap to reach the results screen. This is consistent with `feedbackRequiresManualAdvance = true`.

---

## What is NOT changed

| Area | Status |
|---|---|
| `recall`, `recall_audio`, `passive` modes | Untouched |
| `updateAudioButton()` (non-cloze path) | Untouched |
| `hideAudioButton()` | Untouched |
| `playAudio()` (recall/passive path) | Untouched |
| Existing `AudioAsset` table and constraint | Untouched |
| `GET /api/audio/{unit_id}` | Untouched |

---

## File change map

| File | Change |
|---|---|
| `app/models/audio.py` | Add `SentenceAudioAsset` class |
| `app/models/__init__.py` | Export `SentenceAudioAsset` |
| `alembic/versions/20260404_000001_add_sentence_audio_assets.py` | New migration |
| `app/schemas/audio.py` | Add `SentenceAudioRequest` |
| `app/routers/audio.py` | Add imports + `POST /api/audio/sentence` above `/{unit_id}` |
| `app/templates/study.html` | Phases 5–8 above |

---

## Runtime correctness checklist

| Check | Answer |
|---|---|
| `context_sentence` always set when `exercise_type == "cloze"` | Yes — session_service guarantees it |
| `context_sentence` is clean text (no blanks) | Yes — `cloze_prompt` is the blanked version |
| Hash and generate use the same string | Yes — both use `normalized` |
| Stale DB record handled atomically | Yes — `flush()` + single `commit()` |
| Blob URL revoked on replay | Yes — via `currentAudioBlobUrl` tracker, not DOM-normalized src |
| Safari blob revocation quirk | Yes — explicit variable avoids `audioPlayer.src` normalization |
| Button re-enable on pause/navigate | Yes — re-enabled immediately after `play()` resolves |
| Double-play race prevented | Yes — `audioLoading` guard + `btn.disabled` |
| Preload excluded | Yes — no preload; quota spent only on submit |
| 204 handled silently | Yes — no error shown for recall-fallback units |
| Auto-advance blocked for cloze | Yes — both completed and non-completed paths guarded |
| All other modes unaffected | Yes — every change is behind `sessionMode === 'cloze'` |
