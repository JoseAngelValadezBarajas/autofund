"""Bounded HTTP reads.

``httpx`` reads an entire response into memory by default. Every exchange we talk to is trusted, but
"trusted" is not "can never misbehave": a misconfigured proxy, a compromised CDN, an error page that
turns into a multi-gigabyte stream, or a compressed body that expands enormously would otherwise be
loaded whole into a process that may be holding open orders. :func:`send_limited` streams the body,
counts the bytes *after* decompression, and gives up at a ceiling.

``ResponseTooLarge`` subclasses ``ValueError`` on purpose: every transport already maps
``(httpx.HTTPError, OSError, ValueError)`` to its own sanitized "transport failed" error, so an
oversized response is handled exactly like any other failed read.
"""

from dataclasses import dataclass

import httpx

DEFAULT_MAX_BYTES = 4 * 1024 * 1024


class ResponseTooLarge(ValueError):
    pass


@dataclass(frozen=True)
class LimitedResponse:
    status_code: int
    headers: httpx.Headers
    content: bytes


def send_limited(
    client: httpx.Client, method: str, url: str, *, max_bytes: int = DEFAULT_MAX_BYTES, **kwargs: object
) -> LimitedResponse:
    with client.stream(method, url, **kwargs) as response:  # type: ignore[arg-type]
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise ResponseTooLarge("declared response size exceeds the limit")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ResponseTooLarge("response body exceeds the limit")
            chunks.append(chunk)
        return LimitedResponse(response.status_code, response.headers, b"".join(chunks))
