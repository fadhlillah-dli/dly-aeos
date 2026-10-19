"""PII minimisation never rewrites identity values (audit §10/§13/§14: random acceptance REJECTED).

A digit run inside an alphanumeric token (hex commit SHA, sha256 digest, id) is not a card number or
NIK; evidence identities must be stored bit-exact because acceptance compares them literally."""

from __future__ import annotations

import hashlib
import random

from aeos.core.pii import redact_pii
from aeos.core.redaction import redact
from aeos.verification.collector import is_identity_value, redact_evidence

LUHN_SHA = "5871949640790efd065711ad0d0fdd78b3b4ca44"


def test_luhn_digit_runs_inside_hex_tokens_are_not_pii():
    assert redact_pii(LUHN_SHA) == LUHN_SHA
    digest = "sha256:" + "4111111111111111" + "ab" * 24
    assert redact_pii(digest) == digest
    assert redact_pii("commit " + LUHN_SHA + " merged") == "commit " + LUHN_SHA + " merged"
    # real identifiers separated by non-alphanumerics are still minimised
    assert redact_pii("card 4111 1111 1111 1111.") == "card [PII:CARD]."
    assert redact_pii("kartu:4111111111111111") == "kartu:[PII:CARD]"
    assert redact_pii("NIK 3174015505900001") == "NIK [PII:NIK]"


def test_ten_thousand_random_hashes_survive_pii_redaction_unchanged():
    rng = random.Random(20261019)
    for i in range(10_000):
        digest = hashlib.sha256(f"{i}-{rng.random()}".encode()).hexdigest()
        sha1 = hashlib.sha1(f"{i}-{rng.random()}".encode()).hexdigest()
        for value in (digest, "sha256:" + digest, sha1):
            assert redact_pii(value) == value, value
            assert redact({"commit": value}, pii=True)["commit"] == value, value


def test_collector_redaction_keeps_identities_but_still_masks_secrets_and_pii():
    env = {"commit": LUHN_SHA, "artifact_hash": "sha256:" + "4111111111111111" + "0" * 48,
           "note": "owner budi" + "@example.com", "client_secret": "a" * 40,
           "nested": [{"tree": LUHN_SHA}]}
    out = redact_evidence(env)
    assert out["commit"] == LUHN_SHA and out["artifact_hash"] == env["artifact_hash"]
    assert out["nested"][0]["tree"] == LUHN_SHA
    assert out["note"] == "owner [PII:EMAIL]"
    assert out["client_secret"] == "[REDACTED]"  # the sensitive-key rule is never weakened
    assert is_identity_value(LUHN_SHA) and not is_identity_value("4111 1111 1111 1111")
