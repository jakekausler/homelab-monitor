"""Pydantic models for auth domain entities."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class User(BaseModel):
    """User record exposed to API and CLI. NEVER includes bcrypt_hash."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: int
    username: str
    created_at: str  # ISO-8601 UTC


class Session(BaseModel):
    """Server-side session row. Cookie value is derived from `id` via sessions.py."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    user_id: int
    expires_at: str
    created_ip: str
    csrf_token: str


class ApiToken(BaseModel):
    """API token metadata. The plaintext is NEVER stored; only the SHA-256 hash."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    name: str
    scopes: str  # comma-separated; parse via scopes.parse_scopes
    created_at: str
    last_used_at: str | None
    rotated_at: str | None
