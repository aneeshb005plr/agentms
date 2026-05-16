# app/domains/users/schemas.py

from datetime import datetime
from pydantic import BaseModel


class UserCreate(BaseModel):
    user_id: str
    email:   str
    name:    str
    roles:   list[str] = []


class UserUpdate(BaseModel):
    """Fields user can update — department and preferences not in JWT."""
    name:        str | None = None
    department:  str | None = None
    preferences: dict | None = None


class UserResponse(BaseModel):
    user_id:     str
    email:       str
    name:        str
    roles:       list[str]
    department:  str | None
    preferences: dict | None
    first_seen:  datetime
    last_seen:   datetime
    is_active:   bool