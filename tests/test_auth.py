"""Production sign-in: RS256 tokens verified against the identity provider's
key set (JWKS). Forged, expired, foreign and wrong-audience tokens fail."""
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from wakeel.auth import Auth, AuthError, Verifier


def keypair(kid):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return key, {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


@pytest.fixture(scope="module")
def idp():
    key, jwk = keypair("k1")
    other, _ = keypair("k1")                     # same kid, different key: a forgery
    return key, other, {"keys": [jwk]}


def tok(key, **claims):
    now = int(time.time())
    base = {"iss": "https://login.bank.example", "aud": "wakeel", "sub": "cust-1842", "iat": now, "exp": now + 300}
    return jwt.encode({**base, **claims}, key, algorithm="RS256", headers={"kid": "k1"})


def test_production_tokens(idp):
    key, other, jwks = idp
    v = Verifier("https://login.bank.example", "wakeel", jwks=jwks)
    auth = Auth(customer=v, staff=Verifier("https://login.bank.example", "wakeel", jwks=jwks), demo=None)
    who = auth.customer("Bearer " + tok(key))
    assert who.subject == "cust-1842" and not who.demo
    staff = auth.staff("Bearer " + tok(key, sub="emp-7", roles=["reviewer", "admin"]), "reviewer")
    assert staff.roles == ("reviewer",)                          # unknown roles are ignored
    bad = {"forged": tok(other), "expired": tok(key, exp=int(time.time()) - 3600),
           "other issuer": tok(key, iss="https://evil.example"), "other audience": tok(key, aud="another-app")}
    for name, t in bad.items():
        with pytest.raises(AuthError):
            auth.customer("Bearer " + t)
    with pytest.raises(AuthError) as e:
        auth.staff("Bearer " + tok(key, roles=[]), "supervisor")
    assert e.value.status == 403
    with pytest.raises(AuthError):                                # demo tokens don't exist in production
        auth.customer("Bearer " + jwt.encode({"sub": "sara", "exp": int(time.time()) + 60}, "x", algorithm="HS256"))
