"""The single source of truth for the product version.

This lives in its own leaf module because it is needed by both the orchestrator and the telemetry
writer, and the orchestrator imports the telemetry writer — so neither can own the constant without
creating a cycle.

Having exactly one copy matters for a repository whose evidence is dated. Session telemetry stamps
the running version into every artifact it writes, and a hard-coded copy there once drifted four
releases behind, which would have stamped a fresh capture with an old version and made a new
measurement look like part of an earlier milestone. A version that appears in evidence has to be
the version that produced it.
"""

from __future__ import annotations

# The full display name, used wherever the product identifies itself to a person or in an artifact.
PRODUCT_VERSION = "AutoFund MVP 0.3.0"

# The bare semantic version, used where a machine-readable version is expected.
VERSION = "0.3.0"
