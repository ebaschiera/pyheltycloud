"""Tests of the SRP primitives.

They do not replicate the protocol (the check against the real cloud does
that), but they block the classic mistakes: hex padding and the timestamp
format, which Cognito silently rejects with a generic "Bad Request".
"""

from __future__ import annotations

import re

from pyheltycloud.auth import _cognito_timestamp, _compute_hkdf, _pad_hex

TIMESTAMP_RE = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"\d{1,2} \d{2}:\d{2}:\d{2} UTC \d{4}$"
)


def test_pad_hex_makes_length_even() -> None:
    assert _pad_hex("abc") == "0abc"


def test_pad_hex_prefixes_when_high_bit_set() -> None:
    """Without the leading zero, the value would be read as negative."""
    assert _pad_hex("ff") == "00ff"
    assert _pad_hex("8a") == "008a"


def test_pad_hex_leaves_safe_values_alone() -> None:
    assert _pad_hex("7f") == "7f"


def test_pad_hex_accepts_int() -> None:
    assert _pad_hex(255) == "00ff"


def test_cognito_timestamp_format() -> None:
    """The day is not zero-padded and the names must be in English."""
    assert TIMESTAMP_RE.match(_cognito_timestamp())


def test_compute_hkdf_is_deterministic_and_16_bytes() -> None:
    key = _compute_hkdf(b"shared-secret", b"salt")
    assert len(key) == 16
    assert key == _compute_hkdf(b"shared-secret", b"salt")
    assert key != _compute_hkdf(b"shared-secret", b"other-salt")
