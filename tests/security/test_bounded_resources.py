"""Response sizes and nonce memory are bounded, however hostile the other side or long the session."""

import gzip

import httpx
import pytest

from autofund.exchanges.bitso.auth import BitsoNonceProvider
from autofund.http_limits import ResponseTooLarge, send_limited


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_normal_response_is_returned_whole():
    with client_for(lambda r: httpx.Response(200, json={"ok": True})) as client:
        got = send_limited(client, "GET", "https://x.test/a", max_bytes=1000)
    assert got.status_code == 200 and b"ok" in got.content and got.headers["content-type"].startswith("application/json")


def test_an_oversized_body_is_refused_without_buffering_it_all():
    big = b"x" * 5000
    with client_for(lambda r: httpx.Response(200, content=big)) as client, pytest.raises(ResponseTooLarge):
        send_limited(client, "GET", "https://x.test/a", max_bytes=1000)


def test_a_declared_huge_content_length_is_refused_early():
    with client_for(lambda r: httpx.Response(200, headers={"content-length": "999999999"}, content=b"x")) as client:
        with pytest.raises(ResponseTooLarge):
            send_limited(client, "GET", "https://x.test/a", max_bytes=1000)


def test_a_decompression_bomb_is_measured_after_inflation():
    bomb = gzip.compress(b"0" * 200_000)  # tiny on the wire, large once inflated
    assert len(bomb) < 1000
    with client_for(lambda r: httpx.Response(200, headers={"content-encoding": "gzip"}, content=bomb)) as client:
        with pytest.raises(ResponseTooLarge):
            send_limited(client, "GET", "https://x.test/a", max_bytes=10_000)


def test_too_large_is_a_value_error_so_every_transport_already_maps_it():
    assert issubclass(ResponseTooLarge, ValueError)


def test_every_http_transport_in_the_codebase_uses_the_bounded_reader():
    import re
    from pathlib import Path
    root = Path(__file__).parents[2] / "src" / "autofund"
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == "http_limits.py":
            continue
        if re.search(r"httpx\.Client\(", text) and re.search(r"\b(client|session)\.(request|get|post)\(", text):
            offenders.append(str(path.relative_to(root)))
    assert offenders == []


def test_nonce_memory_is_bounded_in_a_long_session():
    BitsoNonceProvider._used.clear()
    clock = [1_700_000_000_000]
    provider = BitsoNonceProvider(clock_ms=lambda: clock[0], entropy=lambda n: 7)
    seen = set()
    for _ in range(20_000):
        clock[0] += 1000  # one request a second for ~5.5 hours
        nonce = provider.next()
        assert nonce not in seen  # never reused
        seen.add(nonce)
    assert len(BitsoNonceProvider._used) <= 4096 + 1  # pruned to the 10-minute window, never 20,000
    BitsoNonceProvider._used.clear()


def test_a_nonce_is_still_never_reused_within_the_window():
    BitsoNonceProvider._used.clear()
    provider = BitsoNonceProvider(clock_ms=lambda: 1_700_000_000_000, entropy=lambda n: 1)
    provider.next()
    from autofund.exchanges.bitso.errors import BitsoAuthenticationError
    with pytest.raises(BitsoAuthenticationError):
        for _ in range(40):  # same millisecond, same salt: a collision the provider must refuse
            provider.next()
    BitsoNonceProvider._used.clear()
