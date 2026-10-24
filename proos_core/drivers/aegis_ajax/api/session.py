"""gRPC session management for Ajax Systems API."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from custom_components.aegis_ajax.const import (
    APPLICATION_LABEL,
    CLIENT_DEVICE_MODEL,
    CLIENT_DEVICE_TYPE,
    CLIENT_OS,
    CLIENT_VERSION,
    UserRole,
)


def log_fingerprint(value: object) -> str:
    """Stable one-way fingerprint of a session identifier, safe to put in a log.

    `session_token` and `device_id` are both in `diagnostics.TO_REDACT`, so
    neither belongs in a log line either — and debug logs travel *further* than
    a diagnostics dump does, because people paste them into public issues while
    a dump is a file someone downloads. Stripping a value from one and printing
    it in the other would be disagreeing with ourselves.

    What a log actually needs from these is only "is this the same one as
    before?" — which a digest answers without carrying the value. Truncated
    because a log needs to be readable and 48 bits is far more than enough to
    tell two ids apart by eye.

    Same contract as `_fcm_creds_hash`: a change-detection fingerprint compared
    only against itself, never used to authenticate anything, so a fast hash is
    correct and a KDF would be pointless. `usedforsecurity=False` documents that
    intent and keeps it FIPS-safe.

    Accepts `object` and coerces, deliberately. Every call sits inside a
    `_LOGGER.debug(...)` argument list, which Python evaluates eagerly — so a
    `TypeError` in here would not spoil a log line, it would take down the login
    path that was being logged. A diagnostic must not be able to break the thing
    it observes, and no caller should have to defend a log statement.
    """
    if not value:
        return "none"
    return hashlib.sha256(str(value).encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


class AuthenticationError(Exception):
    """Raised when authentication fails."""


class TwoFactorRequiredError(Exception):
    """Raised when 2FA is needed."""

    def __init__(self, request_id: str) -> None:
        super().__init__("Two-factor authentication required")
        self.request_id = request_id


class AjaxSession:
    """Manages authentication and session state for the Ajax gRPC API."""

    def __init__(self, device_id: str | None = None, app_label: str = APPLICATION_LABEL) -> None:
        self._session_token: str | None = None
        self._user_hex_id: str | None = None
        self._device_id: str = device_id or str(uuid.uuid4())
        self._app_label: str = app_label
        self._email: str | None = None
        self._password_hash: str | None = None

    @property
    def is_authenticated(self) -> bool:
        return self._session_token is not None and self._user_hex_id is not None

    @property
    def session_token(self) -> str | None:
        return self._session_token

    @property
    def user_hex_id(self) -> str | None:
        return self._user_hex_id

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def app_label(self) -> str:
        return self._app_label

    @staticmethod
    def hash_password(password: str) -> str:
        """Return the SHA-256 hex digest of the given password.

        The Ajax API requires ``password_sha256_hash`` in the login request —
        this is a protocol constraint, not a design choice.  We cannot use a
        stronger KDF here because the server expects this exact format.
        """
        return hashlib.sha256(password.encode("utf-8")).hexdigest()  # noqa: S324

    def set_credentials(self, email: str, password: str) -> None:
        self._email = email
        self._password_hash = self._hash_password(password)

    def set_credentials_hashed(self, email: str, password_hash: str) -> None:
        """Set credentials using a pre-hashed password (SHA-256 hex digest)."""
        self._email = email
        self._password_hash = password_hash

    def set_session(self, session_token: str, user_hex_id: str) -> None:
        self._session_token = session_token
        self._user_hex_id = user_hex_id

    def clear_session(self) -> None:
        self._session_token = None
        self._user_hex_id = None

    def _hash_password(self, password: str) -> str:
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

    @staticmethod
    def _token_to_hex(token_bytes: bytes) -> str:
        return token_bytes.hex()

    @staticmethod
    def _token_from_hex(token_hex: str) -> bytes:
        return bytes.fromhex(token_hex)

    def get_session_metadata(self) -> list[tuple[str, str]]:
        if not self._session_token or not self._user_hex_id:
            return []
        result = [
            ("client-session-token", self._session_token),
            ("a911-user-id", self._user_hex_id),
        ]
        if self._email:
            result.append(("client-user-login", self._email))
        result.append(("client-user-role", "USER"))
        return result

    def get_device_info_metadata(self) -> list[tuple[str, str]]:
        return [
            ("client-device-id", self._device_id),
            ("client-device-model", CLIENT_DEVICE_MODEL),
            ("client-os", CLIENT_OS),
            ("client-version-major", CLIENT_VERSION),
            ("application-label", self._app_label),
            ("client-device-type", CLIENT_DEVICE_TYPE),
        ]

    def get_call_metadata(self) -> list[tuple[str, str]]:
        return self.get_session_metadata() + self.get_device_info_metadata()

    def get_login_params(self) -> dict[str, Any]:
        if not self._email or not self._password_hash:
            raise AuthenticationError("Credentials not set. Call set_credentials() first.")
        return {
            "email": self._email,
            "password_sha256_hash": self._password_hash,
            "user_role": UserRole.USER,
        }
