"""Public JSON Web Keys (RFC 7517) of EC P-256 keys and their RFC 7638 thumbprints (the `kid`).

The auth-service publishes these at /.well-known/jwks.json; the test helpers build the same shape.
"""
import base64
import hashlib
import json

from cryptography.hazmat.primitives.asymmetric import ec

from agent_common.constants import JWT_ALGORITHM

_P256_COORDINATE_BYTES = 32


def _base64url(raw: bytes) -> str:
    """Base64url without padding, as JOSE requires."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def jwk_thumbprint(jwk: dict) -> str:
    """RFC 7638 thumbprint of an EC public key: SHA-256 over the required members (crv, kty, x, y) in
    lexicographic order, no whitespace, base64url. Same key -> same kid on every replica and every restart."""
    required = {name: jwk[name] for name in ("crv", "kty", "x", "y")}
    canonical = json.dumps(required, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _base64url(hashlib.sha256(canonical).digest())


def public_jwk(public_key: ec.EllipticCurvePublicKey) -> dict:
    """Public JWK of a P-256 key with kid (thumbprint), alg ES256 and use sig. Raises ValueError for other curves."""
    if not isinstance(public_key.curve, ec.SECP256R1):
        raise ValueError("Only EC P-256 keys are supported (ES256).")
    numbers = public_key.public_numbers()
    jwk = {"kty": "EC", "crv": "P-256",
           "x": _base64url(numbers.x.to_bytes(_P256_COORDINATE_BYTES, "big")),
           "y": _base64url(numbers.y.to_bytes(_P256_COORDINATE_BYTES, "big"))}
    return {**jwk, "kid": jwk_thumbprint(jwk), "alg": JWT_ALGORITHM, "use": "sig"}
