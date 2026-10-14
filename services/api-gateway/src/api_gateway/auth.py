"""Bearer-token authentication against Keycloak (OIDC), with realm roles viewer / reviewer / approver / admin."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import jwt
from adce_core.settings import Settings, get_settings
from fastapi import Depends, HTTPException, Request, status

ROLE_ORDER = ["viewer", "reviewer", "approver", "admin"]


@dataclass
class Principal:
    subject: str
    roles: set[str] = field(default_factory=set)

    def has(self, role: str) -> bool:
        if "admin" in self.roles:
            return True
        return role in self.roles or any(ROLE_ORDER.index(r) >= ROLE_ORDER.index(role)
                                         for r in self.roles if r in ROLE_ORDER)


@lru_cache
def _jwks(issuer: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"{issuer}/protocol/openid-connect/certs", cache_keys=True)


def decode_token(token: str, s: Settings) -> Principal:
    key = _jwks(s.keycloak_issuer).get_signing_key_from_jwt(token).key
    claims = jwt.decode(token, key, algorithms=["RS256"], issuer=s.keycloak_issuer, audience=s.keycloak_audience,
                        options={"verify_aud": s.keycloak_audience is not None})
    roles = set(claims.get("realm_access", {}).get("roles", []))
    return Principal(subject=claims.get("preferred_username") or claims["sub"], roles=roles)


def current_principal(request: Request, s: Settings = Depends(get_settings)) -> Principal:
    if s.auth_mode == "off":
        return Principal(subject="local-dev", roles={"admin"})
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        return decode_token(header.split(" ", 1)[1], s)
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {e}") from e


def require(role: str):
    def check(p: Principal = Depends(current_principal)) -> Principal:
        if not p.has(role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"role {role} required")
        return p

    return check
