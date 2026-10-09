"""Who is calling: customers and bank staff, from signed tokens.

Production: tokens come from the bank's identity providers (for example its
mobile-banking login for customers, Microsoft Entra ID for staff), and Wakeel
verifies them against the provider's published keys (JWKS): signature,
issuer, audience and expiry. The customer id is the token's subject, never a
request field, so no request can act on another customer's account. Staff
roles come from a claim (reviewer, supervisor).

Demo: with WAKEEL_DEMO=1 (the default outside production), Wakeel signs its
own short-lived tokens for the demo customers and staff, marked "demo".
Production mode refuses to start with demo tokens enabled (see config.py).
"""
from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass

import jwt

STAFF_ROLES = ("reviewer", "supervisor")


class AuthError(Exception):
    def __init__(self, msg: str, status: int = 401):
        super().__init__(msg)
        self.status = status


@dataclass
class Principal:
    kind: str            # customer or staff
    subject: str         # the customer id, or the staff member's id
    name: str
    roles: tuple[str, ...] = ()
    demo: bool = False


class Verifier:
    """Verifies RS256/ES256 tokens against a JWKS (URL, or a key set for tests)."""

    def __init__(self, issuer: str, audience: str, jwks_url: str | None = None, jwks: dict | None = None):
        self.issuer, self.audience = issuer, audience
        self.client = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600) if jwks_url else None
        self.keys = jwt.PyJWKSet.from_dict(jwks) if jwks else None

    def decode(self, token: str) -> dict:
        try:
            if self.client:
                key = self.client.get_signing_key_from_jwt(token).key
            else:
                kid = jwt.get_unverified_header(token).get("kid")
                key = next(k.key for k in self.keys.keys if k.key_id == kid)
            return jwt.decode(token, key, algorithms=["RS256", "ES256"], issuer=self.issuer, audience=self.audience,
                              options={"require": ["exp", "iss", "aud", "sub"]}, leeway=30)
        except (jwt.PyJWTError, StopIteration) as e:
            raise AuthError(f"invalid token: {type(e).__name__}") from e


class Demo:
    """Short-lived HS256 tokens signed with a per-process secret (demo only)."""

    ISSUER = "wakeel-demo"

    def __init__(self, secret: str | None = None):
        self.secret = secret or secrets.token_urlsafe(32)

    def issue(self, kind: str, subject: str, name: str = "", roles: tuple[str, ...] = (), ttl: int = 3600) -> str:
        now = int(time.time())
        return jwt.encode({"iss": self.ISSUER, "aud": f"wakeel-{kind}", "sub": subject, "name": name or subject,
                           "roles": list(roles), "iat": now, "exp": now + ttl, "demo": True}, self.secret, algorithm="HS256")

    def decode(self, token: str, kind: str) -> dict:
        try:
            return jwt.decode(token, self.secret, algorithms=["HS256"], issuer=self.ISSUER, audience=f"wakeel-{kind}",
                              options={"require": ["exp", "sub"]})
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid token: {type(e).__name__}") from e


class Auth:
    def __init__(self, customer: Verifier | None, staff: Verifier | None, demo: Demo | None, roles_claim: str = "roles"):
        self.customer_verifier, self.staff_verifier, self.demo, self.roles_claim = customer, staff, demo, roles_claim

    @classmethod
    def from_env(cls) -> "Auth":
        def verifier(prefix):
            if not os.getenv(f"{prefix}_ISSUER"):
                return None
            return Verifier(os.environ[f"{prefix}_ISSUER"], os.environ[f"{prefix}_AUDIENCE"], jwks_url=os.environ[f"{prefix}_JWKS_URL"])
        demo = Demo() if os.getenv("WAKEEL_DEMO", "1" if os.getenv("WAKEEL_ENV") != "production" else "0") == "1" else None
        return cls(verifier("CUSTOMER"), verifier("STAFF"), demo, os.getenv("STAFF_ROLES_CLAIM", "roles"))

    def _claims(self, token: str, kind: str) -> tuple[dict, bool]:
        if self.demo is not None:
            try:
                return self.demo.decode(token, kind), True
            except AuthError:
                pass
        v = self.customer_verifier if kind == "customer" else self.staff_verifier
        if v is None:
            raise AuthError(f"{kind} sign-in is not configured")
        return v.decode(token), False

    def customer(self, authorization: str | None) -> Principal:
        claims, demo = self._claims(_bearer(authorization), "customer")
        return Principal("customer", str(claims["sub"]), str(claims.get("name", claims["sub"])), demo=demo)

    def staff(self, authorization: str | None, *need: str) -> Principal:
        claims, demo = self._claims(_bearer(authorization), "staff")
        roles = tuple(r for r in (claims.get(self.roles_claim) or []) if r in STAFF_ROLES)
        p = Principal("staff", str(claims["sub"]), str(claims.get("name", claims["sub"])), roles, demo)
        if need and not set(need) & set(roles):
            raise AuthError(f"needs the {' or '.join(need)} role", 403)
        return p


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError("sign in first: send Authorization: Bearer <token>")
    return authorization[7:].strip()
