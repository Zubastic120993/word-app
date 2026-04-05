"""API routers."""

from app.routers.upload import router as upload_router
from app.routers.sessions import router as sessions_router
from app.routers.ai import router as ai_router
from app.routers.data import router as data_router
from app.routers.ui import router as ui_router
from app.routers.audio import router as audio_router

__all__ = [
    "upload_router",
    "sessions_router",
    "ai_router",
    "data_router",
    "ui_router",
    "audio_router",
]
