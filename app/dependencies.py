"""Shared FastAPI dependencies for cross-cutting concerns."""

import logging
from typing import Literal

from fastapi import HTTPException, Request

from app.config import settings

logger = logging.getLogger(__name__)

ClientRole = Literal["mac", "ipad"]


def get_client_role(request: Request) -> ClientRole:
    """
    Resolve the client role from the request (stateless).

    Resolution order:
    1. If WORD_APP_IPAD_ENABLED is False → always "mac"
    2. In debug mode: check ``?role=ipad`` query parameter
    3. Default: "mac"

    The mechanism is fully stateless — no server-side session storage,
    no token persistence on the server.

    Future: in non-debug mode, resolve from a cookie or HTTP header
    (query-param detection is intentionally disabled outside debug mode).
    """
    if not settings.ipad_enabled:
        return "mac"

    # Debug mode only: allow ?role=ipad query parameter for testing
    if settings.debug:
        role_param = request.query_params.get("role")
        if role_param == "ipad":
            return "ipad"

    return "mac"


def require_mac_role(request: Request) -> None:
    """
    Dependency that enforces mac-only access.

    Raises 403 Forbidden for iPad-role requests.
    Apply to admin-only routes (upload, vocabulary CRUD, data, chat).
    """
    role = get_client_role(request)
    if role == "ipad":
        raise HTTPException(
            status_code=403,
            detail="This action is not available on iPad.",
        )
