from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core import security


def test_access_token_round_trip_uses_fixed_hs256(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security.settings, "semantic_env", "test")
    monkeypatch.setattr(security.settings, "jwt_algorithm", "HS256")
    monkeypatch.setattr(security.settings, "jwt_secret_key", "test-secret-that-is-at-least-32-bytes")

    token = security.create_access_token("user-1", expires_delta_minutes=5)

    assert security.decode_access_token(token)["sub"] == "user-1"
    assert jwt.get_unverified_header(token)["alg"] == "HS256"


def test_access_token_rejects_algorithm_configuration_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security.settings, "jwt_algorithm", "RS256")

    with pytest.raises(RuntimeError, match="fixed HS256"):
        security.create_access_token("user-1")


def test_production_access_token_requires_256_bit_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security.settings, "semantic_env", "production")
    monkeypatch.setattr(security.settings, "jwt_algorithm", "HS256")
    monkeypatch.setattr(security.settings, "jwt_secret_key", "too-short")

    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        security.create_access_token("user-1")


def test_access_token_rejects_unknown_critical_header(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "a" * 32
    monkeypatch.setattr(security.settings, "semantic_env", "production")
    monkeypatch.setattr(security.settings, "jwt_algorithm", "HS256")
    monkeypatch.setattr(security.settings, "jwt_secret_key", secret)
    token = jwt.encode(
        {"sub": "user-1"},
        secret,
        algorithm="HS256",
        headers={"crit": ["x-loci-policy"], "x-loci-policy": "require-review"},
    )

    with pytest.raises(ValueError, match="Invalid access token"):
        security.decode_access_token(token)


def test_access_token_rejects_expired_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "synthetic-expiry-check-secret-at-least-32-bytes"
    monkeypatch.setattr(security.settings, "jwt_algorithm", "HS256")
    monkeypatch.setattr(security.settings, "jwt_secret_key", secret)
    token = jwt.encode({"sub": "user-1", "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
                       secret, algorithm="HS256")

    with pytest.raises(ValueError, match="Invalid access token"):
        security.decode_access_token(token)


def test_access_token_normalizes_deeply_nested_header_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security.settings, "jwt_algorithm", "HS256")
    monkeypatch.setattr(security.settings, "jwt_secret_key", "synthetic-malformed-header-secret-at-least-32-bytes")
    nested = b'{"nested":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'
    header = base64.urlsafe_b64encode(nested).rstrip(b'=').decode()

    with pytest.raises(ValueError, match="Invalid access token"):
        security.decode_access_token(f"{header}.e30.invalid-signature")
