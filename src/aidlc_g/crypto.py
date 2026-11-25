"""Ed25519 signing for machine (CI runner) attestations.

Human approvals are identity-backed records made through the authenticated
service; they are deliberately not modelled as runner-style signatures.
"""

import base64
import binascii
from typing import Any, Dict

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .canonical import canonical_bytes

ATTESTATION_DOMAIN = "aidlc-g/ci-attestation/v1"
QUALITY_DOMAIN = "aidlc-g/quality-attestation/v1"
PROVENANCE_DOMAIN = "aidlc-g/build-provenance/v1"


def _message(payload: Dict[str, Any], domain: str) -> bytes:
    # Domain separation: a signature over one evidence type can't be replayed as another.
    return canonical_bytes({"domain": domain, "payload": payload})


def generate_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw).decode("ascii")


def sign_payload(private_key: Ed25519PrivateKey, payload: Dict[str, Any], domain: str = ATTESTATION_DOMAIN) -> str:
    return base64.b64encode(private_key.sign(_message(payload, domain))).decode("ascii")


def verify_payload(public_key: str, payload: Dict[str, Any], signature: str, domain: str = ATTESTATION_DOMAIN) -> bool:
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        key.verify(base64.b64decode(signature, validate=True), _message(payload, domain))
        return True
    except (InvalidSignature, ValueError, binascii.Error, TypeError):
        return False


def validate_public_key(public_key: str) -> None:
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
