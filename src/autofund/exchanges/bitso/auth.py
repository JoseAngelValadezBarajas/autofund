import hashlib
import hmac
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar

from .errors import BitsoAuthenticationError


@dataclass(frozen=True, slots=True, repr=False)
class BitsoCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not isinstance(self.api_secret, str):
            raise BitsoAuthenticationError("invalid Stage credential types")
        if (
            not self.api_key
            or not self.api_secret
            or any(c.isspace() for c in self.api_key)
            or ":" in self.api_key
            or not self.api_key.isascii()
        ):
            raise BitsoAuthenticationError("invalid Stage credential format")

    def __repr__(self) -> str:
        return 'BitsoCredentials(api_key="***", api_secret="***")'

    @classmethod
    def from_environment(cls) -> "BitsoCredentials":
        key = os.getenv("AUTOFUND_BITSO_STAGE_API_KEY", "")
        secret = os.getenv("AUTOFUND_BITSO_STAGE_API_SECRET", "")
        if not key or not secret:
            raise BitsoAuthenticationError("Stage credentials missing")
        return cls(key, secret)


class BitsoNonceProvider:
    """Nonce v2: 13 ms digits + six cryptographic salt digits.

    Shared process registry prevents reuse across providers and threads. No
    cross-process coordination is claimed; independent processes have entropy.
    """

    _lock = threading.Lock()
    _used: ClassVar[set[str]] = set()

    def __init__(
        self,
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        entropy: Callable[[int], int] = secrets.randbelow,
    ) -> None:
        self._clock = clock_ms
        self._entropy = entropy

    def next(self) -> str:
        with self._lock:
            stamp = self._clock()
            if type(stamp) is not int or len(str(stamp)) != 13 or stamp < 0:
                raise BitsoAuthenticationError("invalid Nonce v2 clock")
            for _ in range(32):
                salt = self._entropy(1_000_000)
                if type(salt) is not int or not 0 <= salt < 1_000_000:
                    raise BitsoAuthenticationError("invalid nonce entropy")
                nonce = f"{stamp}{salt:06d}"
                if nonce not in self._used:
                    self._used.add(nonce)
                    return nonce
            raise BitsoAuthenticationError("nonce collision budget exhausted")


def signature(secret: str, nonce: str, method: str, path: str, body: bytes) -> str:
    message = (nonce + method + path).encode("ascii") + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


@dataclass(frozen=True, repr=False)
class SignedRequest:
    method: str
    path: str
    body: bytes
    authorization: str = field(repr=False)

    def __repr__(self) -> str:
        return "SignedRequest(authorization=***, request=redacted)"


def sign(
    credentials: BitsoCredentials, nonce: str, method: str, path: str, body: bytes
) -> SignedRequest:
    digest = signature(credentials.api_secret, nonce, method, path, body)
    return SignedRequest(
        method, path, body, f"Bitso {credentials.api_key}:{nonce}:{digest}"
    )
