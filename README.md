# Word App — Intelligent Vocabulary Trainer

![Python](https://img.shields.io/badge/python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)
![SQLite](https://img.shields.io/badge/database-SQLite-lightgrey)
![License](https://img.shields.io/badge/license-MIT-blue)

A local-first, AI-assisted vocabulary learning system designed for real retention, not passive memorization.

Word App enforces a **closed-vocabulary learning model**: you only learn words that come from your own materials. It runs entirely on your machine, works without AI, and uses AI as a tutor rather than a source of random content.

## Preview

Add screenshots to:

- `docs/screenshots/study.png`
- `docs/screenshots/chat.png`

Then embed them here for a visual overview of Study Mode and Free Chat.

## Key Concepts

- **Closed vocabulary learning** — no random words outside your own materials
- **Recall-first mastery model** — practice is built around active retrieval
- **AI as constrained tutor** — guidance without uncontrolled vocabulary drift
- **Local-first architecture** — data, sessions, and backups stay on your machine

## Why This Is Different

Most language apps optimize for engagement.

Word App optimizes for:

- **Active recall over passive exposure**
- **Strict vocabulary control**
- **Data-driven scheduling**
- **Full local ownership of data**
- **AI as tutor, not content generator**

## Core Learning Loop

Chat -> Capture -> Study -> Reinforce -> Chat

- Discover or test words in chat
- Save useful vocabulary to your own collection
- Practice in structured study sessions
- Reinforce with recall-first review and scheduling

## Features

- **PDF Import** — Upload vocabulary PDFs with word/phrase pairs and auto-extract learning units
- **Study Mode** — Practice vocabulary in structured sessions with AI responses constrained to your known vocabulary
- **Free Chat** — Unrestricted AI conversation for open-ended language practice
- **Progress Tracking** — Monitor learned units, confidence scores, session history, and identify weak spots
- **Audio Pronunciation** — Text-to-speech audio with automatic engine selection (ElevenLabs for Polish, Murf for English)
- **Data Export/Import** — Full backup and restore functionality for data portability

## Study Mode vs Free Chat

| Aspect | Study Mode | Free Chat |
|--------|------------|-----------|
| Vocabulary | Restricted to session + learned units | Unrestricted |
| Purpose | Structured practice with known material | Open conversation and exploration |
| Validation | AI responses checked for vocabulary compliance | No validation |
| Progress | Affects learning confidence scores | No effect on progress |

**Study Mode** ensures you only encounter vocabulary you're actively learning and can reinforce. **Free Chat** removes those restrictions for open-ended conversation and exploration.

## Tech Stack

- **Backend**: FastAPI 0.115
- **Database**: SQLite via SQLAlchemy 2.0
- **PDF Parsing**: pdfplumber
- **AI (Local)**: Ollama with llama3.2 (default)
- **AI (Optional)**: OpenAI API (gpt-4o-mini)
- **TTS (English)**: Murf AI (optional)
- **TTS (Polish)**: ElevenLabs (optional)
- **Frontend**: Jinja2 templates + static CSS

## Project Structure

```
word_app/
├── app/
│   ├── main.py              # FastAPI application entry
│   ├── config.py            # Settings and environment config
│   ├── database.py          # SQLAlchemy setup
│   ├── models/              # Database models
│   ├── routers/             # API endpoints
│   │   ├── upload.py        # PDF upload endpoints
│   │   ├── sessions.py      # Learning session management
│   │   ├── ai.py            # AI endpoints (study + chat)
│   │   ├── data.py          # Export/import endpoints
│   │   └── ui.py            # HTML page routes
│   ├── schemas/             # Pydantic request/response models
│   ├── services/
│   │   ├── pdf_parser.py    # PDF extraction logic
│   │   ├── export_service.py # Data export logic
│   │   ├── import_service.py # Data import logic
│   │   └── ai/              # AI provider clients
│   ├── templates/           # Jinja2 HTML templates
│   └── static/              # CSS styles
├── data/
│   └── backups/             # Automatic backups
├── scripts/
│   └── rebalance_due_dates.py  # Optional: even out due-word counts across days
├── samples/                 # Sample vocabulary PDFs
├── tests/                   # Unit tests
├── main.py                  # Development server entry point
└── requirements.txt
```

## Local Setup

### 1. Clone and Create Virtual Environment

```bash
git clone <repository-url>
cd word_app

python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# or: .venv\Scripts\activate  # Windows
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Ollama (Optional)

Word App works without AI, but for full functionality install [Ollama](https://ollama.com):

```bash
# Install Ollama (macOS)
brew install ollama

# Start Ollama service
ollama serve

# Pull the default model
ollama pull llama3.2
```

The app auto-detects Ollama at `http://localhost:11434`.

### 4. Run the Application

```bash
python main.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

## Accessing from iPad (LAN)

Word App can be accessed from an iPad over your local network. The Mac remains the server; the iPad connects via Safari.

### 1. Find Your Mac's LAN IP

```bash
# macOS
ipconfig getifaddr en0
```

This returns something like `192.168.1.42`.

### 2. Configure the Server for LAN Access

Add these lines to your `.env` file:

```bash
# Bind to all interfaces so the iPad can reach the server
WORD_APP_HOST=0.0.0.0

# Allow the iPad's requests (replace with your actual Mac IP)
WORD_APP_CORS_ALLOW_ORIGINS=http://127.0.0.1:8000,http://localhost:8000,http://192.168.1.42:8000
```

Then restart the server:

```bash
python main.py
```

### 3. Open on iPad

On your iPad, open Safari and navigate to:

```
http://192.168.1.42:8000
```

Replace `192.168.1.42` with the IP from step 1. Both devices must be on the same Wi-Fi network.

> **Note**: When `WORD_APP_HOST` is not set (or set to `127.0.0.1`), the server is only accessible from the Mac itself. LAN access requires explicitly setting `WORD_APP_HOST=0.0.0.0`.

## Configuration

Configuration is handled via environment variables with the `WORD_APP_` prefix.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WORD_APP_HOST` | `127.0.0.1` | Bind address (`0.0.0.0` for LAN access) |
| `WORD_APP_PORT` | `8000` | Server port |
| `WORD_APP_CORS_ALLOW_ORIGINS` | (localhost) | Comma-separated allowed origins for CORS |
| `WORD_APP_SOURCE_LANGUAGE` | `Polish` | Source language for vocabulary |
| `WORD_APP_TARGET_LANGUAGE` | `English` | Target language for translations |
| `WORD_APP_OLLAMA_MODEL` | `llama3.2` | Ollama model to use |
| `WORD_APP_OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama API URL |
| `WORD_APP_OPENAI_ENABLED` | `false` | Enable OpenAI instead of Ollama |
| `WORD_APP_OPENAI_API_KEY` | — | OpenAI API key (if enabled) |
| `WORD_APP_VOCAB_VALIDATION_ENABLED` | `true` | Enable AI spell-check during upload |
| `WORD_APP_SESSION_SIZE` | `50` | Number of units per study session |
| `WORD_APP_MURF_ENABLED` | `false` | Enable Murf TTS for audio pronunciation |
| `WORD_APP_MURF_API_KEY` | — | Murf API key (get from https://murf.ai) |
| `WORD_APP_MURF_VOICE` | `en-US-marcus` | Murf voice ID for English |
| `WORD_APP_MURF_LANGUAGE` | `en-US` | Murf language code |
| `WORD_APP_ELEVENLABS_ENABLED` | `false` | Enable ElevenLabs TTS for Polish audio |
| `WORD_APP_ELEVENLABS_API_KEY` | — | ElevenLabs API key (get from https://elevenlabs.io) |
| `WORD_APP_ELEVENLABS_MODEL` | `eleven_multilingual_v2` | ElevenLabs model ID |
| `WORD_APP_ELEVENLABS_VOICE_PL` | — | ElevenLabs voice ID for Polish (required when enabled) |
| `WORD_APP_SMOOTH_DUE_LOAD` | `true` | Cap daily scheduled reviews so future days stay even |
| `WORD_APP_MAX_DUE_PER_DAY` | `350` | Max reviews scheduled per calendar day (when smoothing enabled) |
| `WORD_APP_SPREAD_OVERDUE_WHEN_ABOVE` | `400` | When overdue count exceeds this, spread them across the next 7 days |

### Using .env for Local Development

For convenience, you can use a `.env` file to set environment variables locally:

```bash
# Copy the example file
cp .env.example .env

# Edit with your settings
nano .env
```

**Example `.env` for enabling OpenAI validation:**

```bash
WORD_APP_OPENAI_ENABLED=true
WORD_APP_OPENAI_API_KEY=sk-your-api-key-here
WORD_APP_VOCAB_VALIDATION_ENABLED=true
```

**Example `.env` for enabling audio pronunciation (Polish with ElevenLabs):**

```bash
WORD_APP_ELEVENLABS_ENABLED=true
WORD_APP_ELEVENLABS_API_KEY=your-elevenlabs-api-key
WORD_APP_ELEVENLABS_VOICE_PL=your-polish-voice-id
```

**Example `.env` for enabling audio pronunciation (English with Murf):**

```bash
WORD_APP_MURF_ENABLED=true
WORD_APP_MURF_API_KEY=your-murf-api-key
WORD_APP_MURF_VOICE=en-US-marcus
```

> ⚠️ **Important**: `.env` is git-ignored and will NOT be committed. Never commit API keys.

The app works without a `.env` file. If `python-dotenv` is not installed, the app silently skips `.env` loading and uses shell environment variables instead.

To install dotenv support (optional):

```bash
pip install python-dotenv
```

## Usage

### 1. Upload Vocabulary PDF

Navigate to **Upload** and select a PDF file. The parser expects lines in the format:

```
word - translation
phrase (part of speech) – translation
Sentence with punctuation. - Translation sentence.
```

Supported delimiters: ` – `, ` - `, `–`, `-`

### 2. Start a Study Session

Go to **Study** to begin a session with your uploaded vocabulary. The AI will only use words from your current session and previously learned units.

### 3. Practice with Free Chat

Use **Chat** for open-ended conversation practice. No vocabulary restrictions apply.

### 4. Audio Pronunciation

Audio pronunciation is automatically available in Study Mode when enabled. The app automatically selects the best TTS engine based on your source language:
- **Polish**: Uses ElevenLabs (when enabled) for high-quality Polish pronunciation
- **English/Other**: Uses Murf TTS (when enabled)

Audio files are cached locally in `data/audio/` to avoid duplicate API calls. See Configuration section for setup instructions.

#### Automatic audio reuse / relinking

On startup, the app performs a best-effort relink step to reuse existing audio files already present in `data/audio/`, so you don’t regenerate audio unnecessarily after imports/migrations.

#### Audio Cache Cleanup

Over time, orphaned audio files may accumulate in `data/audio/` (e.g., from deleted learning units). The app provides a safe cleanup mechanism to remove unreferenced files without affecting learning data.

**Important**: Cleanup only removes files that are NOT referenced by any AudioAsset in the database. Referenced audio files are never deleted.

**Development Endpoint** (dev-only):

The cleanup endpoint is only available when `WORD_APP_DEBUG=true` or `WORD_APP_ENV=development`:

```bash
curl -X POST http://localhost:8000/api/audio/cleanup
```

Response:
```json
{
  "files_deleted": 5,
  "bytes_freed": 1234567
}
```

**Optional Startup Cleanup**:

You can enable automatic cleanup on startup (disabled by default):

```bash
# In .env file
WORD_APP_AUDIO_CLEANUP_ON_STARTUP=true
```

When enabled, cleanup runs once on application startup and logs the results. This is safe to run multiple times (idempotent).

**Note**: Cleanup does NOT regenerate audio. If you need to regenerate audio files, use the regular audio generation endpoints.

### 5. Track Progress

Visit **Progress** to see:
- Total units learned vs. in progress
- Confidence scores per unit
- Session completion history
- Weak spots requiring more practice
- **Mastery progress per vocabulary source**

#### What does 100% mean?

Word App uses a strict definition of mastery to determine when a vocabulary source is truly complete:

**A word is MASTERED only if ALL conditions are met:**
- ✅ **Introduced** — Word has been seen in Passive mode (`introduced_at IS NOT NULL`)
- ✅ **Recall success** — Last active recall result was correct (`last_recall_result == correct`)
- ✅ **High confidence** — Confidence score is at least 0.85 (`confidence_score >= 0.85`)
- ✅ **Not due** — Word is not currently due for review (`next_review_at > now()`)

**Important distinctions:**
- **Passive ≠ Learned** — Just seeing a word doesn't mean you've learned it
- **Recall ≠ Stable** — One correct recall doesn't guarantee long-term retention
- **100% = Recall + Confidence + Time** — True mastery requires consistent recall success, high confidence, and no pending reviews

A vocabulary source reaches 100% completion only when **all** its words meet these strict criteria. This ensures you have truly stable knowledge, not just exposure or temporary recall.

### 6. Review Session History

Visit **History** to review past study sessions:
- View list of all completed and abandoned sessions
- See date, mode, and results (Correct/Almost/Wrong counts)
- Click any session to see detailed unit-by-unit breakdown
- Completely read-only — does not affect scoring or learning progress

**Important**: Session History is purely for review purposes. Viewing history does not change your learning data, confidence scores, or session generation.

## Data Export / Import

Word App provides comprehensive data export and import functionality for backup and data portability.

### Export

Export creates a complete JSON backup of all your data:

- Learning units (vocabulary)
- Learning progress (scores, confidence)
- Learning sessions
- Application settings

To export:
1. Navigate to **Data** in the web interface
2. Click "Download Export"
3. Save the JSON file to a safe location

Or use the API:
```bash
curl http://localhost:8000/api/export -o backup.json
```

### Import

Import restores data from a previously exported JSON file.

⚠️ **Warning**: Import is a **destructive operation** that will:
1. Create an automatic backup of current data
2. Delete ALL existing data
3. Replace with the imported data

To import:
1. Navigate to **Data** in the web interface
2. Select your export JSON file
3. Check the confirmation checkbox
4. Click "Import Data"
5. Confirm the final warning dialog

Or use the API:
```bash
curl -X POST "http://localhost:8000/api/import?confirm=true" \
  -F "file=@backup.json"
```

### Backup Behavior

- Automatic backups are created before every import
- Backups are stored in `data/backups/`
- If import fails, the previous state is automatically restored
- Export files are human-readable JSON for transparency

### Best Practices

1. **Export regularly** - Create periodic exports for safety
2. **Verify before import** - Use the validate endpoint to check files
3. **Keep multiple backups** - Don't rely on a single export file
4. **Store exports safely** - Keep copies in multiple locations

## Learning Logic v2 (Recall-First)

Word App uses a **recall-first learning model** where active recall is authoritative for determining mastery.

### Study Modes

| Mode | Description | Confidence Impact |
|------|-------------|-------------------|
| **Active Recall** ⭐ | See translation, type answer, auto-evaluate | Authoritative — determines mastery |
| **Passive Mode** | See word, reveal translation, self-assess | Supplementary — cannot override recall failures |

### Answer Evaluation (Active Recall)

| Result | Description | Confidence Impact |
|--------|-------------|-------------------|
| **Correct** ✓ | Exact match after normalization | +1.0 times_correct |
| **Almost** ≈ | ≤1 character typo or punctuation-only | +1.0 times_correct (partial credit) |
| **Wrong** ✗ | More than 1 character difference | +1.0 times_failed |

**Important rules:**
- Diacritics are **required** (żółć ≠ zolc)
- Punctuation is ignored in default lexical mode
- Single character typos get partial credit

### Passive Mode Limitations

Passive mode success **cannot increase confidence** if `last_recall_result == failed`. This prevents users from bypassing the recall requirement by repeatedly marking words as "known" in passive mode after failing active recall.

### Smart Review Scheduling (SRS-Lite)

Word App uses an intelligent review scheduling system that ensures you review words at optimal intervals based on how well you know them.

**How it works:**

1. **After each answer**, the app calculates when you should next review the word
2. **Strong knowledge** → longer intervals (up to 2 weeks)
3. **Weak knowledge** → shorter intervals (reviewed sooner)
4. **Failed recall** → immediate review in next session

**Review indicators in the UI:**
- 📅 **Due today** — Word needs review now
- 📅 **Review in X days** — Scheduled for future review

**Key behaviors:**
- Words due for review are **prioritized** in session selection (up to 70% of session)
- **Failed recall** schedules immediate re-review (you'll see it again soon)
- **Passive mode success cannot delay review** if you failed recall — you must prove mastery through recall

This ensures efficient learning: you spend more time on words you struggle with and less time on words you know well.

#### Due-load smoothing and overdue spread

To keep daily review load sustainable and avoid spikes:

- **Smoothing** (`WORD_APP_SMOOTH_DUE_LOAD=true`, default): When scheduling the next review, the app caps how many words are due on the same calendar day. No single day gets more than `WORD_APP_MAX_DUE_PER_DAY` (default 350) newly scheduled.
- **Overdue spread**: If you miss days and overdue count exceeds `WORD_APP_SPREAD_OVERDUE_WHEN_ABOVE` (default 400), the app automatically reschedules those overdue reviews across the next 7 days (at most `MAX_DUE_PER_DAY` per day). This runs when you open the dashboard or start a session, so a large backlog doesn’t degrade learning.
- **Optional rebalance script**: For a one-time or periodic rebalance of the next N days (e.g. after an import), run from project root:
  ```bash
  python scripts/rebalance_due_dates.py --days 5        # apply
  python scripts/rebalance_due_dates.py --days 5 --dry-run   # preview only
  ```
  With smoothing enabled, you typically don’t need to run this regularly.

### Session Selection Algorithm

Sessions contain 50 words per session by default, configurable via the `WORD_APP_SESSION_SIZE` environment variable, selected with **due-first prioritization** and **weighted random sampling**:

**Priority 1: Due Items** (up to 70% of session)
- Words where scheduled review time has passed
- These are guaranteed to be included

**Priority 2: Bucket-based Selection** (remaining slots)

| Bucket | Target % | Description |
|--------|----------|-------------|
| New | 30% | Never-seen words |
| Weak/Failed | 40% | Low confidence or recall failures |
| Review | 30% | Known words needing reinforcement |

**Weight formula:**
```
weight = base_priority × failure_multiplier × time_decay_boost × recall_penalty
```

- **Failure multiplier (2x)**: Words with more failures than successes
- **Time decay boost**: Words not seen recently
- **Recall penalty (1.5x)**: Words with `last_recall_result == failed`

This ensures failed words appear more frequently while maintaining variety and respecting review schedules.

### Duplicate Prevention

Vocabulary duplicates are prevented at multiple levels:
- **Upload review**: Duplicates detected and marked during PDF parsing
- **Database constraint**: UNIQUE on (normalized_text, normalized_translation)
- **Normalization**: Lowercase, strip whitespace, Unicode NFC

Same word with different translations (multiple meanings) is allowed.

## Offline-First Principles

Word App is designed to run entirely locally:

- **No cloud dependency** — All data stored in local SQLite database
- **Local AI** — Ollama runs on your machine; no data leaves your computer
- **No account required** — Start learning immediately
- **Portable** — Copy the `data/` folder to move your progress
- **Full export** — You can export 100% of your data at any time
- **Human-readable** — Export files are standard JSON
- **Offline capable** — Works without internet connection

## CORS (local-first, configurable)

By default, CORS is restricted to explicit local origins (browser-correct when `allow_credentials=true`). You can override allowed origins via `WORD_APP_CORS_ALLOW_ORIGINS` (comma-separated) if you need to access the API/UI from a different origin.

## AI Safety

- **Study Mode validation** — AI responses are checked against allowed vocabulary
- **No data collection** — When using Ollama, all processing is local
- **Optional cloud AI** — OpenAI integration is disabled by default

## Database Migrations

Word App uses Alembic for database schema version control. This ensures your vocabulary, progress, and audio assets are preserved across app updates.

### Quick Start

For new installations:
```bash
alembic upgrade head
```

For existing databases:
```bash
alembic upgrade head  # Safe to run - won't recreate existing tables
```

### Migration Management

- **Create a migration**: `alembic revision --autogenerate -m "description"`
- **Apply migrations**: `alembic upgrade head`
- **Check status**: `alembic current`
- **View history**: `alembic history`

See [docs/DB_MIGRATIONS.md](docs/DB_MIGRATIONS.md) for detailed documentation.

**Important**: Schema is managed by Alembic migrations only. Never use `Base.metadata.create_all()` in production.

## Running migrations

After pulling changes that include a new Alembic migration, run both steps in order:

```bash
alembic upgrade head
python -m app.tools.verify_db --rebuild-checksum
```

The app uses a checksum to detect unexpected changes to the database file. `alembic upgrade head` modifies the file outside the app's write path, which trips the checksum. `--rebuild-checksum` updates the stored hash to match the new schema. Skipping the second step will cause startup to abort with "Database checksum mismatch."

## Testing

Run the test suite:

```bash
# Install pytest if needed
pip install pytest

# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_pdf_parser.py -v
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api` | GET | Application info |
| `/health` | GET | Health check |
| `/api/pdfs/upload` | POST | Upload vocabulary PDF |
| `/api/units` | GET | List learning units (paginated) |
| `/api/units/{id}` | GET | Get single learning unit |
| `/api/sessions/create` | POST | Create study session |
| `/api/sessions/{id}` | GET | Get session details |
| `/api/sessions/{id}/answer` | POST | Submit answer for a unit |
| `/api/sessions/history` | GET | List session history (paginated, read-only) |
| `/api/ai/status` | GET | Check AI availability |
| `/api/ai/study/respond` | POST | Study Mode AI response |
| `/api/ai/chat/respond` | POST | Free Chat AI response |
| `/api/audio/status` | GET | Check audio pronunciation availability |
| `/api/audio/voices` | GET | Get available Murf voices |
| `/api/audio/{unit_id}` | GET | Get audio pronunciation for a unit |
| `/api/audio/cleanup` | POST | Clean up orphaned audio files (dev-only) |
| `/api/export` | GET | Export all data |
| `/api/import` | POST | Import data (requires `confirm=true`) |
| `/api/import/validate` | POST | Validate import file without changes |

## Version

**v1.9-learning-core**

This release includes:
- Recall-first learning model with partial credit
- SRS-lite smart review scheduling
- Session history with detailed view
- Export/import for data portability
- Recall UX polish

## Roadmap

Planned improvements:

- [x] Spaced repetition algorithm (SRS-Lite) ✓
- [x] Session history with detailed view ✓
- [x] Export/import learning progress ✓
- [x] Audio pronunciation support (Murf + ElevenLabs) ✓
- [ ] Multiple vocabulary lists management
- [ ] Dark mode theme
- [ ] Mobile-responsive improvements

## Author

Built by **Volodymyr Zub**  
Focused on local-first, controlled AI-assisted learning.

## License

MIT

## Database Safety and Recovery

This project includes several tools to protect the SQLite database from schema drift, corruption, or incomplete migrations.

### Verify database health

Before performing risky database operations, run:

```bash
python -m app.tools.verify_db
```

Example output:

```text
Database verification
---------------------
Path: /path/to/data/vocabulary.db
Integrity: OK
Checksum: OK
Revision: 71c88886a5ac
Tables: alembic_version, learning_units, learning_sessions, learning_progress

Status: VALID
```

If the status is VALID, the database is safe.

⸻

If verification reports errors

Typical problems and recommended actions:

Schema drift

Status: SCHEMA_DRIFT

This means the database revision exists but required tables are missing.

Action:

Restore the database from a healthy backup.

Do not attempt to fix this with Alembic commands.

⸻

Checksum mismatch

Checksum: MISMATCH

The database file was modified unexpectedly.

Action:

Inspect the database or restore from backup.

⸻

Integrity failure

Integrity: FAILED

SQLite integrity check failed.

Action:

Restore the database from backup.

⸻

Safe repair tool

A helper tool is provided:

```bash
python -m app.tools.repair_db
```

Behavior:

| State | Action |
| --- | --- |
| VALID | No action needed |
| FRESH | Runs alembic upgrade head |
| PARTIAL | Refuses repair |
| SCHEMA_DRIFT | Refuses repair |
| INTEGRITY_ERROR | Refuses repair |

The tool intentionally refuses to perform unsafe automatic repairs.

⸻

Important safety rule

Never run:

```bash
alembic stamp head
```

on a real database unless you have independently verified the schema already exists.

Normal schema upgrades must always use:

```bash
alembic upgrade head
```


⸻

Migration safety test

The test suite includes a migration smoke test that verifies a fresh database can be built from migrations.

Run:

```bash
pytest
```

This prevents broken migrations from being introduced.

⸻
