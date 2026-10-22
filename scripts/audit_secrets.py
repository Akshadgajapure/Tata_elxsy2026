#!/usr/bin/env python3
"""
audit_secrets.py  –  TataElexy Adaptive QoS Engine
====================================================
Scans all source, config, and documentation files in the project tree for
patterns that are commonly associated with leaked credentials or secrets.

Exit codes
----------
0  No FAIL findings (project is clean).
1  At least one FAIL finding was detected.

Usage
-----
    python scripts/audit_secrets.py [project_root]

If ``project_root`` is omitted the script resolves the parent of its own
``scripts/`` directory (i.e. the TataElexy project root).
"""

from __future__ import annotations

import re
import sys
import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: File extensions to scan (case-insensitive).
SCAN_EXTENSIONS: frozenset[str] = frozenset(
    {".py", ".json", ".yaml", ".yml", ".env", ".cfg", ".ini", ".md", ".sh", ".bat", ".ps1"}
)

#: Directory names to skip entirely.
EXCLUDED_DIRS: frozenset[str] = frozenset(
    {".venv", "__pycache__", ".pytest_cache", ".git"}
)

# ---------------------------------------------------------------------------
# Secret-detection patterns
# ---------------------------------------------------------------------------

# Each entry: (label, compiled-regex, severity)
# Severity: "FAIL" for confirmed secrets, "WARN" for suspicious patterns.

_PATTERNS: List[Tuple[str, re.Pattern, str]] = [
    # --- API keys ---
    (
        "API key prefix (sk-/pk-)",
        re.compile(r'(?<![#\s])(sk-|pk-)[A-Za-z0-9_\-]{16,}', re.IGNORECASE),
        "FAIL",
    ),
    (
        "Bearer token",
        re.compile(r'Bearer\s+[A-Za-z0-9\-._~+/]{20,}', re.IGNORECASE),
        "FAIL",
    ),
    (
        "api_key / apikey / api-key assignment",
        re.compile(
            r'(?:api[_-]?key)\s*[:=]\s*["\']?[A-Za-z0-9_\-]{12,}["\']?',
            re.IGNORECASE,
        ),
        "WARN",
    ),
    # --- Passwords in code ---
    (
        "password / passwd / pwd assignment",
        re.compile(
            r'(?:password|passwd|pwd)\s*[:=]\s*["\']?(?![\s"\'<>\{\$])[^\s"\'<>\{\}]{4,}["\']?',
            re.IGNORECASE,
        ),
        "WARN",
    ),
    # --- Private keys ---
    (
        "RSA/EC/OpenSSH private key header",
        re.compile(
            r'-----BEGIN\s+(?:RSA|EC|DSA|OPENSSH)\s+PRIVATE\s+KEY-----',
            re.IGNORECASE,
        ),
        "FAIL",
    ),
    # --- AWS credentials ---
    (
        "AWS access key ID",
        re.compile(r'(?<![A-Z0-9])(AKIA|ASIA|AROA)[A-Z0-9]{16}(?![A-Z0-9])'),
        "FAIL",
    ),
    (
        "AWS secret access key",
        re.compile(
            r'(?:aws_secret|aws_secret_access_key)\s*[:=]\s*["\']?[A-Za-z0-9/+]{40}["\']?',
            re.IGNORECASE,
        ),
        "FAIL",
    ),
    # --- GCP credentials ---
    (
        "GCP service-account JSON marker",
        re.compile(r'"type"\s*:\s*"service_account"', re.IGNORECASE),
        "WARN",
    ),
    (
        "GCP private key block",
        re.compile(r'"private_key"\s*:\s*"-----BEGIN', re.IGNORECASE),
        "FAIL",
    ),
    # --- Azure credentials ---
    (
        "Azure connection string",
        re.compile(
            r'DefaultEndpointsProtocol=https;AccountName=[^;]+;AccountKey=[A-Za-z0-9+/=]{60,}',
            re.IGNORECASE,
        ),
        "FAIL",
    ),
    (
        "Azure client secret",
        re.compile(
            r'(?:client_secret|AZURE_CLIENT_SECRET)\s*[:=]\s*["\']?[A-Za-z0-9_\-~.]{30,}["\']?',
            re.IGNORECASE,
        ),
        "WARN",
    ),
    # --- JWT tokens ---
    (
        "JWT token (eyJ…)",
        re.compile(r'eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}'),
        "FAIL",
    ),
    # --- Base64-encoded 40+ char strings ---
    (
        "Base64 token (≥40 chars)",
        re.compile(r'(?<![A-Za-z0-9+/])([A-Za-z0-9+/]{40,}={0,2})(?![A-Za-z0-9+/])'),
        "WARN",
    ),
]

# ---------------------------------------------------------------------------
# IP-address helpers
# ---------------------------------------------------------------------------

# Private / link-local / loopback ranges that are explicitly allowed.
_ALLOWED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),    # loopback
    ipaddress.ip_network("10.0.0.0/8"),     # private class A
    ipaddress.ip_network("192.168.0.0/16"), # private class C
    ipaddress.ip_network("172.16.0.0/12"),  # private class B (172.16–31.x.x)
]

_IP_RE = re.compile(
    r'\b((?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b'
)


