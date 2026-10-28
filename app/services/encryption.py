"""Encryption for per-user secrets: Navidrome passwords, ListenBrainz tokens (#85).

``PV_SECRET_KEY`` holds one or more Fernet keys, comma-separated, newest
first. New values are encrypted with the first key; any listed key decrypts,
so a key can be rotated by putting a new one in front and re-encrypting
(``rotate``) before dropping the old one. Losing every key loses every
stored secret: back the key up outside the database.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

ENV_VAR = "PV_SECRET_KEY"


class SecretKeyError(Exception):
    """PV_SECRET_KEY is missing or not a valid Fernet key."""


class DecryptError(Exception):
    """A stored value can't be decrypted: wrong key, or the value was altered."""


def generate_key() -> str:
    """A new key for PV_SECRET_KEY (the setup wizard offers it)."""
    return Fernet.generate_key().decode()


class SecretBox:
    def __init__(self, keys: str) -> None:
        parts = [k.strip() for k in keys.split(",") if k.strip()]
        if not parts:
            raise SecretKeyError(f"{ENV_VAR} is empty")
        try:
            self._fernet = MultiFernet([Fernet(k) for k in parts])
        except ValueError as e:
            raise SecretKeyError(f"{ENV_VAR} holds an invalid key: {e}") from e

    @classmethod
    def from_env(cls) -> SecretBox:
        keys = os.environ.get(ENV_VAR)
        if keys is None:
            raise SecretKeyError(
                f"{ENV_VAR} is not set; generate one and back it up outside the database"
            )
        return cls(keys)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise DecryptError("stored secret can't be decrypted") from e

    def rotate(self, token: str) -> str:
        """Re-encrypt a stored value under the first (newest) key."""
        try:
            return self._fernet.rotate(token.encode()).decode()
        except InvalidToken as e:
            raise DecryptError("stored secret can't be decrypted") from e
