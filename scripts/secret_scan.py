#!/usr/bin/env python3
"""Secret scan for the public Benchmate repo. Stdlib only.

    python3 scripts/secret_scan.py --staged   # pre-commit: staged blobs only
    python3 scripts/secret_scan.py --tracked  # pre-push: every tracked file
    python3 scripts/secret_scan.py FILE...    # explicit files

Exit code 1 if anything is found. A line ending in `secret-scan: allow` is skipped
(use sparingly, e.g. for test fixtures that contain fake keys). If `gitleaks` is on
PATH it runs as a second opinion in --staged mode.
"""
from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
import sys
from pathlib import Path

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}")),
    ("OpenAI ephemeral client secret", re.compile(r"\bek_[A-Za-z0-9]{16,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    (
        "Assigned secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"][A-Za-z0-9_\-/+=]{24,}['\"]"
        ),
    ),
]

# Paths that must never be committed, whatever their content.
FORBIDDEN_PATHS = [".env", ".env.*", ".claude/*", "data/*", "frames/*", "*.sqlite", "*.sqlite3"]
ALLOWED_PATHS = [".env.example"]
ALLOW_MARKER = "secret-scan: allow"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def forbidden(path: str) -> bool:
    path = path.removeprefix("./")
    if path in ALLOWED_PATHS:
        return False
    return any(fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(Path(path).name, pat) for pat in FORBIDDEN_PATHS)


def scan_text(path: str, text: str) -> list[str]:
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARKER in line:
            continue
        for label, pattern in PATTERNS:
            if pattern.search(line):
                findings.append(f"{path}:{lineno}: {label}")
    return findings


def scan(items: list[tuple[str, str | None]]) -> list[str]:
    """items: (path, text or None if unreadable/binary)."""
    findings = []
    for path, text in items:
        if forbidden(path):
            findings.append(f"{path}: path must never be committed")
            continue
        if text is not None:
            findings.extend(scan_text(path, text))
    return findings


def _decode(data: bytes) -> str | None:
    if b"\0" in data[:8000]:
        return None
    return data.decode("utf-8", errors="replace")


def staged_items() -> list[tuple[str, str | None]]:
    paths = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0")
    items = []
    for p in filter(None, paths):
        blob = subprocess.run(["git", "show", f":{p}"], check=True, capture_output=True).stdout
        items.append((p, _decode(blob)))
    return items


def file_items(paths: list[str]) -> list[tuple[str, str | None]]:
    return [(p, _decode(Path(p).read_bytes())) for p in paths if Path(p).is_file()]


def main(argv: list[str]) -> int:
    if argv[:1] == ["--staged"]:
        items = staged_items()
    elif argv[:1] == ["--tracked"]:
        items = file_items([p for p in _git("ls-files", "-z").split("\0") if p])
    else:
        items = file_items(argv)

    findings = scan(items)

    if argv[:1] == ["--staged"] and shutil.which("gitleaks"):
        rc = subprocess.run(["gitleaks", "git", "--staged", "--no-banner", "--redact"]).returncode
        if rc != 0:
            findings.append("gitleaks reported findings (see above)")

    if findings:
        print("secret scan: BLOCKED", file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        print(f"Remove the secret, or end the line with '{ALLOW_MARKER}' if it is a fake.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
