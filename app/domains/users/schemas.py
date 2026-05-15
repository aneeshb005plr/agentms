# app/domains/users/schemas.py
# Pydantic models for the users domain.

from datetime import datetime
from pydantic import BaseModel


class UserCreate(BaseModel):
    """Data needed to create a user on first login — sourced from JWT claims."""
    user_id:  str
    email:    str
    name:     str
    roles:    list[str] = []


class UserUpdate(BaseModel):
    """
    Fields a user can update themselves via profile page (Phase 2).
    Department and preferences are NOT in JWT — filled in by user.
    All fields optional — PATCH semantics.
    """
    name:        str | None = None
    department:  str | None = None
    preferences: dict | None = None


class UserResponse(BaseModel):
    """User document returned from MongoDB — safe to return to client."""
    user_id:     str
    email:       str
    name:        str
    roles:       list[str]
    department:  str | None
    preferences: dict | None
    first_seen:  datetime
    last_seen:   datetime
    is_active:   bool