"""
Word App - Local-First Vocabulary Learning Application

Entry point for running the application.
Run with: python main.py
"""

import os

import uvicorn

# Load .env if available (for WORD_APP_HOST / WORD_APP_PORT)
try:
    from pathlib import Path

    from dotenv import load_dotenv

    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass  # python-dotenv not installed, skip silently

if __name__ == "__main__":
    reload_enabled = os.environ.get("WORD_APP_RELOAD", "false").lower() == "true"
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("WORD_APP_HOST", "127.0.0.1"),
        port=int(os.environ.get("WORD_APP_PORT", "8000")),
        reload=reload_enabled,
    )
