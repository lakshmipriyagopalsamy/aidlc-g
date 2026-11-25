import pytest

from aidlc_g.canonical import CanonicalizationError, canonical_bytes, digest


def test_digest_is_independent_of_key_order():
    assert digest({"a": 1, "b": [1, 2]}) == digest({"b": [1, 2], "a": 1})


def test_digest_changes_with_content():
    assert digest({"a": 1}) != digest({"a": 2})


def test_digest_format():
    d = digest({"x": "y"})
    assert d.startswith("sha256:") and len(d) == len("sha256:") + 64


def test_floats_are_rejected():
    with pytest.raises(CanonicalizationError):
        digest({"target": 0.5})


def test_non_string_keys_rejected():
    with pytest.raises(CanonicalizationError):
        canonical_bytes({1: "x"})


def test_unicode_is_preserved_not_escaped():
    assert "é".encode("utf-8") in canonical_bytes({"k": "é"})


def test_envelope_separates_digest_from_raw_sha256():
    import hashlib

    raw = "sha256:" + hashlib.sha256(canonical_bytes({"a": 1})).hexdigest()
    assert digest({"a": 1}) != raw
