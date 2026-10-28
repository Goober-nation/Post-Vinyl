"""Per-user secret encryption (#85): round trip, rotation, loud failures."""

from __future__ import annotations

import pytest

from app.services.encryption import (
    DecryptError,
    SecretBox,
    SecretKeyError,
    generate_key,
)


def test_round_trip_and_ciphertext_hides_value() -> None:
    box = SecretBox(generate_key())
    token = box.encrypt("hunter2 ü")
    assert "hunter2" not in token and box.decrypt(token) == "hunter2 ü"
    assert box.encrypt("same") != box.encrypt("same")  # random IV per value


def test_old_values_survive_key_rotation() -> None:
    old_key, new_key = generate_key(), generate_key()
    stored = SecretBox(old_key).encrypt("pw")
    rotating = SecretBox(f"{new_key}, {old_key}")
    assert rotating.decrypt(stored) == "pw"
    rotated = rotating.rotate(stored)
    assert SecretBox(new_key).decrypt(rotated) == "pw"  # old key can now go


def test_wrong_key_fails_loudly() -> None:
    stored = SecretBox(generate_key()).encrypt("pw")
    with pytest.raises(DecryptError):
        SecretBox(generate_key()).decrypt(stored)


def test_tampered_value_is_rejected() -> None:
    box = SecretBox(generate_key())
    token = box.encrypt("pw")
    tampered = token[:-6] + ("A" if token[-6] != "A" else "B") + token[-5:]
    with pytest.raises(DecryptError):
        box.decrypt(tampered)


@pytest.mark.parametrize("keys", ["", " , ", "not-a-key", "c2hvcnQ="])
def test_invalid_keys_refused(keys: str) -> None:
    with pytest.raises(SecretKeyError):
        SecretBox(keys)


def test_missing_env_var_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PV_SECRET_KEY", raising=False)
    with pytest.raises(SecretKeyError, match="not set"):
        SecretBox.from_env()


def test_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    key = generate_key()
    monkeypatch.setenv("PV_SECRET_KEY", key)
    assert SecretBox(key).decrypt(SecretBox.from_env().encrypt("x")) == "x"
