# app/domains/auth/schemas.py

from pydantic import BaseModel


class UserClaims(BaseModel):
    """
    Decoded JWT claims — injected into every protected endpoint.

    Claim mapping for XYZ Entra ID token:
        user_id    ← uid   (XYZ internal ID e.g. abahuleyan001)
        email      ← email
        name       ← name  (e.g. "Aneesh Bahuleyan (US)")
        given_name ← given_name
        family_name← family_name
        oid        ← oid   (Entra object ID — kept for reference)
        sid        ← sid   (session ID — useful for logout tracking)
        tenant_id  ← tid
        roles      ← roles (not in current token — defaults to [])
    """
    user_id:     str
    email:       str
    name:        str
    given_name:  str        = ""
    family_name: str        = ""
    oid:         str        = ""   # Entra object ID — kept for reference
    sid:         str        = ""   # session ID
    tenant_id:   str        = ""
    roles:       list[str]  = []   # not in current token — future use


class TokenDebugResponse(BaseModel):
    """Dev only — shows raw JWT claims to help configure claim field names."""
    all_claims: dict