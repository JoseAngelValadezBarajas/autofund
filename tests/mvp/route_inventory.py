"""Route-inventory helpers for tests that assert what the HTTP surface can do.

These exist because route enumeration is easy to get subtly wrong, and the mistake fails in the
worst possible direction: a safety test that cannot see a route passes.

FastAPI 0.141 wraps every `include_router()` call in an `_IncludedRouter` object rather than
splicing the child routes into `app.routes`. Such an object exposes `methods` as an empty set and
has no `path` at all, so a check like *"no route under this prefix accepts POST"* iterating only
`app.routes` silently stops examining every included route — and a write endpoint added through an
included router would be invisible to it. Walking into the child router is the difference between
an assertion and a decoration.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def iter_routes(routes: Any) -> Iterator[tuple[str, frozenset[str]]]:
    """Yield `(path, methods)` for every real route, descending into included routers.

    Routes with no method set are skipped: a mount is not an endpoint.
    """
    for route in routes:
        methods = frozenset(getattr(route, "methods", None) or ())
        path = getattr(route, "path", None)
        if methods and path:
            yield str(path), methods
            continue
        # An included router keeps its real routes on a nested object rather than in this list.
        inner = getattr(route, "router", None) or getattr(route, "original_router", None)
        if inner is not None:
            yield from iter_routes(inner.routes)


def route_methods(app: Any) -> dict[str, frozenset[str]]:
    """Map every reachable path in an app to the methods it accepts."""
    inventory: dict[str, frozenset[str]] = {}
    for path, methods in iter_routes(app.routes):
        inventory[path] = inventory.get(path, frozenset()) | methods
    return inventory


def mutating_routes(app: Any) -> set[tuple[str, str]]:
    """Every `(path, method)` pair that could change state."""
    return {(path, method) for path, methods in route_methods(app).items()
            for method in methods if method in MUTATING_METHODS}
