# app/domains/auth/schemas.py
# Pydantic models for the auth domain.
# UserClaims is the primary output of token validation —
# passed to every endpoint and service that needs user identity.

from pydantic import BaseModel


class UserClaims(BaseModel):
    """
    Decoded and validated JWT claims.
    Produced by AuthService.validate_token().
    Injected into every protected endpoint via get_current_user().

    user_id  — primary identifier — used as MongoDB document key
    email    — for display and audit logs
    name     — for display in UI
    roles    — for future role-based access control (Phase 2 Admin)
    """
    user_id: str
    email:   str
    name:    str
    roles:   list[str] = []


class TokenDebugResponse(BaseModel):
    """
    Response from GET /api/v1/auth/debug/token-claims.
    Dev only — shows all raw JWT claims so team can find correct field names.
    Remove this endpoint before production.
    """
    all_claims: dict