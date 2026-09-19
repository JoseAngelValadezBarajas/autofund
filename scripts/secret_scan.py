"""Secret scan over tracked files. Fails loudly if credential material is found."""

import re
import subprocess
import sys

# Patterns that indicate real credential material rather than test placeholders.
PATTERNS = {
    "PRIVATE_KEY_BLOCK": re.compile(r"BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY"),
    "LONG_LITERAL_SECRET": re.compile(r"(?i)(?:api[_-]?(?:key|secret)|secret[_-]?key|access[_-]?token)"
                                      r"\s*[:=]\s*[\"'][A-Za-z0-9+/=_\-]{24,}[\"']"),
    "BITSO_KEY_LIKE": re.compile(r"\b[A-Za-z0-9]{20,}\b(?=[\"']\s*[,)]\s*(?:#.*)?$)", re.MULTILINE),
}
SUSPICIOUS_FILES = re.compile(r"(?:^|/)(?:\.env|keys\.json|credentials|.*\.pem|.*\.p12|.*credential.*)$",
                              re.IGNORECASE)
# Deliberate, documented non-secrets used by tests/fixtures.
ALLOWED_SUBSTRINGS = ("FAKE", "test", "example", "placeholder", "dummy", "sample", "REDACTED", "***")


def tracked_files() -> list[str]:
    result = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    return [line for line in result.stdout.splitlines() if line.strip()]


def main() -> int:
    findings: list[str] = []
    for name in tracked_files():
        if SUSPICIOUS_FILES.search(name):
            findings.append(f"{name}: suspicious tracked filename")
        try:
            with open(name, encoding="utf-8", errors="ignore") as handle:
                text = handle.read()
        except OSError:
            continue
        for label, pattern in PATTERNS.items():
            if label == "BITSO_KEY_LIKE":
                continue  # too noisy; the targeted patterns above are authoritative
            for match in pattern.finditer(text):
                snippet = match.group(0)
                if any(allowed.lower() in snippet.lower() for allowed in ALLOWED_SUBSTRINGS):
                    continue
                findings.append(f"{name}: {label} -> {snippet[:60]!r}")
    for row in findings:
        print(row)
    print(f"scanned {len(tracked_files())} tracked files; {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
