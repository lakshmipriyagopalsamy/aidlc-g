"""Canonical serialization and content digests.

Digests are taken over a schema-versioned envelope, so a future change to the
serialization rules cannot silently collide with digests made under v1.
Floats are rejected: their textual form is not stable across runtimes, so
measured values must be carried as strings or integers.
"""

import hashlib
import json
from typing import Any

CANONICAL_SCHEMA = "aidlc-g/canonical/v1"


class CanonicalizationError(ValueError):
    pass


def _check(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        raise CanonicalizationError(f"{path}: floats are not canonical; use a string or integer")
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _check(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"{path}: object keys must be strings")
            _check(item, f"{path}.{key}")
        return
    raise CanonicalizationError(f"{path}: unsupported type {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    _check(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value: Any) -> str:
    envelope = {"schema": CANONICAL_SCHEMA, "body": value}
    return "sha256:" + hashlib.sha256(canonical_bytes(envelope)).hexdigest()


def file_digest(data: bytes) -> str:
    """Digest of raw file content (e.g. a protected UAT test file)."""
    return "sha256:" + hashlib.sha256(data).hexdigest()