def _is_allowed_ip(ip_str: str) -> bool:
    """Return True if *ip_str* belongs to an explicitly allowed private range."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # malformed – not our concern here
    return any(addr in net for net in _ALLOWED_NETWORKS)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    """A single secret-pattern match inside a file."""
    line_no: int
    label: str
    severity: str   # "FAIL" | "WARN"
    snippet: str    # redacted line excerpt

    def __str__(self) -> str:
        return f"  [{self.severity}] line {self.line_no:>4d} | {self.label}\n           ↳ {self.snippet}"


@dataclass
class FileResult:
    """Audit result for a single file."""
    path: Path
    findings: List[Finding] = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(f.severity == "FAIL" for f in self.findings):
            return "FAIL"
        if self.findings:
            return "WARN"
        return "PASS"


# ---------------------------------------------------------------------------
# Core scanner
# ---------------------------------------------------------------------------

def _redact(text: str, max_len: int = 80) -> str:
    """Return a redacted, length-limited version of *text* for display."""
    text = text.strip()
    if len(text) > max_len:
        text = text[:max_len] + "…"
    # Partially mask long alphanumeric runs that look like secrets.
    def _mask(m: re.Match) -> str:
        s = m.group(0)
        keep = max(4, len(s) // 4)
        return s[:keep] + "*" * (len(s) - keep)

    return re.sub(r'[A-Za-z0-9+/=_\-]{12,}', _mask, text)


def scan_file(path: Path) -> FileResult:
    """Scan a single file and return a :class:`FileResult`."""
    result = FileResult(path=path)

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result  # skip unreadable files silently

    lines = content.splitlines()

    for line_no, line in enumerate(lines, start=1):
        # Skip comment-only lines (basic heuristic).
        stripped = line.strip()
        if stripped.startswith(("#", "//", "--", "rem ", "REM ")):
            continue

        # Run compiled regex patterns.
        for label, pattern, severity in _PATTERNS:
            if pattern.search(line):
                result.findings.append(
                    Finding(
                        line_no=line_no,
                        label=label,
                        severity=severity,
                        snippet=_redact(line),
                    )
                )
                break  # one finding per line per pass is sufficient

        # Hardcoded non-private IP check (separate from regex table).
        for ip_match in _IP_RE.finditer(line):
            ip_str = ip_match.group(0)
            if not _is_allowed_ip(ip_str):
                result.findings.append(
                    Finding(
                        line_no=line_no,
                        label=f"Hardcoded public IP address ({ip_str})",
                        severity="WARN",
                        snippet=_redact(line),
                    )
                )
                break  # one IP warning per line

    return result


# ---------------------------------------------------------------------------
# File tree walker
# ---------------------------------------------------------------------------

def collect_files(root: Path) -> List[Path]:
    """Recursively collect scannable files, honouring :data:`EXCLUDED_DIRS`."""
    files: List[Path] = []
    for item in root.rglob("*"):
        # Skip excluded directories anywhere in the path.
        if any(part in EXCLUDED_DIRS for part in item.parts):
            continue
        if item.is_file() and item.suffix.lower() in SCAN_EXTENSIONS:
            files.append(item)
    return sorted(files)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

_COLOUR = {
    "PASS": "\033[92m",  # green
    "WARN": "\033[93m",  # yellow
    "FAIL": "\033[91m",  # red
    "RESET": "\033[0m",
}


def _coloured(status: str, text: str) -> str:
    """Wrap *text* in ANSI colour codes for the given *status*."""
    return f"{_COLOUR.get(status, '')}{text}{_COLOUR['RESET']}"


def print_report(results: List[FileResult], root: Path) -> int:
    """
    Print a formatted audit report to stdout.

    Parameters
    ----------
    results:
        All :class:`FileResult` objects (one per scanned file).
    root:
        Project root used to make paths relative for display.

    Returns
    -------
    int
        Exit code: ``0`` if clean, ``1`` if any FAIL findings exist.
    """
    separator = "=" * 72
    print(f"\n{separator}")
    print("  TataElexy Adaptive QoS Engine  –  Secrets Audit Report")
    print(separator)

    fail_count = warn_count = pass_count = 0

    for res in results:
        rel = res.path.relative_to(root)
        status = res.status

        if status == "PASS":
            pass_count += 1
            status_tag = _coloured("PASS", "PASS")
        elif status == "WARN":
            warn_count += 1
            status_tag = _coloured("WARN", "WARN")
        else:
            fail_count += 1
            status_tag = _coloured("FAIL", "FAIL")

        print(f"[{status_tag}]  {rel}")

        for finding in res.findings:
            print(finding)

    print(separator)
    print(f"  Files scanned : {len(results)}")
    print(f"  PASS          : {_coloured('PASS', str(pass_count))}")
    print(f"  WARN          : {_coloured('WARN', str(warn_count))}")
    print(f"  FAIL          : {_coloured('FAIL', str(fail_count))}")
    print(separator)

    if fail_count:
        print(
            _coloured(
                "FAIL",
                f"\n  ✖  {fail_count} FAIL finding(s) detected – review and remediate before commit.\n",
            )
        )
        return 1

    if warn_count:
        print(
            _coloured(
                "WARN",
                f"\n  ⚠  {warn_count} WARN finding(s) – verify these are not real credentials.\n",
            )
        )
    else:
        print(_coloured("PASS", "\n  ✔  No secrets detected.  Audit passed.\n"))

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: List[str] | None = None) -> int:
    """Run the secrets audit and return an exit code."""
    if argv is None:
        argv = sys.argv[1:]

    # Resolve project root.
    if argv:
        project_root = Path(argv[0]).resolve()
    else:
        # Default: parent of this script's directory (…/TataElexy/).
        project_root = Path(__file__).resolve().parent.parent

    if not project_root.is_dir():
        print(f"ERROR: '{project_root}' is not a directory.", file=sys.stderr)
        return 2

    print(f"Scanning project root: {project_root}")

    files = collect_files(project_root)
    if not files:
        print("No scannable files found.")
        return 0

    results = [scan_file(f) for f in files]
    return print_report(results, root=project_root)


if __name__ == "__main__":
    sys.exit(main())
